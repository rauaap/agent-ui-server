"""Original-image storage, atomic queue acceptance and native delivery contracts."""
import asyncio
import base64
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, HTTPException
from PIL import Image
from starlette.requests import ClientDisconnect, Request
from starlette.websockets import WebSocketDisconnect

from agent_ui_server import main
from agent_ui_server.agent import ClaudeCodeAdapter, PiAdapter
from agent_ui_server.db import Database
from agent_ui_server.images import MAX_IMAGE_BYTES, native_images, upload_image
from agent_ui_server.network_guard import NetworkGuardMiddleware
from agent_ui_server.session_tools import MODELS


def original(format="PNG"):
    stream = io.BytesIO()
    Image.new("RGB", (7, 9), "red").save(stream, format=format)
    return stream.getvalue()


def request(chunks, mime="image/png", length=None):
    headers = [(b"content-type", mime.encode())]
    if length is not None:
        headers.append((b"content-length", str(length).encode()))
    iterator = iter(chunks)
    async def receive():
        chunk = next(iterator, None)
        if isinstance(chunk, Exception):
            raise chunk
        if chunk is None:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": chunk, "more_body": True}
    return Request({"type": "http", "headers": headers}, receive)


class ImageStorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.db = Database(self.base / "db.sqlite")
        self.addCleanup(self.db.close)

    async def test_formats_originals_relative_paths_and_relocation(self):
        for format, mime in (("PNG", "image/png"), ("JPEG", "image/jpeg"),
                             ("GIF", "image/gif"), ("WEBP", "image/webp")):
            data = original(format)
            image = await upload_image(request([data[:11], data[11:]], mime), self.db)
            record = self.db.get_image(image["id"])
            self.assertEqual(image, {"id": image["id"], "mime_type": mime, "size": len(data), "width": 7, "height": 9})
            self.assertEqual((self.base / record["relative_path"]).read_bytes(), data)
            self.assertFalse(Path(record["relative_path"]).is_absolute())
            self.assertNotIn("relative_path", image)
            encoded = native_images(self.db, [image, image])
            self.assertEqual(len(encoded), 2)
            self.assertEqual(base64.b64decode(encoded[0]["data"]), data)
        self.db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        relocated = self.base / "relocated"
        relocated.mkdir()
        shutil.copy(self.db.path, relocated / "db.sqlite")
        shutil.copytree(self.base / "images", relocated / "images")
        moved = Database(relocated / "db.sqlite")
        try:
            self.assertEqual(native_images(moved, [image]), encoded[:1])
        finally:
            moved.close()

    async def test_errors_cleanup_and_actual_streamed_size(self):
        data = original()
        cases = [([b""], "image/png", None, 400), ([b"garbage"], "image/png", None, 400),
                 ([data], "image/jpeg", None, 400), ([data], "image/svg+xml", None, 415),
                 ([data], "image/png", MAX_IMAGE_BYTES + 1, 413),
                 ([b"x" * MAX_IMAGE_BYTES, b"x"], "image/png", None, 413),
                 ([b"x" * MAX_IMAGE_BYTES, b"x"], "image/png", 1, 413),
                 ([original("JPEG")[:30]], "image/jpeg", None, 400)]
        for chunks, mime, length, status in cases:
            with self.subTest(status=status, mime=mime), self.assertRaises(HTTPException) as caught:
                await upload_image(request(chunks, mime, length), self.db)
            self.assertEqual(caught.exception.status_code, status)
            self.assertEqual(list((self.base / "images").glob("*")), [])
        with self.assertRaises(ClientDisconnect):
            await upload_image(request([data[:10], ClientDisconnect()]), self.db)
        with patch.object(self.db, "insert_image", side_effect=sqlite3.OperationalError("failed")):
            with self.assertRaises(sqlite3.OperationalError):
                await upload_image(request([data]), self.db)
        self.assertEqual(list((self.base / "images").iterdir()), [])
        self.assertEqual(self.db._conn.execute("SELECT count(*) FROM images").fetchone()[0], 0)

    async def test_publication_never_overwrites_existing_image(self):
        data = original()
        image = await upload_image(request([data]), self.db)
        with patch("agent_ui_server.images.uuid.uuid4", return_value=SimpleNamespace(hex=image["id"])):
            with self.assertRaises(FileExistsError):
                await upload_image(request([data]), self.db)
        self.assertEqual(base64.b64decode(native_images(self.db, [image])[0]["data"]), data)
        self.assertEqual(len(list((self.base / "images").iterdir())), 1)

    async def test_exact_upload_limit_preserved(self):
        data = original()
        data += b"\0" * (MAX_IMAGE_BYTES - len(data))
        image = await upload_image(request([data], length=len(data)), self.db)
        self.assertEqual(image["size"], MAX_IMAGE_BYTES)
        self.assertEqual(base64.b64decode(native_images(self.db, [image])[0]["data"]), data)

    async def test_cancelled_upload_cleanup(self):
        started = asyncio.Event()
        async def receive():
            started.set()
            await asyncio.Future()
        req = Request({"type": "http", "headers": [(b"content-type", b"image/png")]}, receive)
        task = asyncio.create_task(upload_image(req, self.db))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(list((self.base / "images").iterdir()), [])


class ImageAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "db.sqlite")
        self.addCleanup(self.db.close)
        project = self.db.create_project(self.tmp.name, "test")
        self.sid = self.db.create_session("target", project["id"], "fake", model="vision")["id"]
        self.db.update_status(self.sid, "running")
        self.events = []
        self.adapter = SimpleNamespace(start_turn=Mock())
        for p in (patch.object(main, "db", self.db),
                  patch.object(main, "adapters", {"fake": self.adapter}),
                  patch.object(main, "model_catalog", {"fake": {"models": [
                      {"id": "vision", "name": "Vision", "reasoning_levels": [], "input": ["text", "image"]},
                      {"id": "text", "name": "Text", "reasoning_levels": [], "input": ["text"]}], "error": None}}),
                  patch.object(main, "running_tasks", {}),
                  patch.object(main, "stopping_sessions", set()),
                  patch.object(main, "stream_locks", defaultdict(asyncio.Lock)),
                  patch.object(main, "turn_lock", asyncio.Lock()),
                  patch.object(main, "enqueue_for_subscribers", side_effect=lambda sid, frames: self.events.extend(frames) or [])):
            p.start()
            self.addCleanup(p.stop)
        self.addAsyncCleanup(self.cancel_tasks)

    async def cancel_tasks(self):
        tasks = list(main.running_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def image(self, size=None):
        image = await upload_image(request([original()]), self.db)
        if size is not None:
            self.db._conn.execute("UPDATE images SET size = ? WHERE id = ?", (size, image["id"]))
            self.db._conn.commit()
            image["size"] = size
        return image

    async def test_http_ws_acceptance_snapshot_shipment_and_replay(self):
        first, second = await self.image(), await self.image()
        ids = [second["id"], first["id"], second["id"]]
        response = await main.start_turn(self.sid, main.TurnRequest(images=ids))
        websocket = SimpleNamespace(receive_json=AsyncMock(side_effect=[
            {"type": "input", "text": "  mixed  ", "images": ids},
            {"type": "input", "text": "plain"}, WebSocketDisconnect()]))
        subscriber = SimpleNamespace(websocket=websocket)
        await main.receive_subscriber(self.sid, subscriber)
        messages = self.db.pending_inputs(self.sid)
        self.assertEqual([m["text"] for m in messages], ["", "mixed", "plain"])
        self.assertEqual(messages[0]["message_id"], response["message_id"])
        for message in messages[:2]:
            self.assertEqual(message["images"], [second, first, second])
        self.assertNotIn("images", messages[2])
        for event in self.events[:2]:
            self.assertEqual(event["images"], [second, first, second])
        shipment = self.db.ship_inputs(self.sid)
        self.assertEqual([m.get("images") for m in shipment["payload"]["messages"]],
                         [m.get("images") for m in messages])
        self.assertEqual(self.db.recent_scrollback(self.sid)[-1], shipment)
        scrollback = await main.get_scrollback(self.sid, after=None, limit=200)
        self.assertEqual(scrollback["messages"][-1], shipment)
        self.assertEqual(self.db._conn.execute("SELECT count(*) FROM message_images").fetchone()[0], 6)
        self.db.delete_session(self.sid)
        self.assertEqual(self.db._conn.execute("SELECT count(*) FROM message_images").fetchone()[0], 0)
        self.assertIsNotNone(self.db.get_image(first["id"]))
        self.assertTrue((self.db.path.parent / self.db.get_image(first["id"])["relative_path"]).exists())

    async def test_catalog_default_session_accepts_images(self):
        image = await self.image()
        session = await main.create_session(main.CreateSessionRequest(name="default", project_path=self.tmp.name, agent="fake"))
        self.assertEqual(session["model"], "vision")
        self.db.update_status(session["id"], "running")
        await main.begin_turn(session["id"], "", images=[image["id"]])
        self.assertEqual(self.db.pending_inputs(session["id"])[0]["images"], [image])

    async def test_http_json_structure_and_image_only(self):
        image = await self.image()
        auth = {"authorization": "Bearer secret", "content-type": "application/json"}
        for body in ({"images": [image["id"]]}, {"prompt": "mixed", "images": [image["id"]]}, {"prompt": "text"}):
            status, _, content = await ImageHTTPTests.call(self, "POST", f"/sessions/{self.sid}/turn", json.dumps(body).encode(), auth)
            self.assertEqual(status, 202, content)
        for body in ({"images": None}, {"images": "id"}, {"images": [123]}, {"prompt": 123}, {"prompt": None}):
            status, _, _ = await ImageHTTPTests.call(self, "POST", f"/sessions/{self.sid}/turn", json.dumps(body).encode(), auth)
            self.assertEqual(status, 422)
        self.assertEqual(len(self.db.pending_inputs(self.sid)), 3)

    async def test_structure_capabilities_and_availability_reject_before_persistence(self):
        image = await self.image()
        for text, images in ((None, []), (123, []), ("", []), ("", "id"), ("hi", [123]), ("hi", ["missing"])):
            with self.subTest(text=text, images=images), self.assertRaises(HTTPException):
                await main.begin_turn(self.sid, text, images=images)
        for model in ("text", "unknown", None):
            self.db._conn.execute("UPDATE sessions SET model = ? WHERE id = ?", (model, self.sid))
            self.db._conn.commit()
            with self.assertRaises(HTTPException):
                await main.begin_turn(self.sid, "", images=[image["id"]])
        self.db._conn.execute("UPDATE sessions SET model = 'vision' WHERE id = ?", (self.sid,))
        self.db._conn.commit()
        with patch.object(main, "adapters", {}), self.assertRaises(HTTPException) as caught:
            await main.begin_turn(self.sid, "", images=[image["id"]])
        self.assertEqual(caught.exception.status_code, 409)
        self.db.set_session_archived(self.sid, True)
        with self.assertRaises(HTTPException):
            await main.begin_turn(self.sid, "", images=[image["id"]])
        self.assertEqual(self.db.recent_scrollback(self.sid), [])
        self.assertEqual(self.db._conn.execute("SELECT count(*) FROM message_images").fetchone()[0], 0)
        self.assertIsNotNone(self.db.get_image(image["id"]))

    async def test_limits_repeated_occurrences_pending_budget_atomicity(self):
        large = await self.image(MAX_IMAGE_BYTES)
        small = await self.image(1)
        ids = [large["id"], large["id"]]
        await main.begin_turn(self.sid, "", images=ids)
        with self.assertRaises(HTTPException):
            await main.begin_turn(self.sid, "", images=[small["id"]])
        self.assertEqual(len(self.db.pending_inputs(self.sid)), 1)
        self.db.ship_inputs(self.sid)
        with self.assertRaises(HTTPException):
            await main.begin_turn(self.sid, "", images=[large["id"]] * 3)
        await main.begin_turn(self.sid, "", images=[small["id"]] * 10)
        with self.assertRaises(HTTPException):
            await main.begin_turn(self.sid, "", images=[small["id"]] * 11)
        await main.begin_turn(self.sid, "", images=[small["id"]] * 10)
        self.assertEqual(self.db._conn.execute("SELECT count(*) FROM message_images").fetchone()[0], 22)
        self.db._conn.execute("""CREATE TRIGGER fail_accept BEFORE INSERT ON pending_inputs
                                BEGIN SELECT RAISE(ABORT, 'failure'); END""")
        count = len(self.db.recent_scrollback(self.sid))
        with self.assertRaises(sqlite3.IntegrityError):
            await main.begin_turn(self.sid, "", images=[small["id"]])
        self.assertEqual(len(self.db.recent_scrollback(self.sid)), count)
        self.assertEqual(self.db._conn.execute("SELECT count(*) FROM message_images").fetchone()[0], 22)

    async def test_concurrent_acceptance_cannot_exceed_pending_budget(self):
        image = await self.image(MAX_IMAGE_BYTES)
        results = await asyncio.gather(*(main.begin_turn(self.sid, "", images=[image["id"]]) for _ in range(3)), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, int) for r in results), 2)
        self.assertEqual(sum(isinstance(r, HTTPException) for r in results), 1)

    async def test_ws_bad_structure_local_errors(self):
        websocket = SimpleNamespace(receive_json=AsyncMock(side_effect=[
            {"type": "input", "images": None}, {"type": "input", "text": 42},
            {"type": "input", "images": [42]}, WebSocketDisconnect()]))
        with patch.object(main, "enqueue_local", AsyncMock(return_value=True)) as local:
            await main.receive_subscriber(self.sid, SimpleNamespace(websocket=websocket))
        self.assertEqual(local.await_count, 3)
        self.assertEqual(self.db.pending_inputs(self.sid), [])

    async def test_combined_delivery_bytes_order_and_missing_file_failure(self):
        a, b = await self.image(), await self.image()
        await main.begin_turn(self.sid, "first", images=[a["id"], b["id"]])
        await main.begin_turn(self.sid, "", images=[a["id"]])
        calls = []
        async def turn(session, prompt, *, images):
            calls.append((prompt, images))
            yield {"type": "done"}
        self.adapter.start_turn = turn
        async with main.turn_lock:
            await main.ship_queued_turn(self.sid)
        await list(main.running_tasks.values())[0]
        self.assertEqual(calls[0][0], "[Message from user]\nfirst\n[Attachments: image 1, image 2]\n\n[Message from user]\n[Attachments: image 3]")
        self.assertEqual([base64.b64decode(i["data"]) for i in calls[0][1]], [original()] * 3)
        path = self.db.path.parent / self.db.get_image(a["id"])["relative_path"]
        path.unlink()
        await main.run_turn(self.sid, "missing", [a])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.events[-2]["type"], "error")
        self.assertIn("No such file", self.events[-2]["message"])


class ImageHTTPTests(unittest.IsolatedAsyncioTestCase):
    setUp = ImageStorageTests.setUp

    async def call(self, method, path, body=b"", headers=None):
        app = FastAPI()
        app.router.routes = list(main.app.router.routes)
        guarded = NetworkGuardMiddleware(app, allowed_hosts=frozenset({"testserver"}), port=8000,
                                         token=lambda: "secret", is_public=main.is_web_root_request)
        header_values = {"host": "testserver:8000", "content-type": "image/png"}
        header_values.update(headers or {})
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                 "http_version": "1.1", "method": method, "scheme": "http", "path": path,
                 "raw_path": path.encode(), "root_path": "", "query_string": b"",
                 "headers": [(k.lower().encode(), v.encode()) for k, v in header_values.items()],
                 "server": ("testserver", 8000), "client": ("127.0.0.1", 12345)}
        sent = []
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        async def send(message):
            sent.append(message)
        with patch.object(main, "db", self.db):
            await guarded(scope, receive, send)
        return sent[0]["status"], dict(sent[0]["headers"]), b"".join(s.get("body", b"") for s in sent[1:])

    async def test_authenticated_binary_endpoints_and_unknown_missing_files(self):
        auth = {"authorization": "Bearer secret"}
        self.assertEqual((await self.call("POST", "/images", original()))[0], 401)
        status, headers, content = await self.call("POST", "/images", original(), auth)
        self.assertEqual(status, 201)
        image = json.loads(content)
        url = f"/images/{image['id']}"
        self.assertEqual((await self.call("GET", url))[0], 401)
        status, headers, content = await self.call("GET", url, headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(content, original())
        self.assertEqual(headers[b"content-type"], b"image/png")
        self.assertEqual(headers[b"content-length"], str(len(content)).encode())
        self.assertEqual(headers[b"cache-control"], b"private, max-age=31536000, immutable")
        self.assertEqual((await self.call("GET", "/images/missing", headers=auth))[0], 404)
        (self.db.path.parent / self.db.get_image(image["id"])["relative_path"]).unlink()
        with self.assertRaises(RuntimeError):
            await self.call("GET", url, headers=auth)
        self.assertEqual((await self.call("POST", "/images", original(), {**auth, "host": "evil.example:8000"}))[0], 400)


class NativeImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_native_image_blocks_and_text_only(self):
        adapter = ClaudeCodeAdapter(session_operation=AsyncMock())
        process = SimpleNamespace(stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()))
        images = [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(original()).decode()}] * 2
        await adapter._write_user_message(process, "annotated", images=images)
        payload = json.loads(process.stdin.write.call_args.args[0])
        self.assertEqual(payload["message"]["content"], [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": images[0]["data"]}},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": images[0]["data"]}},
            {"type": "text", "text": "annotated"}])
        await adapter._write_user_message(process, "text")
        self.assertEqual(json.loads(process.stdin.write.call_args.args[0])["message"]["content"], "text")

    async def test_pi_native_prompt_images_and_text_only(self):
        ready = {"type": "extension_ui_request", "id": "ready", "method": "notify",
                 "message": json.dumps({"agent-ui": 1, "kind": "ready", "sessionTools": list(MODELS), "assetTool": "resolve_asset_link"})}
        for images in ([], [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(original()).decode()}] * 2):
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "prompt.json"
                stub = Path(tmp) / "pi"
                stub.write_text("#!/usr/bin/env python3\nimport json, sys\n"
                                f"print({json.dumps(ready)!r}, flush=True)\n"
                                "for line in sys.stdin:\n"
                                "    message=json.loads(line)\n"
                                "    if message['type']=='prompt':\n"
                                f"        open({str(output)!r},'w').write(json.dumps(message))\n"
                                "        print(json.dumps({'type':'agent_settled'}),flush=True)\n"
                                "        break\n")
                stub.chmod(0o755)
                adapter = PiAdapter(executable=str(stub), web_extension_path="", session_operation=AsyncMock())
                async with asyncio.timeout(5):
                    events = [event async for event in adapter.start_turn({"id": 1, "working_dir": tmp, "sandbox": False}, "annotated", images=images)]
                self.assertFalse(any(event["type"] == "error" for event in events), events)
                payload = json.loads(output.read_text())
                self.assertEqual(payload, {"id": "prompt", "type": "prompt", "message": "annotated", **({"images": images} if images else {})})
