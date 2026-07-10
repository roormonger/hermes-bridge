"""
In-process TUI gateway session manager.

Instead of spawning `hermes chat -r <id>` in a PTY and scraping ANSI output,
we import `tui_gateway.server` (which is part of the Hermes Python package)
and drive it via its JSON-RPC dispatch function directly in the same process.

Architecture
------------
  REST /v1/chat
       │
       ▼
  GatewaySession.submit(text)          ← JSON-RPC: prompt.submit / session.create
       │
       ▼
  tui_gateway.server.dispatch(req)     ← in-process call, no subprocess/network
       │
       ▼
  events pushed via Transport.write()  ← we inject a QueueTransport
       │
       ▼
  asyncio.Queue  →  SSE stream         ← same shape the UI already consumes

Gate / approval flow
--------------------
  tui_gateway emits  approval.request / clarify.request / sudo.request
  → translated to    {"type": "gate_interrupt", ...}  SSE event
  → resolved via     approval.respond / clarify.respond / sudo.respond  RPC call

Fallback
--------
If `tui_gateway` is not importable (Hermes not installed, wrong Python env,
etc.) we raise ImportError at import time so `main.py` can fall back to the
legacy PTY path and emit a clear log message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger("hermes_bridge.gateway")

# ---------------------------------------------------------------------------
# Import guard — fail loudly if tui_gateway is not available so the caller
# can decide whether to fall back to the PTY path.
# ---------------------------------------------------------------------------
try:
    from tui_gateway import server as _gw_server
    from tui_gateway.transport import bind_transport, Transport
    _GATEWAY_AVAILABLE = True
    _import_err_msg = ""
    logger.info("tui_gateway imported successfully")
except Exception as _import_err:
    _GATEWAY_AVAILABLE = False
    _import_err_msg = str(_import_err)
    logger.warning(
        "tui_gateway not available (%s: %s) — falling back to PTY backend",
        type(_import_err).__name__,
        _import_err_msg,
    )


def gateway_available() -> bool:
    return _GATEWAY_AVAILABLE


def gateway_available_error() -> str:
    return _import_err_msg


# ---------------------------------------------------------------------------
# QueueTransport — injected into tui_gateway so its events reach our queue
# ---------------------------------------------------------------------------

class QueueTransport:
    """A tui_gateway Transport that pushes JSON-RPC frames onto an asyncio Queue."""

    def __init__(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
        self._queue = queue
        self._loop = loop

    # tui_gateway.transport.Transport interface
    def write(self, obj: dict) -> bool:
        self._loop.call_soon_threadsafe(self._queue.put_nowait, obj)
        return True

    def is_alive(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Event translation: tui_gateway JSON-RPC → our SSE event shape
# ---------------------------------------------------------------------------

def _translate_event(frame: dict) -> Optional[dict]:
    """
    Convert a tui_gateway JSON-RPC push frame into our SSE event dict.

    tui_gateway pushes events as:
        {"jsonrpc": "2.0", "method": "event", "params": {"type": "...", ...}}

    Returns None for frames we don't need to forward (e.g. RPC ack frames).
    """
    if frame.get("method") != "event":
        return None

    params = frame.get("params", {})
    etype = params.get("type", "")
    payload = params.get("payload", {})

    if etype == "message.delta":
        text = payload.get("text") or payload.get("delta") or ""
        return {"type": "text", "text": text}

    if etype in ("message.complete", "turn.complete"):
        return {"type": "turn_complete"}

    if etype == "tool.start":
        return {
            "type": "tool_start",
            "tool_id": payload.get("tool_id", ""),
            "name": payload.get("name", ""),
            "context": payload.get("context", ""),
        }

    if etype == "tool.progress":
        return {
            "type": "tool_progress",
            "tool_id": payload.get("tool_id", ""),
            "name": payload.get("name", ""),
            "text": payload.get("text", ""),
        }

    if etype == "tool.complete":
        return {
            "type": "tool_complete",
            "tool_id": payload.get("tool_id", ""),
            "name": payload.get("name", ""),
            "summary": payload.get("summary", ""),
            "duration_s": payload.get("duration_s"),
        }

    if etype == "approval.request":
        return {
            "type": "gate_interrupt",
            "gate_kind": "approval",
            "gate_id": payload.get("request_id", ""),
            "prompt": payload.get("prompt", ""),
            "options": payload.get("choices") or ["approve", "deny"],
            "context": payload.get("context", {}),
        }

    if etype == "clarify.request":
        logger.debug("clarify.request payload keys=%s payload=%r", list(payload.keys()), payload)
        choices = payload.get("choices") or payload.get("options") or []
        return {
            "type": "gate_interrupt",
            "gate_kind": "clarify",
            "gate_id": payload.get("request_id", ""),
            "prompt": payload.get("question", ""),
            "options": choices,
        }

    if etype == "sudo.request":
        return {
            "type": "gate_interrupt",
            "gate_kind": "sudo",
            "gate_id": payload.get("request_id", ""),
            "prompt": payload.get("prompt", "Password required"),
            "options": [],
        }

    if etype == "secret.request":
        return {
            "type": "gate_interrupt",
            "gate_kind": "secret",
            "gate_id": payload.get("request_id", ""),
            "prompt": payload.get("prompt", "Secret required"),
            "options": [],
        }

    if etype == "error":
        return {
            "type": "error",
            "message": payload.get("message", "Unknown error"),
        }

    if etype == "session.title":
        return {
            "type": "session_title",
            "title": payload.get("title", ""),
            "session_id": payload.get("session_id", ""),
        }

    # Ignore internal / UI-only frames
    return None


# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------

def _rpc(method: str, params: dict, rid: Any = None) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": rid if rid is not None else uuid.uuid4().hex[:8],
        "method": method,
        "params": params,
    }


def _dispatch_sync(req: dict, transport: "QueueTransport") -> dict:
    """Call tui_gateway.server.dispatch in a thread-safe way and return the result."""
    from tui_gateway.transport import bind_transport, reset_transport
    token = bind_transport(transport)
    try:
        result = _gw_server.dispatch(req, transport)
        return result or {}
    finally:
        reset_transport(token)


# ---------------------------------------------------------------------------
# GatewaySession — one per active chat
# ---------------------------------------------------------------------------

class GatewaySession:
    """
    Tracks the TUI gateway session for a single chat.

    Lifecycle
    ---------
    1. First message: `ensure_session()` calls session.create → gets a Hermes
       session_id back.  Subsequent messages: session already exists.
    2. `submit(text)` calls prompt.submit with the session_id.
    3. Events arrive on `self.queue` as translated SSE dicts.
    4. `respond_gate(kind, request_id, value)` resolves approval/clarify/sudo.
    5. `close()` calls session.close on teardown.
    """

    def __init__(
        self,
        chat_id: str,
        hermes_session_id: Optional[str],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.chat_id = chat_id
        self.hermes_session_id = hermes_session_id  # None → will be created on first submit
        self.loop = loop
        self.queue: asyncio.Queue = asyncio.Queue()
        self._transport = QueueTransport(self.queue, loop)
        self._lock = threading.Lock()
        self._pending_gate: Optional[dict] = None  # last gate_interrupt event
        self.last_active = time.monotonic()
        # True once we've confirmed this session_id is live in the gateway.
        # Starts False for sessions loaded from DB (may be stale); True for
        # sessions we create ourselves in this process run.
        self._session_verified: bool = hermes_session_id is None

    # ------------------------------------------------------------------
    def _call(self, method: str, params: dict) -> dict:
        """Dispatch a JSON-RPC call in the Hermes thread pool and return the result."""
        req = _rpc(method, params)
        result = _dispatch_sync(req, self._transport)
        if isinstance(result, dict) and "error" in result:
            code = result["error"].get("code", 0)
            msg = result["error"].get("message", "unknown")
            logger.warning("gateway RPC %s error %s: %s", method, code, msg)
        return result

    # ------------------------------------------------------------------
    def ensure_session(self) -> str:
        """Return the Hermes session_id, creating one via the gateway if needed.

        If the stored session_id is stale (gateway restarted, session reaped),
        a new session is created transparently.
        """
        with self._lock:
            if self.hermes_session_id:
                if self._session_verified:
                    return self.hermes_session_id
                # DB-loaded session — probe once to confirm it's still live.
                probe = self._call("session.info", {"session_id": self.hermes_session_id})
                if not (isinstance(probe, dict) and probe.get("error", {}).get("code") == 4001):
                    self._session_verified = True
                    return self.hermes_session_id
                logger.warning(
                    "chat_id=%s stored session_id=%s no longer exists in gateway, recreating",
                    self.chat_id, self.hermes_session_id,
                )
                self.hermes_session_id = None

            result = self._call("session.create", {})
            sid = (result.get("result") or {}).get("session_id") or \
                  (result.get("result") or {}).get("id") or ""
            if not sid:
                logger.error("session.create returned no session_id: %r", result)
                raise RuntimeError("Failed to create Hermes session: no session_id in response")
            self.hermes_session_id = sid
            self._session_verified = True
            logger.info("chat_id=%s created hermes session_id=%s", self.chat_id, sid)
            return sid

    # ------------------------------------------------------------------
    def submit(self, text: str) -> None:
        """Send a user message to the gateway (non-blocking, events arrive on queue)."""
        self.last_active = time.monotonic()
        sid = self.ensure_session()
        result = self._call("prompt.submit", {"session_id": sid, "text": text})
        # 4001 = session not found (e.g. bridge restarted, in-memory state wiped)
        # Reset and recreate the session, then retry once.
        if isinstance(result, dict) and result.get("error", {}).get("code") == 4001:
            logger.warning(
                "chat_id=%s session %s not found in gateway, recreating", self.chat_id, sid
            )
            with self._lock:
                self.hermes_session_id = None
            sid = self.ensure_session()
            self._call("prompt.submit", {"session_id": sid, "text": text})

    # ------------------------------------------------------------------
    def attach_image_bytes(self, content_base64: str, filename: str = "") -> dict:
        """Attach an image to the session from base64 data (before prompt.submit)."""
        self.last_active = time.monotonic()
        sid = self.ensure_session()
        logger.debug("chat_id=%s attach_image_bytes using session_id=%s", self.chat_id, sid)
        params = {"session_id": sid, "content_base64": content_base64, "filename": filename}
        result = self._call("image.attach_bytes", params)
        logger.debug("chat_id=%s attach_image_bytes result=%r", self.chat_id, result)
        if isinstance(result, dict) and result.get("error", {}).get("code") == 4001:
            logger.warning(
                "chat_id=%s session %s not found in gateway during image attach, recreating",
                self.chat_id, sid,
            )
            with self._lock:
                self.hermes_session_id = None
            sid = self.ensure_session()
            params["session_id"] = sid
            result = self._call("image.attach_bytes", params)
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(result["error"].get("message", "image.attach_bytes failed"))
        return (result.get("result") or {})

    # ------------------------------------------------------------------
    def interrupt(self) -> None:
        """Send session.interrupt to the gateway to stop the current turn."""
        self.last_active = time.monotonic()
        with self._lock:
            sid = self.hermes_session_id
        if not sid:
            return
        result = self._call("session.interrupt", {"session_id": sid})
        if isinstance(result, dict) and "error" in result:
            code = result["error"].get("code", 0)
            msg = result["error"].get("message", "session.interrupt failed")
            logger.warning("chat_id=%s session.interrupt error %s: %s", self.chat_id, code, msg)
        else:
            logger.info("chat_id=%s session.interrupt sent", self.chat_id)

    # ------------------------------------------------------------------
    def get_pending_gate(self) -> Optional[dict]:
        with self._lock:
            return self._pending_gate

    def set_pending_gate(self, gate: Optional[dict]) -> None:
        with self._lock:
            self._pending_gate = gate

    # ------------------------------------------------------------------
    def respond_gate(self, gate_kind: str, gate_id: str, value: str) -> None:
        """Resolve an approval / clarify / sudo / secret gate."""
        self.last_active = time.monotonic()
        sid = self.hermes_session_id or ""
        params: dict = {"session_id": sid, "request_id": gate_id}

        if gate_kind == "approval":
            params["choice"] = value
            self._call("approval.respond", params)
        elif gate_kind == "clarify":
            params["answer"] = value
            self._call("clarify.respond", params)
        elif gate_kind == "sudo":
            params["password"] = value
            self._call("sudo.respond", params)
        elif gate_kind == "secret":
            params["value"] = value
            self._call("secret.respond", params)
        else:
            raise ValueError(f"Unknown gate kind: {gate_kind!r}")

    # ------------------------------------------------------------------
    def close(self) -> None:
        sid = self.hermes_session_id
        if sid:
            try:
                self._call("session.close", {"session_id": sid})
            except Exception:
                pass


# ---------------------------------------------------------------------------
# SessionManager — owns all live GatewaySession objects
# ---------------------------------------------------------------------------

class GatewaySessionManager:
    """Owns all live GatewaySession objects, keyed by chat_id."""

    def __init__(self, session_idle_timeout: float = 600.0) -> None:
        self._sessions: dict[str, GatewaySession] = {}
        self._lock = threading.Lock()
        self.session_idle_timeout = session_idle_timeout
        self._reaper_started = False

    def get_or_create(
        self,
        chat_id: str,
        hermes_session_id: Optional[str],
        loop: asyncio.AbstractEventLoop,
    ) -> GatewaySession:
        with self._lock:
            session = self._sessions.get(chat_id)
            if session is None:
                session = GatewaySession(chat_id, hermes_session_id, loop)
                self._sessions[chat_id] = session
                logger.debug("chat_id=%s new GatewaySession", chat_id)
            self._ensure_reaper()
            return session

    def get(self, chat_id: str) -> Optional[GatewaySession]:
        with self._lock:
            return self._sessions.get(chat_id)

    def remove(self, chat_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(chat_id, None)
        if session:
            session.close()

    def _ensure_reaper(self) -> None:
        if self._reaper_started:
            return
        self._reaper_started = True
        threading.Thread(target=self._reap_loop, daemon=True, name="gw-session-reaper").start()

    def _reap_loop(self) -> None:
        while True:
            time.sleep(60)
            now = time.monotonic()
            with self._lock:
                stale = [
                    (cid, s)
                    for cid, s in self._sessions.items()
                    if now - s.last_active > self.session_idle_timeout
                ]
                for cid, _ in stale:
                    self._sessions.pop(cid, None)
            for _, s in stale:
                try:
                    s.close()
                except Exception:
                    pass
