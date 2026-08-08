"""One-shot shell commands, run outside the agent entirely.

Bash mode exists so a quick `git status` costs neither an SSH round trip nor a
whole agent turn. Nothing here is persistent: every call spawns a fresh
`bash -lc`, captures what it prints, and lets it exit — no shell state, no
working-directory changes, and no history carried to the next invocation.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from collections import deque
from typing import Any


# Same 64 MiB ceiling the agent adapters use for a single stream read.
STREAM_LIMIT = 64 * 1024 * 1024

READ_CHUNK = 64 * 1024

# A command with nobody watching it must not run forever, and its output must
# not be able to fill the database or a WebSocket frame. Both are per-command;
# the output cap applies to stdout and stderr separately.
BASH_TIMEOUT_SECONDS = float(os.environ.get("BASH_TIMEOUT_SECONDS", "120"))
BASH_OUTPUT_LIMIT = int(os.environ.get("BASH_OUTPUT_LIMIT", str(100 * 1024)))

# How long a timed-out command gets to honour SIGTERM before SIGKILL.
KILL_GRACE_SECONDS = 3.0


async def run_command(
    command: str,
    cwd: str,
    timeout: float | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Run `command` in `cwd` and return once it has exited.

    Always returns a result dict rather than raising: a working directory that
    vanished, a command killed by the timeout, and a clean exit all come back
    in the same shape, so the caller has exactly one terminal event to persist.
    The one exception is cancellation, which kills the process group and
    propagates — that is how the server stops a runaway command.
    """
    timeout = BASH_TIMEOUT_SECONDS if timeout is None else timeout
    limit = BASH_OUTPUT_LIMIT if limit is None else limit
    started = time.monotonic()

    def elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        process = await asyncio.create_subprocess_exec(
            "/bin/bash",
            "-lc",
            command,
            cwd=cwd,
            env=_build_env(),
            # EOF rather than a terminal: a command that decides to prompt gets
            # a closed stdin and gives up instead of hanging until the timeout.
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Its own process group, so a kill reaches whatever the command
            # backgrounded rather than only the shell itself.
            start_new_session=True,
            limit=STREAM_LIMIT,
        )
    except OSError as exc:
        # Most likely the session's working directory was removed or made
        # unreadable after the session was created.
        return {
            "stdout": "",
            "stderr": f"Could not run the command: {exc}",
            "exit_code": None,
            "duration_ms": elapsed_ms(),
            "timed_out": False,
            "truncated": False,
        }

    stdout_task = asyncio.create_task(_drain(process.stdout, limit))
    stderr_task = asyncio.create_task(_drain(process.stderr, limit))
    timed_out = False

    try:
        try:
            await asyncio.wait_for(process.wait(), timeout)
        except asyncio.TimeoutError:
            timed_out = True
            await _kill_group(process)

        # The readers finish as soon as the pipes close, which the kill above
        # guarantees, so whatever the command managed to print is still here.
        stdout, stdout_truncated = await stdout_task
        stderr, stderr_truncated = await stderr_task
    except asyncio.CancelledError:
        # No awaiting on the way out — a second cancel would strand the
        # process. Signal the group outright and let the loop reap it.
        _signal_group(process, signal.SIGKILL)
        stdout_task.cancel()
        stderr_task.cancel()
        raise

    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": process.returncode,
        "duration_ms": elapsed_ms(),
        "timed_out": timed_out,
        "truncated": stdout_truncated or stderr_truncated,
    }


async def _drain(
    stream: asyncio.StreamReader | None,
    limit: int,
) -> tuple[str, bool]:
    """Read a stream to EOF, keeping at most `limit` bytes of head and tail.

    Reading never stops once the cap is reached — the excess is discarded
    rather than left in the pipe, so a command like `yes` cannot wedge on a
    full buffer and outlive its own output. Head *and* tail are kept because
    the interesting part of a failed build is at one end or the other.
    """
    if stream is None:
        return "", False

    head_limit = max(limit // 2, 1)
    tail_limit = max(limit - head_limit, 0)
    head = bytearray()
    tail: deque[bytes] = deque()
    tail_size = 0
    omitted = 0

    while True:
        chunk = await stream.read(READ_CHUNK)
        if not chunk:
            break

        if len(head) < head_limit:
            take = head_limit - len(head)
            head += chunk[:take]
            chunk = chunk[take:]
            if not chunk:
                continue

        if tail_limit == 0:
            omitted += len(chunk)
            continue

        tail.append(chunk)
        tail_size += len(chunk)
        # Drop whole chunks while the rest still covers the tail budget; the
        # final partial trim happens once, below.
        while tail and tail_size - len(tail[0]) >= tail_limit:
            dropped = tail.popleft()
            tail_size -= len(dropped)
            omitted += len(dropped)

    tail_bytes = b"".join(tail)
    if len(tail_bytes) > tail_limit:
        omitted += len(tail_bytes) - tail_limit
        tail_bytes = tail_bytes[-tail_limit:]

    text = head.decode("utf-8", errors="replace")
    if omitted == 0:
        return text + tail_bytes.decode("utf-8", errors="replace"), False

    marker = f"\n… {omitted} bytes omitted …\n"
    return text + marker + tail_bytes.decode("utf-8", errors="replace"), True


async def _kill_group(process: asyncio.subprocess.Process) -> None:
    """SIGTERM the command's process group, then SIGKILL if it lingers."""
    if process.returncode is not None:
        return

    _signal_group(process, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), KILL_GRACE_SECONDS)
    except asyncio.TimeoutError:
        _signal_group(process, signal.SIGKILL)
        await process.wait()


def _signal_group(process: asyncio.subprocess.Process, sig: int) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except (ProcessLookupError, PermissionError):
        pass


def _build_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("SHELL", "/bin/bash")
    return env
