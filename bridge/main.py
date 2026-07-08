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
import uuid
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .chat_history import ChatHistoryStore
from .config import _plugin_dir, load_config
from .database import ChatSessionStore
from .pty_manager import SessionManager

config = load_config()
_log_level = getattr(logging, config.log_level.upper(), logging.INFO)
if config.debug:
    _log_level = logging.DEBUG
logging.basicConfig(level=_log_level)
logger = logging.getLogger("hermes_bridge")

app = FastAPI(title="hermes-bridge", version="0.1.0")

store = ChatSessionStore()
history = ChatHistoryStore()
sessions = SessionManager(config)


# --------------------------------------------------------------------------- #
# Static web UI
# --------------------------------------------------------------------------- #

_webui_dir = (_plugin_dir() or Path(__file__).resolve().parent.parent) / "webui"
if _webui_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_webui_dir)), name="static")


@app.get("/chat")
async def chat_ui() -> FileResponse:
    index_path = _webui_dir / "index.html"
    if not index_path.exists():
        logger.error("hermes-bridge web UI not found at %s", index_path)
        raise HTTPException(status_code=404, detail=f"web UI not found at {index_path}")
    return FileResponse(str(index_path))


@app.on_event("startup")
async def _on_startup() -> None:
    logger.info("hermes-bridge listening on %s:%s", config.host, config.port)
    logger.info("hermes-bridge web UI directory: %s", _webui_dir)


class ChatRequest(BaseModel):
    chat_id: str
    message: str


class GateResolveRequest(BaseModel):
    chat_id: str
    gate_id: str
    choice: str


class CreateChatRequest(BaseModel):
    title: str = "New chat"


class RenameChatRequest(BaseModel):
    title: str


class SaveMessageRequest(BaseModel):
    role: str
    content: str


class UpdateMessageRequest(BaseModel):
    content: str


class GateChoiceRequest(BaseModel):
    choice: str


def _resolve_gate_choice(options: list[str], choice: str) -> str | None:
    """Return the matched option label, or None if the choice is unrecognised.

    Accepts the exact option label or a 1-based number/index.
    """
    choice = choice.strip()
    lowered = choice.lower()
    for i, opt in enumerate(options):
        if opt.strip().lower() == lowered:
            return opt
        numbered = f"{i + 1}"
        if lowered == numbered or lowered.startswith(numbered + ")") or lowered.startswith(numbered + "."):
            return opt
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(options):
            return options[idx]
    except ValueError:
        pass
    return None


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
        matched = _resolve_gate_choice(pending_gate.options, request.message.strip())
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


# --------------------------------------------------------------------------- #
# Chat history endpoints for the standalone web UI
# --------------------------------------------------------------------------- #


@app.get("/api/chats")
async def list_chats() -> list[dict]:
    return history.list_chats()


@app.post("/api/chats")
async def create_chat(request: CreateChatRequest) -> dict:
    chat_id = str(uuid.uuid4())
    history.create_chat(chat_id, request.title)
    return {"chat_id": chat_id, "title": request.title}


@app.get("/api/chats/{chat_id}")
async def get_chat(chat_id: str) -> dict:
    chat = history.get_chat(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat


@app.patch("/api/chats/{chat_id}")
async def rename_chat(chat_id: str, request: RenameChatRequest) -> dict:
    if history.get_chat(chat_id) is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    history.rename_chat(chat_id, request.title)
    return {"chat_id": chat_id, "title": request.title}


@app.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: str) -> dict:
    if history.get_chat(chat_id) is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    history.delete_chat(chat_id)
    return {"deleted": chat_id}


@app.get("/api/chats/{chat_id}/messages")
async def get_messages(chat_id: str) -> list[dict]:
    if history.get_chat(chat_id) is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return history.get_messages(chat_id)


@app.post("/api/chats/{chat_id}/messages")
async def save_message(chat_id: str, request: SaveMessageRequest) -> dict:
    if history.get_chat(chat_id) is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    message_id = history.add_message(chat_id, request.role, request.content)
    return {"id": message_id, "role": request.role, "content": request.content}


@app.put("/api/chats/{chat_id}/messages/{message_id}")
async def update_message(chat_id: str, message_id: int, request: UpdateMessageRequest) -> dict:
    if history.get_chat(chat_id) is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    history.update_message(message_id, request.content)
    return {"id": message_id, "content": request.content}
