---
name: hermes-webui-reference
description: >-
  Chat-scoped patterns from nesquena/hermes-webui for hermes-chat. Use when
  improving chat UX (streaming, tools, gates, sessions, markdown, voice,
  themes). Ignore admin/workspace/IDE surfaces. Prefer assistant-ui primitives.
---

# hermes-webui → hermes-chat reference

Source: https://github.com/nesquena/hermes-webui (default branch: **`master`**)

Use this when improving **hermes-chat** chat UX. hermes-webui is a full Hermes control plane; hermes-chat is a **chat plugin**. Steal interaction patterns, not product scope.

## Product scope (hard filter)

**IN SCOPE for hermes-chat:** chat UI + light features — markdown edit/render, attachments, themes, session list, tool steps, gates, voice.

**OUT OF SCOPE (do not port):** Hermes settings / Control Center, provider/profile admin, interactive coding-task UI, workspace file browser / Spaces, cron Tasks, Skills editor, Memory.md panels, onboarding wizard, CLI-session bridge as a product surface, public share links, heavy admin.

---

## Architecture overview

| | hermes-webui | hermes-chat (this repo) |
|---|---|---|
| Role | Standalone web app; near CLI parity | Hermes **plugin** |
| Backend | Python **stdlib** `ThreadingHTTPServer` (`server.py` → `api/routes.py`) | FastAPI + in-process `tui_gateway` |
| Frontend | Vanilla JS, **no bundler** (`static/*.js`) | React + Vite + **@assistant-ui/react** (`webui/src/`) |
| Chat run model | `POST /api/chat/start` + `GET /api/chat/stream?stream_id=` | `POST /v1/chat/runs` + `GET /v1/chat/runs/{id}/events?after=` |

Rough file map (patterns only — do not copy structure):

| hermes-webui | hermes-chat |
|---|---|
| `api/streaming.py` | run/SSE in plugin + `webui/src/api.ts` |
| `api/routes.py` | `hermes_chat/main.py` |
| `static/messages.js` | `App.tsx` `handleStreamEvent` + `thread.tsx` |
| `static/ui.js` | markdown/tool/context under `webui/src/components/` |
| `static/sessions.js` | `App.tsx` `ChatSidebar` |
| `static/commands.js` | slash menu in `thread.tsx` |
| `static/panels.js` / `workspace.js` | **ignore** |

Docs worth skimming upstream (chat-relevant only): `ARCHITECTURE.md` §SSE, `docs/rfcs/live-to-final-assistant-replies.md`, `docs/rfcs/session-sse-contract-v1.md`, `THEMES.md`. Skip workspace/onboarding/cron docs.

---

## Already shipped in hermes-chat (do not re-steal)

| Pattern | Where we already have it |
|---|---|
| SSE network retry (backoff) | `api.ts` `streamEvents` |
| Cursor resume (`after` / `seq`) | `streamChatRun` + run recovery in `App.tsx` |
| rAF-batched token flush | `App.tsx` `rafPendingRef` |
| Slash autocomplete | `thread.tsx` `SLASH_COMMANDS` |
| Message timestamps | `thread.tsx` |
| Code block copy | `markdown-text.tsx` `CodeHeader` |
| Context ring / usage | `context-display.tsx` |
| Collapsible tool steps | `tool-fallback.tsx` |
| Inline gates | `thread.tsx` `InlineGate` |
| Pin + date-grouped sessions | `ChatSidebar` |
| Attachments | `attachment.tsx` |
| Light/dark theme | `useTheme.ts` |
| Voice (server STT/TTS) | voice hooks + Edge TTS |

Steal **improvements** to these, not first implementations.

---

## Steal list (chat-relevant only)

### High value gaps

1. **Reasoning / thinking blocks** — map to assistant-ui `reasoning` parts (`thread.tsx` today is mostly text + tool-call).
2. **Session search by message content** — sidebar is title-only today.
3. **Archive** (hide without delete).
4. **Export / import transcript** (MD / JSON) from conversation menu — not a Settings clone.
5. **Selected-text → quoted reply** into composer.
6. **Mermaid** via rehype on `MarkdownText`.
7. **Theme skins** (`data-skin` + CSS vars) — not their Appearance Control Center.
8. **Busy-composer queue / clearer inflight UX**.
9. **Web Speech API fallback** when Whisper path unavailable.
10. **Visible reconnect/sync chip** (we retry quietly).

### Medium / polish

- Duplicate session, light `#tags` — only if session list stays light.
- Retry last assistant / edit past user — wire carefully to run API.
- System theme (`prefers-color-scheme`) in `useTheme.ts`.
- Slash: `/theme` ok; **never** `/workspace`.

---

## DO NOT STEAL (explicit)

- Right-panel **workspace browser**
- **Hermes Control Center** / Settings mega-UI
- **Profiles** create/switch/delete UI
- **Tasks / cron**, **Skills** editor, **Memory** panels, **Spaces**
- **Onboarding wizard** / provider setup
- **CLI session bridge** as a first-class product feature
- **Public share links**
- Full **activity_scene / worklog** complexity — prefer simple tool + reasoning parts
- Vanilla DOM renderers instead of assistant-ui primitives

---

## Key upstream files

**Backend:** `server.py`, `api/routes.py`, `api/streaming.py`, `api/models.py` — skip `api/workspace.py`

**Frontend:** `static/messages.js`, `ui.js`, `sessions.js`, `commands.js`, `boot.js` — skip `panels.js`, `workspace.js`

---

## Event mapping

Upstream `token` / `reasoning` / `tool*` / `approval`/`clarify` / `done` ↔ our `text` / **(missing reasoning)** / `tool_*` / `gate_interrupt` / `turn_complete`.

When in doubt: if removing the feature would still leave a great chat app, it’s in scope; if it only exists to administer Hermes, leave it to hermes-webui / the Hermes dashboard.
