from __future__ import annotations

import asyncio
import errno
import logging
import os
import stat
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import pathspec
from fastapi import WebSocket, WebSocketDisconnect
from inotify_simple import INotify, flags

logger = logging.getLogger(__name__)

MAX_FILE_TREE_WATCHES_GLOBAL = 20_000
MAX_FILE_TREE_PATHS_PER_TREE = 200_000
MAX_FILE_TREE_PATHS_GLOBAL = 500_000
MAX_FILE_TREE_PATH_BYTES_PER_TREE = 16 * 1024 * 1024
MAX_FILE_TREE_PATH_BYTES_GLOBAL = 64 * 1024 * 1024
MAX_FILE_TREE_PATCH_PATHS = 5_000
MAX_FILE_TREE_PATCH_PATH_BYTES = 512 * 1024
FILE_TREE_PATCH_DEBOUNCE = 0.100
FILE_TREE_PATCH_MAX_LATENCY = 0.500
FILE_TREE_SUBSCRIBER_QUEUE_FRAMES = 32
WEBSOCKET_SEND_TIMEOUT_SECONDS = 30.0
WEBSOCKET_CLOSE_TIMEOUT_SECONDS = 5.0

IGNORE_FILE = ".agent-ui-ignore"
BUILTIN_DIRECTORY_EXCLUSIONS = (
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "bower_components",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".hypothesis",
    ".tox",
    ".nox",
    ".gradle",
    ".terraform",
    ".direnv",
    ".turbo",
    ".parcel-cache",
    ".next",
    ".nuxt",
    ".svelte-kit",
)
BUILTIN_JUNK_PATTERNS = ("*.pyc", "*.pyo", "*.swp", "*.swo", "*~", ".DS_Store")
WATCH_MASK = (
    flags.CREATE
    | flags.DELETE
    | flags.MOVED_FROM
    | flags.MOVED_TO
    | flags.ATTRIB
    | flags.CLOSE_WRITE
    | flags.DELETE_SELF
    | flags.MOVE_SELF
)


class EntryKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"


class FileTreeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def frame(self) -> dict[str, str]:
        return {"type": "file_tree_error", "code": self.code, "message": self.message}


@dataclass
class ScanResult:
    root: str
    entries: dict[str, EntryKind]
    path_bytes: int
    inotify: INotify
    wd_to_directory: dict[int, str]
    directory_to_wd: dict[str, int]
    ignore_spec: pathspec.PathSpec
    root_guard_wd: int | None = None

    @property
    def watch_count(self) -> int:
        return len(self.directory_to_wd) + int(self.root_guard_wd is not None)


@dataclass(eq=False)
class FileTreeSubscriber:
    session_id: int
    websocket: WebSocket
    outbound: asyncio.Queue[dict[str, Any]]
    writer: asyncio.Task[None] | None = None
    retired: bool = False
    closed: bool = False
    send_failed: bool = False
    close_code: int = 1000


@dataclass
class FileTree:
    root: str
    generation: str
    revision: int
    entries: dict[str, EntryKind]
    path_bytes: int
    inotify: INotify
    wd_to_directory: dict[int, str]
    directory_to_wd: dict[str, int]
    ignore_spec: pathspec.PathSpec
    root_guard_wd: int | None = None
    subscribers: set[FileTreeSubscriber] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    event_queue: asyncio.Queue[list[Any]] = field(default_factory=asyncio.Queue)
    event_task: asyncio.Task[None] | None = None
    reader_installed: bool = False
    invalidated: bool = False
    rebuilding: bool = False

    @property
    def watch_count(self) -> int:
        return len(self.directory_to_wd) + int(self.root_guard_wd is not None)


def normalize_root(root: str) -> str:
    normalized = os.path.normpath(root)
    if normalized.startswith("//"):
        normalized = normalized[1:]
    return normalized


def _error_for_inotify(exc: OSError) -> FileTreeError:
    if exc.errno in {errno.ENOSPC, errno.EMFILE, errno.ENFILE}:
        return FileTreeError(
            "watch_limit_exceeded",
            "The file-tree watcher limit is unavailable or has been exceeded.",
        )
    return FileTreeError("watcher_failed", f"The file-tree watcher failed: {exc}")


def _check_utf8(name: str, absolute_path: str) -> bool:
    try:
        name.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        logger.warning(
            "Skipping filesystem name that is not valid UTF-8: %r", absolute_path
        )
        return False
    return True


def _decode_mount_field(value: str) -> str:
    # mountinfo escapes whitespace and backslashes as octal sequences.
    for encoded, decoded in (
        ("\\040", " "),
        ("\\011", "\t"),
        ("\\012", "\n"),
        ("\\134", "\\"),
    ):
        value = value.replace(encoded, decoded)
    return value


def _mount_points() -> set[str]:
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as mountinfo:
            result: set[str] = set()
            for line in mountinfo:
                fields = line.split()
                if len(fields) >= 5:
                    result.add(os.path.normpath(_decode_mount_field(fields[4])))
            return result
    except OSError:
        # Linux normally has mountinfo. Duplicate inode detection still prevents
        # cycles if procfs is deliberately unavailable.
        return set()


def _compile_ignore(root: str) -> pathspec.PathSpec:
    lines = [f"{name}/" for name in BUILTIN_DIRECTORY_EXCLUSIONS]
    lines.extend(BUILTIN_JUNK_PATTERNS)
    ignore_path = os.path.join(root, IGNORE_FILE)
    try:
        ignore_mode = os.lstat(ignore_path).st_mode
        if not stat.S_ISREG(ignore_mode):
            raise FileTreeError(
                "invalid_ignore_file", f"{IGNORE_FILE} must be a regular file."
            )
    except FileNotFoundError:
        pass
    try:
        with open(ignore_path, encoding="utf-8", errors="strict") as ignore_file:
            lines.extend(ignore_file.read().splitlines())
    except FileNotFoundError:
        pass
    except FileTreeError:
        raise
    except (OSError, UnicodeError) as exc:
        raise FileTreeError(
            "invalid_ignore_file", f"Could not read {IGNORE_FILE} as UTF-8: {exc}"
        ) from exc
    try:
        return pathspec.PathSpec.from_lines("gitignore", lines)
    except Exception as exc:
        raise FileTreeError(
            "invalid_ignore_file", f"Could not compile {IGNORE_FILE}: {exc}"
        ) from exc


def _is_ignored(spec: pathspec.PathSpec, relative: str, is_directory: bool) -> bool:
    if relative == IGNORE_FILE:
        return False
    candidate = relative + "/" if is_directory else relative
    return bool(spec.match_file(candidate))


def _wire_path(relative: str, kind: EntryKind) -> str:
    return relative + "/" if kind is EntryKind.DIRECTORY else relative


def _add_entry(
    entries: dict[str, EntryKind], relative: str, kind: EntryKind, path_bytes: int
) -> int:
    wire = _wire_path(relative, kind)
    previous = entries.get(relative)
    if previous is not None:
        path_bytes -= len(_wire_path(relative, previous).encode("utf-8"))
    entries[relative] = kind
    path_bytes += len(wire.encode("utf-8"))
    if (
        len(entries) > MAX_FILE_TREE_PATHS_PER_TREE
        or path_bytes > MAX_FILE_TREE_PATH_BYTES_PER_TREE
    ):
        raise FileTreeError(
            "path_limit_exceeded",
            "The working directory contains more than "
            f"{MAX_FILE_TREE_PATHS_PER_TREE} indexed paths or "
            f"{MAX_FILE_TREE_PATH_BYTES_PER_TREE} path bytes.",
        )
    return path_bytes


def _add_watch(inotify: INotify, absolute: str, mask: int | flags = WATCH_MASK) -> int:
    try:
        return inotify.add_watch(absolute, mask)
    except OSError as exc:
        raise _error_for_inotify(exc) from exc


def _remove_watch(inotify: INotify, wd: int) -> None:
    try:
        inotify.rm_watch(wd)
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, errno.ENOENT, errno.EBADF}:
            raise _error_for_inotify(exc) from exc


def _scan_into(
    *,
    root: str,
    relative: str,
    inotify: INotify,
    spec: pathspec.PathSpec,
    entries: dict[str, EntryKind],
    path_bytes: int,
    wd_to_directory: dict[int, str],
    directory_to_wd: dict[str, int],
    seen_directories: set[tuple[int, int]],
    mount_points: set[str],
    include_self: bool,
) -> int:
    absolute = os.path.join(root, relative) if relative else root
    try:
        directory_stat = os.stat(absolute, follow_symlinks=True)
    except (FileNotFoundError, NotADirectoryError):
        if not relative:
            raise FileTreeError(
                "working_directory_unavailable",
                "The working directory is unavailable.",
            ) from None
        return path_bytes
    except PermissionError:
        if not relative:
            raise FileTreeError(
                "working_directory_unavailable",
                "The working directory is not accessible.",
            ) from None
        return path_bytes
    except OSError as exc:
        raise FileTreeError(
            "scan_failed", f"Could not inspect {absolute!r}: {exc}"
        ) from exc

    inode = (directory_stat.st_dev, directory_stat.st_ino)
    real_absolute = os.path.normpath(os.path.realpath(absolute))
    if relative and (inode in seen_directories or real_absolute in mount_points):
        return (
            _add_entry(entries, relative, EntryKind.DIRECTORY, path_bytes)
            if include_self
            else path_bytes
        )
    seen_directories.add(inode)

    wd = _add_watch(inotify, absolute)
    if wd in wd_to_directory and wd_to_directory[wd] != relative:
        # inotify coalesces watches for aliases of one inode. Keep the first
        # lexical location and expose this alias only as a pruned directory.
        return (
            _add_entry(entries, relative, EntryKind.DIRECTORY, path_bytes)
            if include_self
            else path_bytes
        )
    wd_to_directory[wd] = relative
    directory_to_wd[relative] = wd
    if len(directory_to_wd) > MAX_FILE_TREE_WATCHES_GLOBAL:
        raise FileTreeError(
            "watch_limit_exceeded",
            "The application file-tree watch budget of "
            f"{MAX_FILE_TREE_WATCHES_GLOBAL} directories was exceeded.",
        )

    try:
        iterator = os.scandir(absolute)
    except PermissionError:
        _remove_watch(inotify, wd)
        wd_to_directory.pop(wd, None)
        directory_to_wd.pop(relative, None)
        if not relative:
            raise FileTreeError(
                "working_directory_unavailable",
                "The working directory is not accessible.",
            ) from None
        return path_bytes
    except (FileNotFoundError, NotADirectoryError):
        _remove_watch(inotify, wd)
        wd_to_directory.pop(wd, None)
        directory_to_wd.pop(relative, None)
        if not relative:
            raise FileTreeError(
                "working_directory_unavailable",
                "The working directory is unavailable.",
            ) from None
        return path_bytes
    except OSError as exc:
        raise FileTreeError(
            "scan_failed", f"Could not enumerate {absolute!r}: {exc}"
        ) from exc

    if include_self:
        path_bytes = _add_entry(entries, relative, EntryKind.DIRECTORY, path_bytes)

    try:
        with iterator:
            for child in iterator:
                if not _check_utf8(child.name, child.path):
                    continue
                child_relative = f"{relative}/{child.name}" if relative else child.name
                try:
                    child_stat = child.stat(follow_symlinks=False)
                except (FileNotFoundError, NotADirectoryError, PermissionError):
                    continue
                except OSError as exc:
                    raise FileTreeError(
                        "scan_failed", f"Could not inspect {child.path!r}: {exc}"
                    ) from exc

                mode = child_stat.st_mode
                if stat.S_ISLNK(mode):
                    kind = EntryKind.SYMLINK
                elif stat.S_ISREG(mode):
                    kind = EntryKind.FILE
                elif stat.S_ISDIR(mode):
                    kind = EntryKind.DIRECTORY
                else:
                    continue
                if _is_ignored(spec, child_relative, kind is EntryKind.DIRECTORY):
                    continue
                if kind is EntryKind.DIRECTORY:
                    path_bytes = _scan_into(
                        root=root,
                        relative=child_relative,
                        inotify=inotify,
                        spec=spec,
                        entries=entries,
                        path_bytes=path_bytes,
                        wd_to_directory=wd_to_directory,
                        directory_to_wd=directory_to_wd,
                        seen_directories=seen_directories,
                        mount_points=mount_points,
                        include_self=True,
                    )
                else:
                    path_bytes = _add_entry(entries, child_relative, kind, path_bytes)
    except FileTreeError:
        raise
    except OSError as exc:
        raise FileTreeError(
            "scan_failed", f"Could not enumerate {absolute!r}: {exc}"
        ) from exc
    return path_bytes


def scan_tree(root: str) -> ScanResult:
    root = normalize_root(root)
    if not os.path.isdir(root):
        raise FileTreeError(
            "working_directory_unavailable", "The working directory is unavailable."
        )
    try:
        inotify = INotify(nonblocking=True)
    except OSError as exc:
        raise _error_for_inotify(exc) from exc
    root_guard_wd: int | None = None
    try:
        # A normal directory watch follows a symlinked root and cannot observe
        # replacement of the lexical symlink itself. Guard that inode as well.
        if stat.S_ISLNK(os.lstat(root).st_mode):
            root_guard_wd = _add_watch(
                inotify,
                root,
                flags.ATTRIB | flags.DELETE_SELF | flags.MOVE_SELF | flags.DONT_FOLLOW,
            )
        # Watch before loading the ignore file so a concurrent replacement is
        # retained in the kernel queue and causes a post-snapshot rebuild.
        _add_watch(inotify, root)
        spec = _compile_ignore(root)
    except FileTreeError as exc:
        inotify.close()
        if exc.code == "watcher_failed" and not os.path.isdir(root):
            raise FileTreeError(
                "working_directory_unavailable",
                "The working directory is unavailable.",
            ) from exc
        raise

    entries: dict[str, EntryKind] = {}
    wd_to_directory: dict[int, str] = {}
    directory_to_wd: dict[str, int] = {}
    try:
        path_bytes = _scan_into(
            root=root,
            relative="",
            inotify=inotify,
            spec=spec,
            entries=entries,
            path_bytes=0,
            wd_to_directory=wd_to_directory,
            directory_to_wd=directory_to_wd,
            seen_directories=set(),
            mount_points=_mount_points() - {os.path.normpath(os.path.realpath(root))},
            include_self=False,
        )
        return ScanResult(
            root=root,
            entries=entries,
            path_bytes=path_bytes,
            inotify=inotify,
            wd_to_directory=wd_to_directory,
            directory_to_wd=directory_to_wd,
            ignore_spec=spec,
            root_guard_wd=root_guard_wd,
        )
    except Exception:
        inotify.close()
        raise


def _wire_entries(entries: dict[str, EntryKind]) -> list[str]:
    return sorted(_wire_path(relative, kind) for relative, kind in entries.items())


def _snapshot(tree: FileTree) -> dict[str, Any]:
    return {
        "type": "file_tree_snapshot",
        "generation": tree.generation,
        "revision": tree.revision,
        "paths": _wire_entries(tree.entries),
    }


class FileTreeManager:
    def __init__(self) -> None:
        self.file_trees: dict[str, FileTree] = {}
        self.file_tree_initializations: dict[str, asyncio.Task[FileTree]] = {}
        self.pending: dict[str, int] = {}
        self.registry_lock = asyncio.Lock()
        self.global_paths = 0
        self.global_path_bytes = 0
        self.global_watches = 0

    async def acquire(self, root: str) -> FileTree:
        root = normalize_root(root)
        async with self.registry_lock:
            self.pending[root] = self.pending.get(root, 0) + 1
            tree = self.file_trees.get(root)
            if tree is not None and not tree.invalidated:
                return tree
            task = self.file_tree_initializations.get(root)
            if task is None:
                task = asyncio.create_task(self._initialize(root))
                self.file_tree_initializations[root] = task
        try:
            return await asyncio.shield(task)
        except BaseException:
            await self.release_pending(root)
            raise

    async def _initialize(self, root: str) -> FileTree:
        try:
            candidate = await asyncio.to_thread(scan_tree, root)
            tree = FileTree(
                root=root,
                generation=str(uuid.uuid4()),
                revision=0,
                entries=candidate.entries,
                path_bytes=candidate.path_bytes,
                inotify=candidate.inotify,
                wd_to_directory=candidate.wd_to_directory,
                directory_to_wd=candidate.directory_to_wd,
                ignore_spec=candidate.ignore_spec,
                root_guard_wd=candidate.root_guard_wd,
            )
            async with self.registry_lock:
                if self.pending.get(root, 0) == 0:
                    candidate.inotify.close()
                    raise asyncio.CancelledError()
                try:
                    self._check_global_fit(tree, replacing=None)
                except BaseException:
                    candidate.inotify.close()
                    raise
                self._add_accounting(tree)
                self.file_trees[root] = tree
                self.file_tree_initializations.pop(root, None)
            self._start_events(tree)
            return tree
        except BaseException:
            async with self.registry_lock:
                current = self.file_tree_initializations.get(root)
                if current is asyncio.current_task():
                    self.file_tree_initializations.pop(root, None)
            raise

    def _check_global_fit(
        self, candidate: FileTree | ScanResult, replacing: FileTree | None
    ) -> None:
        paths = (
            self.global_paths
            - (len(replacing.entries) if replacing else 0)
            + len(candidate.entries)
        )
        path_bytes = (
            self.global_path_bytes
            - (replacing.path_bytes if replacing else 0)
            + candidate.path_bytes
        )
        watches = (
            self.global_watches
            - (replacing.watch_count if replacing else 0)
            + candidate.watch_count
        )
        if (
            paths > MAX_FILE_TREE_PATHS_GLOBAL
            or path_bytes > MAX_FILE_TREE_PATH_BYTES_GLOBAL
        ):
            raise FileTreeError(
                "global_path_limit_exceeded",
                "Activating this working directory would exceed the server's "
                "global file-tree path budget.",
            )
        if watches > MAX_FILE_TREE_WATCHES_GLOBAL:
            raise FileTreeError(
                "watch_limit_exceeded",
                "Activating this working directory would exceed the server's "
                "global file-tree watch budget.",
            )

    def _add_accounting(self, tree: FileTree) -> None:
        self.global_paths += len(tree.entries)
        self.global_path_bytes += tree.path_bytes
        self.global_watches += tree.watch_count

    def _remove_accounting(self, tree: FileTree) -> None:
        self.global_paths -= len(tree.entries)
        self.global_path_bytes -= tree.path_bytes
        self.global_watches -= tree.watch_count

    async def add_subscriber(
        self, tree: FileTree, session_id: int, websocket: WebSocket
    ) -> FileTreeSubscriber:
        subscriber = FileTreeSubscriber(
            session_id=session_id,
            websocket=websocket,
            outbound=asyncio.Queue(maxsize=FILE_TREE_SUBSCRIBER_QUEUE_FRAMES),
        )
        async with self.registry_lock:
            current = self.file_trees.get(tree.root)
            if current is not tree or tree.invalidated:
                raise FileTreeError(
                    "watcher_failed", "The file tree was replaced during subscription."
                )
            async with tree.lock:
                subscriber.outbound.put_nowait(_snapshot(tree))
                subscriber.writer = asyncio.create_task(
                    self._write_subscriber(subscriber)
                )
                tree.subscribers.add(subscriber)
                await self._decrement_pending_locked(tree.root)
        return subscriber

    async def release_pending(self, root: str) -> None:
        root = normalize_root(root)
        tree_to_stop: FileTree | None = None
        async with self.registry_lock:
            await self._decrement_pending_locked(root)
            tree = self.file_trees.get(root)
            if (
                tree is not None
                and not tree.subscribers
                and self.pending.get(root, 0) == 0
            ):
                self.file_trees.pop(root, None)
                self._remove_accounting(tree)
                tree.invalidated = True
                tree_to_stop = tree
        if tree_to_stop is not None:
            await self._stop_tree(tree_to_stop)

    async def _decrement_pending_locked(self, root: str) -> None:
        count = self.pending.get(root, 0)
        if count <= 1:
            self.pending.pop(root, None)
        else:
            self.pending[root] = count - 1

    def _start_events(self, tree: FileTree) -> None:
        loop = asyncio.get_running_loop()
        loop.add_reader(tree.inotify.fd, self._read_ready, tree)
        tree.reader_installed = True
        tree.event_task = asyncio.create_task(self._event_loop(tree))

    def _remove_reader(self, tree: FileTree) -> None:
        if tree.reader_installed:
            asyncio.get_running_loop().remove_reader(tree.inotify.fd)
            tree.reader_installed = False

    def _read_ready(self, tree: FileTree) -> None:
        if tree.invalidated or tree.rebuilding:
            return
        try:
            events = tree.inotify.read(timeout=0)
            if events:
                tree.event_queue.put_nowait(events)
        except BlockingIOError:
            return
        except Exception as exc:
            error = (
                _error_for_inotify(exc)
                if isinstance(exc, OSError)
                else FileTreeError(
                    "watcher_failed", f"The file-tree watcher failed: {exc}"
                )
            )
            asyncio.create_task(self.invalidate(tree, error))

    async def _event_loop(self, tree: FileTree) -> None:
        try:
            while not tree.invalidated:
                batch = list(await tree.event_queue.get())
                started = asyncio.get_running_loop().time()
                deadline = started + FILE_TREE_PATCH_MAX_LATENCY
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        more = await asyncio.wait_for(
                            tree.event_queue.get(),
                            timeout=min(FILE_TREE_PATCH_DEBOUNCE, remaining),
                        )
                    except TimeoutError:
                        break
                    batch.extend(more)
                await self._process_batch(tree, batch)
        except asyncio.CancelledError:
            raise
        except FileTreeError as exc:
            await self.invalidate(tree, exc)
        except Exception as exc:
            logger.exception("File-tree event processing failed for %r", tree.root)
            await self.invalidate(
                tree,
                FileTreeError("watcher_failed", f"The file-tree watcher failed: {exc}"),
            )

    async def _process_batch(self, tree: FileTree, events: list[Any]) -> None:
        if tree.invalidated:
            return
        overflow = int(flags.Q_OVERFLOW)
        if any(event.mask & overflow for event in events):
            raise FileTreeError(
                "watcher_overflow",
                "The filesystem event queue overflowed; reconnect to rebuild "
                "the file tree.",
            )

        ignore_changed = False
        affected: set[str] = set()
        ignored_mask = int(flags.IGNORED)
        self_mask = int(flags.DELETE_SELF | flags.MOVE_SELF)
        close_write = int(flags.CLOSE_WRITE)
        structural = int(
            flags.CREATE
            | flags.DELETE
            | flags.MOVED_FROM
            | flags.MOVED_TO
            | flags.ATTRIB
        )
        for event in events:
            if event.wd == tree.root_guard_wd:
                raise FileTreeError(
                    "working_directory_unavailable",
                    "The symlinked working-directory root was changed.",
                )
            directory = tree.wd_to_directory.get(event.wd)
            if event.mask & ignored_mask:
                if directory == "":
                    raise FileTreeError(
                        "working_directory_unavailable",
                        "The working-directory watch was removed.",
                    )
                if directory is not None:
                    affected.add(directory)
                continue
            if directory is None:
                continue
            if event.mask & self_mask:
                if directory == "":
                    raise FileTreeError(
                        "working_directory_unavailable",
                        "The working directory was removed or moved.",
                    )
                affected.add(directory)
            if not event.name:
                if event.mask & int(flags.ATTRIB) and directory:
                    affected.add(directory)
                elif event.mask & int(flags.ATTRIB) and not os.path.isdir(tree.root):
                    raise FileTreeError(
                        "working_directory_unavailable",
                        "The working directory is not accessible.",
                    )
                continue
            absolute = os.path.join(tree.root, directory, event.name)
            if not _check_utf8(event.name, absolute):
                continue
            relative = f"{directory}/{event.name}" if directory else event.name
            if relative == IGNORE_FILE and (event.mask & (structural | close_write)):
                ignore_changed = True
            elif event.mask & structural:
                affected.add(relative)

        if ignore_changed:
            await self._rebuild(tree)
            return
        if not affected:
            return

        # An ancestor reconciliation subsumes all children in this batch.
        selected: list[str] = []
        for relative in sorted(affected, key=lambda value: (value.count("/"), value)):
            if not any(
                relative == parent or relative.startswith(parent + "/")
                for parent in selected
            ):
                selected.append(relative)

        self._remove_reader(tree)
        reader_should_resume = True
        try:
            result = await asyncio.to_thread(self._reconcile, tree, selected)
            old_wire = set(_wire_entries(tree.entries))
            new_wire = set(_wire_entries(result.entries))
            added = sorted(new_wire - old_wire)
            removed = sorted(old_wire - new_wire)
            patch_bytes = sum(len(path.encode("utf-8")) for path in added + removed)
            if (
                len(added) + len(removed) > MAX_FILE_TREE_PATCH_PATHS
                or patch_bytes > MAX_FILE_TREE_PATCH_PATH_BYTES
            ):
                reader_should_resume = False
                await self._rebuild(tree)
                return

            retired: list[FileTreeSubscriber] = []
            async with self.registry_lock:
                if self.file_trees.get(tree.root) is not tree or tree.invalidated:
                    return
                self._check_growth_fit(tree, result)
                async with tree.lock:
                    base = tree.revision
                    self.global_paths += len(result.entries) - len(tree.entries)
                    self.global_path_bytes += result.path_bytes - tree.path_bytes
                    self.global_watches += result.watch_count - tree.watch_count
                    tree.entries = result.entries
                    tree.path_bytes = result.path_bytes
                    tree.wd_to_directory = result.wd_to_directory
                    tree.directory_to_wd = result.directory_to_wd
                    if added or removed:
                        tree.revision += 1
                        frame = {
                            "type": "file_tree_patch",
                            "generation": tree.generation,
                            "base_revision": base,
                            "revision": tree.revision,
                            "added": added,
                            "removed": removed,
                        }
                        retired = self._enqueue_locked(tree, frame)
            await self._teardown_many(retired, code=1011)
            await self._cleanup_unused(tree)
        finally:
            if reader_should_resume and not tree.invalidated and not tree.rebuilding:
                self._start_reader_only(tree)

    def _check_growth_fit(self, tree: FileTree, result: ScanResult) -> None:
        paths = self.global_paths - len(tree.entries) + len(result.entries)
        path_bytes = self.global_path_bytes - tree.path_bytes + result.path_bytes
        watches = self.global_watches - tree.watch_count + result.watch_count
        if (
            paths > MAX_FILE_TREE_PATHS_GLOBAL
            or path_bytes > MAX_FILE_TREE_PATH_BYTES_GLOBAL
        ):
            raise FileTreeError(
                "global_path_limit_exceeded",
                "Growing this working directory would exceed the server's "
                "global file-tree path budget.",
            )
        if watches > MAX_FILE_TREE_WATCHES_GLOBAL:
            raise FileTreeError(
                "watch_limit_exceeded",
                "Growing this working directory would exceed the server's "
                "global file-tree watch budget.",
            )

    def _reconcile(self, tree: FileTree, selected: list[str]) -> ScanResult:
        entries = dict(tree.entries)
        path_bytes = tree.path_bytes
        wd_to_directory = dict(tree.wd_to_directory)
        directory_to_wd = dict(tree.directory_to_wd)

        # Remove every old location before scanning any new location. This is
        # important for an in-tree directory rename: both lexical paths refer
        # to the same inode while the batch is reconciled.
        for relative in selected:
            prefixes = [
                key
                for key in entries
                if key == relative or key.startswith(relative + "/")
            ]
            for key in prefixes:
                path_bytes -= len(_wire_path(key, entries.pop(key)).encode("utf-8"))
            watched = [
                key
                for key in directory_to_wd
                if key == relative or key.startswith(relative + "/")
            ]
            for directory in sorted(watched, key=len, reverse=True):
                wd = directory_to_wd.pop(directory)
                wd_to_directory.pop(wd, None)
                _remove_watch(tree.inotify, wd)

        seen: set[tuple[int, int]] = set()
        for directory in directory_to_wd:
            try:
                absolute = (
                    os.path.join(tree.root, directory) if directory else tree.root
                )
                value = os.stat(absolute)
                seen.add((value.st_dev, value.st_ino))
            except OSError:
                pass
        mount_points = _mount_points() - {os.path.normpath(os.path.realpath(tree.root))}

        for relative in selected:
            absolute = os.path.join(tree.root, relative)
            try:
                entry_stat = os.lstat(absolute)
            except (FileNotFoundError, NotADirectoryError, PermissionError):
                continue
            except OSError as exc:
                raise FileTreeError(
                    "scan_failed", f"Could not inspect {absolute!r}: {exc}"
                ) from exc
            mode = entry_stat.st_mode
            if stat.S_ISLNK(mode):
                kind = EntryKind.SYMLINK
            elif stat.S_ISREG(mode):
                kind = EntryKind.FILE
            elif stat.S_ISDIR(mode):
                kind = EntryKind.DIRECTORY
            else:
                continue
            if _is_ignored(tree.ignore_spec, relative, kind is EntryKind.DIRECTORY):
                continue
            if kind is not EntryKind.DIRECTORY:
                path_bytes = _add_entry(entries, relative, kind, path_bytes)
                continue

            path_bytes = _scan_into(
                root=tree.root,
                relative=relative,
                inotify=tree.inotify,
                spec=tree.ignore_spec,
                entries=entries,
                path_bytes=path_bytes,
                wd_to_directory=wd_to_directory,
                directory_to_wd=directory_to_wd,
                seen_directories=seen,
                mount_points=mount_points,
                include_self=True,
            )

        return ScanResult(
            tree.root,
            entries,
            path_bytes,
            tree.inotify,
            wd_to_directory,
            directory_to_wd,
            tree.ignore_spec,
            tree.root_guard_wd,
        )

    async def _rebuild(self, tree: FileTree) -> None:
        if tree.invalidated:
            return
        tree.rebuilding = True
        self._remove_reader(tree)
        # Watch descriptors are scoped to an inotify instance and may be reused
        # by the replacement. Never interpret already-drained old-instance
        # events against the replacement maps.
        while not tree.event_queue.empty():
            try:
                tree.event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        old_inotify = tree.inotify
        old_inotify.close()
        try:
            candidate = await asyncio.to_thread(scan_tree, tree.root)
        except BaseException:
            tree.rebuilding = False
            raise

        retired: list[FileTreeSubscriber] = []
        try:
            async with self.registry_lock:
                if self.file_trees.get(tree.root) is not tree or tree.invalidated:
                    candidate.inotify.close()
                    return
                self._check_global_fit(candidate, replacing=tree)
                async with tree.lock:
                    self._remove_accounting(tree)
                    tree.entries = candidate.entries
                    tree.path_bytes = candidate.path_bytes
                    tree.inotify = candidate.inotify
                    tree.wd_to_directory = candidate.wd_to_directory
                    tree.directory_to_wd = candidate.directory_to_wd
                    tree.ignore_spec = candidate.ignore_spec
                    tree.root_guard_wd = candidate.root_guard_wd
                    tree.generation = str(uuid.uuid4())
                    tree.revision = 0
                    self._add_accounting(tree)
                    retired = self._enqueue_locked(tree, _snapshot(tree))
            tree.rebuilding = False
            self._start_reader_only(tree)
        except BaseException:
            candidate.inotify.close()
            tree.rebuilding = False
            raise
        await self._teardown_many(retired, code=1011)
        await self._cleanup_unused(tree)

    def _start_reader_only(self, tree: FileTree) -> None:
        asyncio.get_running_loop().add_reader(tree.inotify.fd, self._read_ready, tree)
        tree.reader_installed = True

    def _enqueue_locked(
        self, tree: FileTree, frame: dict[str, Any]
    ) -> list[FileTreeSubscriber]:
        retired: list[FileTreeSubscriber] = []
        for subscriber in list(tree.subscribers):
            if subscriber.retired:
                continue
            try:
                subscriber.outbound.put_nowait(frame)
            except asyncio.QueueFull:
                self._retire_locked(tree, subscriber)
                retired.append(subscriber)
        return retired

    def _retire_locked(self, tree: FileTree, subscriber: FileTreeSubscriber) -> None:
        if subscriber.retired:
            return
        subscriber.retired = True
        tree.subscribers.discard(subscriber)
        writer = subscriber.writer
        if (
            writer is not None
            and writer is not asyncio.current_task()
            and not writer.done()
        ):
            writer.cancel()

    async def _cleanup_unused(self, tree: FileTree) -> None:
        stop = False
        async with self.registry_lock:
            if (
                self.file_trees.get(tree.root) is tree
                and not tree.subscribers
                and self.pending.get(tree.root, 0) == 0
            ):
                self.file_trees.pop(tree.root, None)
                self._remove_accounting(tree)
                tree.invalidated = True
                stop = True
        if stop:
            await self._stop_tree(tree)

    async def remove_subscriber(
        self, tree: FileTree, subscriber: FileTreeSubscriber
    ) -> None:
        stop = False
        async with self.registry_lock:
            async with tree.lock:
                self._retire_locked(tree, subscriber)
            if (
                self.file_trees.get(tree.root) is tree
                and not tree.subscribers
                and self.pending.get(tree.root, 0) == 0
            ):
                self.file_trees.pop(tree.root, None)
                self._remove_accounting(tree)
                tree.invalidated = True
                stop = True
        await self._teardown_subscriber(subscriber, subscriber.close_code)
        if stop:
            await self._stop_tree(tree)

    async def invalidate(self, tree: FileTree, error: FileTreeError) -> None:
        doomed: list[FileTreeSubscriber] = []
        async with self.registry_lock:
            if tree.invalidated:
                return
            tree.invalidated = True
            if self.file_trees.get(tree.root) is tree:
                self.file_trees.pop(tree.root, None)
                self._remove_accounting(tree)
            async with tree.lock:
                for subscriber in list(tree.subscribers):
                    subscriber.retired = True
                    subscriber.close_code = 1011
                    # An error supersedes stale patches and must be the next frame.
                    while not subscriber.outbound.empty():
                        try:
                            subscriber.outbound.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    with suppress(asyncio.QueueFull):
                        subscriber.outbound.put_nowait(error.frame())
                    tree.subscribers.discard(subscriber)
                    doomed.append(subscriber)
        await self._stop_tree(tree)
        # Let each sole writer send the prioritized error before closing.
        for subscriber in doomed:
            writer = subscriber.writer
            if writer is not None and not writer.done():
                try:
                    async with asyncio.timeout(WEBSOCKET_SEND_TIMEOUT_SECONDS):
                        await asyncio.shield(writer)
                except TimeoutError:
                    writer.cancel()
        await self._teardown_many(doomed, code=1011)

    async def close_session(self, session_id: int) -> None:
        doomed: list[tuple[FileTree, FileTreeSubscriber]] = []
        async with self.registry_lock:
            for tree in list(self.file_trees.values()):
                async with tree.lock:
                    for subscriber in list(tree.subscribers):
                        if subscriber.session_id == session_id:
                            self._retire_locked(tree, subscriber)
                            doomed.append((tree, subscriber))
        for tree, subscriber in doomed:
            await self.remove_subscriber(tree, subscriber)

    async def shutdown(self) -> None:
        async with self.registry_lock:
            trees = list(self.file_trees.values())
            tasks = list(self.file_tree_initializations.values())
            self.file_trees.clear()
            self.file_tree_initializations.clear()
            self.pending.clear()
            self.global_paths = self.global_path_bytes = self.global_watches = 0
            for tree in trees:
                tree.invalidated = True
        # A filesystem walk running in a worker thread cannot be canceled
        # safely. With pending cleared, each initializer closes its candidate
        # instead of publishing it when the walk returns.
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for tree in trees:
            doomed = list(tree.subscribers)
            for subscriber in doomed:
                subscriber.retired = True
            await self._teardown_many(doomed, code=1001)
            await self._stop_tree(tree)

    async def _stop_tree(self, tree: FileTree) -> None:
        self._remove_reader(tree)
        current = asyncio.current_task()
        if (
            tree.event_task is not None
            and tree.event_task is not current
            and not tree.event_task.done()
        ):
            tree.event_task.cancel()
            await asyncio.gather(tree.event_task, return_exceptions=True)
        with suppress(OSError):
            tree.inotify.close()

    async def _write_subscriber(self, subscriber: FileTreeSubscriber) -> None:
        try:
            while not subscriber.retired or not subscriber.outbound.empty():
                frame = await subscriber.outbound.get()
                async with asyncio.timeout(WEBSOCKET_SEND_TIMEOUT_SECONDS):
                    await subscriber.websocket.send_json(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            subscriber.send_failed = True
            subscriber.close_code = 1011

    async def _teardown_many(
        self, subscribers: list[FileTreeSubscriber], code: int
    ) -> None:
        for subscriber in subscribers:
            await self._teardown_subscriber(subscriber, code)

    async def _teardown_subscriber(
        self, subscriber: FileTreeSubscriber, code: int
    ) -> None:
        if subscriber.closed:
            return
        subscriber.closed = True
        writer = subscriber.writer
        current = asyncio.current_task()
        if writer is not None and writer is not current and not writer.done():
            writer.cancel()
        if writer is not None and writer is not current:
            await asyncio.gather(writer, return_exceptions=True)
        try:
            async with asyncio.timeout(WEBSOCKET_CLOSE_TIMEOUT_SECONDS):
                await subscriber.websocket.close(code=code)
        except Exception:
            pass


async def receive_disconnect(websocket: WebSocket) -> None:
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
    except (WebSocketDisconnect, RuntimeError):
        return


file_tree_manager = FileTreeManager()
