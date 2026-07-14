---
description: Reference patterns from the hermes-webui project (nesquena/hermes-webui) that are applicable to hermes-layer/hermes-chat
---

# hermes-webui Reference Skill

Source: https://github.com/nesquena/hermes-webui

This skill documents patterns, features, and implementation ideas from the reference
`hermes-webui` project that are directly applicable to improving our `hermes-chat` plugin.

---

## Architecture Overview

hermes-webui is a vanilla JS + Python stdlib HTTP server — **no build step, no bundler**.
Our app is a React/Vite plugin served from `webui/dist/`. Key difference: they own
their full server; we are a **Hermes plugin** mounted at `/api/plugins/hermes-chat/`.

Their backend files map roughly to ours:
- `api/routes.py` → our `bridge/main.py`
- `api/config.py` → our `bridge/config.py`
- `api/streaming.py` → our SSE logic in `bridge/main.py`
- `static/*.js` → our `webui/src/`

---

## Features We Don't Have Yet (Ideas for Future Work)

### 1. SSE Auto-Reconnect on Network Blips
hermes-webui reconnects dropped SSE streams automatically (SSH tunnel resilience).
Our `streamEvents` in `webui/src/api.ts` does not retry on disconnect.

**Pattern to adopt:**
```js
// On EventSource error, retry with exponential backoff up to N times
// Track an activeStreamId to deduplicate on reconnect
```

### 2. rAF-Throttled Token Streaming
They use `requestAnimationFrame` to batch token updates instead of setting state
on every SSE token, preventing jank during long responses.

**Pattern to adopt:**
```ts
// Buffer incoming token deltas, flush via requestAnimationFrame
let pending = '';
function flushTokens() {
  if (pending) { setContent(c => c + pending); pending = ''; }
}
eventSource.onmessage = (e) => { pending += e.data; requestAnimationFrame(flushTokens); };
```

### 3. Slash Command Autocomplete
`/` in the composer opens a dropdown with commands like `/clear`, `/model <name>`,
`/compress [topic]`, `/usage`, `/theme`.
Unrecognized commands pass through to the agent.

**Our composer:** `webui/src/components/assistant-ui/thread.tsx` — `ComposerPrimitive.Input`
We could intercept keydown on `/` and render a floating command palette.

### 4. Message Timestamps
HH:MM next to each message, full date on hover.
Our messages have no timestamp display currently.

### 5. Code Block Copy Button
"Copy" button on each code block with "Copied!" feedback.
Our markdown renderer (via `@assistant-ui/react-markdown`) may support custom
`components` for code blocks — we can add a copy button there.

### 6. Session Search by Message Content
hermes-webui searches inside message content, not just session titles.
Our `ChatSidebar` only filters by `title`.

### 7. Session Export / Import
Download as Markdown transcript or full JSON, import from JSON.

### 8. Pin / Archive Sessions
Pin sessions to the top of the sidebar (gold indicator).
Archive (hide without deleting) with a toggle to show archived.

### 9. Session Projects / Tags
Named groups with colors. Tags via `#tag` in title → colored chips + click-to-filter.

### 10. Thinking / Reasoning Blocks
Collapsible gold-themed cards for `<think>...</think>` / `<|channel>thought` fences.
hermes-webui strips these from SSE tokens before rendering markdown and renders
them separately as collapsible blocks.

**Our `thread.tsx`** already uses `assistant-ui` primitives which may support
`reasoning` part type — check `MessagePrimitive.Parts`.

### 11. Context Ring / Token Usage Indicator
Circular ring in the composer footer showing token fill vs context window.
We already track `contextWindow` and `threadUsage` — just need the ring UI.

### 12. Tool Call Cards (Expand/Collapse)
Inline tool call cards showing tool name, args, and result snippet with
expand/collapse toggle per turn.
We have tool call rendering via `assistant-ui` but no collapse behavior.

### 13. Approval Card for Shell Commands
Hermes has a gate system for dangerous tool calls.
We already have `GateDialog` in `App.tsx` — they render it inline in the message
stream instead of a modal, which is less disruptive.

### 14. Selected Text Reply / Named Context Blocks
Select text in a message → "Reply with this" button → adds a named context
block to the next composer message.

### 15. Multiple Themes / Skins
hermes-webui has 10+ skins (`ares`, `mono`, `slate`, `poseidon`, `catppuccin`, etc.)
applied via `data-skin` CSS variable overrides plus `.dark` class.
Our `useTheme` hook only toggles `light`/`dark`. We could add skin presets.

### 16. Mermaid Diagram Rendering
Inline flowcharts/sequence/gantt diagram rendering.
`@assistant-ui/react-markdown` supports `rehype` plugins — `rehype-mermaid` exists.

### 17. PWA / Offline Support
hermes-webui ships a service worker for PWA install (add to home screen on phone).
We serve from a Hermes plugin path which complicates service worker scope, but
a manifest + icon set would still allow "Add to Home Screen".

---

## SSE / Streaming Patterns

hermes-webui's `streaming.py` emits these event types we should be aware of:
```
turn_start        → agent starts processing
token             → streaming text delta
tool_start        → tool invocation begins (name, args)
tool_end          → tool result (result snippet)
think_start/end   → reasoning block delimiters
approval_request  → dangerous command needs user gate
turn_complete     → final message, triggers auto-speak in our app
cancel            → stream was interrupted
session_title     → backend-generated title update
error             → backend error
```

Our `streamEvents` handler in `App.tsx` handles `turn_complete`, `session_title`,
and the token delta types. The `tool_start`/`tool_end`/`think_start`/`think_end`
pattern is how they drive inline tool cards and reasoning blocks.

---

## Voice Patterns

hermes-webui uses **Web Speech API** (browser-native, no backend).
We use **Whisper** (STT, via `faster-whisper`) + **Edge TTS** (TTS, server-side).

Our approach is higher quality but requires deps. Their approach works everywhere
with zero deps. Consider:
- Use Web Speech API as a **fallback** when STT deps are not installed
- Our `useVoiceRecorder.ts` could try `webkitSpeechRecognition` if `faster-whisper` missing

Their voice behavior:
- Auto-stops after ~2s silence
- Appends to existing textarea (doesn't replace)
- Pauses speechSynthesis when composer is focused

---

## Key Files in Our App

| Concern | Our File |
|---|---|
| SSE streaming + message handling | `webui/src/App.tsx` (handleStreamEvent) |
| Composer + voice buttons | `webui/src/components/assistant-ui/thread.tsx` |
| Sidebar + session list | `webui/src/App.tsx` (ChatSidebar) |
| Voice STT hook | `webui/src/hooks/useVoiceRecorder.ts` |
| Voice TTS API | `webui/src/api.ts` (speakText) |
| Theme hook | `webui/src/hooks/useTheme.ts` |
| TTS voice hook | `webui/src/hooks/useTTSVoice.ts` |
| Voice capabilities | `webui/src/hooks/useVoiceCapabilities.ts` |
| Bridge main API | `bridge/main.py` |
| Config | `bridge/config.py` |
| Dashboard UI | `dashboard/dist/index.js` |
| Dashboard API | `dashboard/plugin_api.py` |

---

## Quick Wins (Low Effort, High Impact)

1. **Message timestamps** — add `title={fullDate}` + `text-xs text-muted-foreground` span in `AssistantMessage` / `UserMessage`
2. **Code block copy button** — custom `code` component in `MarkdownTextPrimitive` with clipboard API
3. **rAF token batching** — wrap `setMessages` delta updates in `requestAnimationFrame`
4. **Web Speech API fallback** — try `webkitSpeechRecognition` in `useVoiceRecorder.ts` when STT deps missing
5. **Session search by content** — add `content` field to the `/api/chats` search or filter client-side from loaded messages
