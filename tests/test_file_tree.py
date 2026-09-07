from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from agent_ui_server import file_tree
from agent_ui_server.db import Database
from agent_ui_server.file_tree import FileTreeError, FileTreeManager, scan_tree


class FileTreeScanTests(unittest.TestCase):
    def scan_paths(self, root: Path) -> list[str]:
        result = scan_tree(str(root))
        try:
            return file_tree._wire_entries(result.entries)
        finally:
            result.inotify.close()

    def test_snapshot_format_empty_directories_and_sorting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "z.txt").write_text("")
            (root / "empty").mkdir()
            (root / "a").mkdir()
            (root / "a" / "child.txt").write_text("")

            self.assertEqual(
                self.scan_paths(root),
                ["a/", "a/child.txt", "empty/", "z.txt"],
            )

    def test_types_symlinks_and_special_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "target").mkdir()
            (root / "target" / "inside").write_text("")
            (root / "directory-link").symlink_to("target", target_is_directory=True)
            (root / "broken-link").symlink_to("missing")
            os.mkfifo(root / "pipe")

            paths = self.scan_paths(root)

            self.assertIn("directory-link", paths)
            self.assertNotIn("directory-link/", paths)
            self.assertFalse(any(path.startswith("directory-link/") for path in paths))
            self.assertIn("broken-link", paths)
            self.assertNotIn("pipe", paths)

    def test_builtins_custom_rules_and_negation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("")
            (root / ".venv").mkdir()
            (root / ".venv" / "python").write_text("")
            (root / "nested").mkdir()
            (root / "nested" / "cache.pyc").write_text("")
            (root / "nested" / "keep.py").write_text("")
            (root / ".gitignore").write_text("nested/\n")
            (root / ".agent-ui-ignore").write_text("!.venv/\n/nested/keep.py\n")

            paths = self.scan_paths(root)

            self.assertNotIn(".git/", paths)
            self.assertIn(".venv/", paths)
            self.assertIn(".venv/python", paths)
            self.assertIn("nested/", paths)
            self.assertNotIn("nested/cache.pyc", paths)
            self.assertNotIn("nested/keep.py", paths)
            self.assertIn(".gitignore", paths)
            self.assertIn(".agent-ui-ignore", paths)

    def test_mount_point_subtree_is_pruned_but_directory_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mounted = root / "mounted"
            mounted.mkdir()
            (mounted / "hidden").write_text("")
            with patch.object(
                file_tree, "_mount_points", return_value={str(mounted.resolve())}
            ):
                paths = self.scan_paths(root)
            self.assertEqual(paths, ["mounted/"])

    def test_symlinked_root_is_traversed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "target"
            target.mkdir()
            (target / "visible").write_text("")
            link = base / "root-link"
            link.symlink_to(target, target_is_directory=True)

            self.assertEqual(self.scan_paths(link), ["visible"])

    def test_invalid_ignore_file_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".agent-ui-ignore").write_bytes(b"\xff")
            with self.assertRaises(FileTreeError) as caught:
                scan_tree(str(root))
            self.assertEqual(caught.exception.code, "invalid_ignore_file")

    def test_symlinked_ignore_file_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rules").write_text("ignored\n")
            (root / ".agent-ui-ignore").symlink_to("rules")
            with self.assertRaises(FileTreeError) as caught:
                scan_tree(str(root))
            self.assertEqual(caught.exception.code, "invalid_ignore_file")

    def test_path_limits_stop_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one").write_text("")
            (root / "two").write_text("")
            with (
                patch.object(file_tree, "MAX_FILE_TREE_PATHS_PER_TREE", 1),
                self.assertRaises(FileTreeError) as caught,
            ):
                scan_tree(str(root))
            self.assertEqual(caught.exception.code, "path_limit_exceeded")

    @unittest.skipUnless(hasattr(os, "listdir"), "requires byte-path support")
    def test_undecodable_names_are_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root_bytes = os.fsencode(directory)
            bad = root_bytes + b"/bad-\xff"
            os.mkdir(bad)
            with self.assertLogs(file_tree.logger, level="WARNING"):
                paths = self.scan_paths(Path(directory))
            self.assertEqual(paths, [])


class FakeFileWebSocket:
    def __init__(self, send_open: bool = True) -> None:
        self.sent: list[dict] = []
        self.send_gate = asyncio.Event()
        if send_open:
            self.send_gate.set()
        self.closed = False
        self.close_code: int | None = None

    async def send_json(self, frame: dict) -> None:
        await self.send_gate.wait()
        self.sent.append(frame)

    async def close(self, code: int = 1000) -> None:
        self.closed = True
        self.close_code = code


class EndpointFileWebSocket(FakeFileWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.accepted = False
        self.receive_future: asyncio.Future[dict] | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def receive(self) -> dict:
        if self.closed:
            return {"type": "websocket.disconnect"}
        self.receive_future = asyncio.get_running_loop().create_future()
        return await self.receive_future

    async def disconnect(self) -> None:
        if self.receive_future is not None and not self.receive_future.done():
            self.receive_future.set_result({"type": "websocket.disconnect"})

    async def close(self, code: int = 1000) -> None:
        await super().close(code)
        await self.disconnect()


class FileTreeManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manager = FileTreeManager()

    async def asyncTearDown(self) -> None:
        await self.manager.shutdown()
        self.temporary.cleanup()

    async def wait_for(self, predicate, timeout: float = 2.0) -> None:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.01)

    async def subscribe(self, session_id: int = 1, send_open: bool = True):
        tree = await self.manager.acquire(str(self.root))
        websocket = FakeFileWebSocket(send_open=send_open)
        subscriber = await self.manager.add_subscriber(tree, session_id, websocket)  # type: ignore[arg-type]
        await self.wait_for(lambda: bool(websocket.sent) or not send_open)
        return tree, subscriber, websocket

    async def test_create_delete_and_content_changes(self) -> None:
        (self.root / "existing").write_text("first")
        tree, subscriber, websocket = await self.subscribe()
        self.assertEqual(websocket.sent[0]["paths"], ["existing"])

        (self.root / "added").write_text("")
        await self.wait_for(lambda: len(websocket.sent) == 2)
        self.assertEqual(websocket.sent[1]["added"], ["added"])
        self.assertEqual(websocket.sent[1]["removed"], [])

        (self.root / "existing").write_text("changed")
        await asyncio.sleep(0.25)
        self.assertEqual(len(websocket.sent), 2)

        (self.root / "added").unlink()
        await self.wait_for(lambda: len(websocket.sent) == 3)
        self.assertEqual(websocket.sent[2]["removed"], ["added"])
        self.assertEqual(websocket.sent[2]["base_revision"], 1)
        self.assertEqual(websocket.sent[2]["revision"], 2)
        await self.manager.remove_subscriber(tree, subscriber)

    async def test_directory_rename_removes_and_adds_descendants(self) -> None:
        (self.root / "old").mkdir()
        (self.root / "old" / "child").write_text("")
        tree, subscriber, websocket = await self.subscribe()

        (self.root / "old").rename(self.root / "new")
        await self.wait_for(lambda: len(websocket.sent) == 2)

        patch_frame = websocket.sent[1]
        self.assertEqual(patch_frame["removed"], ["old/", "old/child"])
        self.assertEqual(patch_frame["added"], ["new/", "new/child"])
        await self.manager.remove_subscriber(tree, subscriber)

    async def test_ignore_change_replaces_generation(self) -> None:
        (self.root / "visible").write_text("")
        tree, subscriber, websocket = await self.subscribe()
        generation = websocket.sent[0]["generation"]

        (self.root / ".agent-ui-ignore").write_text("visible\n")
        await self.wait_for(lambda: len(websocket.sent) == 2)

        replacement = websocket.sent[1]
        self.assertEqual(replacement["type"], "file_tree_snapshot")
        self.assertEqual(replacement["revision"], 0)
        self.assertNotEqual(replacement["generation"], generation)
        self.assertEqual(replacement["paths"], [".agent-ui-ignore"])
        await self.manager.remove_subscriber(tree, subscriber)

    async def test_shared_tree_and_last_disconnect_cleanup(self) -> None:
        first_tree, first, first_socket = await self.subscribe(session_id=1)
        second_tree = await self.manager.acquire(str(self.root) + "/.")
        second_socket = FakeFileWebSocket()
        second = await self.manager.add_subscriber(second_tree, 2, second_socket)  # type: ignore[arg-type]
        await self.wait_for(lambda: bool(second_socket.sent))

        self.assertIs(first_tree, second_tree)
        self.assertEqual(
            first_socket.sent[0]["generation"], second_socket.sent[0]["generation"]
        )
        await self.manager.remove_subscriber(first_tree, first)
        self.assertIn(file_tree.normalize_root(str(self.root)), self.manager.file_trees)
        await self.manager.remove_subscriber(second_tree, second)
        self.assertEqual(self.manager.file_trees, {})
        self.assertEqual(self.manager.global_paths, 0)
        self.assertEqual(self.manager.global_watches, 0)

    async def test_symlinked_root_replacement_invalidates(self) -> None:
        first_target = self.root / "first-target"
        second_target = self.root / "second-target"
        first_target.mkdir()
        second_target.mkdir()
        (first_target / "first").write_text("")
        (second_target / "second").write_text("")
        root_link = self.root / "root-link"
        root_link.symlink_to(first_target, target_is_directory=True)
        tree = await self.manager.acquire(str(root_link))
        websocket = FakeFileWebSocket()
        _subscriber = await self.manager.add_subscriber(tree, 1, websocket)  # type: ignore[arg-type]
        await self.wait_for(lambda: bool(websocket.sent))

        replacement = self.root / "replacement-link"
        replacement.symlink_to(second_target, target_is_directory=True)
        os.replace(replacement, root_link)
        await self.wait_for(lambda: websocket.closed)

        self.assertEqual(websocket.sent[-1]["type"], "file_tree_error")
        self.assertEqual(websocket.sent[-1]["code"], "working_directory_unavailable")
        self.assertNotIn(
            file_tree.normalize_root(str(root_link)), self.manager.file_trees
        )

    async def test_root_removal_reports_error_and_invalidates(self) -> None:
        (self.root / "child").write_text("")
        _tree, _subscriber, websocket = await self.subscribe()

        shutil.rmtree(self.root)
        await self.wait_for(lambda: websocket.closed)

        self.assertEqual(websocket.sent[-1]["type"], "file_tree_error")
        self.assertEqual(websocket.sent[-1]["code"], "working_directory_unavailable")
        self.assertEqual(websocket.close_code, 1011)
        self.assertEqual(self.manager.file_trees, {})

    async def test_oversized_patch_rebuilds_with_new_generation(self) -> None:
        tree, subscriber, websocket = await self.subscribe()
        generation = websocket.sent[0]["generation"]

        with patch.object(file_tree, "MAX_FILE_TREE_PATCH_PATHS", 0):
            (self.root / "new").write_text("")
            await self.wait_for(lambda: len(websocket.sent) == 2)

        self.assertEqual(websocket.sent[1]["type"], "file_tree_snapshot")
        self.assertNotEqual(websocket.sent[1]["generation"], generation)
        self.assertEqual(websocket.sent[1]["paths"], ["new"])
        await self.manager.remove_subscriber(tree, subscriber)

    async def test_concurrent_subscribers_share_one_initial_scan(self) -> None:
        original_scan = file_tree.scan_tree
        calls = 0

        def counted_scan(root: str):
            nonlocal calls
            calls += 1
            return original_scan(root)

        with patch.object(file_tree, "scan_tree", side_effect=counted_scan):
            first_tree, second_tree = await asyncio.gather(
                self.manager.acquire(str(self.root)),
                self.manager.acquire(str(self.root)),
            )
        first_socket = FakeFileWebSocket()
        second_socket = FakeFileWebSocket()
        first = await self.manager.add_subscriber(first_tree, 1, first_socket)  # type: ignore[arg-type]
        second = await self.manager.add_subscriber(second_tree, 2, second_socket)  # type: ignore[arg-type]

        self.assertEqual(calls, 1)
        self.assertIs(first_tree, second_tree)
        await self.manager.remove_subscriber(first_tree, first)
        await self.manager.remove_subscriber(second_tree, second)

    async def test_global_path_limit_rejects_only_new_tree(self) -> None:
        (self.root / "first").write_text("")
        tree, subscriber, _websocket = await self.subscribe()
        with tempfile.TemporaryDirectory() as other_directory:
            Path(other_directory, "second").write_text("")
            with (
                patch.object(file_tree, "MAX_FILE_TREE_PATHS_GLOBAL", 1),
                self.assertRaises(FileTreeError) as caught,
            ):
                await self.manager.acquire(other_directory)
        self.assertEqual(caught.exception.code, "global_path_limit_exceeded")
        self.assertIn(file_tree.normalize_root(str(self.root)), self.manager.file_trees)
        await self.manager.remove_subscriber(tree, subscriber)

    async def test_slow_subscriber_does_not_block_healthy_subscriber(self) -> None:
        tree, slow, slow_socket = await self.subscribe(session_id=1, send_open=False)
        second_tree = await self.manager.acquire(str(self.root))
        healthy_socket = FakeFileWebSocket()
        healthy = await self.manager.add_subscriber(second_tree, 2, healthy_socket)  # type: ignore[arg-type]
        await self.wait_for(lambda: bool(healthy_socket.sent))

        (self.root / "new").write_text("")
        await self.wait_for(lambda: len(healthy_socket.sent) == 2)

        self.assertEqual(healthy_socket.sent[1]["added"], ["new"])
        self.assertEqual(slow_socket.sent, [])
        slow_socket.send_gate.set()
        await self.manager.remove_subscriber(tree, slow)
        await self.manager.remove_subscriber(second_tree, healthy)


class FileTreeEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from agent_ui_server import main

        self.main = main
        await main.file_tree_manager.shutdown()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        self.original_db = main.db
        main.db = Database(Path(self.temporary.name) / "sessions.db")
        project = main.db.create_project(str(self.root), PurePosixPath(self.root).name)
        self.session = main.db.create_session(
            name="files", project_id=project["id"], agent="claude-code"
        )

    async def asyncTearDown(self) -> None:
        await self.main.file_tree_manager.shutdown()
        self.main.db.close()
        self.main.db = self.original_db
        self.temporary.cleanup()

    async def wait_for(self, predicate, timeout: float = 2.0) -> None:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.01)

    async def test_rejects_unknown_session_before_accept(self) -> None:
        websocket = EndpointFileWebSocket()

        await self.main.file_tree_websocket(websocket, 999999)  # type: ignore[arg-type]

        self.assertFalse(websocket.accepted)
        self.assertTrue(websocket.closed)
        self.assertEqual(websocket.close_code, 1008)
        self.assertEqual(websocket.sent, [])

    async def test_snapshot_patch_and_disconnect_lifecycle(self) -> None:
        (self.root / "initial").write_text("")
        websocket = EndpointFileWebSocket()
        endpoint = asyncio.create_task(
            self.main.file_tree_websocket(websocket, self.session["id"])  # type: ignore[arg-type]
        )
        await self.wait_for(lambda: bool(websocket.sent))

        self.assertTrue(websocket.accepted)
        self.assertEqual(websocket.sent[0]["type"], "file_tree_snapshot")
        self.assertEqual(websocket.sent[0]["paths"], ["initial"])
        (self.root / "later").write_text("")
        await self.wait_for(lambda: len(websocket.sent) == 2)
        self.assertEqual(websocket.sent[1]["added"], ["later"])

        await websocket.disconnect()
        await asyncio.wait_for(endpoint, timeout=2)
        self.assertEqual(self.main.file_tree_manager.file_trees, {})

    async def test_deletion_does_not_wait_for_pending_scan(self) -> None:
        gate = threading.Event()
        started = threading.Event()
        original_scan = file_tree.scan_tree

        def blocked_scan(root: str):
            started.set()
            gate.wait()
            return original_scan(root)

        websocket = EndpointFileWebSocket()
        with patch.object(file_tree, "scan_tree", side_effect=blocked_scan):
            endpoint = asyncio.create_task(
                self.main.file_tree_websocket(websocket, self.session["id"])  # type: ignore[arg-type]
            )
            await asyncio.to_thread(started.wait, 1)
            try:
                await asyncio.wait_for(
                    self.main.teardown_session(self.session), timeout=1
                )
            finally:
                gate.set()
            await asyncio.gather(endpoint, return_exceptions=True)
            await self.wait_for(
                lambda: not self.main.file_tree_manager.file_tree_initializations
            )

        self.assertTrue(websocket.closed)
        self.assertIsNone(self.main.db.get_session(self.session["id"]))
        self.assertEqual(self.main.file_tree_manager.file_trees, {})

    async def test_session_deletion_closes_file_socket(self) -> None:
        websocket = EndpointFileWebSocket()
        endpoint = asyncio.create_task(
            self.main.file_tree_websocket(websocket, self.session["id"])  # type: ignore[arg-type]
        )
        await self.wait_for(lambda: bool(websocket.sent))

        await self.main.teardown_session(self.session)
        await asyncio.gather(endpoint, return_exceptions=True)

        self.assertTrue(websocket.closed)
        self.assertIsNone(self.main.db.get_session(self.session["id"]))
        self.assertEqual(self.main.file_tree_manager.file_trees, {})
