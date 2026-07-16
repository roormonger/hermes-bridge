from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncGenerator, Callable, Optional

from .chat_history import ChatHistoryStore
from .gateway_session import _translate_event

logger = logging.getLogger("hermes_chat.runs")

_TERMINAL_STATUSES = frozenset({"complete", "error", "cancelled"})


@dataclass
class ChatRun:
    run_id: str
    chat_id: str
    user_id: Optional[str]
    assistant_message_id: int
    session: object
    status: str = "starting"
    seq: int = 0
    content: str = ""
    reasoning: str = ""
    tool_steps: list[dict] = field(default_factory=list)
    pending_gate: Optional[dict] = None
    events: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    task: Optional[asyncio.Task] = None

    def snapshot(self) -> dict:
        return {
            "run_id": self.run_id,
            "chat_id": self.chat_id,
            "assistant_message_id": self.assistant_message_id,
            "status": self.status,
            "last_seq": self.seq,
            "pending_gate": self.pending_gate,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ChatRunManager:
    def __init__(
        self,
        history: ChatHistoryStore,
        session_store,
        *,
        max_events: int = 4096,
        completed_ttl: float = 900.0,
    ) -> None:
        self.history = history
        self.session_store = session_store
        self.max_events = max_events
        self.completed_ttl = completed_ttl
        self._runs: dict[str, ChatRun] = {}
        self._active_by_chat: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        *,
        chat_id: str,
        user_id: Optional[str],
        assistant_message_id: int,
        message: str,
        session,
        submit: Callable[[str], None],
    ) -> ChatRun:
        assistant = self.history.get_message(assistant_message_id, user_id)
        if assistant is None or assistant["chat_id"] != chat_id or assistant["role"] != "assistant":
            raise ValueError("assistant_message_id must identify an assistant message in this chat")

        async with self._lock:
            self._reap_completed()
            existing_id = self._active_by_chat.get(chat_id)
            if existing_id is not None:
                existing = self._runs.get(existing_id)
                if existing is not None and existing.status not in _TERMINAL_STATUSES:
                    raise RuntimeError(f"Chat {chat_id} already has an active run")
                self._active_by_chat.pop(chat_id, None)

            run = ChatRun(
                run_id=uuid.uuid4().hex,
                chat_id=chat_id,
                user_id=user_id,
                assistant_message_id=assistant_message_id,
                session=session,
                content=assistant["content"],
                reasoning=assistant.get("reasoning") or "",
                tool_steps=assistant["tool_steps"],
                seq=int(assistant.get("stream_seq") or 0),
            )
            self._runs[run.run_id] = run
            self._active_by_chat[chat_id] = run.run_id

        try:
            await asyncio.get_running_loop().run_in_executor(None, submit, message)
        except Exception:
            async with self._lock:
                self._active_by_chat.pop(chat_id, None)
                self._runs.pop(run.run_id, None)
            raise

        run.status = "running"
        run.updated_at = time.time()
        run.task = asyncio.create_task(self._collect(run), name=f"chat-run-{run.run_id}")
        return run

    def get(self, run_id: str, user_id: Optional[str]) -> Optional[ChatRun]:
        run = self._runs.get(run_id)
        if run is None or run.user_id != user_id:
            return None
        return run

    def get_active(self, chat_id: str, user_id: Optional[str]) -> Optional[ChatRun]:
        run_id = self._active_by_chat.get(chat_id)
        if run_id is None:
            return None
        run = self.get(run_id, user_id)
        if run is None or run.status in _TERMINAL_STATUSES:
            return None
        return run

    async def subscribe(
        self, run_id: str, user_id: Optional[str], after: int = 0
    ) -> AsyncGenerator[bytes, None]:
        run = self.get(run_id, user_id)
        if run is None:
            raise KeyError(run_id)

        cursor = max(0, after)
        while True:
            async with run.condition:
                available = [item for item in run.events if item["seq"] > cursor]
                if not available and run.status not in _TERMINAL_STATUSES:
                    await run.condition.wait_for(
                        lambda: any(item["seq"] > cursor for item in run.events)
                        or run.status in _TERMINAL_STATUSES
                    )
                    available = [item for item in run.events if item["seq"] > cursor]

                terminal = run.status in _TERMINAL_STATUSES

            for item in available:
                cursor = item["seq"]
                payload = {**item["event"], "seq": cursor, "run_id": run.run_id}
                yield f"id: {cursor}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")

            if terminal and not any(item["seq"] > cursor for item in run.events):
                return

    async def resolve_gate(
        self,
        *,
        chat_id: str,
        user_id: Optional[str],
        gate_id: str,
        gate_kind: str,
        choice: str,
    ) -> ChatRun:
        run = self.get_active(chat_id, user_id)
        if run is None:
            raise KeyError(chat_id)
        if run.pending_gate is None or run.pending_gate.get("gate_id") != gate_id:
            raise ValueError("Gate is no longer pending")

        await asyncio.get_running_loop().run_in_executor(
            None, run.session.respond_gate, gate_kind, gate_id, choice
        )
        run.session.set_pending_gate(None)
        run.pending_gate = None
        run.status = "running"
        run.updated_at = time.time()
        return run

    async def cancel(self, chat_id: str, user_id: Optional[str]) -> Optional[ChatRun]:
        run = self.get_active(chat_id, user_id)
        if run is None:
            return None
        await asyncio.get_running_loop().run_in_executor(None, run.session.interrupt)
        return run

    async def shutdown(self) -> None:
        tasks = [run.task for run in self._runs.values() if run.task and not run.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _collect(self, run: ChatRun) -> None:
        try:
            while True:
                frame = await run.session.queue.get()
                event = _translate_event(frame)
                if event is None:
                    if "error" not in frame:
                        continue
                    event = {
                        "type": "error",
                        "message": frame["error"].get("message", "RPC error"),
                    }

                event_type = event.get("type", "")
                if event_type == "gate_interrupt":
                    run.pending_gate = event
                    run.session.set_pending_gate(event)
                    run.status = "waiting_for_gate"
                elif event_type == "turn_complete":
                    run.status = "complete"
                elif event_type == "error":
                    run.status = "error"

                await self._record(run, event)
                if event_type == "turn_complete":
                    if run.session.hermes_session_id:
                        self.session_store.set_hermes_session_id(
                            run.chat_id, run.session.hermes_session_id
                        )
                    break
                if event_type == "error":
                    break
        except asyncio.CancelledError:
            run.status = "cancelled"
            raise
        except Exception as exc:
            logger.exception("chat run %s collector failed", run.run_id)
            await self._record(run, {"type": "error", "message": str(exc)})
            run.status = "error"
        finally:
            run.updated_at = time.time()
            if run.status in _TERMINAL_STATUSES:
                async with self._lock:
                    if self._active_by_chat.get(run.chat_id) == run.run_id:
                        self._active_by_chat.pop(run.chat_id, None)
            async with run.condition:
                run.condition.notify_all()

    async def _record(self, run: ChatRun, event: dict) -> None:
        event_type = event.get("type", "")
        if event_type == "text":
            run.content += event.get("text", "")
        elif event_type == "reasoning":
            text = event.get("text", "")
            if event.get("replace"):
                run.reasoning = text
            else:
                run.reasoning += text
        elif event_type == "tool_start":
            source_id = event.get("tool_id") or event.get("name", "")
            run.tool_steps.append(
                {
                    "id": f"{run.assistant_message_id}-tool-{len(run.tool_steps)}-{source_id}",
                    "sourceId": source_id,
                    "name": event.get("name", ""),
                    "context": event.get("context", ""),
                    "status": "running",
                }
            )
        elif event_type == "tool_complete":
            self._complete_tool(run.tool_steps, event)

        run.seq += 1
        run.updated_at = time.time()
        self.history.update_message(
            run.assistant_message_id,
            run.user_id,
            run.content,
            run.tool_steps,
            stream_seq=run.seq,
            reasoning=run.reasoning or None,
        )

        async with run.condition:
            run.events.append({"seq": run.seq, "event": event})
            if len(run.events) > self.max_events:
                del run.events[: len(run.events) - self.max_events]
            run.condition.notify_all()

    @staticmethod
    def _complete_tool(tool_steps: list[dict], event: dict) -> None:
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

    def _reap_completed(self) -> None:
        cutoff = time.time() - self.completed_ttl
        stale = [
            run_id
            for run_id, run in self._runs.items()
            if run.status in _TERMINAL_STATUSES and run.updated_at < cutoff
        ]
        for run_id in stale:
            self._runs.pop(run_id, None)
