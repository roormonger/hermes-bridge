"""
hermes-bridge FastAPI service.

Runs on the same host as the `hermes` CLI. Exposes:

  POST /v1/chat          -> SSE stream of {"type": "text"|"gate_interrupt", ...}
  POST /v1/gate/resolve  -> unblocks a paused subprocess awaiting a decision
  GET  /healthz          -> liveness probe

See README.md for the full protocol description and Open WebUI plugin setup.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .database import ChatSessionStore
from .pty_manager import SessionManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hermes_bridge")

app = FastAPI(title="hermes-bridge", version="0.1.0")

store = ChatSessionStore()
sessions = SessionManager()


class ChatRequest(BaseModel):
    chat_id: str
    message: str


class GateResolveRequest(BaseModel):
    chat_id: str
    gate_id: str
    choice: str


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.post("/v1/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    if not request.chat_id.strip():
        raise HTTPException(status_code=400, detail="chat_id is required")
    loop = asyncio.get_event_loop()
    hermes_session_id, _created = store.get_or_create_hermes_session_id(request.chat_id)
    session = sessions.get_or_start(request.chat_id, hermes_session_id, loop)

    pending_gate = session.get_pending_gate()
    if pending_gate is not None:
        matched = next(
            (opt for opt in pending_gate.options if opt.strip().lower() == request.message.strip().lower()),
            None,
        )
        if matched is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Chat {request.chat_id} has an unresolved gate "
                    f"({pending_gate.gate_id}). Reply with one of "
                    f"{pending_gate.options} or call /v1/gate/resolve."
                ),
            )
        await loop.run_in_executor(None, session.resolve_gate, pending_gate.gate_id, matched)
        return StreamingResponse(_sse_stream(session), media_type="text/event-stream")

    try:
        await loop.run_in_executor(None, session.send_text, request.message)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return StreamingResponse(_sse_stream(session), media_type="text/event-stream")


async def _sse_stream(session) -> AsyncGenerator[bytes, None]:
    while True:
        event = await session.queue.get()
        yield f"data: {json.dumps(event)}\n\n".encode("utf-8")

        if event.get("type") == "gate_interrupt":
            # Pause the stream here; the client must call /v1/gate/resolve
            # and open a new /v1/chat "continuation" (or the same request,
            # per client design) to keep receiving tokens. We simply stop
            # this generator -- the Pipe script is expected to re-poll by
            # issuing a resolve call, which resumes the subprocess, and a
            # fresh /v1/chat-less drain happens automatically because the
            # queue keeps accumulating events server-side between requests.
            return
        if event.get("type") == "process_exit":
            return


@app.post("/v1/gate/resolve")
async def resolve_gate(request: GateResolveRequest) -> dict:
    if not request.chat_id.strip():
        raise HTTPException(status_code=400, detail="chat_id is required")
    if not request.gate_id.strip():
        raise HTTPException(status_code=400, detail="gate_id is required")
    if not request.choice.strip():
        raise HTTPException(status_code=400, detail="choice is required")
    loop = asyncio.get_event_loop()
    session = sessions.get(request.chat_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"No active session for chat {request.chat_id}")

    try:
        await loop.run_in_executor(None, session.resolve_gate, request.gate_id, request.choice)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"status": "resolved", "gate_id": request.gate_id}


@app.post("/v1/chat/drain")
async def drain(request: ChatRequest) -> StreamingResponse:
    """Resume streaming any output queued after a gate was resolved.

    The Pipe script should call this (with the same chat_id, message
    ignored) immediately after a successful /v1/gate/resolve to continue
    rendering tokens without sending a new user message.
    """
    if not request.chat_id.strip():
        raise HTTPException(status_code=400, detail="chat_id is required")
    session = sessions.get(request.chat_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"No active session for chat {request.chat_id}")
    return StreamingResponse(_sse_stream(session), media_type="text/event-stream")
