from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import shell
from agent import AgentAdapter, ClaudeCodeAdapter, OpenCodeAdapter
from db import Database


SCROLLBACK_REPLAY_LIMIT = 200

app = FastAPI(title="agent-ui-server")
db = Database(os.environ.get("SESSION_DB", "sessions.db"))
adapters: dict[str, AgentAdapter] = {
    "claude-code": ClaudeCodeAdapter(),
    "opencode": OpenCodeAdapter(),
}
subscribers: dict[str, set[WebSocket]] = defaultdict(set)
running_tasks: dict[str, asyncio.Task[None]] = {}
# Bash-mode commands are tracked separately from agent turns on purpose: they
# are allowed to run alongside one, so they must not share the turn's slot.
bash_tasks: dict[str, asyncio.Task[None]] = {}
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


class CreateSessionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    working_dir: str = Field(min_length=1)
    agent: str = "claude-code"


class UpdateSessionRequest(BaseModel):
    """Partial update of a session: rename and/or flip auto-approve toggles.

    Every field is optional; only the ones supplied are applied. `name` keeps
    the old rename contract (non-empty, trimmed) when present.
    """

    name: str | None = Field(default=None, max_length=120)
    auto_approve_write: bool | None = None
    auto_approve_command: bool | None = None

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


@app.delete("/projects")
async def delete_project(payload: DeleteProjectRequest) -> dict[str, Any]:
    """Forget a project and its sessions. Never touches the directory on disk.

    The sessions go with it: they are reachable only through their project, so
    leaving them behind would strand their scrollback with no way to open or
    delete it. The files the agent produced are left exactly where they are.

    Takes the path in the body rather than the URL — a filesystem path does not
    belong in a path segment, and the same reasoning kept `/sessions` flat.
    """
    path = normalize_project_path(payload.path)
    if db.get_project(path) is None:
        raise HTTPException(status_code=404, detail="Project not found")

    doomed = [s for s in db.list_sessions() if s["working_dir"] == path]
    for session in doomed:
        await teardown_session(session)
    db.delete_project(path)
    return {"status": "deleted", "sessions_deleted": len(doomed)}


@app.get("/sessions")
async def list_sessions() -> list[dict[str, Any]]:
    return db.list_sessions()


@app.post("/sessions", status_code=201)
async def create_session(payload: CreateSessionRequest) -> dict[str, Any]:
    if payload.agent not in adapters:
        raise HTTPException(status_code=400, detail="Unknown agent")
    name = payload.name.strip()
    working_dir = payload.working_dir.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name cannot be empty")
    if not working_dir:
        raise HTTPException(status_code=400, detail="working_dir cannot be empty")
    if not working_dir.startswith("/"):
        raise HTTPException(status_code=400, detail="working_dir must be absolute")
    try:
        Path(working_dir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not create working directory: {exc}",
        )
    return db.create_session(
        name=name,
        working_dir=working_dir,
        agent=payload.agent,
    )


@app.patch("/sessions/{session_id}")
async def update_session(
    session_id: str, payload: UpdateSessionRequest
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

    return session if session is not None else require_session_or_404(session_id)


@app.post("/sessions/{session_id}/stop")
async def stop_session(session_id: str) -> dict[str, str]:
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
async def delete_session(session_id: str) -> dict[str, str]:
    await teardown_session(require_session_or_404(session_id))
    return {"status": "deleted"}


@app.post("/sessions/{session_id}/turn", status_code=202)
async def start_turn(session_id: str, payload: TurnRequest) -> dict[str, str]:
    await begin_turn(session_id, payload.prompt)
    return {"status": "running"}


@app.post("/sessions/{session_id}/bash", status_code=202)
async def start_bash(session_id: str, payload: BashRequest) -> dict[str, str]:
    await begin_bash(session_id, payload.command)
    return {"status": "running"}


@app.websocket("/ws/sessions/{session_id}")
async def session_websocket(websocket: WebSocket, session_id: str) -> None:
    session = db.get_session(session_id)
    if session is None:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    subscribers[session_id].add(websocket)

    try:
        await replay_scrollback(websocket, session_id)
        await websocket.send_json({"type": "status", "status": session["status"]})

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


async def begin_turn(session_id: str, prompt: str) -> None:
    async with turn_lock:
        session = require_session_or_404(session_id)
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


async def run_turn(session_id: str, prompt: str) -> None:
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


async def begin_bash(session_id: str, command: str) -> None:
    """Start a one-shot shell command in the session's working directory.

    Deliberately none of what `begin_turn` does: no turn lock, no status
    change, no adapter. Bash mode bypasses the agent entirely, so it must also
    bypass the turn state machine — a command can run while the agent is
    mid-turn or blocked on an approval, and neither notices the other.
    """
    require_session_or_404(session_id)
    existing = bash_tasks.get(session_id)
    if existing and not existing.done():
        raise HTTPException(
            status_code=409, detail="A command is already running in this session"
        )

    db.append_scrollback(session_id, "bash_input", {"command": command})
    db.touch_session(session_id)
    await broadcast(session_id, {"type": "bash_input", "command": command})

    bash_tasks[session_id] = asyncio.create_task(run_bash(session_id, command))


async def run_bash(session_id: str, command: str) -> None:
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


async def report_bash_error(session_id: str, message: str) -> None:
    """Record a command failure, unless the session itself is already gone.

    Deleting a session cancels its command, so the cancellation lands with the
    row possibly on its way out; scrollback has a foreign key to it, and an
    insert that loses that race must not surface as a crashed task.
    """
    if db.get_session(session_id) is None:
        return
    db.append_scrollback(session_id, "error", {"message": message})
    await broadcast(session_id, {"type": "error", "message": message})


async def cancel_bash(session_id: str) -> None:
    """Kill this session's in-flight command, if any, and wait for it to end."""
    task = bash_tasks.pop(session_id, None)
    if task is None or task.done():
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def handle_approval(
    session_id: str,
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
    session_id: str,
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


async def replay_scrollback(websocket: WebSocket, session_id: str) -> None:
    for row in db.recent_scrollback(session_id, SCROLLBACK_REPLAY_LIMIT):
        payload = row["payload"] if isinstance(row["payload"], dict) else {}
        await websocket.send_json({"type": row["type"], **payload})


async def broadcast(session_id: str, message: dict[str, Any]) -> None:
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


def with_existence(project: dict[str, Any]) -> dict[str, Any]:
    """Annotate a project with whether its directory is still there.

    One stat per project, not a scan: the client needs it to explain a project
    whose directory was removed outside the app and offer to forget it.
    """
    return {**project, "exists": os.path.isdir(project["path"])}


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
    if path == "/":
        raise HTTPException(
            status_code=400, detail="path must name a directory, not /"
        )
    return path


def require_session_or_404(session_id: str) -> dict[str, Any]:
    session = db.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
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
