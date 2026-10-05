from __future__ import annotations

import json as jsonlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
from types import SimpleNamespace
from fastapi import FastAPI
from starlette.staticfiles import StaticFiles
from starlette.routing import Mount

from agent_ui_server import main
from agent_ui_server.db import Database
from agent_ui_server.network_guard import NetworkGuardMiddleware
from agent_ui_server.shared_assets import ASSET_HEADERS, AssetFiles, resolve_asset_link


class ASGIClient:
    def __init__(self, app):
        self.app = app

    async def request(self, method, url, headers=None, json=None):
        parts = urlsplit(url)
        body = b"" if json is None else jsonlib.dumps(json).encode()
        request_headers = {"host": "testserver:8000", "content-type": "application/json"}
        request_headers.update({key.lower(): value for key, value in (headers or {}).items()})
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method.upper(), "scheme": "http", "path": unquote(parts.path),
            "raw_path": parts.path.encode(), "root_path": "", "query_string": parts.query.encode(),
            "headers": [(key.encode(), value.encode()) for key, value in request_headers.items()],
            "server": ("testserver", 8000), "client": ("127.0.0.1", 12345),
        }
        sent = []

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            sent.append(message)

        await self.app(scope, receive, send)
        start = sent[0]
        content = b"".join(message.get("body", b"") for message in sent[1:])
        return SimpleNamespace(
            status_code=start["status"], content=content, text=content.decode(),
            headers={key.decode(): value.decode() for key, value in start["headers"]},
            json=lambda: jsonlib.loads(content),
        )

    def __getattr__(self, method):
        async def request(url, **kwargs):
            return await self.request(method, url, **kwargs)
        return request


class SharedAssetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.files = self.base / "files"
        self.files.mkdir()
        self.database = Database(self.base / "db.sqlite")
        self.db_patch = patch.object(main, "db", self.database)
        self.db_patch.start()
        self.app = FastAPI()
        self.app.router.routes = list(main.app.router.routes)
        self.web = self.base / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("desktop")
        self.app.mount("/", StaticFiles(directory=self.web, html=True), name=main.WEB_ROOT_MOUNT)
        self.router_patch = patch.object(main.app, "router", self.app.router)
        self.router_patch.start()
        guarded = NetworkGuardMiddleware(
            self.app, allowed_hosts=frozenset({"testserver"}), port=8000,
            token=lambda: "secret", is_public=main.is_web_root_request,
        )
        self.client = ASGIClient(guarded)
        self.auth = {"Authorization": "Bearer secret"}

    async def asyncTearDown(self):
        self.router_patch.stop()
        self.db_patch.stop()
        self.database.close()
        self.temp.cleanup()

    async def create(self, name="notes", path=None, **extra):
        return await self.client.post("/shared-asset-roots", headers=self.auth, json={
            "asset_root": name, "path": str(path or self.files), **extra,
        })

    async def test_crud_validation_and_persistence(self):
        missing = self.base / "missing"
        response = await self.create(path=missing / ".." / "missing")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"asset_root": "notes", "path": str(missing), "project_id": None, "url": "/shared-assets/notes/"})
        self.assertFalse(missing.exists())
        self.assertEqual((await self.create("duplicate", missing)).status_code, 409)
        self.assertEqual((await self.create("notes", self.files)).status_code, 409)
        for fields in ({"asset_root": "a/b"}, {"asset_root": "é"}, {"path": "relative"}, {"path": "~/notes"}, {"unexpected": True}, {"project_id": "1"}, {"project_id": 999}):
            expected = 404 if fields == {"project_id": 999} else 422
            self.assertEqual((await self.create("valid", **fields)).status_code, expected)
        for fields in ({"path": None}, {"asset_root": None}, {"unknown": 1}):
            self.assertEqual((await self.client.patch("/shared-asset-roots/notes", headers=self.auth, json=fields)).status_code, 422)
        response = await self.client.patch("/shared-asset-roots/notes", headers=self.auth, json={"asset_root": "renamed", "path": str(self.files)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["url"], "/shared-assets/renamed/")
        self.assertEqual((await self.client.get("/shared-assets/notes/")).status_code, 404)
        self.assertEqual((await self.client.patch("/shared-asset-roots/absent", headers=self.auth, json={})).status_code, 404)
        reopened = Database(self.database.path)
        self.assertEqual(reopened.list_shared_asset_roots(), self.database.list_shared_asset_roots())
        reopened.close()
        self.assertEqual(len((await self.client.get("/shared-asset-roots", headers=self.auth)).json()), 1)
        await self.create("other", missing)
        for changes in ({"asset_root": "other"}, {"path": str(missing)}):
            self.assertEqual((await self.client.patch("/shared-asset-roots/renamed", headers=self.auth, json=changes)).status_code, 409)
        self.assertEqual((await self.client.patch("/shared-asset-roots/renamed", headers=self.auth, json={"project_id": 999})).status_code, 404)
        self.assertEqual((await self.client.delete("/shared-asset-roots/renamed", headers=self.auth)).status_code, 204)
        self.assertTrue(self.files.exists())
        self.assertEqual((await self.client.delete("/shared-asset-roots/renamed", headers=self.auth)).status_code, 404)

    async def test_serving_indexes_nested_head_live_edits_and_headers(self):
        await self.create()
        (self.files / "index.html").write_text("root")
        nested = self.files / "report"
        nested.mkdir()
        (nested / "index.html").write_text("report")
        (nested / "chart.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        for url, text in (("/shared-assets/notes/", "root"), ("/shared-assets/notes/report/", "report"), ("/shared-assets/notes/report/chart.svg", '<svg xmlns="http://www.w3.org/2000/svg"/>')):
            response = await self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text, text)
            for name, value in ASSET_HEADERS.items():
                self.assertEqual(response.headers[name.lower()], value)
            head = await self.client.head(url)
            self.assertEqual(head.status_code, 200)
            self.assertEqual(head.content, b"")
            self.assertEqual(head.headers["content-length"], str(len(text)))
        self.assertIn("image/svg+xml", (await self.client.get("/shared-assets/notes/report/chart.svg")).headers["content-type"])
        for url in ("/shared-assets/notes", "/shared-assets/notes/report"):
            response = await self.client.get(url)
            self.assertEqual(response.status_code, 307)
            self.assertTrue(response.headers["location"].endswith("/"))
        (self.files / "index.html").write_text("changed")
        self.assertEqual((await self.client.get("/shared-assets/notes/")).text, "changed")
        self.assertEqual((await self.client.get("/")).text, "desktop")
        for value, expected in (("bytes=0-2", 206), ("invalid", 400), ("bytes=999-", 416)):
            response = await self.client.get("/shared-assets/notes/index.html", headers={"Range": value})
            self.assertEqual(response.status_code, expected)
            self.assertEqual(response.headers["content-security-policy"], ASSET_HEADERS["Content-Security-Policy"])
            self.assertEqual(response.headers["cache-control"], "no-cache")
            head = await self.client.head("/shared-assets/notes/index.html", headers={"Range": value})
            self.assertEqual(head.status_code, 200)
            self.assertEqual(head.content, b"")
            self.assertEqual(head.headers["content-length"], "7")

    async def test_missing_and_security_boundaries(self):
        await self.create()
        await self.create("missing", self.base / "absent")
        (self.files / "empty").mkdir()
        (self.files / "404.html").write_text("must not be a fallback")
        outside = self.base / "secret.txt"
        outside.write_text("secret")
        (self.files / "escape").symlink_to(outside)
        (self.files / "dir-escape").symlink_to(self.base, target_is_directory=True)
        for suffix in ("escape", "dir-escape/secret.txt", "%2e%2e/secret.txt", "%2fsecret.txt", "missing.html", "empty/"):
            response = await self.client.get("/shared-assets/notes/" + suffix)
            self.assertEqual(response.status_code, 404, suffix)
            self.assertEqual(response.headers["content-security-policy"], ASSET_HEADERS["Content-Security-Policy"])
        for url in ("/shared-assets/missing/", "/shared-assets/unknown/"):
            self.assertEqual((await self.client.get(url)).status_code, 404)
        for url in ("/shared-asset-roots", "/projects", "/shared-assets/notes/", "/shared-assets-lookalike"):
            method = "post" if url.startswith("/shared-assets/notes") else "get"
            if url == "/shared-assets-lookalike":
                # Desktop static exemptions remain public, but do not serve assets.
                self.assertEqual((await self.client.get(url)).status_code, 404)
                continue
            self.assertEqual((await getattr(self.client, method)(url)).status_code, 401)
        self.assertEqual((await self.client.get("/shared-assets/notes/", headers={"Host": "evil.example"})).status_code, 400)
        response = await self.client.get("/shared-asset-roots", headers={"Origin": "null"})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("access-control-allow-origin", response.headers)

    async def test_public_asset_route_without_desktop_mount(self):
        self.app.router.routes = [route for route in self.app.router.routes if not isinstance(route, Mount)]
        await self.create()
        (self.files / "index.html").write_text("public asset")
        self.assertEqual((await self.client.get("/shared-assets/notes/")).text, "public asset")
        self.assertEqual((await self.client.get("/shared-asset-roots")).status_code, 401)
        self.assertEqual((await self.client.post("/shared-assets/notes/")).status_code, 401)

    async def test_project_delete_cascade_runs_session_teardown(self):
        project = self.database.create_project(str(self.base / "project"), "project")
        session = self.database.create_session("test", project["id"], "pi")
        await self.create(project_id=project["id"])
        await self.create("global", self.base / "global")
        updated = await self.client.patch("/shared-asset-roots/notes", headers=self.auth, json={"project_id": None})
        self.assertIsNone(updated.json()["project_id"])
        await self.client.patch("/shared-asset-roots/notes", headers=self.auth, json={"project_id": project["id"]})
        adapter = main.adapters["pi"]
        with patch.object(adapter, "stop", return_value=None) as stop:
            response = await self.client.request("DELETE", "/projects", headers=self.auth, json={"path": project["path"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sessions_deleted"], 1)
        self.assertIsNone(self.database.get_session(session["id"]))
        self.assertIsNone(self.database.get_shared_asset_root("notes"))
        self.assertIsNotNone(self.database.get_shared_asset_root("global"))
        self.assertTrue(self.files.exists())
        stop.assert_awaited_once()

    async def test_resolution_overlap_encoding_nonexistent_and_symlinks(self):
        await self.create()
        nested = self.files / "reports"
        nested.mkdir()
        await self.create("reports", nested)
        result = resolve_asset_link(self.database, str(nested / "é #?.html"))
        self.assertEqual(result, {"url": "/shared-assets/reports/%C3%A9%20%23%3F.html"})
        self.assertFalse((nested / "é #?.html").exists())
        (nested / "é #?.html").write_text("encoded asset")
        self.assertEqual((await self.client.get(result["url"])).text, "encoded asset")
        self.assertEqual(resolve_asset_link(self.database, str(self.files))["url"], "/shared-assets/notes/")
        outside = self.base / "outside"
        (self.files / "escape").symlink_to(outside)
        for path in (str(self.files / "escape" / "future.html"), str(self.base / "files-prefix" / "x"), "relative"):
            with self.assertRaises(ValueError):
                resolve_asset_link(self.database, path)
        (self.files / "inside").symlink_to(nested, target_is_directory=True)
        self.assertEqual(resolve_asset_link(self.database, str(self.files / "inside" / "future.html"))["url"], "/shared-assets/reports/future.html")
        (nested / "index.html").write_text("inside")
        self.assertEqual((await self.client.get("/shared-assets/notes/inside/")).text, "inside")
        self.assertEqual((await main.session_tool_operation(999999, "resolve_asset_link", {"path": str(nested / "future.html")}))["url"], "/shared-assets/reports/future.html")

    async def test_symlink_swap_after_lookup_cannot_escape(self):
        await self.create()
        inside = self.files / "index.html"
        inside.write_text("inside")
        outside = self.base / "secret"
        outside.write_text("secret")
        original = AssetFiles.get_response

        async def swap(files, path, scope):
            response = await original(files, path, scope)
            inside.unlink()
            inside.symlink_to(outside)
            return response

        with patch.object(AssetFiles, "get_response", swap):
            self.assertEqual((await self.client.get("/shared-assets/notes/index.html")).status_code, 404)

    async def test_directory_swap_after_lookup_cannot_escape(self):
        await self.create()
        nested = self.files / "nested"
        nested.mkdir()
        (nested / "index.html").write_text("inside")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "index.html").write_text("secret")
        original = AssetFiles.get_response

        async def swap(files, path, scope):
            response = await original(files, path, scope)
            nested.rename(self.files / "previous")
            nested.symlink_to(outside, target_is_directory=True)
            return response

        with patch.object(AssetFiles, "get_response", swap):
            self.assertEqual((await self.client.get("/shared-assets/notes/nested/index.html")).status_code, 404)

    async def test_resolution_tie_break_and_registration_symlink_normalization(self):
        alias = self.base / "alias"
        alias.symlink_to(self.files, target_is_directory=True)
        await self.create("z", alias)
        self.assertEqual(self.database.get_shared_asset_root("z")["path"], str(self.files))
        self.assertEqual((await self.create("duplicate", self.files)).status_code, 409)
        future = self.base / "future"
        await self.create("a", future)
        future.symlink_to(self.files, target_is_directory=True)
        self.assertEqual(resolve_asset_link(self.database, str(self.files / "x"))["url"], "/shared-assets/a/x")
