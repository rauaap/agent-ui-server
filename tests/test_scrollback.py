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
        })
        status, body = await self.get(f"after={body['next_cursor']}&limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(body, {
            "messages": [third], "next_cursor": third["id"], "has_more": False,
        })
        _, body = await self.get(f"after={third['id']}&limit=2")
        self.assertEqual(body, {
            "messages": [], "next_cursor": third["id"], "has_more": False,
        })

    async def test_empty_cursor_defaults_and_beyond_end(self):
        for query, cursor in [("", None), ("after=0", 0), ("after=999", 999)]:
            with self.subTest(query=query):
                status, body = await self.get(query)
                self.assertEqual(status, 200)
                self.assertEqual(body, {
                    "messages": [], "next_cursor": cursor, "has_more": False,
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

    async def test_archived_session_is_readable(self):
        row = self.append()
        self.db.set_session_archived(self.session, True)
        status, body = await self.get("limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(body["messages"], [row])
        self.assertFalse(body["has_more"])

    async def test_missing_session(self):
        status, _ = await self.get(session_id=999999)
        self.assertEqual(status, 404)

    async def test_validation(self):
        for query in ["after=-1", "after=no", "after=1.5", "limit=0", "limit=-1",
                      "limit=1001", "limit=no", "limit=1.5"]:
            with self.subTest(query=query):
                status, _ = await self.get(query)
                self.assertEqual(status, 422)


if __name__ == "__main__":
    unittest.main()
