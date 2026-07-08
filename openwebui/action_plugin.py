"""
title: Hermes Gate Resolver
author: hermes-bridge
version: 0.1.0
description: Resolves a pending Hermes TUI decision gate by posting to the
    hermes-bridge /v1/gate/resolve endpoint, then drains and appends the
    resumed token stream back into the chat.
requirements: requests

Usage:
    Register this once per fixed choice you want a one-click button for
    (e.g. "Hermes: Approve" with CHOICE=Yes, "Hermes: Deny" with CHOICE=No),
    or leave CHOICE empty and it will fall back to the first option embedded
    in the gate's metadata comment. Open WebUI attaches one button per
    registered Action under the assistant message; clicking it runs
    `action()` below against that message's content, which is why the
    pipe_plugin embeds a hidden `<!-- hermes-gate ... -->` comment with the
    chat_id/gate_id/options for this script to recover.
"""

from __future__ import annotations

import json
import re
from typing import Optional

import requests
from pydantic import BaseModel, Field

GATE_COMMENT_RE = re.compile(
    r"<!--\s*hermes-gate\s+chat_id=(?P<chat_id>\S+)\s+gate_id=(?P<gate_id>\S+)\s+"
    r"options=(?P<options>\[.*?\])\s*-->"
)


class Action:
    class Valves(BaseModel):
        BRIDGE_URL: str = Field(
            default="http://localhost:8000",
            description="Base URL of the hermes-bridge FastAPI service.",
        )
        CHOICE: str = Field(
            default="",
            description=(
                "Fixed choice this action always sends (e.g. 'Yes', 'No'). "
                "Leave blank to use the first option found in the gate."
            ),
        )
        REQUEST_TIMEOUT: int = Field(default=30)

    def __init__(self) -> None:
        self.valves = self.Valves()

    async def action(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__=None,
        __metadata__: Optional[dict] = None,
    ) -> Optional[dict]:
        message_content = self._latest_assistant_content(body)
        match = GATE_COMMENT_RE.search(message_content or "")
        if not match:
            await self._emit_status(
                __event_emitter__, "No pending Hermes gate found in this message.", done=True
            )
            return None

        chat_id = match.group("chat_id")
        gate_id = match.group("gate_id")
        options = json.loads(match.group("options"))
        choice = self.valves.CHOICE.strip() or (options[0] if options else "Yes")

        await self._emit_status(__event_emitter__, f"Resolving Hermes gate with '{choice}'...")

        try:
            resp = requests.post(
                f"{self.valves.BRIDGE_URL}/v1/gate/resolve",
                json={"chat_id": chat_id, "gate_id": gate_id, "choice": choice},
                timeout=self.valves.REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            await self._emit_status(__event_emitter__, f"Failed to resolve gate: {exc}", done=True)
            return None

        await self._emit_status(__event_emitter__, f"Resolved with '{choice}'. Resuming...", done=False)
        await self._drain_and_emit(chat_id, __event_emitter__)
        await self._emit_status(__event_emitter__, "Done.", done=True)
        return None

    # -- helpers -------------------------------------------------------------

    def _latest_assistant_content(self, body: dict) -> str:
        messages = body.get("messages", [])
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                return msg.get("content", "") or ""
        return ""

    async def _emit_status(self, event_emitter, description: str, done: bool = False) -> None:
        if event_emitter is None:
            return
        await event_emitter(
            {"type": "status", "data": {"description": description, "done": done}}
        )

    async def _drain_and_emit(self, chat_id: str, event_emitter) -> None:
        if event_emitter is None:
            return
        try:
            resp = requests.post(
                f"{self.valves.BRIDGE_URL}/v1/chat/drain",
                json={"chat_id": chat_id, "message": ""},
                stream=True,
                timeout=self.valves.REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            await self._emit_status(event_emitter, f"Failed to resume stream: {exc}", done=True)
            return

        for raw_line in resp.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            line = raw_line[len("data:"):].strip() if raw_line.startswith("data:") else raw_line
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")
            if event_type == "text":
                await event_emitter({"type": "message", "data": {"content": event.get("text", "")}})
            elif event_type == "gate_interrupt":
                gate_id = event["gate_id"]
                options = event.get("options", ["Yes", "No"])
                comment = (
                    f"<!-- hermes-gate chat_id={chat_id} gate_id={gate_id} "
                    f"options={json.dumps(options)} -->"
                )
                block = (
                    f"\n\n---\n🚦 **Hermes needs your input:** {event.get('prompt', '')}\n\n"
                    f"_Reply with one of: {', '.join(options)}_\n---\n{comment}\n"
                )
                await event_emitter({"type": "message", "data": {"content": block}})
                return
            elif event_type == "process_exit":
                return
