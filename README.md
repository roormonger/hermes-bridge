# hermes-bridge

A lightweight middle-tier bridge that lets you drive a [Hermes Agent](https://github.com/NousResearch/hermes-agent)
chat session from inside [Open WebUI](https://github.com/open-webui/open-webui) — including its
interactive `questionary`/TUI decision gates (tool-approval prompts, model pickers, confirm/select
dialogs) — as native, asynchronous chat UI instead of a blocking terminal.

Inspired by [lotsoftick/hermes_client](https://github.com/lotsoftick/hermes_client), but scoped down
to a single-purpose bridge + Open WebUI plugin bundle instead of a standalone web client.

## Table of contents

- [How it works](#how-it-works)
- [Repository layout](#repository-layout)
- [Requirements](#requirements)
- [Quickstart](#quickstart)
- [API reference](#api-reference)
- [Open WebUI plugin setup](#open-webui-plugin-setup)
- [Configuration reference](#configuration-reference)
- [Gate detection internals](#gate-detection-internals)
- [Troubleshooting](#troubleshooting)
- [Known limitations / roadmap](#known-limitations--roadmap)
- [Contributing](#contributing)
- [License](#license)

## How it works

```
                    POST /v1/chat {chat_id, message}
Open WebUI  ───────────────────────────────────────────►  hermes-bridge (FastAPI)
   ▲                                                             │
   │        SSE: {"type":"text",...} / {"type":"gate_interrupt"} │
   └─────────────────────────────────────────────────────────────┘
                                                                  │
                                                     spawns / reuses PTY
                                                                  ▼
                                              hermes chat -r <hermes_session_id>
                                                                  │
                                              stdout/stdin over a pseudo-terminal
                                                                  ▼
                                                        SQLite (chat_id -> hermes_session_id)
```

1. Open WebUI's **Pipe** plugin forwards each user turn to `POST /v1/chat` on the bridge, tagged with
   Open WebUI's `chat_id`.
2. The bridge looks up (or creates) a persistent Hermes session id for that `chat_id` in a small SQLite
   table, then spawns `hermes chat -r <session_id>` inside a pseudo-terminal (PTY) so Hermes' classic
   CLI behaves exactly as it would in a real terminal — this matters because `questionary` prompts
   render differently (or not at all) over plain pipes.
3. Stdout is streamed back to Open WebUI as newline-delimited SSE JSON lines:
   `{"type": "text", "text": "..."}`.
4. If the bridge's regex/heuristic detector spots a `questionary` decision gate (a confirm `(Y/n)`
   prompt or an arrow-key single-select list), it **pauses** the stream and instead emits:
   `{"type": "gate_interrupt", "gate_id": "...", "prompt": "...", "options": [...]}`.
5. The **Pipe** script renders that as a Markdown block with both a clickable-button affordance and a
   plain-text fallback ("reply with Yes or No"), plus a hidden `<!-- hermes-gate ... -->` comment
   carrying the machine-readable metadata.
6. Resolving the gate — via the **Action** plugin, or simply by typing one of the option names as your
   next chat message — writes the right keystrokes into the subprocess's stdin and resumes the stream.
7. Idle subprocesses are killed after `HERMES_SESSION_IDLE_TIMEOUT` seconds of inactivity; the next
   message transparently respawns `hermes chat -r <session_id>`, which resumes the native Hermes
   session exactly where it left off. No process sits around burning resources for an abandoned chat.

> **Note on the "button" links.** Open WebUI does not natively support a `button://` markdown link
> scheme. The Pipe script still emits one for forward-compatibility with custom front-ends, but the
> **reliable** path today is either (a) reply with the option's text as a normal chat message, or
> (b) register `openwebui/action_plugin.py` as an Open WebUI Action, which reads the hidden metadata
> comment from the message and calls `/v1/gate/resolve` for you.

## Repository layout

```
├── plugin.yaml          Hermes plugin manifest
├── plugin.py            Hermes plugin entry point (CLI commands + tools)
├── README.md
├── requirements.txt
├── bridge/
│   ├── __init__.py
│   ├── main.py          FastAPI app: /v1/chat, /v1/gate/resolve, /v1/chat/drain, /healthz
│   ├── pty_manager.py   PTY subprocess lifecycle + gate-detection heuristics
│   ├── database.py      SQLite chat_id -> hermes_session_id mapping
│   ├── config.py        YAML config loading/validation
│   └── daemon.py        Background daemon + watchdog
└── openwebui/
    ├── pipe_plugin.py    Open WebUI Pipe function (chat model entry point)
    ├── action_plugin.py  Open WebUI Action function (gate resolver button)
    ├── build_exports.py  JSON export generator
    ├── pipe_plugin.json  Ready-to-import Open WebUI Pipe JSON
    └── action_plugin.json Ready-to-import Open WebUI Action JSON
```

## Requirements

- **Linux, macOS, or WSL** host with `hermes` on `PATH`. The bridge uses the POSIX-only `pty` module
  and **cannot run on native Windows**.
- Python 3.10+
- An Open WebUI instance that can reach the bridge over HTTP (same host, LAN, or tunneled).

## Quickstart

### Install as a Hermes plugin

```bash
hermes plugins install https://github.com/roormonger/hermes-bridge.git
hermes hermes-bridge install-deps
hermes hermes-bridge start
```

`install-deps` checks that the bridge's Python packages (`fastapi`, `uvicorn`, `pydantic`, `pyyaml`) are available in the Hermes Python environment and installs them from `requirements.txt` if any are missing. Hermes itself normally ships with these, but the command makes the install self-contained.

The plugin is installed into `~/.hermes/plugins/hermes-bridge/`. `hermes hermes-bridge start` spawns a
detached background daemon that runs the FastAPI server and watches its own health, restarting on failure.

Verify it's alive:

```bash
hermes hermes-bridge status
# {"running": true, "pid": ..., "healthy": true, "config": {...}}
```

### Plugin CLI

| Command | Purpose |
|---|---|
| `hermes hermes-bridge start` | Start the background daemon with health watchdog |
| `hermes hermes-bridge stop` | Stop the daemon |
| `hermes hermes-bridge restart` | Restart the daemon |
| `hermes hermes-bridge status` | Show PID, health, and current config |
| `hermes hermes-bridge logs` | Tail the daemon log |
| `hermes hermes-bridge configure --port 8080 --restart` | Write config and optionally restart |
| `hermes hermes-bridge install-deps` | Install missing Python dependencies into the Hermes environment |
| `hermes hermes-bridge test-gates -v` | Run gate-detection dev tests against representative questionary prompts |

### Manual / development install

```bash
git clone https://github.com/roormonger/hermes-bridge.git
cd hermes-bridge
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Sanity-check that Hermes is installed and reachable:
hermes --version

uvicorn bridge.main:app --host 0.0.0.0 --port 8000
```

Verify it's alive:

```bash
curl http://localhost:6969/healthz
# {"status":"ok"}
```

Try a chat turn directly (without Open WebUI) to confirm the PTY/streaming path works end to end:

```bash
curl -N -X POST http://localhost:6969/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"chat_id": "test-chat-1", "message": "hello"}'
```

You should see a stream of `data: {"type": "text", ...}` lines. If Hermes hits a decision gate, you'll
instead see a single `data: {"type": "gate_interrupt", ...}` line and the stream will end — resolve it
with:

```bash
curl -X POST http://localhost:6969/v1/gate/resolve \
  -H "Content-Type: application/json" \
  -d '{"chat_id": "test-chat-1", "gate_id": "<gate_id from above>", "choice": "Yes"}'
```

then resume the stream with `/v1/chat/drain` (same payload shape as `/v1/chat`, `message` is ignored).

## API reference

### `POST /v1/chat`

Request:

```json
{"chat_id": "abc123", "message": "what's in this directory?"}
```

Response: `text/event-stream`, one JSON object per SSE `data:` line.

| Event | Shape | Meaning |
|---|---|---|
| Text token | `{"type": "text", "text": "..."}` | Plain Hermes output; concatenate in order. |
| Gate interrupt | `{"type": "gate_interrupt", "gate_id": "...", "prompt": "...", "options": [...]}` | Stream paused; subprocess is blocked on stdin awaiting a decision. |
| Process exit | `{"type": "process_exit", "code": 0}` | The Hermes subprocess terminated; stream ends. |

If a gate is already pending for `chat_id`, `/v1/chat` will:
- Resolve it and resume streaming, if `message` case-insensitively matches one of the pending gate's
  `options`.
- Return `409 Conflict` otherwise, with the pending `gate_id` and valid options in the error detail.

### `POST /v1/gate/resolve`

```json
{"chat_id": "abc123", "gate_id": "b7e1...", "choice": "Yes"}
```

Writes the appropriate keystroke(s) to the subprocess (`y\n`/`n\n` for confirm prompts, arrow-key
navigation + Enter for select prompts) and unblocks it. Returns `{"status": "resolved", "gate_id": "..."}`.
Call `/v1/chat/drain` afterwards to keep consuming the resumed token stream.

### `POST /v1/chat/drain`

Same payload as `/v1/chat` (`message` is ignored). Re-opens the SSE stream for a chat that already has
a running subprocess — used after a gate is resolved out-of-band (e.g. from the Action plugin) to
continue rendering tokens without submitting a new user message.

### `GET /healthz`

Liveness probe; returns `{"status": "ok"}`.

## Open WebUI plugin setup

The Open WebUI **Workspace → Functions → Import** dialog expects a JSON export file, not a raw `.py`
file. We ship ready-to-import JSON exports alongside the plugin source.

1. In Open WebUI, go to **Workspace → Functions → Import**.
2. Import `openwebui/pipe_plugin.json`. Open its valves and set `BRIDGE_URL` to your bridge's address
   (default: `http://localhost:6969`). Enable it as a model under **Settings → Models**.
3. Import `openwebui/action_plugin.json`. Set the same `BRIDGE_URL` valve. Optionally register it twice
   with different `CHOICE` valves (e.g. `Yes` / `No`) to get one-click approve/deny buttons under each
   assistant message, or leave `CHOICE` blank to default to the gate's first option.
4. Start a chat using the Hermes pipe model. When Hermes hits a decision gate, the prompt renders
   inline in the chat — resolve it by clicking the registered Action button(s) or by typing the option
   name as your next message.

> **Plugin sources:** `openwebui/pipe_plugin.py` and `openwebui/action_plugin.py` are the Python sources.
> `openwebui/pipe_plugin.json` and `openwebui/action_plugin.json` are Open WebUI function-export files
> generated by `openwebui/build_exports.py`. If you edit the `.py` sources, run `python openwebui/build_exports.py`
> to regenerate the JSON files.

## Configuration reference

The bridge reads its runtime settings from `~/.hermes/plugins/hermes-bridge/config.yaml` (or
`config.yaml` in the repo root for a manual install):

| Key | Default | Purpose |
|---|---|---|
| `host` | `127.0.0.1` | HTTP host to bind. |
| `port` | `6969` | HTTP port to bind. |
| `hermes_bin` | `hermes` | Path or name of the Hermes CLI executable. `HERMES_BIN` env var overrides this. |
| `session_idle_timeout` | `600` | Seconds of chat inactivity before the subprocess is killed. Resumed transparently on the next message. |
| `gate_idle_threshold` | `0.35` | Seconds of output silence before the trailing buffer is checked for a gate prompt that never ended in a newline. |
| `log_level` | `INFO` | Server log level. |
| `auto_start` | `true` | Start the daemon automatically when the Hermes plugin loads. |
| `debug` | `false` | Enable verbose PTY/gate-detection logging (forces log level to DEBUG for the bridge). |

Change settings with the CLI:

```bash
hermes hermes-bridge configure --port 8080 --hermes-bin /usr/local/bin/hermes --restart
hermes hermes-bridge configure --debug true --restart
```

When `debug` is enabled, the daemon log records every raw PTY chunk, the ANSI-stripped version, and every gate-detection attempt. Use it to capture prompt shapes that `GateDetector` misses.

Or let Hermes change them via the registered tools:

- `hermes_bridge_configure` — write any bridge-side setting.
- `hermes_bridge_status` — check daemon health and current config.
- `hermes_bridge_restart` — restart the daemon to apply config changes.
- `hermes_bridge_install_dependencies` — install missing Python packages into the Hermes environment.

Open WebUI plugin valves (set per-import in the Open WebUI UI):

| Plugin | Valve | Default | Purpose |
|---|---|---|---|
| Pipe | `BRIDGE_URL` | `http://localhost:6969` | Base URL of the bridge. |
| Pipe | `REQUEST_TIMEOUT` | `600` | Seconds to wait on the SSE stream. |
| Action | `BRIDGE_URL` | `http://localhost:6969` | Base URL of the bridge. |
| Action | `CHOICE` | `""` (uses first option) | Fixed choice this button always resolves with. |
| Action | `STREAM_TOKENS` | `true` | Stream resumed tokens via message events. Set to `false` on older Open WebUI versions that don't support `event_emitter` message events; the action will return the full resumed text as a single message. |
| Action | `REQUEST_TIMEOUT` | `30` | Seconds to wait on the resolve/drain calls. |

## Gate detection internals

`bridge/pty_manager.py::GateDetector` strips ANSI escape codes from the PTY output and looks for two
`questionary`-style shapes on the trailing, not-yet-flushed line buffer:

- **Confirm prompts:** a line ending in `(Y/n)` or `[y/n]`, e.g. `? Proceed with this action? (Y/n)`.
  Detected options are always normalized to `["Yes", "No"]`.
- **Select prompts:** a `?`-prefixed header line followed by option lines, at least one of which starts
  with a cursor marker (`❯`, `▶`, or `>`), e.g.:
  ```
  ? Choose a model:
    gpt-4o
  ❯ gpt-4o-mini
    claude-3-5-sonnet
  ```

Detection runs opportunistically after every PTY read, and again after `gate_idle_threshold`
seconds of output silence (to catch prompts that don't end in a newline, since the cursor just sits
there waiting for a keypress). Text that doesn't match either shape is released to the normal `"text"`
stream as soon as the idle check clears it.

Extending detection for other prompt shapes (checkboxes, autocomplete, masked/secret input) means
adding a new pattern + branch to `GateDetector.detect` and, if it needs a non-trivial keystroke
sequence to resolve, a matching branch in `HermesPtySession.resolve_gate`.

## Troubleshooting

**`RuntimeError: The 'pty' module is POSIX-only...`**
You're running the bridge on native Windows. Use WSL, a Linux VM/container, or macOS instead.

**`/v1/chat` returns `409 Conflict`**
The chat has an unresolved gate. Reply with one of the option names listed in the error detail, or call
`/v1/gate/resolve` directly.

**Gate never gets detected (agent looks "stuck")**
Hermes may be emitting an ANSI escape sequence shape or prompt wording not covered by `GateDetector`.
Enable `debug: true` in the config, restart the daemon, and trigger the gate again. The daemon log will
show the raw and stripped PTY output for every chunk plus the exact buffer being checked. Use that
output to add a new pattern per [Gate detection internals](#gate-detection-internals). You can also
lower `gate_idle_threshold` to detect faster.

**Action button does nothing**
Confirm the Action plugin's `BRIDGE_URL` valve matches the running bridge, and that the assistant
message still contains the hidden `<!-- hermes-gate ... -->` comment (don't edit/regenerate the
message after a gate is emitted). If the action resolves the gate but the resumed text doesn't appear,
your Open WebUI version may not support `event_emitter` message events. Set the Action's `STREAM_TOKENS`
valve to `false` to receive the resumed text as a single batched message instead.

## Known limitations / roadmap

- Gate detection is regex/heuristic-based against ANSI-stripped PTY output. It handles the common
  `questionary` confirm and single-select shapes; checkboxes, autocomplete, and masked/secret input
  need additional patterns. Enable `debug: true` to capture the raw prompt output needed to add new
  patterns.
- One subprocess per active chat, in-process `SessionManager` state — no cross-process/multi-worker
  session sharing. Fine for a single-instance bridge deployment; not suitable behind a
  horizontally-scaled/load-balanced uvicorn setup without moving session state to a shared store.
- The bridge service requires a POSIX host (Linux/macOS/WSL) because of the `pty` module. The plugin
  can be installed on any Hermes host, but the daemon will fail on native Windows.
- The plugin runs inside Hermes' Python environment. `hermes hermes-bridge install-deps` handles the
  bridge's Python dependencies, but it needs network access to pip and permission to write into that
  environment.
- The bridge endpoints have no built-in auth, but in the normal setup they are protected by Open WebUI's
  authentication because the Open WebUI Pipe/Action plugins run on the Open WebUI backend and the bridge
  defaults to `host: 127.0.0.0`. Only expose the bridge beyond localhost (e.g. `0.0.0.0` or a remote host)
  if you also put it behind a reverse proxy, VPN, or auth middleware.

## Development

`scripts/test_gate_detection.py` exercises `GateDetector` against a library of representative
questionary prompt shapes. Use it while adding new gate-detection patterns:

```bash
python scripts/test_gate_detection.py
python scripts/test_gate_detection.py -v
```

Or run it through the Hermes plugin CLI:

```bash
hermes hermes-bridge test-gates -v
```

The test cases cover confirm prompts (`Y/n`, `[y/n]`) and single-select prompts with `❯`/`>` cursors.
When you hit a prompt shape in the wild that the bridge misses, enable `debug: true`, capture the
ANSI-stripped buffer from the daemon log, and add it to `scripts/test_gate_detection.py` before
extending `GateDetector`.

## Contributing

Issues and PRs welcome at [github.com/roormonger/hermes-bridge](https://github.com/roormonger/hermes-bridge).
Please include the Hermes CLI version and a snippet of the raw (ANSI-stripped) prompt output when
reporting gate-detection misses.

## License

MIT — see [LICENSE](LICENSE).