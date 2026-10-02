import asyncio
from collections import defaultdict
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from agent_ui_server import main
from agent_ui_server.db import Database
from agent_ui_server.session_tools import batch_delivery_prompt


USER = {"type": "user"}
AGENT = {"type": "agent", "session_id": 7}


class QueueDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "queue.db"
        self.db = Database(self.path)
        self.addCleanup(lambda: self.db.close())
        project = self.db.create_project(self.tmp.name, "test")
        self.sid = self.db.create_session("test", project["id"], "fake")["id"]

    def test_persistence_ship_order_and_no_replay_after_restart(self):
        first = self.db.enqueue_input(self.sid, "one\nline two", AGENT)
        second = self.db.enqueue_input(self.sid, "two", USER)
        self.db.close()
        self.db = Database(self.path)
        self.db.reset_active_sessions()
        self.assertEqual([m["message_id"] for m in self.db.pending_inputs(self.sid)],
                         [first["id"], second["id"]])
        row = self.db.ship_inputs(self.sid)
        self.assertEqual(row["type"], "inputs_shipped")
        self.assertEqual(row["payload"]["messages"], [
            {"message_id": first["id"], "text": "one\nline two", "source": AGENT, "delivery": "shipped"},
            {"message_id": second["id"], "text": "two", "source": USER, "delivery": "shipped"},
        ])
        self.assertEqual(self.db.pending_inputs(self.sid), [])
        self.assertIsNone(self.db.ship_inputs(self.sid))
        self.db.enqueue_input(self.sid, "three", USER)
        self.db.close()
        self.db = Database(self.path)
        self.db.reset_active_sessions()
        self.assertEqual([m["text"] for m in self.db.pending_inputs(self.sid)], ["three"])
        self.assertEqual(self.db.require_session(self.sid)["status"], "idle")

    def test_acceptance_transaction_rolls_back(self):
        self.db._conn.execute("""
            CREATE TRIGGER fail_enqueue BEFORE INSERT ON pending_inputs
            BEGIN SELECT RAISE(ABORT, 'test failure'); END
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.enqueue_input(self.sid, "not accepted", USER)
        self.assertEqual(self.db.recent_scrollback(self.sid), [])
        self.assertEqual(self.db.pending_inputs(self.sid), [])

    def test_shipping_transaction_rolls_back(self):
        self.db.enqueue_input(self.sid, "retained", USER)
        self.db._conn.execute("""
            CREATE TRIGGER fail_ship BEFORE DELETE ON pending_inputs
            BEGIN SELECT RAISE(ABORT, 'test failure'); END
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.ship_inputs(self.sid)
        self.assertEqual(len(self.db.pending_inputs(self.sid)), 1)
        self.assertEqual([r["type"] for r in self.db.recent_scrollback(self.sid)], ["input"])
        self.assertEqual(self.db.require_session(self.sid)["status"], "idle")

    def test_deletion_cascades_pending_membership(self):
        self.db.enqueue_input(self.sid, "pending", USER)
        self.db.delete_session(self.sid)
        self.assertEqual(self.db.pending_inputs(self.sid), [])
        self.assertEqual(self.db._conn.execute("SELECT count(*) FROM pending_inputs").fetchone()[0], 0)

    def test_identical_format_for_single_and_multiple_messages(self):
        user = {"text": "user text", "source": USER}
        agent = {"text": "agent text", "source": AGENT}
        self.assertEqual(batch_delivery_prompt([user]), "[Message from user]\nuser text")
        self.assertEqual(batch_delivery_prompt([agent, user]),
                         "[Message from agent session 7; not a direct user instruction]\nagent text"
                         "\n\n[Message from user]\nuser text")


class GatedAdapter:
    def __init__(self):
        self.calls = asyncio.Queue()
        self.prompts = []
        self.active = 0
        self.maximum_active = 0
        self.stop = mock.AsyncMock()

    async def start_turn(self, session, prompt):
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        self.prompts.append(prompt)
        gate = asyncio.get_running_loop().create_future()
        self.calls.put_nowait(gate)
        try:
            result = await gate
            if isinstance(result, Exception):
                raise result
            for event in result or [{"type": "output", "text": "answer"}]:
                yield event
            yield {"type": "done"}
        finally:
            self.active -= 1


class MessageQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        project = self.db.create_project(self.tmp.name, "test")
        self.sid = self.db.create_session("target", project["id"], "fake")["id"]
        self.sender = self.db.create_session("sender", project["id"], "fake")["id"]
        self.adapter = GatedAdapter()
        self.events = []
        for patch in (
            mock.patch.object(main, "db", self.db),
            mock.patch.object(main, "adapters", {"fake": self.adapter}),
            mock.patch.object(main, "running_tasks", {}),
            mock.patch.object(main, "bash_tasks", {}),
            mock.patch.object(main, "stopping_sessions", set()),
            mock.patch.object(main, "stop_locks", defaultdict(asyncio.Lock)),
            mock.patch.object(main, "stream_locks", defaultdict(asyncio.Lock)),
            mock.patch.object(main, "turn_lock", asyncio.Lock()),
            mock.patch.object(main, "enqueue_for_subscribers", side_effect=lambda sid, frames: self.events.extend(frames) or []),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.addAsyncCleanup(self.cancel_tasks)

    async def cancel_tasks(self):
        tasks = list(main.running_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def next_call(self):
        return await asyncio.wait_for(self.adapter.calls.get(), 1)

    async def finish(self, gate):
        task = main.running_tasks[self.sid]
        gate.set_result(None)
        await asyncio.wait_for(task, 1)

    async def test_busy_user_and_agent_messages_form_one_next_turn(self):
        first = await main.begin_turn(self.sid, "first")
        gate = await self.next_call()
        response = await main.start_turn(self.sid, main.TurnRequest(prompt="user followup"))
        agent_mid = await main.session_tool_operation(self.sender, "message_session", {
            "session_id": self.sid, "message": "agent followup",
        })
        last = await main.begin_turn(self.sid, "last")
        self.assertEqual(response["status"], "accepted")
        self.assertEqual(len(self.adapter.prompts), 1)
        self.assertEqual([m["message_id"] for m in self.db.pending_inputs(self.sid)],
                         [response["message_id"], agent_mid, last])
        self.assertEqual(self.adapter.prompts[0], "[Message from user]\nfirst")
        await self.finish(gate)
        next_gate = await self.next_call()
        self.assertEqual(self.adapter.prompts[1],
                         "[Message from user]\nuser followup\n\n"
                         f"[Message from agent session {self.sender}; not a direct user instruction]\nagent followup\n\n"
                         "[Message from user]\nlast")
        self.assertEqual(self.db.pending_inputs(self.sid), [])
        self.assertNotIn({"type": "status", "status": "idle"}, self.events)
        shipments = [e for e in self.events if e["type"] == "inputs_shipped"]
        self.assertEqual([[m["message_id"] for m in e["messages"]] for e in shipments],
                         [[first], [response["message_id"], agent_mid, last]])
        await self.finish(next_gate)
        self.assertEqual(self.events[-1], {"type": "status", "status": "idle"})
        self.assertEqual(self.adapter.maximum_active, 1)

    async def test_later_arrivals_belong_to_following_batch(self):
        await main.begin_turn(self.sid, "first")
        first = await self.next_call()
        await main.begin_turn(self.sid, "second")
        await self.finish(first)
        second = await self.next_call()
        await main.begin_turn(self.sid, "third")
        await self.finish(second)
        third = await self.next_call()
        self.assertEqual(self.adapter.prompts, [
            "[Message from user]\nfirst", "[Message from user]\nsecond", "[Message from user]\nthird",
        ])
        await self.finish(third)

    async def test_concurrent_senders_do_not_start_overlapping_turns(self):
        await main.begin_turn(self.sid, "first")
        gate = await self.next_call()
        mids = await asyncio.gather(*(main.begin_turn(self.sid, str(i)) for i in range(20)))
        self.assertEqual([m["message_id"] for m in self.db.pending_inputs(self.sid)], mids)
        await self.finish(gate)
        gate = await self.next_call()
        self.assertEqual(self.adapter.prompts[1], "\n\n".join(f"[Message from user]\n{i}" for i in range(20)))
        await self.finish(gate)
        self.assertEqual(self.adapter.maximum_active, 1)

    async def test_stop_preserves_queue_and_next_submission_resumes(self):
        await main.begin_turn(self.sid, "first")
        await self.next_call()
        self.db.update_status(self.sid, "awaiting_approval")
        mid = await main.begin_turn(self.sid, "pending")
        await main.stop_session(self.sid)
        self.assertNotIn(self.sid, main.running_tasks)
        self.assertEqual(self.db.require_session(self.sid)["status"], "idle")
        self.assertEqual(self.db.pending_inputs(self.sid)[0]["message_id"], mid)
        await main.begin_turn(self.sid, "continue")
        gate = await self.next_call()
        self.assertEqual(self.adapter.prompts[-1], "[Message from user]\npending\n\n[Message from user]\ncontinue")
        await self.finish(gate)

    async def test_turn_finishing_during_stop_does_not_drain_queue(self):
        await main.begin_turn(self.sid, "first")
        gate = await self.next_call()
        stopping = asyncio.Event()
        release_stop = asyncio.Event()

        async def stop(session):
            stopping.set()
            await release_stop.wait()
        self.adapter.stop.side_effect = stop
        stop_task = asyncio.create_task(main.stop_session(self.sid))
        await stopping.wait()
        mid = await main.begin_turn(self.sid, "during stop")
        await self.finish(gate)
        self.assertEqual(len(self.adapter.prompts), 1)
        release_stop.set()
        await asyncio.wait_for(stop_task, 1)
        self.assertEqual(self.db.pending_inputs(self.sid)[0]["message_id"], mid)
        self.assertNotIn(self.sid, main.stopping_sessions)

    async def test_failure_and_error_event_leave_queue_pending(self):
        for result in (RuntimeError("broken"), [{"type": "error", "message": "broken"}]):
            with self.subTest(result=result):
                await main.begin_turn(self.sid, "start")
                gate = await self.next_call()
                await main.begin_turn(self.sid, "pending")
                task = main.running_tasks[self.sid]
                gate.set_result(result)
                await task
                self.assertNotIn(self.sid, main.running_tasks)
                self.assertEqual(self.db.require_session(self.sid)["status"], "idle")
                self.assertEqual([m["text"] for m in self.db.pending_inputs(self.sid)], ["pending"])
                # Explicitly resume and consume the retained input before the next case.
                await main.begin_turn(self.sid, "resume")
                await self.finish(await self.next_call())

    async def test_empty_archived_missing_and_unavailable_rejections_write_nothing(self):
        for text in ("", " \n "):
            with self.assertRaises(HTTPException):
                await main.begin_turn(self.sid, text)
        with self.assertRaises(HTTPException):
            await main.begin_turn(999, "hello")
        self.db.set_session_archived(self.sid, True)
        with self.assertRaises(HTTPException):
            await main.begin_turn(self.sid, "hello")
        self.db.set_session_archived(self.sid, False)
        with mock.patch.object(main, "adapters", {}), self.assertRaises(HTTPException):
            await main.begin_turn(self.sid, "hello")
        self.assertEqual(self.db.recent_scrollback(self.sid), [])

    async def test_rest_queue_snapshot_is_not_limited_by_scrollback_page(self):
        self.db.update_status(self.sid, "running")
        mid = await main.begin_turn(self.sid, "old pending")
        for i in range(main.SCROLLBACK_REPLAY_LIMIT + 1):
            self.db.append_scrollback(self.sid, "output", {"text": str(i)})
        page = await main.get_scrollback(self.sid, after=mid, limit=1)
        self.assertEqual(page["queued_messages"], self.db.pending_inputs(self.sid))
        self.assertEqual(page["queued_messages"][0]["message_id"], mid)
        self.assertEqual(len(page["messages"]), 1)

    async def test_deletion_cancels_turn_without_shipping_pending_messages(self):
        await main.begin_turn(self.sid, "first")
        await self.next_call()
        await main.begin_turn(self.sid, "pending")
        with mock.patch.object(main.file_tree_manager, "close_session", new=mock.AsyncMock()):
            await main.teardown_session(self.db.require_session(self.sid))
        self.assertIsNone(self.db.get_session(self.sid))
        self.assertEqual(self.db.pending_inputs(self.sid), [])
        self.assertEqual(len(self.adapter.prompts), 1)
        self.assertNotIn(self.sid, main.running_tasks)
