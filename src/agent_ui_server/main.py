from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from . import git, shell
from .agent import AgentAdapter, ClaudeCodeAdapter, OpenCodeAdapter, PiAdapter
from .db import Database


SCROLLBACK_REPLAY_LIMIT = 200

app = FastAPI(title="agent-ui-server")
db = Database(os.environ.get("SESSION_DB", "sessions.db"))
adapters: dict[str, AgentAdapter] = {
    "claude-code": ClaudeCodeAdapter(),
    "opencode": OpenCodeAdapter(),
    "pi": PiAdapter(),
}
subscribers: dict[int, set[WebSocket]] = defaultdict(set)
running_tasks: dict[int, asyncio.Task[None]] = {}
# Bash-mode commands are tracked separately from agent turns on purpose: they
# are allowed to run alongside one, so they must not share the turn's slot.
bash_tasks: dict[int, asyncio.Task[None]] = {}
turn_lock = asyncio.Lock()


class CreateProjectRequest(BaseModel):
    """A project's directory, and optionally a label that differs from it.

    The client seeds the path from the name, but lets the user break that link
    and point the project somewhere else — so the two are stored separately.
    """

    path: str = Field(min_length=1)
    name: str | None = Field(default=None, max_length=120)

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
    """Archive or unarchive a project, addressed by path like the delete does."""

    path: str = Field(min_length=1)
    archived: bool


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
    """Partial update of a session: rename, auto-approve toggles, archive flag.

    Every field is optional; only the ones supplied are applied. `name` keeps
    the old rename contract (non-empty, trimmed) when present.
    """

    name: str | None = Field(default=None, max_length=120)
    auto_approve_write: bool | None = None
    auto_approve_command: bool | None = None
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
    pending = list(running_tasks.values()) + list(bash_tasks.values())
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
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
    return with_existence(db.create_project(path=path, name=name))


@app.patch("/projects")
async def update_project(payload: UpdateProjectRequest) -> dict[str, Any]:
    """Archive or unarchive a project, taking its sessions with it.

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

    sessions = db.list_sessions_for_project(project["id"])
    if payload.archived:
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
    else:
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

    # The clients holding one of these sessions open are the reason this moved
    # server-side; tell them rather than making them refetch to find out. Only
    # the ones somebody is actually watching are worth re-reading.
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
    require_session_or_404(session_id)
    session: dict[str, Any] | None = None

    if payload.name is not None:
        session = db.rename_session(session_id, payload.name)
        await broadcast(session_id, {"type": "renamed", "name": payload.name})

    if payload.auto_approve_write is not None or payload.auto_approve_command is not None:
        session = db.set_auto_approve(
            session_id,
            write=payload.auto_approve_write,
            command=payload.auto_approve_command,
        )
        await broadcast(
            session_id,
            {
                "type": "settings",
                "auto_approve_write": session["auto_approve_write"],
                "auto_approve_command": session["auto_approve_command"],
            },
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
    try:
        session = db.detach_session_from_worktree(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await broadcast(
        session_id,
        {
            "type": "worktree_detached",
            "worktree_id": None,
            "working_dir": session["working_dir"],
        },
    )
    return session


@app.post("/sessions/{session_id}/stop")
async def stop_session(session_id: int) -> dict[str, str]:
    session = require_session_or_404(session_id)
    adapter = adapters[session["agent"]]
    await adapter.stop(session)
    # Stop means everything this session is running, agent or not — otherwise a
    # runaway `!` command would have no kill switch short of the timeout.
    await cancel_bash(session_id)
    db.update_status(session_id, "idle")
    await broadcast(session_id, {"type": "status", "status": "idle"})
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


@app.websocket("/ws/sessions/{session_id}")
async def session_websocket(websocket: WebSocket, session_id: int) -> None:
    session = db.get_session(session_id)
    if session is None:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    subscribers[session_id].add(websocket)

    try:
        await replay_scrollback(websocket, session_id)
        await websocket.send_json({"type": "status", "status": session["status"]})
        # Alongside status for the same reason: a client that was offline when
        # another device archived this session would otherwise never hear.
        await websocket.send_json(
            {"type": "archived", "archived_at": session["archived_at"]}
        )

        while True:
            message = await websocket.receive_json()
            message_type = message.get("type")

            if message_type == "input":
                prompt = str(message.get("text", "")).strip()
                if not prompt:
                    await websocket.send_json(
                        {"type": "error", "message": "Prompt cannot be empty"}
                    )
                    continue
                try:
                    await begin_turn(session_id, prompt)
                except HTTPException as exc:
                    await websocket.send_json(
                        {"type": "error", "message": str(exc.detail)}
                    )

            elif message_type == "bash":
                command = str(message.get("command", "")).strip()
                if not command:
                    await websocket.send_json(
                        {"type": "error", "message": "Command cannot be empty"}
                    )
                    continue
                try:
                    await begin_bash(session_id, command)
                except HTTPException as exc:
                    await websocket.send_json(
                        {"type": "error", "message": str(exc.detail)}
                    )

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
                    await websocket.send_json({"type": "error", "message": str(exc)})

            elif message_type == "question_response":
                request_id = str(message.get("request_id", ""))
                answers = message.get("answers")
                try:
                    await handle_question_answer(session_id, request_id, answers)
                except (KeyError, ValueError, NotImplementedError) as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
            else:
                await websocket.send_json(
                    {"type": "error", "message": "Unsupported WebSocket message"}
                )
    except WebSocketDisconnect:
        pass
    finally:
        subscribers[session_id].discard(websocket)
        if not subscribers[session_id]:
            subscribers.pop(session_id, None)


async def begin_turn(session_id: int, prompt: str) -> None:
    async with turn_lock:
        session = require_session_or_404(session_id)
        require_not_archived(session)
        existing_task = running_tasks.get(session_id)
        if session["status"] != "idle" or (
            existing_task and not existing_task.done()
        ):
            raise HTTPException(status_code=409, detail="Session is already running")

        db.append_scrollback(session_id, "input", {"text": prompt})
        db.update_status(session_id, "running")
        db.touch_session(session_id)
        await broadcast(session_id, {"type": "input", "text": prompt})
        await broadcast(session_id, {"type": "status", "status": "running"})

        task = asyncio.create_task(run_turn(session_id, prompt))
        running_tasks[session_id] = task


async def run_turn(session_id: int, prompt: str) -> None:
    session = db.require_session(session_id)
    adapter = adapters[session["agent"]]
    followup_prompt: str | None = None

    try:
        async for event in adapter.start_turn(session, prompt):
            event_type = event.get("type")

            # Internal orchestration event: the adapter ended the turn and wants
            # a new one started (OpenCode's deny-with-reason). Not persisted or
            # broadcast on its own — it surfaces as the next turn's input.
            if event_type == "followup":
                followup_prompt = event.get("prompt")
                continue

            # Decide auto-approval before persisting/broadcasting so the event
            # carries the marker and we can skip the awaiting_approval status.
            # Re-read the session so a toggle flipped mid-turn applies on the
            # next approval, not only on the next turn.
            auto_category: str | None = None
            if event_type == "approval_request":
                category = event.get("category")
                current = db.get_session(session_id) or session
                if category in {"write", "command"} and current.get(
                    f"auto_approve_{category}"
                ):
                    auto_category = category
                    event = {**event, "auto_approved": True}

            if event_type in {
                "output",
                "tool_use",
                "approval_request",
                "question",
                "error",
            }:
                db.append_scrollback(
                    session_id,
                    event_type,
                    {key: value for key, value in event.items() if key != "type"},
                )

            # A pending question blocks on the user just like an approval; reuse
            # the awaiting_approval status (the client tells them apart by
            # event). An auto-approved request never blocks, so it stays running.
            if event_type == "question" or (
                event_type == "approval_request" and auto_category is None
            ):
                db.update_status(session_id, "awaiting_approval")
                await broadcast(
                    session_id,
                    {"type": "status", "status": "awaiting_approval"},
                )

            if event_type == "done" and event.get("session_id"):
                db.set_agent_session_id(session_id, event["session_id"])

            await broadcast(session_id, event)

            # Answer on the user's behalf right after the request is on the wire,
            # so the transcript shows the request followed by the auto-approval.
            if auto_category is not None:
                await handle_approval(
                    session_id, event["request_id"], "allow", auto=True
                )
    except Exception as exc:
        message = f"Agent turn failed: {exc}"
        db.append_scrollback(session_id, "error", {"message": message})
        await broadcast(session_id, {"type": "error", "message": message})
    finally:
        db.update_status(session_id, "idle")
        await broadcast(session_id, {"type": "status", "status": "idle"})
        running_tasks.pop(session_id, None)

    if followup_prompt:
        # Chain the denial follow-up as a fresh turn now that this one is idle.
        # A new turn only auto-chains again if the user denies again, so this
        # can't spin on its own.
        try:
            await begin_turn(session_id, followup_prompt)
        except HTTPException:
            pass


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

    db.append_scrollback(session_id, "bash_input", {"command": command})
    db.touch_session(session_id)
    await broadcast(session_id, {"type": "bash_input", "command": command})

    bash_tasks[session_id] = asyncio.create_task(run_bash(session_id, command))


async def run_bash(session_id: int, command: str) -> None:
    try:
        session = db.require_session(session_id)
        result = await shell.run_command(command, cwd=session["working_dir"])
        payload = {"command": command, **result}
        db.append_scrollback(session_id, "bash_output", payload)
        await broadcast(session_id, {"type": "bash_output", **payload})
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
    db.append_scrollback(session_id, "error", {"message": message})
    await broadcast(session_id, {"type": "error", "message": message})


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
    adapter = adapters[session["agent"]]
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
    db.append_scrollback(session_id, "approval_response", payload)
    db.update_status(session_id, "running")
    await broadcast(session_id, {"type": "approval_response", **payload})
    await broadcast(session_id, {"type": "status", "status": "running"})


async def handle_question_answer(
    session_id: int,
    request_id: str,
    answers: Any,
) -> None:
    session = db.require_session(session_id)
    adapter = adapters[session["agent"]]
    validated = await adapter.send_answer(session, request_id, answers)

    payload = {"request_id": request_id, "answers": validated}
    db.append_scrollback(session_id, "question_response", payload)
    db.update_status(session_id, "running")
    await broadcast(session_id, {"type": "question_response", **payload})
    await broadcast(session_id, {"type": "status", "status": "running"})


async def replay_scrollback(websocket: WebSocket, session_id: int) -> None:
    for row in db.recent_scrollback(session_id, SCROLLBACK_REPLAY_LIMIT):
        payload = row["payload"] if isinstance(row["payload"], dict) else {}
        await websocket.send_json({"type": row["type"], **payload})


async def broadcast(session_id: int, message: dict[str, Any]) -> None:
    stale: list[WebSocket] = []
    for websocket in list(subscribers.get(session_id, set())):
        try:
            await websocket.send_json(message)
        except Exception:
            stale.append(websocket)

    for websocket in stale:
        subscribers[session_id].discard(websocket)
    if session_id in subscribers and not subscribers[session_id]:
        subscribers.pop(session_id, None)


async def teardown_session(session: dict[str, Any]) -> None:
    """Stop a session's process, drop its subscribers, and delete its row.

    Shared by deleting one session and deleting a whole project's worth.
    Nothing on disk is touched: a session owns no directory, so there is
    nothing here that can fail halfway.
    """
    session_id = session["id"]
    adapter = adapters[session["agent"]]

    await adapter.stop(session)
    task = running_tasks.pop(session_id, None)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    # Before the row goes: the command's own error path writes scrollback.
    await cancel_bash(session_id)

    for websocket in list(subscribers.get(session_id, set())):
        try:
            await websocket.close(code=1000)
        except Exception:
            pass
    subscribers.pop(session_id, None)

    db.delete_session(session_id)


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
    current = require_session_or_404(session_id)
    if archived and session_is_busy(current):
        raise HTTPException(
            status_code=409, detail="Cannot archive a session while it is busy"
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

    await broadcast_archived(session)
    return session


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
