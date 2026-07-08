"""
PTY-backed subprocess management for Hermes chat sessions.

Each Open WebUI chat is bound to a persistent Hermes session id (see
`database.py`). When a message arrives we lazily spawn a `hermes chat -r
<session_id>` subprocess inside a pseudo-terminal so that Hermes believes
it is talking to a real interactive terminal (this matters because Hermes'
classic CLI uses `questionary`/`prompt_toolkit` prompts for tool-approval
and other decision gates, which behave differently -- or not at all -- when
stdin/stdout are plain pipes).

Because idle chats should not hold resources, subprocesses are torn down
after a period of inactivity. The next message for that chat simply
respawns `hermes chat -r <session_id>`, which resumes the native Hermes
session from wherever it left off.
"""

from __future__ import annotations

import asyncio
import os
import re
import select
import shlex
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

try:
    import pty
except ImportError:  # pragma: no cover - PTY is POSIX-only.
    pty = None  # type: ignore


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

from .config import BridgeConfig, effective_hermes_bin

# Defaults are now supplied by BridgeConfig. These module-level fallbacks only
# matter if the module is imported without config (e.g. ad-hoc testing).
HERMES_BIN = os.environ.get("HERMES_BIN", "hermes")
GATE_IDLE_THRESHOLD = float(os.environ.get("HERMES_GATE_IDLE_THRESHOLD", "0.35"))
SESSION_IDLE_TIMEOUT = float(os.environ.get("HERMES_SESSION_IDLE_TIMEOUT", "600"))

READ_CHUNK_SIZE = 4096

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")
CURSOR_MARKER_RE = re.compile(r"[\u276f\u25b6>]")  # ❯, ▶, >


# --------------------------------------------------------------------------- #
# Gate detection
# --------------------------------------------------------------------------- #

@dataclass
class GateInterrupt:
    gate_id: str
    prompt: str
    options: list[str]
    kind: str  # "confirm" | "select"


class GateDetector:
    """Best-effort regex/heuristic detector for questionary-style prompts.

    This intentionally favors precision over cleverness: Hermes' classic CLI
    renders `questionary` prompts as plain-ish text once ANSI codes are
    stripped, generally in one of two shapes:

        ? Proceed with this action? (Y/n)
        ? Choose a model:
          gpt-4o
        ❯ gpt-4o-mini
          claude-3-5-sonnet

    Confirm-style prompts are single-line and end without a trailing
    newline (the cursor sits right after them waiting for a keypress).
    Select-style prompts span multiple lines, with a cursor marker
    (`❯`/`>`) in front of the currently highlighted option.
    """

    CONFIRM_RE = re.compile(
        r"\?\s*(?P<prompt>.+?)\s*\(Y/n\)\s*$", re.IGNORECASE
    )
    CONFIRM_RE_ALT = re.compile(
        r"\?\s*(?P<prompt>.+?)\s*\[y/n\]\s*:?\s*$", re.IGNORECASE
    )
    SELECT_HEADER_RE = re.compile(r"^\?\s*(?P<prompt>.+?)\s*:?\s*$")

    def detect(self, tail: str) -> Optional[tuple[str, str, list[str]]]:
        """Inspect the trailing (unflushed) output buffer for a gate.

        Returns (kind, prompt, options) or None.
        """
        stripped = tail.rstrip(" \t")
        if not stripped:
            return None

        # --- Confirm-style: "? <question> (Y/n)" ---------------------------
        last_line = stripped.splitlines()[-1] if stripped.splitlines() else stripped
        m = self.CONFIRM_RE.search(last_line) or self.CONFIRM_RE_ALT.search(last_line)
        if m:
            return "confirm", m.group("prompt").strip(), ["Yes", "No"]

        # --- Select-style: header line + cursor-marked option lines --------
        # Scan from the end of the buffer backwards to find the last "? ..." header
        # followed by option lines with a cursor marker. This tolerates leading
        # log/output noise above the prompt.
        lines = [l for l in stripped.splitlines()]
        for idx in range(len(lines) - 1, -1, -1):
            line = lines[idx].strip()
            header_match = self.SELECT_HEADER_RE.match(line)
            if not header_match:
                continue
            # Collect following non-empty lines as option candidates.
            option_lines = []
            for j in range(idx + 1, len(lines)):
                opt_line = lines[j].strip()
                if not opt_line:
                    break
                option_lines.append(opt_line)
            has_cursor = any(CURSOR_MARKER_RE.match(l) for l in option_lines)
            if has_cursor:
                options = [
                    CURSOR_MARKER_RE.sub("", l, count=1).strip()
                    for l in option_lines
                ]
                options = [o for o in options if o]
                if options:
                    return "select", header_match.group("prompt").strip(), options

        return None


# --------------------------------------------------------------------------- #
# PTY-backed session
# --------------------------------------------------------------------------- #

class HermesPtySession:
    """Owns a single PTY + `hermes chat -r <session_id>` subprocess."""

    def __init__(
        self,
        chat_id: str,
        hermes_session_id: str,
        loop: asyncio.AbstractEventLoop,
        config: BridgeConfig | None = None,
    ) -> None:
        if pty is None:
            raise RuntimeError(
                "The 'pty' module is POSIX-only. hermes-bridge's bridge "
                "service must run on Linux/macOS (or WSL), not native Windows."
            )

        self.chat_id = chat_id
        self.hermes_session_id = hermes_session_id
        self.config = config
        self.hermes_bin = effective_hermes_bin(config) if config else HERMES_BIN
        self.gate_idle_threshold = config.gate_idle_threshold if config else GATE_IDLE_THRESHOLD
        self.loop = loop

        self.master_fd: Optional[int] = None
        self.proc: Optional[subprocess.Popen] = None
        self.reader_thread: Optional[threading.Thread] = None
        self.queue: asyncio.Queue = asyncio.Queue()

        self._stop_event = threading.Event()
        self._line_buffer = ""
        self._gate_detector = GateDetector()
        self._pending_gate: Optional[GateInterrupt] = None
        self._gate_lock = threading.Lock()
        self.last_active = time.monotonic()

    # -- lifecycle ---------------------------------------------------------

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        if self.is_running():
            return

        master_fd, slave_fd = pty.openpty()
        args = [self.hermes_bin, "chat", "-r", self.hermes_session_id]

        try:
            self.proc = subprocess.Popen(
                args,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                preexec_fn=os.setsid,
                close_fds=True,
            )
        except FileNotFoundError as exc:
            os.close(slave_fd)
            os.close(master_fd)
            raise RuntimeError(
                f"Hermes binary not found: {self.hermes_bin!r}. "
                "Install Hermes or set the HERMES_BIN environment variable."
            ) from exc
        os.close(slave_fd)
        self.master_fd = master_fd
        self._stop_event.clear()
        self._line_buffer = ""
        self._pending_gate = None

        self.reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name=f"hermes-pty-{self.chat_id}"
        )
        self.reader_thread.start()
        self.last_active = time.monotonic()

    def stop(self) -> None:
        self._stop_event.set()
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None

    # -- reader thread -------------------------------------------------------

    def _reader_loop(self) -> None:
        last_data_time = time.monotonic()
        gate_checked_for_current_buffer = False

        while not self._stop_event.is_set():
            if self.master_fd is None:
                break
            try:
                ready, _, _ = select.select([self.master_fd], [], [], 0.1)
            except (OSError, ValueError):
                break

            if ready:
                try:
                    chunk = os.read(self.master_fd, READ_CHUNK_SIZE)
                except OSError:
                    break
                if not chunk:
                    break

                text = ANSI_ESCAPE_RE.sub("", chunk.decode("utf-8", errors="ignore"))
                self._line_buffer += text
                last_data_time = time.monotonic()
                gate_checked_for_current_buffer = False
                self._flush_complete_lines()
            else:
                idle_for = time.monotonic() - last_data_time
                if (
                    self._line_buffer
                    and not gate_checked_for_current_buffer
                    and idle_for >= self.gate_idle_threshold
                ):
                    gate_checked_for_current_buffer = True
                    self._check_for_gate()

            if self.proc is not None and self.proc.poll() is not None:
                # Process exited; drain anything left and stop.
                self._flush_complete_lines(force=True)
                self._emit_event({"type": "process_exit", "code": self.proc.returncode})
                break

        self._stop_event.set()

    def _flush_complete_lines(self, force: bool = False) -> None:
        """Emit any fully-formed lines in the buffer as text tokens.

        The last (possibly partial) line is intentionally withheld so it can
        be evaluated for gate patterns before being shown to the user.
        """
        if self._pending_gate is not None:
            return  # hold everything while a gate is unresolved

        if "\n" not in self._line_buffer:
            if force and self._line_buffer:
                self._emit_event({"type": "text", "text": self._line_buffer})
                self._line_buffer = ""
            return

        *complete, remainder = self._line_buffer.split("\n")
        if complete:
            text = "\n".join(complete) + "\n"
            self._emit_event({"type": "text", "text": text})
        self._line_buffer = remainder
        if force and self._line_buffer:
            self._emit_event({"type": "text", "text": self._line_buffer})
            self._line_buffer = ""

    def _check_for_gate(self) -> None:
        if self._pending_gate is not None or not self._line_buffer.strip():
            return

        result = self._gate_detector.detect(self._line_buffer)
        if result is None:
            # Not a gate -- release it to the normal text stream instead of
            # holding it forever.
            self._flush_complete_lines(force=True)
            return

        kind, prompt, options = result
        gate = GateInterrupt(gate_id=uuid.uuid4().hex, prompt=prompt, options=options, kind=kind)
        with self._gate_lock:
            self._pending_gate = gate
        self._line_buffer = ""
        self._emit_event(
            {
                "type": "gate_interrupt",
                "gate_id": gate.gate_id,
                "prompt": gate.prompt,
                "options": gate.options,
            }
        )

    def _emit_event(self, event: dict) -> None:
        self.loop.call_soon_threadsafe(self.queue.put_nowait, event)

    # -- input / gate resolution --------------------------------------------

    def get_pending_gate(self) -> Optional[GateInterrupt]:
        with self._gate_lock:
            return self._pending_gate

    def send_text(self, text: str) -> None:
        """Write a raw user chat message into the subprocess stdin."""
        self.last_active = time.monotonic()
        if self.master_fd is None or not self.is_running():
            raise RuntimeError("Session is not running")
        os.write(self.master_fd, (text + "\n").encode("utf-8"))

    def resolve_gate(self, gate_id: str, choice: str) -> None:
        with self._gate_lock:
            gate = self._pending_gate
            if gate is None or gate.gate_id != gate_id:
                raise ValueError(f"No pending gate with id {gate_id!r} for this chat")
            self._pending_gate = None

        self.last_active = time.monotonic()
        if self.master_fd is None:
            raise RuntimeError("Session is not running")

        if gate.kind == "confirm":
            key = "y" if choice.strip().lower() in ("yes", "y", "true", "approve") else "n"
            os.write(self.master_fd, (key + "\n").encode("utf-8"))
            return

        # select-style: move the cursor to the matching option, then Enter.
        try:
            target_index = next(
                i for i, opt in enumerate(gate.options)
                if opt.strip().lower() == choice.strip().lower()
            )
        except StopIteration:
            target_index = 0

        # questionary select prompts start with the cursor on option 0.
        down_arrow = b"\x1b[B"
        for _ in range(target_index):
            os.write(self.master_fd, down_arrow)
            time.sleep(0.02)
        os.write(self.master_fd, b"\r")


# --------------------------------------------------------------------------- #
# Session manager
# --------------------------------------------------------------------------- #

class SessionManager:
    """Owns all live `HermesPtySession`s, keyed by Open WebUI chat_id."""

    def __init__(self, config: BridgeConfig | None = None) -> None:
        self._sessions: dict[str, HermesPtySession] = {}
        self._lock = threading.Lock()
        self._reaper_started = False
        self.config = config
        self.session_idle_timeout = config.session_idle_timeout if config else SESSION_IDLE_TIMEOUT

    def get_or_start(
        self, chat_id: str, hermes_session_id: str, loop: asyncio.AbstractEventLoop
    ) -> HermesPtySession:
        with self._lock:
            session = self._sessions.get(chat_id)
            if session is None or not session.is_running():
                session = HermesPtySession(chat_id, hermes_session_id, loop, self.config)
                session.start()
                self._sessions[chat_id] = session
            self._ensure_reaper()
            return session

    def get(self, chat_id: str) -> Optional[HermesPtySession]:
        with self._lock:
            return self._sessions.get(chat_id)

    def stop(self, chat_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(chat_id, None)
        if session:
            session.stop()

    def _ensure_reaper(self) -> None:
        if self._reaper_started:
            return
        self._reaper_started = True
        threading.Thread(target=self._reap_idle_loop, daemon=True, name="hermes-session-reaper").start()

    def _reap_idle_loop(self) -> None:
        while True:
            time.sleep(30)
            now = time.monotonic()
            with self._lock:
                stale = [
                    (chat_id, session)
                    for chat_id, session in self._sessions.items()
                    if now - session.last_active > self.session_idle_timeout
                ]
                for chat_id, _ in stale:
                    self._sessions.pop(chat_id, None)
            for _, session in stale:
                session.stop()
