from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from . import git, shell
from .actions import auto_approval_setting
from .agent import AgentAdapter, ClaudeCodeAdapter, PiAdapter
from .db import Database
from .network_guard import NetworkGuardMiddleware, allowed_hosts_from_env
from .file_tree import (
    FileTreeError,
    file_tree_manager,
    normalize_root,
    receive_disconnect,
)
from .sandbox_paths import merge_paths, validate_paths
from .usage import collect_usage

SCROLLBACK_REPLAY_LIMIT = 200
WEBSOCKET_LIVE_QUEUE_CAPACITY = 256
WEBSOCKET_SEND_TIMEOUT_SECONDS = 30.0
WEBSOCKET_CLOSE_TIMEOUT_SECONDS = 5.0

app = FastAPI(title="agent-ui-server")
app.add_middleware(
    NetworkGuardMiddleware,
    allowed_hosts=allowed_hosts_from_env(),
    port=int(os.environ.get("PORT", "8000")),
)
db =Database(os.environ.get("SESSION_DB", "sessions.db"))
adapters: dict[str, AgentAdapter] = {
    "claude-code": ClaudeCodeAdapter(),
    "pi": PiAdapter(),
}


@dataclass(eq=False)
class Subscriber:
    websocket: WebSocket
    outbound: asyncio.Queue[dict[str, Any]]
    writer: asyncio.Task[None] | None = None
    retired: bool = False
    closed: bool = False
    send_failed: bool = False


subscribers: dict[int, set[Subscriber]] = defaultdict(set)
file_socket_tasks: dict[int, set[asyncio.Task[None]]] = defaultdict(set)
# Session ids are monotonic and the expected count is tiny. Keeping locks for
# the process lifetime avoids unsafe cleanup while another task is waiting.
stream_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
running_tasks: dict[int, asyncio.Task[None]] = {}
# Bash-mode commands are tracked separately from agent turns on purpose: they
# are allowed to run alongside one, so they must not share the turn's slot.
bash_tasks: dict[int, asyncio.Task[None]] = {}
turn_lock = asyncio.Lock()


class SandboxPathRequest(BaseModel):
    path: str = Field(min_length=1)
    write: bool = False


class UpdateSandboxPathsRequest(BaseModel):
    sandbox_paths: list[SandboxPathRequest] | None = None


def checked_sandbox_paths(entries: list[SandboxPathRequest]) -> list[dict[str, Any]]:
    paths = [entry.model_dump() for entry in entries]
    try:
        validate_paths(paths)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return paths


class CreateProjectRequest(BaseModel):
    """A project's directory, and optionally a label that differs from it.

    The client seeds the path from the name, but lets the user break that link
    and point the project somewhere else — so the two are stored separately.
    """

    path: str = Field(min_length=1)
    name: str | None = Field(default=None, max_length=120)
    sandbox_paths: list[SandboxPathRequest] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class DeleteProjectRequest(BaseModel):
    path: str = Field(min_length=1)


class UpdateProjectRequest(BaseModel):
    """Update paths or archive state, addressing the project by its path."""

    path: str = Field(min_length=1)
    archived: bool | None = None
    sandbox_paths: list[SandboxPathRequest] | None = None


class CreateWorktreeRequest(BaseModel):
    """A worktree under a project: where to put it, what to call its branch.

    The branch is always new and always cut from the project's current HEAD;
    attaching to an existing branch is not offered.
    """

    project_path: str = Field(min_length=1)
    path: str = Field(min_length=1)
    branch: str = Field(min_length=1, max_length=200)


class CreateSessionRequest(BaseModel):
    """A session under a project, optionally attached to one of its worktrees.

    The worktree must already exist — it is created by `POST /worktrees`, and
    any number of sessions can attach to the same one. Without a `worktree_id`
    the session runs in the project's own directory.
    """

    name: str = Field(min_length=1, max_length=120)
    project_path: str = Field(min_length=1)
    agent: str = "claude-code"
    worktree_id: int | None = None
    sandbox: bool = True

    @model_validator(mode="before")
    @classmethod
    def accept_working_dir_alias(cls, data: Any) -> Any:
        """Take `working_dir` as a deprecated spelling of `project_path`.

        The field was renamed because it no longer describes what it sets: a
        session's working directory is now the worktree when there is one. The
        alias keeps already-deployed clients working; responses are unaffected,
        since the session dict still carries `working_dir`.
        """
        if isinstance(data, dict) and not data.get("project_path"):
            legacy = data.get("working_dir")
            if legacy:
                return {**data, "project_path": legacy}
        return data


class UpdateSessionRequest(BaseModel):
    """Partial update: name, auto-approve toggles, sandbox, archive flag.

    Every field is optional; only the ones supplied are applied. `name` keeps
    the old rename contract (non-empty, trimmed) when present.
    """

    name: str | None = Field(default=None, max_length=120)
    auto_approve_write: bool | None = None
    auto_approve_command: bool | None = None
    sandbox: bool | None = None
    archived: bool | None = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("name cannot be empty")
        return stripped


class TurnRequest(BaseModel):
    prompt: str = Field(min_length=1)


class BashRequest(BaseModel):
    command: str = Field(min_length=1)


@app.on_event("startup")
async def startup() -> None:
    db.reset_active_sessions()


@app.on_event("shutdown")
async def shutdown() -> None:
    pending = (
        list(running_tasks.values())
        + list(bash_tasks.values())
        + [task for group in file_socket_tasks.values() for task in group]
    )
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await file_tree_manager.shutdown()
    db.close()


@app.get("/agents")
async def list_agents() -> list[dict[str, Any]]:
    """List the adapters this server can run, in picker order.

    An id alone does not make a picker — a client also needs something to
    display and needs to know which one to preselect. The label comes off the
    adapter class and the default off `CreateSessionRequest`, so neither is
    restated here and neither can drift from what `POST /sessions` accepts.
    """
    default = CreateSessionRequest.model_fields["agent"].default
    return [
        {
            "id": agent_id,
            "name": type(adapter).LABEL or agent_id,
            "default": agent_id == default,
        }
        for agent_id, adapter in adapters.items()
    ]


@app.get("/usage")
async def get_usage() -> dict[str, Any]:
    """Five-hour and weekly consumption for each subscription, as percentages.

    Subscriptions are reported independently: one that is unauthenticated or
    unreachable carries an `error` and null windows rather than failing the
    request, so a user who has only authenticated one of them still sees it.
    """
    return await collect_usage()


@app.get("/sandbox-paths")
async def get_sandbox_paths() -> dict[str, Any]:
    return {"sandbox_paths": db.get_sandbox_paths()}


@app.patch("/sandbox-paths")
async def update_sandbox_paths(payload: UpdateSandboxPathsRequest) -> dict[str, Any]:
    if payload.sandbox_paths is not None:
        db.set_sandbox_paths(checked_sandbox_paths(payload.sandbox_paths))
    return {"sandbox_paths": db.get_sandbox_paths()}


@app.get("/projects")
async def list_projects() -> list[dict[str, Any]]:
    return [with_existence(project) for project in db.list_projects()]


@app.post("/projects", status_code=201)
async def create_project(payload: CreateProjectRequest) -> dict[str, Any]:
    """Register a project and create its directory.

    The row is the record — nothing is inferred from the filesystem — so a
    project lists from the moment it is created rather than only once its first
    session exists. Creating one that already exists is a no-op.
    """
    paths = checked_sandbox_paths(payload.sandbox_paths)
    path = normalize_project_path(payload.path)
    target = Path(path)

    # Adopting a directory that already exists is normal; adopting something
    # that is not a usable directory is not, and failing here beats failing on
    # the first turn the agent tries to run.
    if target.exists() and not target.is_dir():
        raise HTTPException(
            status_code=400, detail="path exists but is not a directory"
        )
    if target.is_dir() and not os.access(target, os.W_OK | os.X_OK):
        raise HTTPException(
            status_code=400, detail="directory exists but is not writable"
        )

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not create project directory: {exc}",
        )

    name = payload.name or PurePosixPath(path).name
    return with_existence(db.create_project(path=path, name=name, sandbox_paths=paths))


@app.patch("/projects")
async def update_project(payload: UpdateProjectRequest) -> dict[str, Any]:
    """Update sandbox paths or archive/unarchive a project and its sessions.

    Sandbox paths affect future turns, without changing running sessions.

    Archiving cascades: an archived project must not leave sessions showing in
    the main list, so every live session under it is archived too. A session
    that is mid-turn or running a command blocks the whole thing with a 409
    rather than being archived out from under itself — nothing is written when
    that happens.

    Unarchiving restores exactly the sessions that cascade archived, so the
    round trip leaves the project as it was found. Sessions archived on their
    own account before that stay archived.
    """
    path = normalize_project_path(payload.path)
    project = db.get_project(path)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    paths = (
        checked_sandbox_paths(payload.sandbox_paths)
        if payload.sandbox_paths is not None else None
    )
    sessions = db.list_sessions_for_project(project["id"])
    affected = 0
    if payload.archived is True:
        busy = [session for session in sessions if session_is_busy(session)]
        if busy:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cannot archive a project with busy sessions: "
                    + ", ".join(session["name"] for session in busy)
                ),
            )
        affected = db.archive_project(project["id"])
    elif payload.archived is False:
        restoring = db.list_sessions_archived_with_project(project["id"])
        missing = [
            session for session in restoring
            if not Path(session["working_dir"]).is_dir()
        ]
        if missing:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cannot unarchive a project with missing session directories: "
                    + ", ".join(
                        f"{session['name']} ({session['working_dir']})"
                        for session in missing
                    )
                ),
            )
        affected = db.unarchive_project(project["id"])

    if paths is not None:
        db.set_sandbox_paths(paths, project["id"])

    # The clients holding one of these sessions open are the reason this moved
    # server-side; tell them rather than making them refetch to find out. Only
    # the ones somebody is actually watching are worth re-reading.
    if payload.archived is not None:
        for session in sessions:
            if session["id"] not in subscribers:
                continue
            updated = db.get_session(session["id"])
            if updated is not None:
                await broadcast_archived(updated)

    return {
        **with_existence(db.get_project(path) or project),
        "sessions_affected": affected,
    }


@app.delete("/projects")
async def delete_project(payload: DeleteProjectRequest) -> dict[str, Any]:
    """Forget a project and its sessions. Never touches the directory on disk.

    The sessions go with it: they are reachable only through their project, so
    leaving them behind would strand their scrollback with no way to open or
    delete it. The files the agent produced are left exactly where they are.

    Worktrees the server created for those sessions are removed under the same
    policy as deleting a session one at a time: never forced, and reported
    rather than fatal when git refuses.

    Takes the path in the body rather than the URL — a filesystem path does not
    belong in a path segment, and the same reasoning kept `/sessions` flat.
    """
    path = normalize_project_path(payload.path)
    project = db.get_project(path)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    doomed = db.list_sessions_for_project(project["id"])
    for session in doomed:
        await teardown_session(session)

    # Sessions first: a worktree with any still attached would refuse to go,
    # and by here they are all gone.
    worktrees_removed = 0
    worktree_errors: list[dict[str, str]] = []
    for worktree in db.list_worktrees(project["id"]):
        error = await git.remove_worktree(project["path"], worktree["path"])
        if error is None:
            worktrees_removed += 1
        else:
            worktree_errors.append({"path": worktree["path"], "error": error})

    # The worktree rows go with the project either way, by cascade. A directory
    # git refused to remove stays on disk — as the project's own directory
    # always has — and is reported above rather than blocking the delete.
    db.delete_project(path)
    return {
        "status": "deleted",
        "sessions_deleted": len(doomed),
        "worktrees_removed": worktrees_removed,
        "worktree_errors": worktree_errors,
    }


@app.get("/sessions")
async def list_sessions() -> list[dict[str, Any]]:
    return db.list_sessions()


@app.post("/sessions", status_code=201)
async def create_session(payload: CreateSessionRequest) -> dict[str, Any]:
    """Create a session under an existing project.

    Without a `worktree_id` the session runs in the project directory, as it
    always has. With one it runs in that worktree instead, alongside however
    many other sessions are already attached to it.
    """
    if payload.agent not in adapters:
        raise HTTPException(status_code=400, detail="Unknown agent")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name cannot be empty")

    project_path = normalize_project_path(payload.project_path)
    project = db.get_project(project_path)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if project["archived_at"] is not None:
        # Otherwise the new session lands in a project the UI is not showing.
        raise HTTPException(
            status_code=409, detail="Cannot create a session in an archived project"
        )

    if payload.worktree_id is not None:
        worktree = db.get_worktree(payload.worktree_id)
        if worktree is None:
            raise HTTPException(status_code=404, detail="Worktree not found")
        if worktree["project_id"] != project["id"]:
            raise HTTPException(
                status_code=400,
                detail="worktree belongs to a different project",
            )
        # No mkdir: the directory is git's, and re-creating one the user
        # removed by hand would produce a plain directory the agent would run
        # in as though it were a worktree. `GET /worktrees` reports `exists` so
        # the client can offer to clean it up instead.
    else:
        try:
            Path(project_path).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Could not create working directory: {exc}",
            )

    return db.create_session(
        name=name,
        project_id=project["id"],
        agent=payload.agent,
        worktree_id=payload.worktree_id,
        sandbox=payload.sandbox,
    )


@app.get("/worktrees")
async def list_worktrees(project_path: str | None = None) -> list[dict[str, Any]]:
    """Every worktree, or every worktree of one project."""
    project_id: int | None = None
    if project_path is not None:
        project = db.get_project(normalize_project_path(project_path))
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        project_id = project["id"]
    return [
        with_worktree_existence(worktree)
        for worktree in db.list_worktrees(project_id)
    ]


@app.post("/worktrees", status_code=201)
async def create_worktree(payload: CreateWorktreeRequest) -> dict[str, Any]:
    """Create a git worktree for a project, on a new branch off its HEAD.

    A worktree is its own thing now, so this succeeding commits the server to
    nothing else: it may sit with no sessions attached for as long as the user
    likes, and it is removed only by `DELETE /worktrees/{id}`.
    """
    project_path = normalize_project_path(payload.project_path)
    project = db.get_project(project_path)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if project["archived_at"] is not None:
        # Same rule as creating a session in one: archiving is for projects
        # being put down, and this would cut a branch and a directory for one
        # the UI is not showing. Removing a worktree stays allowed, like every
        # other bit of housekeeping on archived things.
        raise HTTPException(
            status_code=409, detail="Cannot create a worktree in an archived project"
        )

    worktree_path = normalize_project_path(payload.path)
    branch = payload.branch.strip()
    if worktree_path == project_path:
        raise HTTPException(
            status_code=400,
            detail="worktree path must differ from the project directory",
        )
    # Checked before the directory is: a client that derives the path from a
    # template hits this whenever two worktrees would be named the same way, and
    # both of the errors further down describe the symptom rather than the
    # cause. `path` is UNIQUE, so this is also what keeps the insert from
    # failing after git has already done the work.
    existing = db.get_worktree_by_path(worktree_path)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"a worktree already exists at {worktree_path}"
                + (f" on branch {existing['branch']}" if existing["branch"] else "")
            ),
        )
    # git takes over an existing *empty* directory and refuses a non-empty one;
    # checking here matches that rule and gives a better message than parsing
    # git's.
    if not is_empty_or_missing(worktree_path):
        raise HTTPException(
            status_code=400,
            detail="worktree path already exists and is not an empty directory",
        )
    if not await git.is_git_repo(project_path):
        raise HTTPException(
            status_code=400, detail="project is not a git repository"
        )
    if not await git.check_branch_name(branch):
        raise HTTPException(status_code=400, detail="invalid branch name")

    # Created up front even though `git worktree add` would create it itself,
    # because that command is not atomic: it writes the new branch ref *before*
    # creating the leading directories, and a failure there leaves the branch
    # behind (verified, git 2.47.3). The retry then fails with "a branch named
    # '…' already exists", which points at the wrong problem entirely. Doing it
    # here moves the failure ahead of the ref, so there is nothing to unwind —
    # and `OSError` carries a real errno, so the message says `Permission
    # denied` rather than git's `could not create leading directories of
    # '…/.git'`.
    try:
        Path(worktree_path).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not create worktree directory: {exc}",
        )

    error = await git.add_worktree(project_path, worktree_path, branch)
    if error is not None:
        raise HTTPException(status_code=400, detail=error)

    try:
        worktree = db.create_worktree(
            project_id=project["id"], path=worktree_path, branch=branch
        )
    except Exception:
        # The directory exists but no row will ever claim it, so it has to go
        # back before the failure propagates.
        await git.remove_worktree(project_path, worktree_path)
        raise
    return with_worktree_existence(worktree)


@app.delete("/worktrees/{worktree_id}")
async def delete_worktree(worktree_id: int) -> dict[str, Any]:
    """Remove a worktree from disk and forget it.

    A 409 either way when it cannot be removed — with sessions still attached,
    or with work in it git refuses to discard. Unlike deleting a session, the
    row does **not** go regardless: the row *is* the worktree, so keeping one
    whose directory is still there is what stops it becoming an orphan nothing
    can see.
    """
    worktree = db.get_worktree(worktree_id)
    if worktree is None:
        raise HTTPException(status_code=404, detail="Worktree not found")

    attached = db.list_sessions_for_worktree(worktree_id)
    if attached:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{len(attached)} session(s) are still using this worktree: "
                + ", ".join(session["name"] for session in attached)
            ),
        )

    # A detached session no longer references the worktree row, but once it is
    # live its preserved cwd is an active harness dependency. Removing that
    # directory would strand the session even though the foreign key permits it.
    live_detached = db.list_live_detached_sessions_for_worktree(worktree_id)
    if live_detached:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{len(live_detached)} live detached session(s) still use this "
                "worktree directory: "
                + ", ".join(session["name"] for session in live_detached)
            ),
        )

    project = db.get_project_by_id(worktree["project_id"])
    if project is not None:
        error = await git.remove_worktree(project["path"], worktree["path"])
        if error is not None:
            raise HTTPException(status_code=409, detail=error)

    db.delete_worktree(worktree_id)
    return {"status": "deleted"}


@app.patch("/sessions/{session_id}")
async def update_session(
    session_id: int, payload: UpdateSessionRequest
) -> dict[str, Any]:
    # Serialize with begin_turn: no turn can capture the old setting while
    # this request is waiting to commit its change (or vice versa).
    async with turn_lock:
        return await _update_session(session_id, payload)


async def _update_session(
    session_id: int, payload: UpdateSessionRequest
) -> dict[str, Any]:
    current = require_session_or_404(session_id)
    task = running_tasks.get(session_id)
    if payload.sandbox is not None and (
        current["status"] != "idle" or (task is not None and not task.done())
    ):
        # Check before applying any other fields in a mixed PATCH.
        raise HTTPException(
            status_code=409,
            detail="Cannot change sandbox while a turn is in progress",
        )
    session: dict[str, Any] | None = None

    if payload.name is not None:
        session = await commit_stream(
            session_id,
            lambda: db.rename_session(session_id, payload.name),
            lambda _session: [{"type": "renamed", "name": payload.name}],
        )

    if any(
        value is not None
        for value in (
            payload.auto_approve_write, payload.auto_approve_command, payload.sandbox
        )
    ):
        def update_settings() -> dict[str, Any]:
            if payload.sandbox is not None:
                db.set_sandbox(session_id, payload.sandbox)
            return db.set_auto_approve(
                session_id,
                write=payload.auto_approve_write,
                command=payload.auto_approve_command,
            )

        session = await commit_stream(
            session_id,
            update_settings,
            lambda updated: [
                {
                    "type": "settings",
                    "auto_approve_write": updated["auto_approve_write"],
                    "auto_approve_command": updated["auto_approve_command"],
                    "sandbox": updated["sandbox"],
                }
            ],
        )

    if payload.archived is not None:
        session = await set_session_archived(session_id, payload.archived)

    return session if session is not None else require_session_or_404(session_id)


@app.post("/sessions/{session_id}/detach-worktree")
async def detach_session_worktree(session_id: int) -> dict[str, Any]:
    """Detach an archived session while preserving its harness working directory.

    This changes only the database association. Removing the worktree from disk
    remains a separate, explicit `DELETE /worktrees/{id}` request.
    """
    require_session_or_404(session_id)

    def detach() -> dict[str, Any]:
        try:
            return db.detach_session_from_worktree(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return await commit_stream(
        session_id,
        detach,
        lambda session: [
            {
                "type": "worktree_detached",
                "worktree_id": None,
                "working_dir": session["working_dir"],
            }
        ],
    )


@app.post("/sessions/{session_id}/stop")
async def stop_session(session_id: int) -> dict[str, str]:
    session = require_session_or_404(session_id)
    adapter = adapters.get(session["agent"])
    if adapter is not None:
        await adapter.stop(session)

    # Stopping the provider process does not necessarily finish run_turn. The
    # provider may already have written another approval request to stdout; the
    # reader can register it after adapter.stop() cleared the pending requests
    # and then wait forever for its decision. Cancel and join the owning task so
    # no buffered provider event can keep the in-memory turn slot occupied.
    task = running_tasks.pop(session_id, None)
    if task is not None and task is not asyncio.current_task() and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    # Stop means everything this session is running, agent or not — otherwise a
    # runaway `!` command would have no kill switch short of the timeout.
    await cancel_bash(session_id)
    await commit_stream(
        session_id,
        lambda: db.update_status(session_id, "idle"),
        lambda _result: [{"type": "status", "status": "idle"}],
    )
    return {"status": "idle"}


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: int) -> dict[str, str]:
    """Delete a session. Never touches a directory.

    A session's worktree outlives it — other sessions may be attached, and even
    the last one leaving does not imply the user is done with the branch.
    Removing it is `DELETE /worktrees/{id}`, deliberately a separate decision.
    """
    session = require_session_or_404(session_id)
    await teardown_session(session)
    return {"status": "deleted"}


@app.post("/sessions/{session_id}/turn", status_code=202)
async def start_turn(session_id: int, payload: TurnRequest) -> dict[str, str]:
    await begin_turn(session_id, payload.prompt)
    return {"status": "running"}


@app.post("/sessions/{session_id}/bash", status_code=202)
async def start_bash(session_id: int, payload: BashRequest) -> dict[str, str]:
    await begin_bash(session_id, payload.command)
    return {"status": "running"}


@app.websocket("/ws/sessions/{session_id}/files")
async def file_tree_websocket(websocket: WebSocket, session_id: int) -> None:
    session = db.get_session(session_id)
    if session is None or not os.path.isdir(session["working_dir"]):
        await websocket.close(code=1008)
        return

    root = normalize_root(session["working_dir"])
    await websocket.accept()
    endpoint_task = asyncio.current_task()
    assert endpoint_task is not None
    endpoint_registered = False
    async with stream_locks[session_id]:
        current = db.get_session(session_id)
        if (
            current is not None
            and normalize_root(current["working_dir"]) == root
            and os.path.isdir(root)
        ):
            file_socket_tasks[session_id].add(endpoint_task)
            endpoint_registered = True
    if not endpoint_registered:
        await close_websocket(websocket, code=1008)
        return

    receiver = asyncio.create_task(receive_disconnect(websocket))
    acquisition = asyncio.create_task(file_tree_manager.acquire(root))
    tree = None
    subscriber = None
    pending_reserved = False
    try:
        done, _pending = await asyncio.wait(
            {receiver, acquisition}, return_when=asyncio.FIRST_COMPLETED
        )
        if receiver in done:
            if not acquisition.done():
                acquisition.cancel()
            await asyncio.gather(acquisition, return_exceptions=True)
            if not acquisition.cancelled() and acquisition.exception() is None:
                pending_reserved = True
            return

        tree = acquisition.result()
        pending_reserved = True
        unavailable = False
        async with stream_locks[session_id]:
            current = db.get_session(session_id)
            if (
                current is None
                or normalize_root(current["working_dir"]) != root
                or not os.path.isdir(root)
            ):
                unavailable = True
            else:
                subscriber = await file_tree_manager.add_subscriber(
                    tree, session_id, websocket
                )
                pending_reserved = False
        if unavailable:
            await file_tree_manager.release_pending(root)
            pending_reserved = False
            await close_websocket(websocket, code=1008)
            return

        assert subscriber is not None and subscriber.writer is not None
        await asyncio.wait(
            {receiver, subscriber.writer}, return_when=asyncio.FIRST_COMPLETED
        )
    except FileTreeError as exc:
        if pending_reserved:
            await file_tree_manager.release_pending(root)
            pending_reserved = False
        try:
            async with asyncio.timeout(WEBSOCKET_SEND_TIMEOUT_SECONDS):
                await websocket.send_json(exc.frame())
        except Exception:
            pass
        await close_websocket(websocket, code=1011)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if pending_reserved:
            await file_tree_manager.release_pending(root)
            pending_reserved = False
        error = FileTreeError("watcher_failed", f"The file-tree watcher failed: {exc}")
        try:
            async with asyncio.timeout(WEBSOCKET_SEND_TIMEOUT_SECONDS):
                await websocket.send_json(error.frame())
        except Exception:
            pass
        await close_websocket(websocket, code=1011)
    finally:
        if pending_reserved:
            await file_tree_manager.release_pending(root)
        if subscriber is not None and tree is not None:
            await file_tree_manager.remove_subscriber(tree, subscriber)
        for task in (receiver, acquisition):
            if not task.done():
                task.cancel()
        await asyncio.gather(receiver, acquisition, return_exceptions=True)
        if endpoint_registered:
            async with stream_locks[session_id]:
                tasks = file_socket_tasks.get(session_id)
                if tasks is not None:
                    tasks.discard(endpoint_task)
                    if not tasks:
                        file_socket_tasks.pop(session_id, None)
        if subscriber is None:
            await close_websocket(websocket, code=1000)


@app.websocket("/ws/sessions/{session_id}")
async def session_websocket(websocket: WebSocket, session_id: int) -> None:
    if db.get_session(session_id) is None:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    subscriber: Subscriber | None = None
    async with stream_locks[session_id]:
        session = db.get_session(session_id)
        if session is not None:
            outbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
                maxsize=(
                    SCROLLBACK_REPLAY_LIMIT
                    + 2
                    + WEBSOCKET_LIVE_QUEUE_CAPACITY
                )
            )
            subscriber = Subscriber(websocket=websocket, outbound=outbound)
            for row in db.recent_scrollback(
                session_id, SCROLLBACK_REPLAY_LIMIT
            ):
                outbound.put_nowait(frame_for_scrollback(row))
            outbound.put_nowait(
                {"type": "status", "status": session["status"]}
            )
            outbound.put_nowait(
                {"type": "archived", "archived_at": session["archived_at"]}
            )
            subscriber.writer = asyncio.create_task(
                write_subscriber(subscriber)
            )
            subscribers[session_id].add(subscriber)

    if subscriber is None:
        await close_websocket(websocket, code=1008)
        return

    receiver = asyncio.create_task(receive_subscriber(session_id, subscriber))
    assert subscriber.writer is not None
    tasks = {subscriber.writer, receiver}
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        async with stream_locks[session_id]:
            retire_subscriber_locked(session_id, subscriber)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await teardown_subscribers(
            [subscriber], code=1011 if subscriber.send_failed else 1000
        )


async def receive_subscriber(
    session_id: int, subscriber: Subscriber
) -> None:
    websocket = subscriber.websocket
    try:
        while True:
            message = await websocket.receive_json()
            message_type = message.get("type")

            if message_type == "input":
                prompt = str(message.get("text", "")).strip()
                if not prompt:
                    if not await enqueue_local(
                        session_id,
                        subscriber,
                        {"type": "error", "message": "Prompt cannot be empty"},
                    ):
                        return
                    continue
                try:
                    await begin_turn(session_id, prompt)
                except HTTPException as exc:
                    if not await enqueue_local(
                        session_id,
                        subscriber,
                        {"type": "error", "message": str(exc.detail)},
                    ):
                        return

            elif message_type == "bash":
                command = str(message.get("command", "")).strip()
                if not command:
                    if not await enqueue_local(
                        session_id,
                        subscriber,
                        {"type": "error", "message": "Command cannot be empty"},
                    ):
                        return
                    continue
                try:
                    await begin_bash(session_id, command)
                except HTTPException as exc:
                    if not await enqueue_local(
                        session_id,
                        subscriber,
                        {"type": "error", "message": str(exc.detail)},
                    ):
                        return

            elif message_type == "approval_response":
                request_id = str(message.get("request_id", ""))
                behavior = str(message.get("behavior", ""))
                raw_option_id = message.get("option_id")
                option_id = str(raw_option_id) if raw_option_id else None
                raw_message = message.get("message")
                deny_message = str(raw_message) if raw_message else None
                try:
                    await handle_approval(
                        session_id,
                        request_id,
                        behavior,
                        option_id=option_id,
                        message=deny_message,
                    )
                except (KeyError, ValueError) as exc:
                    if not await enqueue_local(
                        session_id,
                        subscriber,
                        {"type": "error", "message": str(exc)},
                    ):
                        return

            elif message_type == "question_response":
                request_id = str(message.get("request_id", ""))
                answers = message.get("answers")
                try:
                    await handle_question_answer(session_id, request_id, answers)
                except (KeyError, ValueError, NotImplementedError) as exc:
                    if not await enqueue_local(
                        session_id,
                        subscriber,
                        {"type": "error", "message": str(exc)},
                    ):
                        return
            elif not await enqueue_local(
                session_id,
                subscriber,
                {"type": "error", "message": "Unsupported WebSocket message"},
            ):
                return
    except WebSocketDisconnect:
        return


async def begin_turn(session_id: int, prompt: str) -> None:
    async with turn_lock:
        session = require_session_or_404(session_id)
        require_not_archived(session)
        if session["agent"] not in adapters:
            raise HTTPException(
                status_code=409,
                detail=f"Agent is no longer available: {session['agent']}",
            )
        existing_task = running_tasks.get(session_id)
        if session["status"] != "idle" or (
            existing_task and not existing_task.done()
        ):
            raise HTTPException(status_code=409, detail="Session is already running")

        def start() -> None:
            db.append_scrollback(session_id, "input", {"text": prompt})
            db.update_status(session_id, "running")
            db.touch_session(session_id)

        await commit_stream(
            session_id,
            start,
            lambda _result: [
                {"type": "input", "text": prompt},
                {"type": "status", "status": "running"},
            ],
        )

        task = asyncio.create_task(run_turn(session_id, prompt))
        running_tasks[session_id] = task


async def run_turn(session_id: int, prompt: str) -> None:
    session = db.require_session(session_id)
    adapter = adapters[session["agent"]]

    try:
        if session.get("sandbox", True):
            defaults, project_paths = db.sandbox_paths_snapshot(session["project_id"])
            session["sandbox_paths"] = merge_paths(defaults, project_paths)
        async for event in adapter.start_turn(session, prompt):
            event_type = event.get("type")

            # Decide auto-approval before persisting/broadcasting so the event
            # carries the marker and we can skip the awaiting_approval status.
            # Re-read the session so a toggle flipped mid-turn applies on the
            # next approval, not only on the next turn.
            auto_setting: str | None = None
            if event_type == "approval_request":
                setting = auto_approval_setting(event.get("action"))
                current = db.get_session(session_id) or session
                if setting is not None and current.get(setting):
                    auto_setting = setting
                    event = {**event, "auto_approved": True}

            persisted = event_type in {
                "output",
                "tool_use",
                "approval_request",
                "question",
                "error",
            }
            awaiting = event_type == "question" or (
                event_type == "approval_request" and auto_setting is None
            )

            def record_event() -> None:
                if persisted:
                    db.append_scrollback(
                        session_id,
                        event_type,
                        {
                            key: value
                            for key, value in event.items()
                            if key != "type"
                        },
                    )
                # A pending question blocks on the user just like an approval.
                # Auto-approved requests never leave running state.
                if awaiting:
                    db.update_status(session_id, "awaiting_approval")
                if event_type == "done" and event.get("session_id"):
                    db.set_agent_session_id(session_id, event["session_id"])

            frames = (
                [{"type": "status", "status": "awaiting_approval"}, event]
                if awaiting
                else [event]
            )
            await commit_stream(
                session_id,
                record_event,
                lambda _result: frames,
            )

            # Answer on the user's behalf right after the request is on the wire,
            # so the transcript shows the request followed by the auto-approval.
            if auto_setting is not None:
                await handle_approval(
                    session_id, event["request_id"], "allow", auto=True
                )
    except Exception as exc:
        message = f"Agent turn failed: {exc}"
        await commit_stream(
            session_id,
            lambda: db.append_scrollback(
                session_id, "error", {"message": message}
            ),
            lambda _result: [{"type": "error", "message": message}],
        )
    finally:
        await commit_stream(
            session_id,
            lambda: db.update_status(session_id, "idle"),
            lambda _result: [{"type": "status", "status": "idle"}],
        )
        running_tasks.pop(session_id, None)


async def begin_bash(session_id: int, command: str) -> None:
    """Start a one-shot shell command in the session's working directory.

    Deliberately none of what `begin_turn` does: no turn lock, no status
    change, no adapter. Bash mode bypasses the agent entirely, so it must also
    bypass the turn state machine — a command can run while the agent is
    mid-turn or blocked on an approval, and neither notices the other.
    """
    require_not_archived(require_session_or_404(session_id))
    existing = bash_tasks.get(session_id)
    if existing and not existing.done():
        raise HTTPException(
            status_code=409, detail="A command is already running in this session"
        )

    def start() -> None:
        db.append_scrollback(session_id, "bash_input", {"command": command})
        db.touch_session(session_id)

    await commit_stream(
        session_id,
        start,
        lambda _result: [{"type": "bash_input", "command": command}],
    )

    bash_tasks[session_id] = asyncio.create_task(run_bash(session_id, command))


async def run_bash(session_id: int, command: str) -> None:
    try:
        session = db.require_session(session_id)
        result = await shell.run_command(command, cwd=session["working_dir"])
        payload = {"command": command, **result}
        await commit_stream(
            session_id,
            lambda: db.append_scrollback(session_id, "bash_output", payload),
            lambda _result: [{"type": "bash_output", **payload}],
        )
    except asyncio.CancelledError:
        # Stopped by the user or shutting down; the process group is already
        # dead. Say so in the transcript rather than leaving the command
        # hanging with no result.
        await report_bash_error(session_id, f"Command stopped: {command}")
        raise
    except Exception as exc:
        await report_bash_error(session_id, f"Command failed: {exc}")
    finally:
        bash_tasks.pop(session_id, None)


async def report_bash_error(session_id: int, message: str) -> None:
    """Record a command failure, unless the session itself is already gone.

    Deleting a session cancels its command, so the cancellation lands with the
    row possibly on its way out; scrollback has a foreign key to it, and an
    insert that loses that race must not surface as a crashed task.
    """
    if db.get_session(session_id) is None:
        return
    await commit_stream(
        session_id,
        lambda: db.append_scrollback(session_id, "error", {"message": message}),
        lambda _result: [{"type": "error", "message": message}],
    )


async def cancel_bash(session_id: int) -> None:
    """Kill this session's in-flight command, if any, and wait for it to end."""
    task = bash_tasks.pop(session_id, None)
    if task is None or task.done():
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def handle_approval(
    session_id: int,
    request_id: str,
    behavior: str,
    option_id: str | None = None,
    message: str | None = None,
    auto: bool = False,
) -> None:
    session = db.require_session(session_id)
    adapter = adapters.get(session["agent"])
    if adapter is None:
        raise KeyError(f"Agent is no longer available: {session['agent']}")
    effective = await adapter.send_approval(
        session, request_id, behavior, option_id=option_id, message=message
    )

    payload: dict[str, Any] = {"request_id": request_id, "behavior": effective}
    if option_id:
        payload["option_id"] = option_id
    if message:
        payload["message"] = message
    if auto:
        payload["auto"] = True

    def record_response() -> None:
        db.append_scrollback(session_id, "approval_response", payload)
        db.update_status(session_id, "running")

    await commit_stream(
        session_id,
        record_response,
        lambda _result: [
            {"type": "approval_response", **payload},
            {"type": "status", "status": "running"},
        ],
    )


async def handle_question_answer(
    session_id: int,
    request_id: str,
    answers: Any,
) -> None:
    session = db.require_session(session_id)
    adapter = adapters.get(session["agent"])
    if adapter is None:
        raise KeyError(f"Agent is no longer available: {session['agent']}")
    validated = await adapter.send_answer(session, request_id, answers)

    payload = {"request_id": request_id, "answers": validated}

    def record_response() -> None:
        db.append_scrollback(session_id, "question_response", payload)
        db.update_status(session_id, "running")

    await commit_stream(
        session_id,
        record_response,
        lambda _result: [
            {"type": "question_response", **payload},
            {"type": "status", "status": "running"},
        ],
    )


def frame_for_scrollback(row: dict[str, Any]) -> dict[str, Any]:
    payload = row["payload"] if isinstance(row["payload"], dict) else {}
    return {"type": row["type"], **payload}


def retire_subscriber_locked(session_id: int, subscriber: Subscriber) -> bool:
    if subscriber.retired:
        return False
    subscriber.retired = True
    session_subscribers = subscribers.get(session_id)
    if session_subscribers is not None:
        session_subscribers.discard(subscriber)
        if not session_subscribers:
            subscribers.pop(session_id, None)
    writer = subscriber.writer
    if (
        writer is not None
        and writer is not asyncio.current_task()
        and not writer.done()
    ):
        writer.cancel()
    return True


def enqueue_for_subscribers(
    session_id: int, frames: list[dict[str, Any]]
) -> list[Subscriber]:
    retired: list[Subscriber] = []
    for subscriber in list(subscribers.get(session_id, set())):
        if subscriber.retired:
            continue
        try:
            for frame in frames:
                subscriber.outbound.put_nowait(frame)
        except asyncio.QueueFull:
            if retire_subscriber_locked(session_id, subscriber):
                retired.append(subscriber)
    return retired


async def close_websocket(websocket: WebSocket, *, code: int) -> None:
    try:
        async with asyncio.timeout(WEBSOCKET_CLOSE_TIMEOUT_SECONDS):
            await websocket.close(code=code)
    except Exception:
        # Closing is best effort. The endpoint tasks are canceled independently.
        pass


async def teardown_subscribers(
    doomed: list[Subscriber], *, code: int = 1011
) -> None:
    current = asyncio.current_task()
    for subscriber in doomed:
        if subscriber.closed:
            continue
        subscriber.closed = True
        writer = subscriber.writer
        if writer is not None and writer is not current and not writer.done():
            writer.cancel()
        if writer is not None and writer is not current:
            try:
                async with asyncio.timeout(WEBSOCKET_CLOSE_TIMEOUT_SECONDS):
                    await asyncio.gather(writer, return_exceptions=True)
            except TimeoutError:
                pass
        await close_websocket(subscriber.websocket, code=code)


async def commit_stream(
    session_id: int,
    mutation: Callable[[], Any],
    frames_for_result: Callable[[Any], list[dict[str, Any]]],
) -> Any:
    async with stream_locks[session_id]:
        result = mutation()
        retired = enqueue_for_subscribers(session_id, frames_for_result(result))
    await teardown_subscribers(retired)
    return result


async def broadcast(session_id: int, message: dict[str, Any]) -> None:
    await commit_stream(session_id, lambda: None, lambda _result: [message])


async def enqueue_local(
    session_id: int,
    subscriber: Subscriber,
    message: dict[str, Any],
) -> bool:
    retired: list[Subscriber] = []
    async with stream_locks[session_id]:
        if subscriber.retired:
            return False
        try:
            subscriber.outbound.put_nowait(message)
        except asyncio.QueueFull:
            if retire_subscriber_locked(session_id, subscriber):
                retired.append(subscriber)
    await teardown_subscribers(retired)
    return not retired


async def write_subscriber(subscriber: Subscriber) -> None:
    try:
        while True:
            frame = await subscriber.outbound.get()
            async with asyncio.timeout(WEBSOCKET_SEND_TIMEOUT_SECONDS):
                await subscriber.websocket.send_json(frame)
    except asyncio.CancelledError:
        raise
    except Exception:
        subscriber.send_failed = True


async def teardown_session(session: dict[str, Any]) -> None:
    """Stop a session's process, drop its subscribers, and delete its row.

    Shared by deleting one session and deleting a whole project's worth.
    Nothing on disk is touched: a session owns no directory, so there is
    nothing here that can fail halfway.
    """
    session_id = session["id"]
    adapter = adapters.get(session["agent"])

    if adapter is not None:
        await adapter.stop(session)
    task = running_tasks.pop(session_id, None)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    # Before the row goes: the command's own error path writes scrollback.
    await cancel_bash(session_id)

    async with stream_locks[session_id]:
        doomed = list(subscribers.get(session_id, set()))
        for subscriber in doomed:
            retire_subscriber_locked(session_id, subscriber)
        file_endpoints = list(file_socket_tasks.get(session_id, set()))
        db.delete_session(session_id)

    for endpoint in file_endpoints:
        if endpoint is not asyncio.current_task() and not endpoint.done():
            endpoint.cancel()
    if file_endpoints:
        await asyncio.gather(*file_endpoints, return_exceptions=True)
    await teardown_subscribers(doomed, code=1000)
    await file_tree_manager.close_session(session_id)


def with_worktree_existence(worktree: dict[str, Any]) -> dict[str, Any]:
    """Annotate a worktree with whether its directory is still there.

    Same one stat per row as `with_existence` does for projects. A worktree
    removed by hand outside the app still has a row, and this is how the client
    can tell — `DELETE /worktrees/{id}` then succeeds and tidies up, since git
    prunes a worktree whose directory is gone without complaint.
    """
    return {**worktree, "exists": os.path.isdir(worktree["path"])}


def with_existence(project: dict[str, Any]) -> dict[str, Any]:
    """Annotate a project with whether its directory is still there.

    One stat per project, not a scan: the client needs it to explain a project
    whose directory was removed outside the app and offer to forget it.

    `is_git_repo` rides along as a hint for hiding the worktree toggle on
    projects that cannot have one — one more cheap `exists`, no subprocess.
    `.git` as a *file* counts, so a project that is itself a worktree reads as
    a repo. Creating a session is where this is actually enforced.
    """
    return {
        **project,
        "exists": os.path.isdir(project["path"]),
        "is_git_repo": os.path.exists(os.path.join(project["path"], ".git")),
    }


def is_empty_or_missing(path: str) -> bool:
    """Whether `git worktree add` would accept `path` as its target."""
    target = Path(path)
    if not target.exists():
        return True
    if not target.is_dir():
        return False
    try:
        return not any(target.iterdir())
    except OSError:
        return False


def normalize_project_path(raw: str) -> str:
    """Absolute, lexically normalised project path.

    Normalising is purely textual — no filesystem access — so it collapses
    `..` and duplicate slashes without resolving symlinks or requiring the
    directory to exist yet.
    """
    path = raw.strip()
    if not path.startswith("/"):
        raise HTTPException(status_code=400, detail="path must be absolute")
    path = os.path.normpath(path)
    # `normpath` keeps *exactly* two leading slashes — POSIX leaves `//`
    # implementation-defined — while collapsing three or more. Nothing here
    # wants that: it gives one directory two spellings, which would walk past
    # the UNIQUE on `projects.path` and `worktrees.path`. A client joining a
    # template naively onto a top-level project (`/` + `/app-fix`) lands here.
    if path.startswith("//"):
        path = path[1:]
    if path == "/":
        raise HTTPException(
            status_code=400, detail="path must name a directory, not /"
        )
    return path


def require_session_or_404(session_id: int) -> dict[str, Any]:
    session = db.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


async def set_session_archived(session_id: int, archived: bool) -> dict[str, Any]:
    """Archive or unarchive one session, keeping its project consistent.

    Archiving a session that is mid-turn or running a command is refused: the
    work would carry on writing scrollback into something the user has filed
    away. Unarchiving takes the project with it — a live session under an
    archived project would have nowhere to show — but only that one session
    comes back, not everything the project's archive swept up.
    """

    def update_archive() -> dict[str, Any]:
        current = require_session_or_404(session_id)
        if archived and session_is_busy(current):
            raise HTTPException(
                status_code=409,
                detail="Cannot archive a session while it is busy",
            )
        if (
            not archived
            and current["archived_at"] is not None
            and not Path(current["working_dir"]).is_dir()
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cannot unarchive a session whose working directory is missing: "
                    + current["working_dir"]
                ),
            )

        session = db.set_session_archived(session_id, archived)
        if not archived:
            db.unarchive_project(session["project_id"], restore_sessions=False)
        return session

    return await commit_stream(
        session_id,
        update_archive,
        lambda session: [
            {"type": "archived", "archived_at": session["archived_at"]}
        ],
    )


def session_is_busy(session: dict[str, Any]) -> bool:
    """Whether anything is still running for this session.

    `status` covers the agent's turn state machine. Bash mode deliberately sits
    outside it, so a command running there shows up nowhere in `status` and has
    to be checked separately.
    """
    task = bash_tasks.get(session["id"])
    return session["status"] != "idle" or bool(task and not task.done())


async def broadcast_archived(session: dict[str, Any]) -> None:
    await broadcast(
        session["id"],
        {"type": "archived", "archived_at": session["archived_at"]},
    )


def require_not_archived(session: dict[str, Any]) -> dict[str, Any]:
    """Refuse to start new work in an archived session.

    Archived is read-only, not hidden: the scrollback still replays over the
    WebSocket and the session can still be renamed or deleted. Only starting
    something new is blocked, because that is what would need un-archiving to
    be visible again.
    """
    if session["archived_at"] is not None:
        raise HTTPException(status_code=409, detail="Session is archived")
    return session


# Serving the desktop client (agent-ui-desktop) from the API's own origin means
# it needs no server address configured and no CORS. Mounting is opt-in via
# WEB_ROOT so a server with no client checked out behaves exactly as before.
#
# This must stay at the bottom of the module: Starlette matches routes in
# registration order, and a mount at "/" registered earlier would shadow
# /projects, /sessions and the WebSocket endpoint.
def mount_web_root(app: FastAPI) -> None:
    web_root = os.environ.get("WEB_ROOT", "").strip()
    if not web_root:
        return
    if not os.path.isdir(web_root):
        # A typo here would otherwise surface as 404s on every page load.
        raise RuntimeError(f"WEB_ROOT is not a directory: {web_root}")
    app.mount("/", StaticFiles(directory=web_root, html=True), name="web")


mount_web_root(app)


def main() -> None:
    import uvicorn

    # The service binds to the WireGuard interface in every deployment.
    uvicorn.run(
        app,
        host=os.environ.get("WIREGUARD_IP", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":
    main()
