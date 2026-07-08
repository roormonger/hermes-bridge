"""
title: Hermes Bridge
author: hermes-bridge
version: 0.1.0
description: Routes chat turns through hermes-bridge, translating Hermes'
    interactive TUI decision gates (questionary confirm/select prompts)
    into clickable Markdown "buttons" backed by the companion Action plugin.
requirements: requests
"""

from __future__ import annotations

import json
from typing import Generator, Optional

import requests
from pydantic import BaseModel, Field


class Pipe:
    class Valves(BaseModel):
        BRIDGE_URL: str = Field(
            default="http://localhost:6969",
            description="Base URL of the hermes-bridge FastAPI service.",
        )
        REQUEST_TIMEOUT: int = Field(
            default=600,
            description="Seconds to wait for the bridge's SSE stream before giving up.",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()
        self.id = "hermes_bridge"
        self.name = "Hermes"

    def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
    ) -> Generator[str, None, None]:
        chat_id = self._extract_chat_id(body, __metadata__)
        message = self._extract_last_user_message(body)

        if not chat_id:
            yield "**hermes-bridge error:** could not determine `chat_id` from the request."
            return

        yield from self._stream_from(
            f"{self.valves.BRIDGE_URL}/v1/chat",
            {"chat_id": chat_id, "message": message},
            chat_id,
        )

    # -- helpers -------------------------------------------------------------

    def _extract_chat_id(self, body: dict, metadata: Optional[dict]) -> Optional[str]:
        if metadata and metadata.get("chat_id"):
            return metadata["chat_id"]
        return body.get("chat_id") or body.get("id")

    def _extract_last_user_message(self, body: dict) -> str:
        messages = body.get("messages", [])
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, list):
                    # Multimodal content parts; keep text parts only.
                    return " ".join(
                        part.get("text", "") for part in content if part.get("type") == "text"
                    )
                return content
        return ""

    def _stream_from(
        self, url: str, payload: dict, chat_id: str
    ) -> Generator[str, None, None]:
        try:
            response = requests.post(
                url, json=payload, stream=True, timeout=self.valves.REQUEST_TIMEOUT
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            yield f"**hermes-bridge error:** {exc}"
            return

        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            line = raw_line[len("data:"):].strip() if raw_line.startswith("data:") else raw_line
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")
            if event_type == "text":
                yield event.get("text", "")
            elif event_type == "gate_interrupt":
                yield self._render_gate(chat_id, event)
                return
            elif event_type == "process_exit":
                return

    def _render_gate(self, chat_id: str, event: dict) -> str:
        gate_id = event["gate_id"]
        prompt = event.get("prompt", "Hermes is waiting for your input.")
        options = event.get("options", ["Yes", "No"])

        buttons = " &nbsp; ".join(
            f"[✔️ {opt}](button://action=hermes_resolve_gate"
            f"&chat_id={chat_id}&gate_id={gate_id}&choice={opt})"
            for opt in options
        )

        # NOTE: `button://` links are not a native Open WebUI markdown
        # feature. They are rendered here as a readable affordance and are
        # primarily intended to be intercepted by custom front-end tooling.
        # The companion `action_plugin.py` is the *reliable* path: register
        # it as an Open WebUI Action (one instance per choice, or a single
        # generic one) and it will resolve the most recent gate embedded in
        # this message via the hidden metadata comment below.
        metadata_comment = (
            f"<!-- hermes-gate chat_id={chat_id} gate_id={gate_id} "
            f"options={json.dumps(options)} -->"
        )

        return (
            f"\n\n---\n"
            f"🚦 **Hermes needs your input:** {prompt}\n\n"
            f"{buttons}\n\n"
            f"_Or just reply with one of: {', '.join(options)}_\n"
            f"---\n{metadata_comment}\n"
        )
