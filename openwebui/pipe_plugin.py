"""
title: Hermes Chat
author: hermes-chat
version: 0.2.0
description: Routes chat turns through Hermes Chat, translating Hermes'
    interactive TUI decision gates (questionary confirm/select prompts)
    into clickable buttons inside the chat message.
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
            description="Base URL of the Hermes Chat FastAPI service.",
        )
        REQUEST_TIMEOUT: int = Field(
            default=600,
            description="Seconds to wait for the bridge's SSE stream before giving up.",
        )
        RICH_UI_BUTTONS: bool = Field(
            default=True,
            description=(
                "Render gate choices as clickable HTML buttons in an embedded iframe. "
                "Disable this if your Open WebUI version or settings block rich UI embeds."
            ),  
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
        __event_emitter__=None,
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
            __event_emitter__,
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
        self,
        url: str,
        payload: dict,
        chat_id: str,
        event_emitter=None,
    ) -> Generator[str, None, None]:
        try:
            response = requests.post(
                url, json=payload, stream=True, timeout=self.valves.REQUEST_TIMEOUT
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            yield f"**hermes-chat error:** {exc}"
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
                if self.valves.RICH_UI_BUTTONS and event_emitter is not None:
                    self._emit_button_embed(event_emitter, chat_id, event)
                yield self._render_gate_text(chat_id, event)
                return
            elif event_type == "process_exit":
                return

    def _emit_button_embed(self, event_emitter, chat_id: str, event: dict) -> None:
        """Emit an HTML iframe with one button per option."""
        try:
            html = self._build_button_html(chat_id, event)
            event_emitter(
                {
                    "type": "embeds",
                    "data": {
                        "embeds": [html],
                        "replace": False,
                    },
                }
            )
        except Exception:
            # If the embed fails, the text fallback still renders.
            pass

    def _build_button_html(self, chat_id: str, event: dict) -> str:
        gate_id = event["gate_id"]
        prompt = event.get("prompt", "Hermes is waiting for your input.")
        options = event.get("options", ["Yes", "No"])

        # Build a compact, dependency-free HTML page with buttons.
        # Each button posts a message back to the Open WebUI parent asking it
        # to fill the chat input with the chosen option label and submit.
        buttons = "\n".join(
            f'<button class="hb-btn" onclick="choose({json.dumps(opt)})">{i + 1}. {opt}</button>'
            for i, opt in enumerate(options)
        )

        html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ margin: 0; padding: 0.5rem; font-family: system-ui, sans-serif; }}
  .hb-prompt {{ margin-bottom: 0.75rem; font-weight: 600; }}
  .hb-btn {{ margin: 0.25rem 0.25rem 0.25rem 0; padding: 0.5rem 0.75rem; cursor: pointer;
            border: 1px solid #888; border-radius: 0.375rem; background: #f3f4f6; }}
  .hb-btn:hover {{ background: #e5e7eb; }}
  .hb-note {{ margin-top: 0.75rem; font-size: 0.75rem; color: #666; }}
</style>
</head>
<body>
<div class="hb-prompt">{prompt}</div>
{buttons}
<div class="hb-note">Clicking a button submits your choice. If a confirmation dialog appears, enable <strong>allowSameOrigin</strong> for this iframe in Open WebUI settings.</div>
<script>
  function choose(option) {{
    if (window.parent && window.parent.postMessage) {{
      window.parent.postMessage({{ type: "input:prompt:submit", text: option }}, "*");
    }}
  }}
</script>
</body>
</html>"""
        return html

    def _render_gate_text(self, chat_id: str, event: dict) -> str:
        gate_id = event["gate_id"]
        prompt = event.get("prompt", "Hermes is waiting for your input.")
        options = event.get("options", ["Yes", "No"])

        numbered = "\n".join(f"{i + 1}. {opt}" for i, opt in enumerate(options))

        # Hidden metadata for the companion action plugin.
        metadata_comment = (
            f"<!-- hermes-gate chat_id={chat_id} gate_id={gate_id} "
            f"options={json.dumps(options)} -->"
        )

        return (
            f"\n\n---\n"
            f"🚦 **Hermes needs your input:** {prompt}\n\n"
            f"{numbered}\n\n"
            f"_Reply with the number (1-{len(options)}) or the option name._\n"
            f"---\n{metadata_comment}\n"
        )
