---
name: hermes-gateway-protocol
description: "Reference for the Hermes tui_gateway JSON-RPC protocol as consumed by hermes-layer/bridge. Use when adding new gateway features (reasoning display, session usage, file/PDF attachments, TUI session import, multi-agent/subagent windows) or debugging bridge<->gateway RPC issues."
version: 1.0.0
---

# Hermes TUI Gateway Protocol — Reference for Hermes Chat

This document catalogs the `tui_gateway/server.py` JSON-RPC surface (from the
installed Hermes agent at `hermes install/hermes-agent/tui_gateway/`) that is
relevant to building out `hermes-layer`'s web chat UI. It exists so future
work doesn't have to re-derive this by reading a 14k-line file again.

Official docs: https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration

## Three integration protocols (context)

| Protocol | Transport | Defined by |
|---|---|---|
| ACP | JSON-RPC over stdio | `acp_adapter/` |
| **TUI gateway** (what we use) | JSON-RPC over stdio or WebSocket | `tui_gateway/server.py` |
| API server | HTTP + SSE, OpenAI-compatible | `gateway/platforms/api_server.py` |

We use the TUI gateway because it's the only protocol that exposes slash
commands, approval/clarify/sudo/secret gates, session branching, and
multi-agent — i.e. "every Hermes feature". Our own `bridge/gateway_session.py`
(`GatewaySession` class) wraps this: one Python subprocess-free RPC channel
per chat, JSON-RPC requests via `_call()`, events drained onto an
`asyncio.Queue` and translated to our SSE shape in `_translate_event()`.

## RPC methods currently used by our bridge

- `session.create` — first message in a chat; returns `session_id`.
- `session.info` — cheap liveness probe (used to detect stale DB session ids after a bridge restart — see `ensure_session()`).
- `prompt.submit` — send a user turn.
- `image.attach_bytes` — attach base64 image before submitting a prompt.
- `session.interrupt` — **now wired** (`GatewaySession.interrupt()`) to stop an in-flight turn from the stop button.
- `approval.respond` / `clarify.respond` / `sudo.respond` / `secret.respond` — resolve gate interrupts.

## Full method catalog (selected, from official docs + server.py grep)

```
session.create        session.list           session.most_recent
session.resume         session.active_list     session.activate
session.close          session.interrupt       session.history
session.compress       session.branch          session.title
session.usage          session.context_breakdown  session.status
session.undo           session.steer           session.cwd.set
session.save

prompt.submit          prompt.background       clarify.respond
sudo.respond            secret.respond          approval.respond

image.attach            image.attach_bytes      image.detach
pdf.attach              file.attach

config.set / config.get   commands.catalog     command.resolve
command.dispatch         cli.exec

delegation.status       subagent.interrupt      spawn_tree.save/list/load
terminal.resize          clipboard.paste         input.detect_drop

reload.mcp              reload.env              process.stop / process.list
```

**Important distinction** (from official docs): `session.active_list`,
`session.activate`, `session.close` are *process-local live-session*
controls (sessions currently open in this gateway process). `session.list`
+ `session.resume` are the *saved transcript* browser/loader — these work
across gateway restarts and are what a "resume picker" or session importer
needs.

## Event catalog (streamed back via `{"method": "event", "params": {"type": ..., "session_id": ..., "payload": {...}}}`)

Events we already translate in `gateway_session.py::_translate_event()`:

- `message.delta` → `{"type": "text", "text": ...}`
- `message.complete` / `turn.complete` → `{"type": "turn_complete"}`
- `tool.start` / `tool.complete` → `tool_start` / `tool_complete`
- `approval.request` / `clarify.request` / `sudo.request` / `secret.request` → `gate_interrupt`

Events emitted by the gateway that we **don't** yet surface (grepped every
`_emit("...", ...)` call in `server.py`) — ranked by likely UI value:

| Event | Payload | Potential UI use |
|---|---|---|
| `reasoning.delta` | `{text, verbose?}` | Extended-thinking / chain-of-thought stream, like Claude/ChatGPT "Thinking" panel. Also `thinking.delta`, `reasoning.available`. |
| `tool.generating` | `{name}` | Show "calling {tool}..." before args are fully streamed (currently we only get `tool.start` once args are ready). |
| `status.update` | `{kind, text}` | `kind` includes `"compacting"` — useful loading state during auto-compression. |
| `session.info` | model, context window, token usage, cwd, git branch | Header info: current model, token/context usage bar. |
| `notification.show` / `notification.clear` | `{text, level?, id, key}` | Toast notifications (credit warnings, background task completion). |
| `moa.reference` / `moa.aggregating` | reference-model outputs | If Mixture-of-Agents mode is ever exposed, shows individual model outputs before aggregation. |
| `review.summary` | `{text}` | Background code-review summaries. |
| `terminal.read.request` (a **gate**, via `_block`) | `{start?, count?}` | Not yet in our gate-kind switch — would 500 if hit. Low priority (desktop-GUI-only feature: reading a PTY buffer). |
| `agent.terminal.output` / close | subagent PTY passthrough | Only relevant if we ever add a "watch subagent" window. |

Adding one of these is the same pattern every time: add a branch to
`_translate_event()` in `bridge/gateway_session.py` mapping the gateway event
to a new SSE `type`, add that type to `SseEvent` in `webui/src/api.ts`, handle
it in `handleStreamEvent` in `webui/src/App.tsx`.

## Gates we handle vs. don't

Handled (`respond_gate()` in `gateway_session.py`): `approval`, `clarify`,
`sudo`, `secret`.

Not handled: `terminal.read.request` (desktop-GUI PTY read gate — skip,
not relevant to a web chat client).

## Attachment methods beyond images

We currently only use `image.attach_bytes`. Two more exist and would be easy
wins for feature parity with the TUI:

- **`pdf.attach`** — takes a PDF, server-side renders each page to PNG and
  queues them as vision tiles, same downstream path as image attach. Would
  let users drag in PDFs the same way they drag in images today.
- **`file.attach`** — stages an arbitrary non-image file into the session
  workspace. Params: `session_id`, and either `path` (gateway-visible path)
  or `data_url` (base64 upload — **this is our path**, since the browser
  can't give the gateway a local filesystem path). Returns `ref_path` +
  `ref_text` (`@file:<path>`) that should be **inserted into the prompt
  text**, not attached silently — the agent's file tools resolve `@file:`
  refs. This is how the TUI does "attach a text file / code file / log" for
  non-image content, as opposed to base64-embedding.

## TUI session import — the key finding

This is directly useful for the "import TUI chat sessions" goal.

**`session.list`** — params: `{limit?}` (default 200). Returns saved
transcripts across every surface (CLI, TUI, ACP, gateway platforms), not just
currently-open ones:

```json
{"sessions": [
  {"id": "...", "title": "...", "preview": "...",
   "started_at": 172..., "message_count": 28, "source": "tui"}
]}
```

Internally filters out `source == "tool"` (sub-agent runs) but keeps
everything else — `tui`, `api_server`, and any custom source. Our local
`state.db` inspection (see below) confirms `source` values seen in the wild
are `"tui"` and `"api_server"`.

**`session.resume`** — params: `{session_id, cols?, profile?, lazy?}`. This
is the one-call answer to "load a saved TUI session for use in the web UI":
it loads the session (following the compression-continuation chain to the
live tip if the session was auto-compressed), makes it live in the gateway
process, and returns:

```json
{
  "info": {...},
  "message_count": N,
  "messages": [{"role": "user"|"assistant"|"tool", "text": "...", ...}],
  "running": false,
  "session_id": "...",
  "session_key": "...",
  "started_at": ...,
  "status": "idle"
}
```

`messages` is already normalized by `_history_to_messages()` — tool-call
pairs are collapsed to `{"role": "tool", "name": ..., "context": ...}`
(no raw OpenAI `tool_calls` schema to parse), assistant reasoning is
preserved under `reasoning`/`reasoning_content` keys when present. This is
directly usable to hydrate our `ChatMessage[]` state and backend `messages`
table.

**`session.history`** — same normalized shape, but for a session that's
*already* referenced by `session_id` (must already be resolvable via
`_sess_nowait`, i.e. either live or has a `session_key` in the DB). Less
useful for cold import than `session.resume`, which works even if the
session was never opened in this gateway process.

### Prerequisite fix: tag our own sessions with a distinct `source`

**Confirmed by reading `session.create`'s handler**: `source =
str(params.get("source") or "tui").strip() or "tui"` — when no `source` param
is given, the gateway defaults to `"tui"`. Our `GatewaySession.ensure_session()`
calls `self._call("session.create", {})` with **no `source` param at all**,
so every chat created through `hermes-layer` is *currently indistinguishable
from a real TUI session* in `session.list` / the `sessions` table.

**Fix before shipping the import picker**: pass `{"source": "hermes-chat"}`
(or similar) in that `session.create` call. This is a one-line change in
`bridge/gateway_session.py::ensure_session()`. Without it, every chat created
via the web UI would show up in its own "import from TUI" picker, and worse,
re-importing one would resume a session that's already backing an existing
hermes-chat conversation.

### Recommended import flow

1. New endpoint e.g. `GET /v1/tui-sessions` → bridge calls `session.list`
   on a throwaway/shared `GatewaySession`, filters to `source == "tui"`
   (excluding `"hermes-chat"` and the internal `"tool"` deny-list already
   applied by the gateway), returns
   `{id, title, preview, started_at, message_count}[]` for a picker UI.
2. `POST /v1/tui-sessions/{id}/import` → bridge calls `session.resume` with
   that `session_id`, gets back `messages`. Create a new chat in
   `chat_history.py` (`create_chat`), bulk-insert the returned messages via
   `add_message()` (mapping `role` directly, `tool` role messages can be
   rendered as a collapsed tool-step pill or skipped for MVP), and — critically
   — store the **resolved** `session_id` from the resume response (it may
   differ from the requested one if compression forked it) as this chat's
   `hermes_session_id` via `store.set_hermes_session_id(...)` so subsequent
   `prompt.submit` calls continue the *same* Hermes session instead of
   creating a new empty one.
3. UI: a "Import from TUI" entry in the sidebar (or new-chat menu) opens a
   picker fed by step 1, calls step 2 on selection, then `loadMessages()` +
   switch to the new chat.

Caveats to verify before shipping:
- Multiple concurrent imports of the *same* TUI session would each call
  `session.resume`, which makes it live under one gateway session — need to
  decide whether re-importing an already-imported session should re-resume
  (continue) vs. clone-as-read-only-then-fork. Simplest MVP: only allow
  importing a given TUI session once; track imported `session_id`s in our DB
  and grey them out in the picker.
- `session.resume`'s `messages` list drops empty/whitespace-only turns and
  collapses tool calls — good for display, but if we want faithful re-export
  later we'd want `session.history` too (it has the same normalization, so no
  extra fidelity there — the *raw* schema lives only in `state.db`'s
  `messages` table, see below).

## `state.db` schema (ground truth for message/session persistence)

Confirmed by direct inspection of the installed `hermes install/state.db`
(SQLite). Relevant tables:

**`sessions`**: `id, source, user_id, session_key, chat_id, chat_type,
thread_id, model, model_config, system_prompt, parent_session_id,
started_at, ended_at, end_reason, message_count, tool_call_count,
input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
reasoning_tokens, cwd, git_branch, git_repo_root, title, archived,
display_name, ...` (plus billing/cost columns, handoff state, rewind count).

**`messages`**: `id, session_id, role, content, tool_call_id, tool_calls,
tool_name, timestamp, token_count, finish_reason, reasoning,
reasoning_content, reasoning_details, codex_reasoning_items,
codex_message_items, platform_message_id, observed, active, compacted`.

- `role` ∈ `{user, assistant, tool, system}`.
- `tool_calls` (assistant rows only) is a JSON array, OpenAI function-call
  shape: `[{"id", "call_id", "type": "function", "function": {"name",
  "arguments"}}]`.
- `tool` rows have `tool_call_id` linking back to the originating
  `tool_calls[].id`, and `content` holding the JSON-stringified tool result.
- `active`/`compacted` flags: rows can be soft-retired by compression without
  deletion — filter `active=1` (or trust `session.resume`/`session.history`
  which already do this via the agent's live `history`, not a raw table
  scan).
- There's also `messages_fts` / `messages_fts_trigram` (SQLite FTS5) for
  full-text + fuzzy search across all messages — could power a global search
  feature across imported + native chats later.

We do **not** need to touch `state.db` directly for the import feature —
`session.resume` already does the equivalent read + compression-chain
resolution correctly. This section is here so a future "raw export" or
"cross-session search" feature doesn't have to redo this discovery.

## `hermes_client` (lotsoftick/hermes_client) — architectural comparison

https://github.com/lotsoftick/hermes_client — a different, CLI-subprocess
based approach worth knowing about even though we deliberately don't follow
it:

- **No gateway at all.** Every turn spawns `hermes -p <profile> chat -Q -q
  "<message>"` as a fresh subprocess and streams its stdout over SSE. No
  persistent process, no JSON-RPC.
- **Multi-agent = Hermes profiles.** Each UI "agent" is a separate Hermes
  `profile` (own home dir/config/sessions), managed via `hermes profile ...`
  CLI commands.
- **Cross-app session sync** — because it shells out to the same `hermes`
  CLI the terminal uses, a session started in a plain terminal REPL shows up
  in their sidebar automatically (same `state.db`), and can be continued from
  either side via `hermes -p <profile> chat -r <sessionKey>`.
- **File uploads** — saved to `~/.hermes_client/uploads/<conversationId>/`,
  then passed to Hermes via `--image <path>` (images) or referenced inline in
  the prompt text (everything else) — i.e. no `file.attach`/`data_url` RPC,
  just CLI flags/paths.
- **Auth**: JWT, default admin/admin bootstrap — same shape as our own
  `bridge` auth, nothing new there.

**Why our gateway-RPC approach is strictly better for feature parity with
the TUI**: no per-turn subprocess spawn latency, native streaming events
(tool steps, reasoning, gates) instead of parsing CLI stdout formatting,
and access to gates (approval/clarify/sudo/secret) which the `-Q -q` quiet
CLI mode can't surface interactively at all — hermes_client's CLI mode would
just hang or fail on any turn that needs a gate. The one thing they get "for
free" that we don't — automatic visibility of sessions from *any* Hermes
surface without an explicit import step — is exactly what our `session.list`
+ `session.resume` import flow above would replicate deliberately instead.

## Open items / not yet investigated

- `session.branch` (fork a session) and `session.compress` (manual compact) —
  not investigated in depth; likely future "fork this conversation" /
  "compact to save tokens" UI affordances, same low-risk RPC-wrapper pattern
  as `interrupt()`.
- `session.steer` — inject a message into the *next* tool result without
  interrupting the current turn. Interesting for a "nudge while it's working"
  input, distinct from cancel-and-resubmit.
