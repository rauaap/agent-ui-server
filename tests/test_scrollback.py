from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI

from agent_ui_server import main
from agent_ui_server.db import Database


class ScrollbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Database(Path(tmp.name) / "sessions.db")
        self.addCleanup(self.db.close)
        patch = mock.patch.object(main, "db", self.db)
        patch.start()
        self.addCleanup(patch.stop)
        project = self.db.create_project(tmp.name, "test")
        self.session = self.db.create_session(
            name="test", project_id=project["id"], agent="claude-code"
        )["id"]
        self.other = self.db.create_session(
            name="other", project_id=project["id"], agent="claude-code"
        )["id"]
        self.app = FastAPI()
        self.app.include_router(main.app.router)

    async def get(self, query="", session_id=None):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await self.app(
            {
                "type": "http",
                "method": "GET",
                "path": f"/sessions/{self.session if session_id is None else session_id}/scrollback",
                "root_path": "",
                "query_string": query.encode(),
                "headers": [],
            },
            receive,
            send,
        )
        return sent[0]["status"], json.loads(
            b"".join(message.get("body", b"") for message in sent)
        )

    def append(self, text="hello"):
        return self.db.append_scrollback(self.session, "output", {"text": text})

    async def test_pages_preserve_rows_and_exclude_other_sessions(self):
        first = self.append("héllo")
        self.db.append_scrollback(self.other, "output", {"text": "unrelated"})
        second = self.append("second")
        third = self.append("third")
        status, body = await self.get("limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(body, {
            "messages": [first, second], "next_cursor": second["id"], "has_more": True,
            "queued_messages": [],
        })
        status, body = await self.get(f"after={body['next_cursor']}&limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(body, {
            "messages": [third], "next_cursor": third["id"], "has_more": False,
            "queued_messages": [],
        })
        _, body = await self.get(f"after={third['id']}&limit=2")
        self.assertEqual(body, {
            "messages": [], "next_cursor": third["id"], "has_more": False,
            "queued_messages": [],
        })

    async def test_backward_pages_use_nearest_events_with_gapped_ids(self):
        rows = []
        for i in range(405):
            rows.append(self.append(str(i)))
            self.db.append_scrollback(self.other, "output", {"text": "unrelated"})
        _, body = await self.get(f"before={rows[-1]['id']}&limit=200")
        self.assertEqual(body["messages"], rows[204:404])
        self.assertEqual(body["next_cursor"], rows[204]["id"])
        self.assertTrue(body["has_more"])
        _, body = await self.get(f"before={body['next_cursor']}&limit=200")
        self.assertEqual(body["messages"], rows[4:204])
        self.assertTrue(body["has_more"])
        _, body = await self.get(f"before={body['next_cursor']}&limit=200")
        self.assertEqual(body["messages"], rows[:4])
        self.assertFalse(body["has_more"])
        _, body = await self.get(f"before={body['next_cursor']}&limit=200")
        self.assertEqual(body["messages"], [])
        self.assertEqual(body["next_cursor"], rows[0]["id"])
        self.assertFalse(body["has_more"])

    async def test_backward_exact_page_and_zero_cursor(self):
        rows = [self.append(str(i)) for i in range(201)]
        _, body = await self.get(f"before={rows[-1]['id']}")
        self.assertEqual(body["messages"], rows[:200])
        self.assertFalse(body["has_more"])
        _, body = await self.get("before=0")
        self.assertEqual(body["messages"], [])
        self.assertEqual(body["next_cursor"], 0)
        self.assertFalse(body["has_more"])

    def test_persisted_frames_include_event_id(self):
        for event_type in ("input", "output", "tool_use", "approval_response",
                           "question_response", "bash_input", "bash_output", "error"):
            row = self.db.append_scrollback(self.session, event_type, {"text": "hi"})
            frame = main.frame_for_scrollback(row)
            self.assertEqual(frame["event_id"], row["id"])
            if event_type == "input":
                self.assertEqual(frame["message_id"], row["id"])

    def corrupt_payload(self):
        row = self.append()
        with self.db._conn:
            self.db._conn.execute(
                "UPDATE scrollback SET payload = ? WHERE id = ?",
                ("not JSON", row["id"]),
            )

    def test_malformed_stored_payload_raises(self):
        self.corrupt_payload()
        for read in (self.db.scrollback_after, self.db.recent_scrollback):
            with self.subTest(read=read.__name__):
                with self.assertRaises(json.JSONDecodeError):
                    read(self.session)

    async def test_malformed_stored_payload_surfaces_through_api(self):
        self.corrupt_payload()
        with self.assertRaises(json.JSONDecodeError):
            await self.get()

    def test_recent_scrollback_decodes_valid_payloads(self):
        first = self.append("héllo")
        second = self.append("second")
        self.assertEqual(self.db.recent_scrollback(self.session), [first, second])
        self.assertEqual(self.db.recent_scrollback(self.session, limit=1), [second])

    async def test_empty_cursor_defaults_and_beyond_end(self):
        for query, cursor in [("", None), ("after=0", 0), ("after=999", 999)]:
            with self.subTest(query=query):
                status, body = await self.get(query)
                self.assertEqual(status, 200)
                self.assertEqual(body, {
                    "messages": [], "next_cursor": cursor, "has_more": False,
                    "queued_messages": [],
                })

    async def test_default_limit_and_exact_page(self):
        rows = [self.append(str(i)) for i in range(201)]
        _, body = await self.get()
        self.assertEqual(body["messages"], rows[:200])
        self.assertEqual(body["next_cursor"], rows[199]["id"])
        self.assertTrue(body["has_more"])
        _, body = await self.get("limit=201")
        self.assertEqual(body["messages"], rows)
        self.assertFalse(body["has_more"])
        _, body = await self.get("after=0&limit=1000")
        self.assertEqual(body["messages"], rows)
        self.assertFalse(body["has_more"])

    async def test_session_with_missing_working_directory_is_readable(self):
        row = self.append()
        self.db._conn.execute("UPDATE projects SET path = '/missing-session-directory'")
        self.db._conn.commit()
        status, body = await self.get("limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(body["messages"], [row])
        self.assertFalse(body["has_more"])

    async def test_missing_session(self):
        status, _ = await self.get(session_id=999999)
        self.assertEqual(status, 404)

    async def test_validation(self):
        for query in ["before=-1", "before=no", "before=1.5", "before=0&after=0",
                      "after=-1", "after=no", "after=1.5", "limit=0", "limit=-1",
                      "limit=1001", "limit=no", "limit=1.5"]:
            with self.subTest(query=query):
                status, _ = await self.get(query)
                self.assertEqual(status, 422)


if __name__ == "__main__":
    unittest.main()
