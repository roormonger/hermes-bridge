# Hermes Chat — Improvement Backlog

## assistant-ui Opportunities

### 🔴 High value

- [x] **Wire `ActionBarPrimitive.Reload`** — The regenerate button renders but does nothing. Add an `onReload` handler to `useExternalStoreRuntime` that re-submits the last user message to the stream.

- [x] **Better error surface via `ErrorPrimitive`** — Stream errors currently silently set `status: "complete"`. Set `status: { type: "incomplete", reason: "error" }` on the message and attach an error string so `ErrorPrimitive` / `MessageError` actually shows it inline in the thread.

- [x] **Tool steps as assistant-ui `tool-call` parts + reasoning** — Tools render via `ToolFallback` (assistant-ui registry pattern; `makeAssistantToolUI` is for named tools, Hermes tools are dynamic). Model thinking streams from `reasoning.delta` / `thinking.delta`, shows in a Thoughts accordion, and persists as `messages.reasoning_text`.

### 🟡 Medium value

- [ ] **`SuggestionPrimitive` starter prompts** — `ThreadSuggestions` is wired in `thread.tsx` but no suggestions are configured. Add a small set of starter prompts (e.g. "What files are in the current directory?", "Show me running processes", "Summarize what Hermes can do") to the welcome screen.

- [ ] **`@assistant-ui/react-devtools`** — Install and add to the dev build for a live inspector of runtime state (messages, branches, status, metadata). Remove before production builds.

### 🟢 Longer term / architectural

- [ ] **`assistant-stream` wire protocol** — Replace the hand-rolled SSE parser in `App.tsx` and `api.ts` with the native `assistant-stream` protocol. Unlocks tool-call parts, reasoning parts, source parts, and file parts natively without custom mapping. Big refactor.

- [ ] **`@assistant-ui/react-mcp`** — If Hermes exposes tools via MCP, this lets users manage which tools are active directly from the chat UI.

---

## General UX

- [ ] **Confirm before delete** — Chat deletion in the sidebar has no confirmation. Add a small inline confirm state (show "Delete?" + Yes/No buttons on second click) instead of a modal.

- [ ] **Keyboard shortcut for New Chat** — `Cmd/Ctrl+N` or similar to start a new chat without touching the sidebar.

- [ ] **Message timestamps on hover** — User messages don't show timestamps. Show them on hover like assistant messages already do.
