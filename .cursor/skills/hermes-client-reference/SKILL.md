---
name: hermes-client-reference
description: >-
  Chat-scoped patterns from lotsoftick/hermes_client for hermes-chat. Use when
  improving streaming resilience, tool/thinking UX, attachments, session sync,
  themes, or PWA. Do not port profiles/admin/xterm or their auto-approve gates.
---

# hermes_client → hermes-chat reference

Source: https://github.com/lotsoftick/hermes_client

hermes_client is a fuller Hermes control plane (profiles, cron, skills, plugins, xterm setup). Steal **chat UX / resilience** only.

## Product scope (hard filter)

**IN SCOPE for hermes-chat:** chat, streaming, tool steps, **user-visible decision gates**, markdown, attachments, themes, session list, optional voice.

**OUT OF SCOPE:** Hermes settings, coding IDE, workspace browser, cron/skills/plugins admin, multi-profile agent management, xterm config wizards.

## Architecture (their stack)

- Express + TypeORM + SQLite API (`:18889`) + Vite/React/MUI client (`:18888`)
- **Not assistant-ui** — custom `MessageBubble` / `ToolStepsBlock` / `ThinkingBlock`
- Preferred agent I/O: `python -m tui_gateway.entry` subprocess; fallback: spawn `hermes chat -Q -q "…"`
- Chat: `POST /api/message/chat` → SSE; ends with `data: [DONE]`; **15s `: ping` heartbeats**
- Sessions: UI conversation ↔ Hermes session key; disk under `~/.hermes` mirrored to SQLite; poll sync for REPL↔web
- **Gates: auto-approves `approval.request` with `choice: 'once'`** — fail-open like non-TTY CLI

## Steal list (with why)

| Pattern | Why |
|--------|-----|
| SSE keepalives (`: ping` + `X-Accel-Buffering: no`) | Long tool turns go silent; proxies kill idle streams |
| Persist assistant turn before closing SSE; signal saved then done | Avoids refetch race losing the bubble |
| Live tool activity + collapsible post-turn tool cards | Fills silent gap; history stays readable |
| Collapsible thinking from `thinking` / reasoning deltas | Maps to gateway `reasoning.delta` |
| Attachment pipeline (store under chat id; images first-class) | Complements assistant-ui attachments |
| Safe image serving (allowlisted roots + `realpath`) | Agent images without arbitrary file read |
| Light session sync / poll (Hermes as SoT) | REPL continuity without a profiles product |
| Active-chat guard while a turn is in flight | Prevents duplicate conversation on discovery |
| Host-derived public URL for media | Fixes LAN / Tailscale asset links |
| Theme presets + optional PWA banner | Light UX, in scope |
| Cursor-paginated history | Scales long threads |
| Client abort → interrupt only while turn in flight | Matches cancel; avoid sticky interrupt |

## DO NOT STEAL

| Surface | Reason |
|--------|--------|
| Multi-agent / Hermes profiles UI | Out of scope |
| xterm setup / PTY for `hermes -p model` | Settings IDE |
| Cron / skills / plugins / spend admin | Admin UIs |
| **Auto-approve `approval.request`** | Conflicts with hermes-chat’s gate UI — steal wiring, not policy |
| CLI-spawn as primary architecture | We use in-process `tui_gateway` |
| Express/MUI/Redux rewrite | Keep FastAPI + assistant-ui |
| Deploy-to-`~/.hermes_client` + OS auto-start | Different packaging |
| Arbitrary workspace file browser | Out of scope |

## File / API pointers

**API:** `api/src/routes/message/` (chat SSE, poll, uploads), `api/src/services/hermes/tuiGateway.ts`, `chat.ts`, `sync.ts`, `uploads.ts`, `images.ts`, `api/src/middlewares/auth.ts`

**Client:** `client/src/widgets/chat/`, `features/message/send/`, `entities/message/ui/ToolStepsBlock.tsx`, `ThinkingBlock.tsx`, `StreamingToolActivity.tsx`, `shared/ui/MarkdownContent.tsx`, `features/theme/`, `features/pwa/`

**SSE shapes:** `response.output_text.delta`, `response.thinking.delta`, `tool.start` / `tool.complete`, `session.update`, `message.saved`, `response.error`, then `[DONE]`.

## vs hermes-chat

| | hermes_client | hermes-chat |
|--|---------------|-------------|
| Runtime | Separate Node app | Hermes plugin + FastAPI |
| Agent I/O | Gateway subprocess or CLI | **In-process** `tui_gateway` |
| UI | Custom MUI | **assistant-ui** |
| Runs | Implicit per POST stream | Server-owned runs + seq replay |
| Gates | **Auto-approve** | **`gate_interrupt` + resolve** — keep this |

**Bottom line:** chat UX / resilience / attachments / sync reference. Keep our architecture and user-visible gates.
