from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from agent import AgentAdapter, ClaudeCodeAdapter, OpenCodeAdapter
from db import Database


SCROLLBACK_REPLAY_LIMIT = 200

app = FastAPI(title="agent-ui")
db = Database(os.environ.get("SESSION_DB", "sessions.db"))
adapters: dict[str, AgentAdapter] = {
    "claude-code": ClaudeCodeAdapter(),
    "opencode": OpenCodeAdapter(),
}
subscribers: dict[str, set[WebSocket]] = defaultdict(set)
running_tasks: dict[str, asyncio.Task[None]] = {}
turn_lock = asyncio.Lock()


class CreateSessionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    working_dir: str = Field(min_length=1)
    agent: str = "claude-code"


class TurnRequest(BaseModel):
    prompt: str = Field(min_length=1)


@app.on_event("startup")
async def startup() -> None:
    db.reset_active_sessions()


@app.on_event("shutdown")
async def shutdown() -> None:
    for task in list(running_tasks.values()):
        task.cancel()
    if running_tasks:
        await asyncio.gather(*running_tasks.values(), return_exceptions=True)
    db.close()


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


@app.post("/sessions/{session_id}/stop")
async def stop_session(session_id: str) -> dict[str, str]:
    session = require_session_or_404(session_id)
    adapter = adapters[session["agent"]]
    await adapter.stop(session)
    db.update_status(session_id, "idle")
    await broadcast(session_id, {"type": "status", "status": "idle"})
    return {"status": "idle"}


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, str]:
    session = require_session_or_404(session_id)
    adapter = adapters[session["agent"]]

    await adapter.stop(session)
    task = running_tasks.pop(session_id, None)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    for websocket in list(subscribers.get(session_id, set())):
        try:
            await websocket.close(code=1000)
        except Exception:
            pass
    subscribers.pop(session_id, None)

    db.delete_session(session_id)
    return {"status": "deleted"}


@app.post("/sessions/{session_id}/turn", status_code=202)
async def start_turn(session_id: str, payload: TurnRequest) -> dict[str, str]:
    await begin_turn(session_id, payload.prompt)
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

            elif message_type == "approval_response":
                request_id = str(message.get("request_id", ""))
                behavior = str(message.get("behavior", ""))
                try:
                    await handle_approval(session_id, request_id, behavior)
                except (KeyError, ValueError) as exc:
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

    try:
        async for event in adapter.start_turn(session, prompt):
            event_type = event.get("type")

            if event_type in {"output", "tool_use", "approval_request", "error"}:
                db.append_scrollback(
                    session_id,
                    event_type,
                    {key: value for key, value in event.items() if key != "type"},
                )

            if event_type == "approval_request":
                db.update_status(session_id, "awaiting_approval")
                await broadcast(
                    session_id,
                    {"type": "status", "status": "awaiting_approval"},
                )

            if event_type == "done" and event.get("session_id"):
                db.set_agent_session_id(session_id, event["session_id"])

            await broadcast(session_id, event)
    except Exception as exc:
        message = f"Agent turn failed: {exc}"
        db.append_scrollback(session_id, "error", {"message": message})
        await broadcast(session_id, {"type": "error", "message": message})
    finally:
        db.update_status(session_id, "idle")
        await broadcast(session_id, {"type": "status", "status": "idle"})
        running_tasks.pop(session_id, None)


async def handle_approval(session_id: str, request_id: str, behavior: str) -> None:
    session = db.require_session(session_id)
    adapter = adapters[session["agent"]]
    await adapter.send_approval(session, request_id, behavior)

    payload = {"request_id": request_id, "behavior": behavior}
    db.append_scrollback(session_id, "approval_response", payload)
    db.update_status(session_id, "running")
    await broadcast(session_id, {"type": "approval_response", **payload})
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


def require_session_or_404(session_id: str) -> dict[str, Any]:
    session = db.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


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
