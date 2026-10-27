"""Deterministic model streaming: native normalization, persistence and FIFO replay."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_ui_server.actions import tool_use_event
from agent_ui_server.agent import ClaudeCodeAdapter, PiAdapter
from agent_ui_server.db import Database
from agent_ui_server.streaming import ClaudeBlocks, PiBlocks, DraftRegistry, argument_preview
from agent_ui_server.tool_actions import action_or_other, PI_TOOL_TRANSLATORS
from test_core import _FakeWebSocket, make_session


def canonical(block):
    return tool_use_event(block["id"], action_or_other(block["name"], block["input"], PI_TOOL_TRANSLATORS))


def text(value):
    return {"type": "text", "text": value}


def tool(args=None, call_id="c", name="message_session"):
    return {"type": "toolCall", "id": call_id, "name": name, "arguments": {} if args is None else args}


class NativeStreamingTests(unittest.TestCase):
    def test_claude_interleaving_authoritative_artifact_and_stop_orders(self):
        for stop_first in (True, False):
            stream = ClaudeBlocks(canonical)
            stream.partial({"type": "message_start", "message": {"id": "m"}})
            starts = []
            for index, block in enumerate([text(""), {"type": "tool_use", "id": "c", "name": "message_session", "input": {}}]):
                starts += stream.partial({"type": "content_block_start", "index": index, "content_block": block})
            raw = json.dumps({"message": 'hello "world" \\ \n 😀', "session_id": 42}, ensure_ascii=True)
            updates = []
            for char in raw:
                updates += stream.partial({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": char}})
            updates += stream.partial({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}})
            stop = {"type": "content_block_stop", "index": 1}
            if stop_first:
                self.assertEqual(stream.partial(stop), [])
            artifact = {"id": "m", "content": [text("Hello corrected"), {"type": "tool_use", "id": "c", "name": "message_session", "input": json.loads(raw)}]}
            finals = stream.assistant(artifact)
            self.assertEqual([e["type"] for e in finals], ["output", "tool_use"])
            self.assertEqual(finals[0]["text"], "Hello corrected")
            self.assertEqual(finals[1]["stream_id"], starts[1]["draft"]["stream_id"])
            self.assertEqual(stream.blocks[("m", 1)]["arguments_json"], raw)
            self.assertEqual(updates[-2]["arguments_preview"], json.loads(raw))
            self.assertEqual(stream.partial(stop), [])
            self.assertEqual(stream.assistant(artifact), [])
            stream.partial({"type": "message_start", "message": {"id": "m2"}})
            other = stream.partial({"type": "content_block_start", "index": 0, "content_block": text("")})
            self.assertNotEqual(other[0]["draft"]["stream_id"], starts[0]["draft"]["stream_id"])

    def test_claude_single_block_artifacts_share_message_id_not_array_index(self):
        stream = ClaudeBlocks(canonical)
        stream.partial({"type": "message_start", "message": {"id": "m"}})
        stream.partial({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "private"}})
        first = stream.partial({"type": "content_block_start", "index": 1, "content_block": text("first")})
        final_text = stream.assistant({"id": "m", "content": [text("first")]}, "artifact-text")
        self.assertEqual(final_text[0]["stream_id"], first[0]["draft"]["stream_id"])
        for index, call_id in [(2, "c1"), (3, "c2")]:
            block = {"type": "tool_use", "id": call_id, "name": "message_session", "input": {"message": call_id, "session_id": 42}}
            start = stream.partial({"type": "content_block_start", "index": index, "content_block": {**block, "input": {}}})
            final = stream.assistant({"id": "m", "content": [block]}, "artifact-" + call_id)
            self.assertEqual(len(final), 1)
            self.assertEqual(final[0]["stream_id"], start[0]["draft"]["stream_id"])
            self.assertEqual(final[0]["call_id"], call_id)
            stream.partial({"type": "content_block_stop", "index": index})
        stream.partial({"type": "content_block_start", "index": 4, "content_block": text("second")})
        self.assertEqual(stream.assistant({"id": "m", "content": [text("first")]}, "artifact-text"), [])
        self.assertEqual(stream.assistant({"id": "m", "content": [text("second")]}, "artifact-second")[0]["text"], "second")

    def test_claude_meaningful_start_input_and_empty_metadata(self):
        stream = ClaudeBlocks(canonical)
        for key, args, expected in [(1, {}, ""), (2, {"path": "/tmp/x"}, '{"path": "/tmp/x"}')]:
            event = stream.start(key, {"type": "tool_use", "id": str(key), "name": "read", "input": args})[0]
            self.assertEqual(event["draft"]["arguments_json"], expected)
        self.assertEqual(stream.start(3, {"type": "thinking", "thinking": "secret"}), [])

    def test_pi_rpc_accumulation_siblings_and_final_correction(self):
        stream = PiBlocks(canonical)
        start = stream.update_event({"type": "text_start", "contentIndex": 0})
        tool_start = stream.update_event({"type": "toolcall_start", "contentIndex": 1, "id": "c", "toolName": "message_session"})
        self.assertEqual(start[0]["draft"]["text"], "")
        self.assertEqual(tool_start[0]["draft"]["arguments_preview"], {})
        first = stream.update_event({"type": "text_delta", "contentIndex": 0, "delta": "Hello"})
        update = stream.update_event({"type": "text_delta", "contentIndex": 0, "delta": " world"})
        self.assertEqual(first[0]["text"], "Hello")
        self.assertEqual(update[0]["delta"], " world")
        self.assertEqual(update[0]["text"], "Hello world")
        end = stream.update_event({"type": "text_end", "contentIndex": 0, "content": "final correction"})
        self.assertEqual(end[0]["stream_id"], start[0]["draft"]["stream_id"])
        self.assertEqual(end[0]["text"], "final correction")
        raw = '{"session_id":42,"message":"growing'
        delta = stream.update_event({"type": "toolcall_delta", "contentIndex": 1, "delta": raw})
        self.assertEqual(delta[0]["arguments_preview"], {"session_id": 42, "message": "growing"})
        self.assertEqual(delta[0]["arguments_json"], raw)
        final_tool = tool({"message": "complete", "session_id": 42})
        final = stream.update_event({"type": "toolcall_end", "contentIndex": 1, "toolCall": final_tool})
        self.assertEqual(final[0]["type"], "tool_use")
        self.assertEqual(final[0]["action"]["arguments"], final_tool["arguments"])
        self.assertEqual(stream.execution("c", "message_session", final_tool["arguments"]), [])
        self.assertEqual(stream.message_end({"content": [text("final correction"), final_tool], "stopReason": "toolUse"}), [])

    def test_pi_initial_message_content_is_preserved_not_an_update_snapshot(self):
        stream = PiBlocks(canonical)
        starts = stream.new_message({"content": [text("initial"), tool({"message": "initial arguments"})]})
        self.assertEqual(starts[0]["draft"]["text"], "initial")
        self.assertEqual(starts[1]["draft"]["arguments_preview"], {"message": "initial arguments"})
        self.assertEqual(starts[1]["draft"]["arguments_json"], "")
        self.assertEqual(stream.update_event({"type": "text_start", "contentIndex": 0}), [])
        delta = stream.update_event({"type": "text_delta", "contentIndex": 0, "delta": " plus"})
        self.assertEqual(delta[0]["text"], "initial plus")

    def test_pi_native_rpc_fixture_has_no_sdk_snapshots(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/pi_1_1_0_rpc_streaming.json").read_text())
        self.assertTrue(fixture["capture"]["generated_using_installed_serializer"])
        updates = [event for event in fixture["events"] if event["type"] == "message_update"]
        for update in updates:
            self.assertNotIn("message", update)
            self.assertNotIn("partial", update["assistantMessageEvent"])
        start = next(event["assistantMessageEvent"] for event in updates if event["assistantMessageEvent"]["type"] == "toolcall_start")
        self.assertEqual(start, {"type": "toolcall_start", "contentIndex": 2, "id": "rpc-call", "toolName": "message_session"})
        stream = PiBlocks(canonical)
        for event in [{"type": "toolcall_start", "contentIndex": 0, "toolName": "bash"}, {"type": "toolcall_start", "contentIndex": 0, "id": "c"}, {"type": "text_delta", "contentIndex": 0, "delta": "unstarted"}]:
            with self.subTest(event=event), self.assertRaises(KeyError):
                stream.update_event(event)

    def test_pi_execution_before_artifact_and_unknown_empty_tools(self):
        stream = PiBlocks(canonical)
        generated = stream.execution("c", "unknown_tool", {})
        self.assertEqual(generated[-1]["action"], {"kind": "other", "name": "unknown_tool", "arguments": {}})
        self.assertEqual(stream.update_event({"type": "toolcall_end", "contentIndex": 0, "toolCall": tool({}, name="unknown_tool")}), [])
        self.assertEqual(stream.update_event({"type": "thinking_delta", "delta": "secret"}), [])

    def test_pi_delayed_tool_identity_buffers_exact_fragments(self):
        stream = PiBlocks(canonical)
        self.assertEqual(stream.update_event({"type": "toolcall_start", "contentIndex": 0, "id": "", "toolName": ""}), [])
        for delta in ['{"message":"hi', ' there"}']:
            self.assertEqual(stream.update_event({"type": "toolcall_delta", "contentIndex": 0, "delta": delta}), [])
        events = stream.update_event({"type": "toolcall_end", "contentIndex": 0, "toolCall": tool({"message": "hi there"})})
        self.assertEqual([e["type"] for e in events], ["_stream_start", "tool_use"])
        self.assertEqual(events[0]["draft"]["arguments_json"], '{"message":"hi there"}')
        self.assertEqual(events[0]["draft"]["arguments_preview"], {"message": "hi there"})

    def test_pi_repeated_artifacts_and_retry_identity(self):
        stream = PiBlocks(canonical)
        start = stream.update_event({"type": "text_start", "contentIndex": 0})
        stream.update_event({"type": "text_delta", "contentIndex": 0, "delta": "unfinished"})
        interrupted = stream.message_end({"content": [text("unfinished")], "stopReason": "error"})
        self.assertEqual(interrupted[0]["stream_id"], start[0]["draft"]["stream_id"])
        self.assertEqual(stream.interrupt("retry"), [])
        stream.new_message()
        final = stream.message_end({"content": [text("complete")], "stopReason": "stop"})
        self.assertNotEqual(final[0]["draft"]["stream_id"], start[0]["draft"]["stream_id"])
        self.assertEqual(stream.message_end({"content": [text("complete")], "stopReason": "stop"}), [])

    def test_partial_parser_arbitrary_boundaries_large_content_and_field_order(self):
        for args in ({"session_id": 42, "message": 'a"b\\c\n😀'}, {"message": 'a"b\\c\n😀', "session_id": 42}, {"content": "x" * 100000}):
            raw = json.dumps(args)
            # Every escape boundary, including half a surrogate pair.
            for index in range(min(len(raw), 100)):
                self.assertIsInstance(argument_preview(raw[:index]), dict)
            self.assertEqual(argument_preview(raw), args)
        self.assertEqual(argument_preview('{"message":"hello'), {"message": "hello"})
        self.assertEqual(argument_preview('not json'), {})
        self.assertEqual(argument_preview('[]'), {})
        # Malformed final arguments retain established unknown-tool handling.
        self.assertEqual(canonical({"id": "bad", "name": "bash", "input": None})["action"], {"kind": "other", "name": "bash", "arguments": {}})

    def test_registry_revision_frozen_snapshots_and_persistence_failure(self):
        native = PiBlocks(canonical)
        registry = DraftRegistry("turn")
        event = native.start(0, tool({"message": "initial"}), pi=True)[0]
        start = registry.apply(event)
        stream_id = start["draft"]["stream_id"]
        frozen = registry.snapshot()
        frame = registry.apply(native.update(0, '{"session_id":42}')[0])
        self.assertEqual(frame, {"type": "stream_delta", "stream_id": stream_id, "revision": 1, "delta": '{"session_id":42}', "arguments_preview": {"session_id": 42}})
        self.assertEqual(frozen["drafts"][0]["revision"], 0)
        self.assertEqual(frozen["drafts"][0]["arguments_preview"], {"message": "initial"})
        with self.assertRaisesRegex(RuntimeError, "disk"):
            registry.interrupt("error", mock.Mock(side_effect=RuntimeError("disk")))
        self.assertIn(stream_id, registry.drafts)
        frames = registry.interrupt("error", lambda event: {**event, "event_id": 1})
        self.assertEqual(frames[0]["draft"]["revision"], 1)
        self.assertEqual(frames[0]["reason"], "error")
        self.assertEqual(registry.snapshot()["drafts"], [])

    def test_empty_drafts_removed_and_substantive_retained_for_every_reason(self):
        for reason in ("error", "retry", "cancelled", "incomplete"):
            stream = PiBlocks(canonical)
            registry = DraftRegistry("t")
            for key, block in enumerate([text(""), tool(), text("unfinished"), tool({"message": "unfinished"})]):
                registry.apply(stream.start(key, block, pi=True)[0])
            persisted = []
            def persist(event):
                persisted.append(event)
                return event
            frames = registry.interrupt(reason, persist)
            self.assertEqual([f["type"] for f in frames], ["stream_remove", "stream_remove", "stream_interrupted", "stream_interrupted"])
            self.assertEqual(len(persisted), 2)
            self.assertEqual([f["reason"] for f in persisted], [reason, reason])
            self.assertFalse(registry.drafts)


class ClaudeFilteringTests(unittest.IsolatedAsyncioTestCase):
    async def test_children_and_reasoning_are_not_top_level_output(self):
        adapter = ClaudeCodeAdapter(session_operation=mock.AsyncMock())
        for event in [
            {"type": "assistant", "parent_tool_use_id": "parent", "message": {"id": "child", "content": [text("secret")]}},
            {"type": "stream_event", "parent_tool_use_id": "parent", "event": {"type": "message_start", "message": {"id": "child"}}},
            {"type": "assistant", "message": {"id": "top", "content": [{"type": "thinking", "thinking": "secret"}]}},
        ]:
            self.assertEqual([e async for e in adapter._events_from_json(1, None, event)], [])


class ServerStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from agent_ui_server import main
        self.main = main
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db = main.db
        main.db = Database(Path(self.tmp.name) / "sessions.db")
        self.session = make_session(main.db, self.tmp.name)
        self.sid = self.session["id"]
        main.subscribers.clear()
        main.stream_locks.clear()
        main.active_streams.clear()
        main.running_tasks.clear()
        main.stopping_sessions.clear()
        self.original_adapter = main.adapters["claude-code"]
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.main.teardown_subscribers([s for group in self.main.subscribers.values() for s in group])
        self.main.subscribers.clear()
        self.main.active_streams.clear()
        self.main.running_tasks.clear()
        self.main.adapters["claude-code"] = self.original_adapter
        self.main.db.close()
        self.main.db = self.original_db
        self.tmp.cleanup()

    async def wait_for(self, predicate):
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(0)

    def start(self, adapter):
        self.main.adapters["claude-code"] = adapter
        task = asyncio.create_task(self.main.run_turn(self.sid, "go"))
        self.main.running_tasks[self.sid] = task
        self.tasks.append(task)
        return task

    async def subscribe(self, *, send_open=True):
        socket = _FakeWebSocket(send_open=send_open)
        task = asyncio.create_task(self.main.session_websocket(socket, self.sid))
        self.tasks.append(task)
        await self.wait_for(lambda: bool(self.main.subscribers.get(self.sid)))
        return socket

    async def test_atomic_snapshot_next_delta_final_and_durable_only_scrollback(self):
        partial_ready, resume = asyncio.Event(), asyncio.Event()
        native = PiBlocks(canonical)
        class Adapter:
            async def start_turn(_, session, prompt):
                for event in native.start(0, text("")) + native.update(0, "Hello"):
                    yield event
                partial_ready.set()
                await resume.wait()
                for event in native.update(0, " world") + native.finish(0, text("Hello world")):
                    yield event
                yield {"type": "done"}
        task = self.start(Adapter())
        await partial_ready.wait()
        self.assertEqual(self.main.db.recent_scrollback(self.sid), [])
        first = await self.subscribe(send_open=False)
        registry = self.main.active_streams[self.sid]
        frozen = registry.snapshot()
        resume.set()
        await task
        first.send_gate.set()
        await self.wait_for(lambda: first.sent and first.sent[-1] == {"type": "status", "status": "idle"})
        self.assertEqual([f["type"] for f in first.sent], ["stream_snapshot", "input_queue", "status", "stream_delta", "output", "done", "status"])
        self.assertEqual(first.sent[0], frozen)
        self.assertEqual(first.sent[0]["drafts"][0]["text"], "Hello")
        self.assertEqual(first.sent[3]["revision"], 2)
        final = first.sent[4]
        self.assertEqual(final["stream_id"], frozen["drafts"][0]["stream_id"])
        self.assertEqual(final["ordinal"], 0)
        self.assertEqual(len(self.main.db.recent_scrollback(self.sid)), 1)
        self.assertFalse(self.main.active_streams)
        # A later reconnect has final history plus an explicitly empty snapshot.
        second = _FakeWebSocket()
        endpoint = asyncio.create_task(self.main.session_websocket(second, self.sid))
        self.tasks.append(endpoint)
        await self.wait_for(lambda: len(second.sent) == 4)
        self.assertEqual(second.sent[:2], [final, {"type": "stream_snapshot", "drafts": []}])

    async def test_actual_websocket_writer_handles_partial_unicode_surrogates(self):
        from starlette.websockets import WebSocket
        sent = []
        async def send(message):
            if message["type"] == "websocket.send":
                # The ASGI server ultimately encodes this str as UTF-8.
                message["text"].encode("utf-8")
                sent.append(message["text"])
        socket = WebSocket({"type": "websocket"}, mock.AsyncMock(return_value={"type": "websocket.connect"}), send)
        await socket.accept()
        subscriber = self.main.Subscriber(socket, asyncio.Queue(maxsize=10))
        frames = [
            {"type": "stream_delta", "stream_id": "s", "revision": 1, "delta": "\ud83d", "arguments_preview": {"message": "emoji \ud83d"}},
            {"type": "stream_delta", "stream_id": "s", "revision": 2, "delta": "\ude00", "arguments_preview": {"message": "emoji 😀"}},
        ]
        for frame in frames:
            subscriber.outbound.put_nowait(frame)
        task = asyncio.create_task(self.main.write_subscriber(subscriber))
        self.tasks.append(task)
        await self.wait_for(lambda: len(sent) == 2 or task.done())
        self.assertFalse(subscriber.send_failed)
        self.assertEqual([json.loads(raw) for raw in sent], frames)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await socket.close()

    async def test_installed_pi_rpc_serializer_fixture_through_adapter_and_server(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/pi_1_1_0_rpc_streaming.json").read_text())
        executable = str(Path(__file__).parent / "fixtures/pi_rpc_streaming.py")
        operation = mock.AsyncMock()
        adapter = PiAdapter(session_operation=operation, executable=executable, web_extension_path="")
        self.main.db.set_sandbox(self.sid, False)
        socket = await self.subscribe()
        async with asyncio.timeout(5):
            await self.start(adapter)
        await self.wait_for(lambda: socket.sent and socket.sent[-1] == {"type": "status", "status": "idle"})
        frames = socket.sent
        self.assertFalse([frame for frame in frames if frame["type"] in {"error", "stream_interrupted"}], frames)
        starts = [frame for frame in frames if frame["type"] == "stream_start"]
        self.assertEqual([frame["draft"]["kind"] for frame in starts], ["text", "tool"])
        self.assertEqual([frame["draft"]["ordinal"] for frame in starts], [0, 1])
        finals = [frame for frame in frames if frame["type"] in {"output", "tool_use"}]
        self.assertEqual(len(finals), 2)
        self.assertEqual(finals[0]["text"], fixture["expected"]["text"])
        self.assertEqual(finals[1]["action"]["arguments"], fixture["expected"]["arguments"])
        self.assertEqual([frame["stream_id"] for frame in finals], [frame["draft"]["stream_id"] for frame in starts])
        tool_id = starts[1]["draft"]["stream_id"]
        deltas = [frame for frame in frames if frame["type"] == "stream_delta"]
        tool_deltas = [frame for frame in deltas if frame["stream_id"] == tool_id]
        self.assertEqual("".join(frame["delta"] for frame in tool_deltas), fixture["expected"]["arguments_json"])
        self.assertEqual(tool_deltas[-1]["arguments_preview"], fixture["expected"]["arguments"])
        self.assertEqual([frame["revision"] for frame in tool_deltas], list(range(1, len(tool_deltas) + 1)))
        self.assertTrue(all("event_id" not in frame for frame in deltas))
        self.assertEqual(len(self.main.db.recent_scrollback(self.sid)), 2)
        self.assertFalse(self.main.active_streams)
        operation.assert_not_awaited()

    async def test_cancel_error_eof_retry_and_completed_block_survival(self):
        for mode, reason in [("cancel", "cancelled"), ("exception", "error"), ("error", "error"), ("eof", "incomplete"), ("retry", "retry")]:
            with self.subTest(mode=mode):
                native = PiBlocks(canonical)
                ready = asyncio.Event()
                class Adapter:
                    async def start_turn(_, session, prompt):
                        for event in native.finish(0, text("completed")) + native.start(1, tool({"message": "not delivered"}), pi=True):
                            yield event
                        ready.set()
                        if mode == "cancel":
                            await asyncio.Future()
                        elif mode == "exception":
                            raise RuntimeError("provider")
                        elif mode == "error":
                            yield {"type": "error", "message": "provider"}
                        elif mode == "retry":
                            yield {"type": "_stream_interrupt", "reason": "retry"}
                            for event in native.finish(2, text("next attempt")):
                                yield event
                            yield {"type": "done"}
                before = self.main.db.recent_scrollback(self.sid)
                task = self.start(Adapter())
                await ready.wait()
                if mode == "cancel":
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                rows = self.main.db.recent_scrollback(self.sid)[len(before):]
                self.assertEqual(rows[0]["type"], "output")
                interruptions = [row for row in rows if row["type"] == "stream_interrupted"]
                self.assertEqual(len(interruptions), 1)
                frame = self.main.frame_for_scrollback(interruptions[0])
                self.assertEqual(frame["reason"], reason)
                self.assertEqual(frame["draft"]["arguments_preview"], {"message": "not delivered"})
                self.assertFalse(any(row["type"] == "tool_use" for row in rows))
                self.assertFalse(self.main.active_streams)
                self.assertFalse(self.main.db.pending_inputs(self.sid))

    async def test_snapshot_200_history_plus_drafts_and_bounded_slow_subscriber(self):
        for index in range(self.main.SCROLLBACK_REPLAY_LIMIT):
            self.main.db.append_scrollback(self.sid, "output", {"text": str(index)})
        native = PiBlocks(canonical)
        registry = DraftRegistry("t")
        registry.apply(native.start(0, text("current"))[0])
        self.main.active_streams[self.sid] = registry
        slow = await self.subscribe(send_open=False)
        fast = _FakeWebSocket()
        endpoint = asyncio.create_task(self.main.session_websocket(fast, self.sid))
        self.tasks.append(endpoint)
        await self.wait_for(lambda: len(fast.sent) == 203)
        self.assertEqual(fast.sent[200]["type"], "stream_snapshot")
        self.assertEqual(fast.sent[200]["drafts"][0]["text"], "current")
        # Fill only slow queue; a healthy subscriber drains between updates.
        slow_subscriber = next(s for s in self.main.subscribers[self.sid] if s.websocket is slow)
        for index in range(slow_subscriber.outbound.maxsize + 1):
            event = native.update(0, "x")[0]
            await self.main.commit_stream(self.sid, lambda: registry.apply(event), lambda frame: [frame])
            await asyncio.sleep(0)
        self.assertTrue(slow_subscriber.retired)
        self.assertTrue(slow.closed)
        self.assertEqual(fast.sent[-1]["type"], "stream_delta")
        self.assertEqual(len(self.main.db.recent_scrollback(self.sid)), 200)
        latest = registry.snapshot()
        self.assertEqual(latest["drafts"][0]["revision"], slow_subscriber.outbound.maxsize + 1)
        restored = _FakeWebSocket()
        endpoint = asyncio.create_task(self.main.session_websocket(restored, self.sid))
        self.tasks.append(endpoint)
        await self.wait_for(lambda: len(restored.sent) == 203)
        self.assertEqual(restored.sent[200], latest)

    async def test_failed_or_cancelled_turn_does_not_ship_queued_work(self):
        for cancel in (False, True):
            ready = asyncio.Event()
            class Adapter:
                async def start_turn(_, session, prompt):
                    yield {"type": "_stream_start", "draft": {"stream_id": "draft", "kind": "text", "text": "unfinished"}}
                    ready.set()
                    if cancel:
                        await asyncio.Future()
                    else:
                        raise RuntimeError("failed")
            self.main.db.enqueue_input(self.sid, "queued", {"type": "user"})
            task = self.start(Adapter())
            await ready.wait()
            if cancel:
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertNotIn(self.sid, self.main.running_tasks)
            self.assertEqual(len(self.main.db.pending_inputs(self.sid)), 1 + int(cancel))
            self.assertEqual(self.main.db.require_session(self.sid)["status"], "idle")

    async def test_long_messaging_both_native_transports_only_deliver_after_approval(self):
        fixture = str(Path(__file__).parent / "fixtures/session_tools.py")
        recipient = make_session(self.main.db, self.tmp.name, name="recipient")
        target = recipient["id"]
        self.main.db.update_status(target, "running")
        subscriber = self.main.Subscriber(_FakeWebSocket(), asyncio.Queue(maxsize=10))
        self.main.subscribers[target].add(subscriber)
        payload = 'Long message with quotes " and slash \\ and emoji 😀\n' * 300
        args = {"session_id": target, "message": payload}
        for cls in (ClaudeCodeAdapter, PiAdapter):
            for behavior in ("deny", "allow"):
                prior = self.main.db.pending_inputs(target)
                operation = mock.AsyncMock(wraps=self.main.session_tool_operation)
                adapter = cls(session_operation=operation, executable=fixture)
                if isinstance(adapter, PiAdapter):
                    adapter.web_extension_path = ""
                events = []
                approval_seen = False
                session = {**self.session, "sandbox": False}
                async with asyncio.timeout(10):
                    async for event in adapter.start_turn(session, json.dumps({"name": "message_session", "arguments": args, "stream": True})):
                        events.append(event)
                        if not approval_seen and event["type"] in {"_stream_start", "_stream_delta", "tool_use", "approval_request"}:
                            operation.assert_not_awaited()
                            self.assertEqual(self.main.db.pending_inputs(target), prior)
                            self.assertTrue(subscriber.outbound.empty())
                        if event["type"] == "approval_request":
                            approval_seen = True
                            await adapter.send_approval(session, event["request_id"], behavior)
                self.assertFalse([e for e in events if e["type"] == "error"], events)
                self.assertGreater(sum(e["type"] == "_stream_delta" for e in events), 100)
                self.assertEqual(sum(e["type"] == "tool_use" for e in events), 1)
                complete = next(e for e in events if e["type"] == "tool_use")
                self.assertEqual(complete["action"]["arguments"], args)
                self.assertEqual(len(self.main.db.pending_inputs(target)), len(prior) + int(behavior == "allow"))
                if behavior == "allow":
                    operation.assert_awaited_once_with(self.sid, "message_session", args)
                    accepted = subscriber.outbound.get_nowait()
                    self.assertEqual(accepted["type"], "input")
                    self.assertEqual(accepted["text"], payload)
                    self.assertEqual(accepted["source"], {"type": "agent", "session_id": self.sid})
                else:
                    operation.assert_not_awaited()
                self.assertTrue(subscriber.outbound.empty())

    async def test_shutdown_preserves_drafts_before_database_close(self):
        ready = asyncio.Event()
        class Adapter:
            async def start_turn(_, session, prompt):
                yield {"type": "_stream_start", "draft": {"stream_id": "s", "kind": "text", "text": "unfinished"}}
                ready.set()
                await asyncio.Future()
        self.start(Adapter())
        await ready.wait()
        with mock.patch.object(self.main.db, "close") as close, mock.patch.object(self.main.file_tree_manager, "shutdown", new=mock.AsyncMock()):
            await self.main.shutdown()
            close.assert_called_once_with()
        self.assertFalse(self.main.active_streams)
        frames = [self.main.frame_for_scrollback(row) for row in self.main.db.recent_scrollback(self.sid)]
        self.assertEqual(frames[0]["type"], "stream_interrupted")
        self.assertEqual(frames[0]["reason"], "cancelled")
        self.assertEqual(self.main.db.require_session(self.sid)["status"], "idle")

    async def test_deletion_stops_and_clears_without_resurrecting_rows(self):
        ready = asyncio.Event()
        class Adapter:
            async def start_turn(_, session, prompt):
                yield {"type": "_stream_start", "draft": {"stream_id": "s", "kind": "text", "text": "unfinished"}}
                ready.set()
                await asyncio.Future()
            async def stop(_, session):
                pass
        self.start(Adapter())
        await ready.wait()
        await self.main.teardown_session(self.session)
        self.assertIsNone(self.main.db.get_session(self.sid))
        self.assertNotIn(self.sid, self.main.active_streams)
        self.assertEqual(self.main.db.recent_scrollback(self.sid), [])
