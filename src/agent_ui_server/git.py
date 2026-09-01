"""Git commands the server runs on its own behalf, for per-session worktrees.

Deliberately not routed through `shell.run_command`: that runs `bash -lc` with
an interpolated string, so a path or branch name containing shell metacharacters
would be an injection. Everything here is an argv list handed straight to
`git`, with no shell in between.

Like `shell.py`, these return a result rather than raising — a missing git, a
hung git and a git that refused all come back as an error string the caller can
put in a 400 body.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any


# A wedged git must not hold a request open indefinitely. All of these are
# local operations, so anything near this is already pathological.
GIT_TIMEOUT_SECONDS = float(os.environ.get("GIT_TIMEOUT_SECONDS", "30"))

# git's diagnostics are a few lines; the cap is only there so a runaway message
# cannot end up in an HTTP body verbatim.
GIT_OUTPUT_LIMIT = int(os.environ.get("GIT_OUTPUT_LIMIT", str(4 * 1024)))


async def is_git_repo(path: str) -> bool:
    """Whether `path` is inside a git working tree."""
    code, stdout, _ = await _run("-C", path, "rev-parse", "--is-inside-work-tree")
    return code == 0 and stdout == "true"


async def check_branch_name(name: str) -> bool:
    """Validate a branch name with git's own rules rather than a regex.

    Rejects `..`, spaces, a leading `-`, and the rest of the ref-format list —
    including the spellings a hand-rolled check tends to miss.
    """
    code, _, _ = await _run("check-ref-format", "--branch", name)
    return code == 0


async def add_worktree(repo: str, path: str, branch: str) -> str | None:
    """Create a worktree at `path` on a new `branch`. Returns git's error, if any.

    `path` may already exist as long as it is an **empty** directory — git's own
    rule — and the caller is expected to have created it. This command is not
    atomic: it writes the new branch ref before creating the leading
    directories, so letting it create them itself means a filesystem failure
    leaves an orphaned branch with no worktree attached to it.
    """
    code, _, stderr = await _run(
        "-C", repo, "worktree", "add", "-b", branch, path
    )
    return None if code == 0 else _describe(code, stderr, "git worktree add")


async def remove_worktree(repo: str, path: str) -> str | None:
    """Remove a worktree. Returns git's error, if any.

    **No `--force`**: a worktree with modified or untracked files refuses to be
    removed, and that refusal is the point — deleting a session must not be
    able to destroy uncommitted work. A worktree whose directory was already
    deleted by hand needs no special case; git exits 0 and cleans up its own
    admin files.
    """
    code, _, stderr = await _run("-C", repo, "worktree", "remove", path)
    return None if code == 0 else _describe(code, stderr, "git worktree remove")


async def _run(*args: str) -> tuple[int | None, str, str]:
    """Run `git` with these arguments; return (exit code, stdout, stderr).

    A git that could not be started or had to be killed comes back with a
    `None` exit code and the reason in stderr, so callers have one shape to
    handle.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_build_env(),
        )
    except OSError as exc:
        return None, "", f"Could not run git: {exc}"

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), GIT_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        _kill(process)
        await process.wait()
        return None, "", f"git timed out after {GIT_TIMEOUT_SECONDS:g}s"
    except asyncio.CancelledError:
        _kill(process)
        raise

    return process.returncode, _trim(stdout), _trim(stderr)


def _kill(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.kill()
    except ProcessLookupError:
        pass


def _trim(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    if len(text) > GIT_OUTPUT_LIMIT:
        return text[:GIT_OUTPUT_LIMIT] + "…"
    return text


def _describe(code: int | None, stderr: str, command: str) -> str:
    """git's own message, or something to show when it exited silently."""
    return stderr or f"{command} failed (exit {code})"


def _build_env() -> dict[str, Any]:
    env = dict(os.environ)
    # Nothing here talks to a remote, and a git that decides to ask for
    # credentials would hang until the timeout instead of failing.
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env
