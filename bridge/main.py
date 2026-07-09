"""
hermes-bridge FastAPI service.

Runs on the same host as the `hermes` CLI. Exposes:

  POST /v1/chat          -> SSE stream of {"type": "text"|"gate_interrupt"|"tool_start"|...
  POST /v1/gate/resolve  -> resolves an approval/clarify/sudo gate
  WS   /api/ws           -> raw tui_gateway JSON-RPC WebSocket passthrough
  GET  /healthz          -> liveness probe

See README.md for the full protocol description.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import AsyncGenerator

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .chat_history import ChatHistoryStore
from .config import _plugin_dir, auth_secret, load_config
from .database import ChatSessionStore
from .users import UserStore

config = load_config()
_log_level = getattr(logging, config.log_level.upper(), logging.INFO)
if config.debug:
    _log_level = logging.DEBUG
logging.basicConfig(level=_log_level)
logger = logging.getLogger("hermes_bridge")

# ---------------------------------------------------------------------------
# Gateway backend selection: prefer tui_gateway (in-process JSON-RPC) over
# the legacy PTY path. Both expose the same interface to the REST layer.
# ---------------------------------------------------------------------------
from .gateway_session import gateway_available, gateway_available_error, GatewaySessionManager, _translate_event

if gateway_available():
    logger.info("tui_gateway available — using in-process JSON-RPC backend")
    _BACKEND = "gateway"
else:
    logger.warning(
        "tui_gateway not available (%s) — using legacy PTY backend",
        gateway_available_error() or "unknown import error",
    )
    from .pty_manager import SessionManager as _PtySessionManager  # type: ignore
    _BACKEND = "pty"

app = FastAPI(title="hermes-bridge", version="0.1.0")

store = ChatSessionStore()
history = ChatHistoryStore()
users = UserStore(secret=auth_secret())

if _BACKEND == "gateway":
    sessions = GatewaySessionManager(session_idle_timeout=config.session_idle_timeout)
else:
    sessions = _PtySessionManager(config)  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Static web UI
# --------------------------------------------------------------------------- #

_webui_dir = (_plugin_dir() or Path(__file__).resolve().parent.parent) / "webui" / "dist"
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
    logger.info("hermes-bridge backend: %s", _BACKEND)


class ChatRequest(BaseModel):
    chat_id: str
    message: str


class GateResolveRequest(BaseModel):
    chat_id: str
    gate_id: str
    choice: str
    gate_kind: str = "approval"  # approval | clarify | sudo | secret


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


class LoginRequest(BaseModel):
    username: str
    password: str


class AuthResponse(BaseModel):
    user_id: str
    username: str
    token: str


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


def get_current_user(authorization: str | None = Header(default=None)) -> dict:
    token = _extract_bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing authorization header")
    user = users.decode_token(token)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    return user


def _verify_chat_access(chat_id: str, current_user: dict) -> dict:
    """Return the chat if the user owns it (or it is unowned), otherwise raise 404."""
    chat = history.get_chat(chat_id, current_user["user_id"])
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat


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


@app.post("/api/auth/login")
async def login(request: LoginRequest) -> AuthResponse:
    user = users.verify_user(request.username, request.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = users.create_token(user["user_id"])
    return AuthResponse(user_id=user["user_id"], username=user["username"], token=token)


@app.get("/api/auth/me")
async def me(current_user: dict = Depends(get_current_user)) -> dict:
    return {"user_id": current_user["user_id"], "username": current_user["username"]}


@app.post("/v1/chat")
async def chat(request: ChatRequest, current_user: dict = Depends(get_current_user)) -> StreamingResponse:
    if not request.chat_id.strip():
        raise HTTPException(status_code=400, detail="chat_id is required")
    _verify_chat_access(request.chat_id, current_user)
    loop = asyncio.get_running_loop()
    if _BACKEND == "gateway":
        return await _chat_gateway(request, loop)
    return await _chat_pty(request, loop)


# ---------------------------------------------------------------------------
# Gateway backend
# ---------------------------------------------------------------------------

async def _chat_gateway(request: ChatRequest, loop: asyncio.AbstractEventLoop) -> StreamingResponse:
    hermes_sid = store.get_hermes_session_id(request.chat_id)
    gw = sessions.get_or_create(request.chat_id, hermes_sid, loop)  # type: ignore[attr-defined]

    pending = gw.get_pending_gate()
    if pending is not None:
        gate_kind = pending.get("gate_kind", "approval")
        gate_id = pending.get("gate_id", "")
        options = pending.get("options", [])
        choice = request.message.strip()
        if options:
            matched = _resolve_gate_choice(options, choice)
            if matched is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Chat {request.chat_id} has an unresolved {gate_kind} gate "
                        f"({gate_id}). Reply with one of {options} or call /v1/gate/resolve."
                    ),
                )
            choice = matched
        gw.set_pending_gate(None)
        try:
            await loop.run_in_executor(None, gw.respond_gate, gate_kind, gate_id, choice)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return StreamingResponse(_sse_stream_gateway(gw), media_type="text/event-stream")

    try:
        await loop.run_in_executor(None, gw.submit, request.message)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return StreamingResponse(_sse_stream_gateway(gw), media_type="text/event-stream")


async def _sse_stream_gateway(gw) -> AsyncGenerator[bytes, None]:
    """Translate tui_gateway JSON-RPC frames into SSE events for the web UI."""
    while True:
        frame = await gw.queue.get()
        event = _translate_event(frame)

        if event is None:
            if "error" in frame:
                event = {"type": "error", "message": frame["error"].get("message", "RPC error")}
            else:
                continue

        yield f"data: {json.dumps(event)}\n\n".encode("utf-8")

        etype = event.get("type", "")

        if etype == "gate_interrupt":
            gw.set_pending_gate(event)
            if gw.hermes_session_id:
                store.set_hermes_session_id(gw.chat_id, gw.hermes_session_id)
            return

        if etype == "turn_complete":
            if gw.hermes_session_id:
                store.set_hermes_session_id(gw.chat_id, gw.hermes_session_id)
            return

        if etype == "error":
            return


# ---------------------------------------------------------------------------
# Legacy PTY backend (fallback)
# ---------------------------------------------------------------------------

async def _chat_pty(request: ChatRequest, loop: asyncio.AbstractEventLoop) -> StreamingResponse:
    hermes_sid, _created = store.get_or_create_hermes_session_id(request.chat_id)
    session = sessions.get_or_start(request.chat_id, hermes_sid, loop)  # type: ignore[attr-defined]

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
        return StreamingResponse(_sse_stream_pty(session), media_type="text/event-stream")

    try:
        await loop.run_in_executor(None, session.send_text, request.message)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return StreamingResponse(_sse_stream_pty(session), media_type="text/event-stream")


async def _sse_stream_pty(session) -> AsyncGenerator[bytes, None]:
    while True:
        event = await session.queue.get()
        yield f"data: {json.dumps(event)}\n\n".encode("utf-8")
        if event.get("type") in ("gate_interrupt", "process_exit"):
            return


# ---------------------------------------------------------------------------
# Gate resolve (both backends)
# ---------------------------------------------------------------------------

@app.post("/v1/gate/resolve")
async def resolve_gate(request: GateResolveRequest, current_user: dict = Depends(get_current_user)) -> dict:
    if not request.chat_id.strip():
        raise HTTPException(status_code=400, detail="chat_id is required")
    if not request.gate_id.strip():
        raise HTTPException(status_code=400, detail="gate_id is required")
    if not request.choice.strip():
        raise HTTPException(status_code=400, detail="choice is required")
    _verify_chat_access(request.chat_id, current_user)
    loop = asyncio.get_running_loop()
    session = sessions.get(request.chat_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"No active session for chat {request.chat_id}")

    try:
        if _BACKEND == "gateway":
            session.set_pending_gate(None)  # type: ignore[attr-defined]
            await loop.run_in_executor(
                None, session.respond_gate, request.gate_kind, request.gate_id, request.choice  # type: ignore[attr-defined]
            )
        else:
            await loop.run_in_executor(None, session.resolve_gate, request.gate_id, request.choice)  # type: ignore[attr-defined]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"status": "resolved", "gate_id": request.gate_id}


@app.post("/v1/chat/drain")
async def drain(request: ChatRequest, current_user: dict = Depends(get_current_user)) -> StreamingResponse:
    """Resume streaming output after a gate was resolved out-of-band."""
    if not request.chat_id.strip():
        raise HTTPException(status_code=400, detail="chat_id is required")
    _verify_chat_access(request.chat_id, current_user)
    session = sessions.get(request.chat_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"No active session for chat {request.chat_id}")
    if _BACKEND == "gateway":
        return StreamingResponse(_sse_stream_gateway(session), media_type="text/event-stream")  # type: ignore[arg-type]
    return StreamingResponse(_sse_stream_pty(session), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# WebSocket passthrough — raw tui_gateway JSON-RPC
# ---------------------------------------------------------------------------

@app.websocket("/api/ws")
async def ws_gateway(ws: WebSocket) -> None:
    """
    Raw tui_gateway JSON-RPC WebSocket passthrough.

    Lets the web UI speak the full tui_gateway protocol directly —
    session management, slash commands, model switching, approvals, etc.
    Only available when the gateway backend is active.
    """
    if _BACKEND != "gateway":
        await ws.accept()
        await ws.close(code=1011, reason="tui_gateway not available — PTY backend active")
        return
    try:
        from tui_gateway.ws import handle_ws
        await handle_ws(ws)
    except Exception as exc:
        logger.exception("ws_gateway error: %s", exc)


# --------------------------------------------------------------------------- #
# Chat history endpoints for the standalone web UI
# --------------------------------------------------------------------------- #


@app.get("/api/chats")
async def list_chats(current_user: dict = Depends(get_current_user)) -> list[dict]:
    return history.list_chats(current_user["user_id"])


@app.post("/api/chats")
async def create_chat(request: CreateChatRequest, current_user: dict = Depends(get_current_user)) -> dict:
    chat_id = str(uuid.uuid4())
    history.create_chat(chat_id, current_user["user_id"], request.title)
    return {"chat_id": chat_id, "title": request.title}


@app.get("/api/chats/{chat_id}")
async def get_chat(chat_id: str, current_user: dict = Depends(get_current_user)) -> dict:
    chat = history.get_chat(chat_id, current_user["user_id"])
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat


@app.patch("/api/chats/{chat_id}")
async def rename_chat(chat_id: str, request: RenameChatRequest, current_user: dict = Depends(get_current_user)) -> dict:
    if history.get_chat(chat_id, current_user["user_id"]) is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    history.rename_chat(chat_id, current_user["user_id"], request.title)
    return {"chat_id": chat_id, "title": request.title}


@app.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: str, current_user: dict = Depends(get_current_user)) -> dict:
    if history.get_chat(chat_id, current_user["user_id"]) is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    history.delete_chat(chat_id, current_user["user_id"])
    return {"deleted": chat_id}


@app.get("/api/chats/{chat_id}/messages")
async def get_messages(chat_id: str, current_user: dict = Depends(get_current_user)) -> list[dict]:
    if history.get_chat(chat_id, current_user["user_id"]) is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return history.get_messages(chat_id, current_user["user_id"])


@app.post("/api/chats/{chat_id}/messages")
async def save_message(chat_id: str, request: SaveMessageRequest, current_user: dict = Depends(get_current_user)) -> dict:
    if history.get_chat(chat_id, current_user["user_id"]) is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    message_id = history.add_message(chat_id, current_user["user_id"], request.role, request.content)
    return {"id": message_id, "role": request.role, "content": request.content}


@app.put("/api/chats/{chat_id}/messages/{message_id}")
async def update_message(chat_id: str, message_id: int, request: UpdateMessageRequest, current_user: dict = Depends(get_current_user)) -> dict:
    if history.get_chat(chat_id, current_user["user_id"]) is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    history.update_message(message_id, current_user["user_id"], request.content)
    return {"id": message_id, "content": request.content}
