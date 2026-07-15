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
import importlib.util
import json
import logging
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import AsyncGenerator

from fastapi import Depends, FastAPI, Header, HTTPException, UploadFile, WebSocket, status
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .analytics import get_models_analytics as _get_models_analytics_db
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


@app.get("/")
async def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/chat")


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

    if _BACKEND == "gateway":
        loop = asyncio.get_running_loop()
        try:
            gw = sessions.get_or_create(_MODEL_CATALOG_CHAT_ID, None, loop)
            await loop.run_in_executor(None, gw.model_options)
            await loop.run_in_executor(None, gw.current_model)
            logger.info("hermes-bridge model catalog pre-warmed")
        except Exception as exc:
            logger.warning("hermes-bridge model catalog pre-warm failed: %s", exc)


class ChatRequest(BaseModel):
    chat_id: str
    message: str
    assistant_message_id: Optional[int] = None


class GateResolveRequest(BaseModel):
    chat_id: str
    gate_id: str
    choice: str
    gate_kind: str = "approval"  # approval | clarify | sudo | secret


class CreateChatRequest(BaseModel):
    title: str = "New chat"


class RenameChatRequest(BaseModel):
    title: Optional[str] = None
    pinned: Optional[bool] = None


class SaveMessageRequest(BaseModel):
    role: str
    content: str
    images: list[str] = []
    tool_steps: list[dict] | None = None


class UpdateMessageRequest(BaseModel):
    content: str
    tool_steps: list[dict] | None = None


class UsageSaveRequest(BaseModel):
    usage: dict
    context_window: int | None = None


class GateChoiceRequest(BaseModel):
    choice: str


class ImageAttachRequest(BaseModel):
    chat_id: str
    content_base64: str
    filename: str = ""


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


def get_current_user_query(token: str | None = None, authorization: str | None = Header(default=None)) -> dict:
    """Like get_current_user but also accepts ?token= query param (for <img src=> URLs)."""
    raw = token or _extract_bearer_token(authorization)
    if not raw:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    user = users.decode_token(raw)
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
        return await _chat_gateway(request, loop, current_user["user_id"])
    return await _chat_pty(request, loop)


@app.get("/v1/file/download")
async def file_download(
    path: str,
    current_user: dict = Depends(get_current_user),
) -> FileResponse:
    """Stream an agent-produced file as a download.

    ``path`` must be an absolute path that lives under HERMES_HOME
    (``~/.hermes``) or under the current working directory / workspace.
    Symlinks are resolved before the check so traversal tricks don't work.
    """
    import mimetypes
    import os

    hermes_home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser().resolve()
    try:
        target = Path(path).expanduser().resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")

    # Must be under HERMES_HOME or cwd
    allowed_roots = [hermes_home, Path.cwd().resolve()]
    if not any(target == r or r in target.parents for r in allowed_roots):
        raise HTTPException(status_code=403, detail="Path is outside allowed directories")

    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="Path is not a file")

    # Block credential files
    blocked_names = {".env", ".envrc", "auth.json", "auth.lock", ".auth_secret"}
    if target.name.lower() in blocked_names:
        raise HTTPException(status_code=403, detail="Access to this file is not allowed")

    size = target.stat().st_size
    if size > 100 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large to download")

    mime_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(
        path=str(target),
        filename=target.name,
        media_type=mime_type,
        headers={"Content-Disposition": f'attachment; filename="{target.name}"'},
    )


_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"})


@app.get("/v1/file/preview")
async def file_preview(
    path: str,
    current_user: dict = Depends(get_current_user_query),
) -> FileResponse:
    """Serve an image file for inline display (no Content-Disposition attachment).

    Accepts auth token via ``?token=`` query param so ``<img src=...>`` tags
    can load it without custom headers.  Restricted to image extensions only.
    """
    import mimetypes
    import os

    hermes_home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser().resolve()
    try:
        target = Path(path).expanduser().resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")

    allowed_roots = [hermes_home, Path.cwd().resolve()]
    if not any(target == r or r in target.parents for r in allowed_roots):
        raise HTTPException(status_code=403, detail="Path is outside allowed directories")

    if target.suffix.lower() not in _IMAGE_EXTENSIONS:
        raise HTTPException(status_code=403, detail="Only image files can be previewed")

    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="Path is not a file")

    size = target.stat().st_size
    if size > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image too large to preview")

    mime_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(path=str(target), media_type=mime_type)


@app.post("/v1/image/attach")
async def image_attach(request: ImageAttachRequest, current_user: dict = Depends(get_current_user)) -> dict:
    if _BACKEND != "gateway":
        raise HTTPException(status_code=400, detail="Image attachment requires gateway backend")
    if not request.chat_id.strip():
        raise HTTPException(status_code=400, detail="chat_id is required")
    if not request.content_base64.strip():
        raise HTTPException(status_code=400, detail="content_base64 is required")
    _verify_chat_access(request.chat_id, current_user)
    loop = asyncio.get_running_loop()
    hermes_sid = store.get_hermes_session_id(request.chat_id)
    gw = sessions.get_or_create(request.chat_id, hermes_sid, loop)  # type: ignore[attr-defined]
    try:
        result = await loop.run_in_executor(
            None, gw.attach_image_bytes, request.content_base64, request.filename
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


class CancelRequest(BaseModel):
    chat_id: str


@app.post("/v1/chat/cancel")
async def cancel_chat(request: CancelRequest, current_user: dict = Depends(get_current_user)) -> dict:
    """Interrupt the currently running agent turn for a chat."""
    if _BACKEND != "gateway":
        raise HTTPException(status_code=400, detail="Cancel requires gateway backend")
    _verify_chat_access(request.chat_id, current_user)
    loop = asyncio.get_running_loop()
    gw = sessions.get(request.chat_id)  # type: ignore[attr-defined]
    if gw is None:
        return {"status": "no_session"}
    await loop.run_in_executor(None, gw.interrupt)
    return {"status": "interrupted"}


@app.post("/v1/chat/undo")
async def undo_chat(request: CancelRequest, current_user: dict = Depends(get_current_user)) -> dict:
    """Undo the last user/assistant turn for a chat session."""
    if _BACKEND != "gateway":
        raise HTTPException(status_code=400, detail="Undo requires gateway backend")
    _verify_chat_access(request.chat_id, current_user)
    loop = asyncio.get_running_loop()
    gw = sessions.get(request.chat_id)  # type: ignore[attr-defined]
    if gw is not None:
        await loop.run_in_executor(None, gw.session_undo)
    deleted = history.delete_last_turn(request.chat_id, current_user["user_id"])
    return {"status": "ok", "deleted": deleted}


@app.get("/v1/usage")
async def get_usage(
    chat_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Return the current session's token usage and context breakdown from Hermes."""
    if _BACKEND != "gateway":
        raise HTTPException(status_code=400, detail="Usage requires gateway backend")
    _verify_chat_access(chat_id, current_user)
    loop = asyncio.get_running_loop()
    hermes_sid = store.get_hermes_session_id(chat_id)
    gw = sessions.get_or_create(chat_id, hermes_sid, loop)  # type: ignore[attr-defined]
    usage, breakdown = await asyncio.gather(
        loop.run_in_executor(None, gw.session_usage),
        loop.run_in_executor(None, gw.session_context_breakdown),
    )
    return {"usage": usage, "context_breakdown": breakdown}


class ModelSwitchRequest(BaseModel):
    chat_id: str
    model: str
    provider: str = ""
    confirm_expensive_model: bool = False


# Shared scratch session key for catalog RPCs that don't belong to a specific chat.
_MODEL_CATALOG_CHAT_ID = "__hermes_bridge_models_catalog__"
_DEFAULT_HERMES_DASHBOARD_URL = "http://127.0.0.1:9119"


def _hermes_dashboard_url() -> str:
    """Return the Hermes dashboard URL, in order of precedence:

    1. Explicit config value (hermes_dashboard_url)
    2. HERMES_DASHBOARD_URL environment variable
    3. HERMES_DASHBOARD_PORT environment variable (host 127.0.0.1)
    4. Default localhost port used by Hermes dashboard
    """
    if config.hermes_dashboard_url:
        return config.hermes_dashboard_url
    env_url = os.environ.get("HERMES_DASHBOARD_URL")
    if env_url:
        return env_url
    env_port = os.environ.get("HERMES_DASHBOARD_PORT")
    if env_port:
        return f"http://127.0.0.1:{env_port}"
    return _DEFAULT_HERMES_DASHBOARD_URL


@app.get("/v1/models")
async def list_models(current_user: dict = Depends(get_current_user)) -> dict:
    """List all available models/providers configured in Hermes."""
    if _BACKEND != "gateway":
        raise HTTPException(status_code=400, detail="Model picker requires gateway backend")
    loop = asyncio.get_running_loop()
    gw = sessions.get_or_create(_MODEL_CATALOG_CHAT_ID, None, loop)  # type: ignore[attr-defined]
    try:
        result = await loop.run_in_executor(None, gw.model_options)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


@app.get("/v1/analytics/models")
async def list_analytics_models(current_user: dict = Depends(get_current_user)) -> dict:
    """Return Hermes dashboard analytics models (recently-used / saved profiles).

    Tries to read directly from the Hermes session database so the request
    works even when the dashboard requires authentication. Falls back to proxying
    the dashboard HTTP endpoint only when hermes_state is not available.
    """
    try:
        return _get_models_analytics_db(days=30)
    except RuntimeError as exc:
        # hermes_state unavailable; fall back to dashboard HTTP proxy.
        logging.getLogger(__name__).warning("Direct analytics DB read failed: %s", exc)
    url = _hermes_dashboard_url().rstrip("/") + "/api/analytics/models"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data
    except urllib.error.HTTPError as exc:
        raise HTTPException(
            status_code=exc.code,
            detail=exc.read().decode("utf-8", errors="ignore"),
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/v1/model")
async def get_model(
    chat_id: str | None = None,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Get the currently selected model/provider. Optionally for a live chat session."""
    if _BACKEND != "gateway":
        raise HTTPException(status_code=400, detail="Model info requires gateway backend")
    loop = asyncio.get_running_loop()
    if chat_id:
        _verify_chat_access(chat_id, current_user)
        hermes_sid = store.get_hermes_session_id(chat_id)
        gw = sessions.get_or_create(chat_id, hermes_sid, loop)  # type: ignore[attr-defined]
    else:
        gw = sessions.get_or_create(_MODEL_CATALOG_CHAT_ID, None, loop)  # type: ignore[attr-defined]
    try:
        result = await loop.run_in_executor(None, gw.current_model)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


@app.post("/v1/model")
async def set_model(request: ModelSwitchRequest, current_user: dict = Depends(get_current_user)) -> dict:
    """Switch the model for a chat session."""
    if _BACKEND != "gateway":
        raise HTTPException(status_code=400, detail="Model switch requires gateway backend")
    _verify_chat_access(request.chat_id, current_user)
    loop = asyncio.get_running_loop()
    hermes_sid = store.get_hermes_session_id(request.chat_id)
    gw = sessions.get_or_create(request.chat_id, hermes_sid, loop)  # type: ignore[attr-defined]
    value = request.model
    if request.provider:
        value = f"{value} --provider {request.provider}"
    try:
        result = await loop.run_in_executor(None, gw.set_model, value, request.confirm_expensive_model)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


# ---------------------------------------------------------------------------
# Gateway backend
# ---------------------------------------------------------------------------

async def _chat_gateway(
    request: ChatRequest, loop: asyncio.AbstractEventLoop, user_id: Optional[str]
) -> StreamingResponse:
    hermes_sid = store.get_hermes_session_id(request.chat_id)
    gw = sessions.get_or_create(request.chat_id, hermes_sid, loop)  # type: ignore[attr-defined]

    pending = gw.get_pending_gate()
    if pending is None and gw.is_turn_active():
        return StreamingResponse(
            _sse_stream_gateway(gw, request.assistant_message_id, user_id),
            media_type="text/event-stream",
        )
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
        return StreamingResponse(
            _sse_stream_gateway(gw, request.assistant_message_id, user_id),
            media_type="text/event-stream",
        )

    try:
        await loop.run_in_executor(None, gw.submit, request.message)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return StreamingResponse(
        _sse_stream_gateway(gw, request.assistant_message_id, user_id),
        media_type="text/event-stream",
    )


async def _sse_stream_gateway(
    gw, assistant_message_id: Optional[int] = None, user_id: Optional[str] = None
) -> AsyncGenerator[bytes, None]:
    """Translate tui_gateway JSON-RPC frames into SSE events for the web UI."""
    content = ""
    tool_steps: list[dict] = []
    if assistant_message_id is not None:
        existing = next(
            (
                message
                for message in reversed(history.get_messages(gw.chat_id, user_id))
                if message["id"] == assistant_message_id and message["role"] == "assistant"
            ),
            None,
        )
        if existing is not None:
            content = existing["content"]
            tool_steps = existing["tool_steps"]

    async with gw.stream_lock:
        while True:
            frame = await gw.queue.get()
            event = _translate_event(frame)

            if event is None:
                if "error" in frame:
                    event = {"type": "error", "message": frame["error"].get("message", "RPC error")}
                else:
                    continue

            etype = event.get("type", "")
            if etype == "text":
                content += event.get("text", "")
            elif etype == "tool_start":
                source_id = event.get("tool_id") or event.get("name", "")
                tool_steps.append(
                    {
                        "id": f"{assistant_message_id}-tool-{len(tool_steps)}-{source_id}",
                        "sourceId": source_id,
                        "name": event.get("name", ""),
                        "context": event.get("context", ""),
                        "status": "running",
                    }
                )
            elif etype == "tool_complete":
                source_id = event.get("tool_id") or event.get("name", "")
                index = next(
                    (
                        i
                        for i in range(len(tool_steps) - 1, -1, -1)
                        if tool_steps[i].get("status") == "running"
                        and (
                            tool_steps[i].get("sourceId") == source_id
                            or tool_steps[i].get("name") == event.get("name", "")
                        )
                    ),
                    -1,
                )
                if index >= 0:
                    tool_steps[index] = {
                        **tool_steps[index],
                        "summary": event.get("summary", ""),
                        "durationS": event.get("duration_s"),
                        "status": "done",
                    }

            if assistant_message_id is not None:
                history.update_message(assistant_message_id, user_id, content, tool_steps)

            terminal = etype in ("gate_interrupt", "turn_complete", "error")
            if etype == "gate_interrupt":
                gw.set_pending_gate(event)
            if terminal:
                gw.mark_turn_finished()
            if etype in ("gate_interrupt", "turn_complete") and gw.hermes_session_id:
                store.set_hermes_session_id(gw.chat_id, gw.hermes_session_id)

            yield f"data: {json.dumps(event)}\n\n".encode("utf-8")

            if terminal:
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


@app.get("/v1/chat/status")
async def chat_status(chat_id: str, current_user: dict = Depends(get_current_user)) -> dict:
    _verify_chat_access(chat_id, current_user)
    if _BACKEND != "gateway":
        return {"recoverable": False, "active": False, "queued_events": 0, "pending_gate": None}
    session = sessions.get(chat_id)
    if session is None:
        return {"recoverable": False, "active": False, "queued_events": 0, "pending_gate": None}
    pending_gate = session.get_pending_gate()
    queued_events = session.queue.qsize()
    active = session.is_turn_active() if _BACKEND == "gateway" else False
    return {
        "recoverable": active or pending_gate is not None,
        "active": active,
        "queued_events": queued_events,
        "pending_gate": pending_gate,
    }


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
        return StreamingResponse(
            _sse_stream_gateway(session, request.assistant_message_id, current_user["user_id"]),
            media_type="text/event-stream",
        )  # type: ignore[arg-type]
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
    chat = history.get_chat(chat_id, current_user["user_id"])
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    if request.title is not None:
        history.rename_chat(chat_id, current_user["user_id"], request.title)
    if request.pinned is not None:
        history.pin_chat(chat_id, current_user["user_id"], request.pinned)
    updated = history.get_chat(chat_id, current_user["user_id"])
    return updated or chat


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
    message_id = history.add_message(
        chat_id,
        current_user["user_id"],
        request.role,
        request.content,
        images=request.images or None,
        tool_steps=request.tool_steps or None,
    )
    return {"id": message_id, "role": request.role, "content": request.content}


@app.put("/api/chats/{chat_id}/messages/{message_id}")
async def update_message(chat_id: str, message_id: int, request: UpdateMessageRequest, current_user: dict = Depends(get_current_user)) -> dict:
    if history.get_chat(chat_id, current_user["user_id"]) is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    history.update_message(message_id, current_user["user_id"], request.content, tool_steps=request.tool_steps or None)
    return {"id": message_id, "content": request.content}


@app.get("/api/chats/{chat_id}/usage")
async def get_chat_usage(chat_id: str, current_user: dict = Depends(get_current_user)) -> dict:
    if history.get_chat(chat_id, current_user["user_id"]) is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    payload = history.get_chat_usage(chat_id, current_user["user_id"]) or {}
    context_window = payload.pop("_context_window", None)
    return {"usage": payload, "context_window": context_window}


@app.put("/api/chats/{chat_id}/usage")
async def save_chat_usage(chat_id: str, request: UsageSaveRequest, current_user: dict = Depends(get_current_user)) -> dict:
    if history.get_chat(chat_id, current_user["user_id"]) is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    payload = {**request.usage}
    if request.context_window:
        payload["_context_window"] = request.context_window
    history.set_chat_usage(chat_id, current_user["user_id"], payload)
    return {"status": "ok"}


@app.put("/api/chats/{chat_id}/messages/{message_id}/usage")
async def save_message_usage(chat_id: str, message_id: int, request: UsageSaveRequest, current_user: dict = Depends(get_current_user)) -> dict:
    if history.get_chat(chat_id, current_user["user_id"]) is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    history.update_message_usage(message_id, current_user["user_id"], request.usage)
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Voice support: speech-to-text and text-to-speech
# --------------------------------------------------------------------------- #

class SpeakRequest(BaseModel):
    text: str
    lang: str | None = None
    voice: str | None = None


@app.get("/api/plugins/hermes-chat/voice-config")
async def get_voice_config(current_user: dict = Depends(get_current_user)) -> dict:
    cfg = load_config()
    return {
        "voice_enabled": cfg.voice_enabled,
        "default_tts_voice": cfg.default_tts_voice,
        "tts_available": importlib.util.find_spec("edge_tts") is not None,
        "stt_available": (
            importlib.util.find_spec("faster_whisper") is not None
            and importlib.util.find_spec("imageio_ffmpeg") is not None
        ),
    }


@app.post("/v1/audio/transcribe")
async def transcribe_audio(file: UploadFile, current_user: dict = Depends(get_current_user)) -> dict:
    """Transcribe uploaded audio (webm/opus from browser) to text via Whisper."""
    if not load_config().voice_enabled:
        raise HTTPException(status_code=503, detail="Voice is disabled in settings.")
    import shutil
    import tempfile

    suffix = "." + (file.filename or "audio.webm").rsplit(".", 1)[-1] if "." in (file.filename or "") else ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)
    try:
        from .voice import transcribe

        text = transcribe(tmp_path)
        return {"text": text}
    except ImportError as e:
        logger.error("Voice dependencies not installed: %s", e)
        raise HTTPException(status_code=503, detail="Voice support not installed. Install dependencies via the dashboard → Install Voice button.")
    except Exception as e:
        logger.error("Transcription failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post("/v1/audio/speak")
async def speak_text(request: SpeakRequest, current_user: dict = Depends(get_current_user)) -> FileResponse:
    """Synthesize speech from text via Piper TTS. Returns a wav audio file."""
    if not load_config().voice_enabled:
        raise HTTPException(status_code=503, detail="Voice is disabled in settings.")
    if request.voice is None:
        request.voice = load_config().default_tts_voice
    try:
        from .voice import synthesize

        wav_path = await synthesize(request.text, lang=request.lang, voice=request.voice)
        media_type = "audio/mpeg" if str(wav_path).endswith(".mp3") else "audio/wav"
        filename = "speech.mp3" if str(wav_path).endswith(".mp3") else "speech.wav"
        return FileResponse(
            str(wav_path),
            media_type=media_type,
            filename=filename,
            background=None,
        )
    except ImportError as e:
        logger.error("Voice dependency import failed: %s", e)
        raise HTTPException(status_code=503, detail=f"Voice dependency not available: {e}. Try reinstalling via the dashboard → Install Voice button.")
    except Exception as e:
        logger.error("TTS failed: %s", e)
        raise HTTPException(status_code=500, detail=f"TTS failed: {e}")
