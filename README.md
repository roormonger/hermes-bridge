# Hermes Chat

A standalone web chat UI for the [Hermes Agent](https://github.com/NousResearch/hermes-agent).

Hermes Chat gives you a clean, modern chat interface that talks directly to the Hermes TUI gateway — with streaming responses, live tool-step display, and interactive decision gates (tool-approval, confirm/select dialogs) surfaced as native UI elements.

## Table of contents

- [How it works](#how-it-works)
- [Repository layout](#repository-layout)
- [Requirements](#requirements)
- [Quickstart](#quickstart)
- [API reference](#api-reference)
- [Configuration reference](#configuration-reference)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)

## How it works

```
Browser
        │
        │  POST /v1/chat {chat_id, message}
        ▼
Hermes Chat  (FastAPI daemon)
        │
        │  JSON-RPC: session.create / prompt.submit
        ▼
tui_gateway  (in-process, part of Hermes)
        │
        │  events: message.delta, tool.start, tool.complete, turn.complete, …
        ▼
SSE stream → browser
```

1. Each user message hits `POST /v1/chat`. The daemon looks up (or creates) a persistent TUI gateway session for that `chat_id` in SQLite.
2. The message is submitted to the Hermes TUI gateway via `prompt.submit`. The gateway drives the Hermes agent in-process — no PTY, no subprocess scraping.
3. Events stream back as SSE JSON lines: text tokens, tool start/complete steps, gate interrupts, and a final `turn_complete`.
4. The standalone web UI renders text with Markdown, shows live tool steps in a collapsible pill, and surfaces decision gates inline.
5. If a gate interrupt arrives (tool-approval, confirm, sudo, secret), the stream pauses and the UI displays the gate for user resolution. Resolving it via `/v1/gate/resolve` resumes the stream.
6. Idle sessions are reaped after `session_idle_timeout` seconds. The next message transparently recreates the session.

## Repository layout

```
├── plugin.yaml           Hermes plugin manifest
├── plugin.py             Hermes plugin entry point (CLI commands + tools)
├── requirements.txt
├── bridge/
│   ├── main.py           FastAPI app: /v1/chat, /v1/gate/resolve, /healthz, /api/ws
│   ├── gateway_session.py  TUI gateway session manager (JSON-RPC, in-process)
│   ├── pty_manager.py    Fallback PTY backend (used if tui_gateway is unavailable)
│   ├── database.py       SQLite: chat_id → hermes_session_id mapping + users
│   ├── users.py          User store (bcrypt passwords, JWT auth)
│   ├── config.py         YAML config loading/validation
│   └── daemon.py         Background daemon + health watchdog
├── webui/                Standalone React chat UI (Vite + Tailwind + assistant-ui)
│   └── src/
│       ├── App.tsx       Chat state, SSE event handling, session management
│       └── components/   UI components (thread, markdown, tool fallback, …)
└── dashboard/            Hermes dashboard plugin tab
    ├── manifest.json
    ├── plugin_api.py     Dashboard API routes (status, logs, users, controls)
    └── dist/index.js     Dashboard UI bundle
```

## Requirements

- **Linux, macOS, or WSL** host with `hermes` on `PATH`.
- Python 3.10+
- The Hermes `tui_gateway` module (ships with recent Hermes builds). If unavailable, the daemon falls back to a PTY backend automatically.

## Quickstart

### Install as a Hermes plugin

```bash
hermes plugins install https://github.com/roormonger/hermes-chat.git
hermes hermes-chat install-deps
hermes hermes-chat start
```

`install-deps` installs the required Python packages (`fastapi`, `uvicorn`, `pydantic`, `pyyaml`, `bcrypt`, `pyjwt`) into the Hermes Python environment.

The plugin installs to `~/.hermes/plugins/hermes-chat/`. The daemon runs detached and restarts on failure.

Verify it's alive:

```bash
hermes hermes-chat status
# {"running": true, "pid": ..., "healthy": true, "config": {...}}
```

Open `http://<host>:6969` in a browser for the standalone chat UI.

### Plugin CLI

| Command | Purpose |
|---|---|
| `hermes hermes-chat start` | Start the background daemon |
| `hermes hermes-chat stop` | Stop the daemon |
| `hermes hermes-chat restart` | Restart the daemon |
| `hermes hermes-chat status` | Show PID, health, and current config |
| `hermes hermes-chat logs` | Tail the daemon log |
| `hermes hermes-chat configure --port 8080 --restart` | Write config and optionally restart |
| `hermes hermes-chat install-deps` | Install missing Python dependencies |
| `hermes hermes-chat users list` | List chat UI users |
| `hermes hermes-chat users add --username alice --password secret` | Add a user |
| `hermes hermes-chat users delete --user-id <id>` | Delete a user |

### Manual / development install

```bash
git clone https://github.com/roormonger/hermes-chat.git
cd hermes-chat
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn bridge.main:app --host 0.0.0.0 --port 6969
```

Verify:

```bash
curl http://localhost:6969/healthz
# {"status":"ok"}
```

Test a chat turn:

```bash
curl -N -X POST http://localhost:6969/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"chat_id": "test-1", "message": "hello"}'
```

## API reference

### `POST /v1/chat`

```json
{"chat_id": "abc123", "message": "what's in this directory?"}
```

Response: `text/event-stream`, one JSON object per SSE `data:` line.

| Event | Shape | Meaning |
|---|---|---|
| Text token | `{"type": "text", "text": "..."}` | Streamed response text. |
| Tool start | `{"type": "tool_start", "name": "...", "context": "..."}` | Tool execution began. |
| Tool complete | `{"type": "tool_complete", "name": "...", "summary": "...", "duration_s": 1.2}` | Tool finished. |
| Turn complete | `{"type": "turn_complete"}` | Agent turn finished; stream ends. |
| Gate interrupt | `{"type": "gate_interrupt", "gate_kind": "approval\|clarify\|sudo\|secret", "gate_id": "...", "prompt": "...", "options": [...]}` | Agent is waiting for user input. |
| Session title | `{"type": "session_title", "title": "..."}` | Hermes auto-named the session. |
| Error | `{"type": "error", "message": "..."}` | Agent or gateway error. |

### `POST /v1/gate/resolve`

```json
{"chat_id": "abc123", "gate_id": "b7e1...", "choice": "Yes", "gate_kind": "approval"}
```

Resolves a pending gate and resumes the session. The next `POST /v1/chat` will continue from where the agent left off.

### `GET /healthz`

Liveness probe; returns `{"status": "ok"}`.

## Configuration reference

Settings live in `~/.hermes/plugins/hermes-chat/config.yaml` (or `config.yaml` in the repo root for a manual install):

| Key | Default | Purpose |
|---|---|---|
| `host` | `127.0.0.1` | HTTP host to bind. Use `0.0.0.0` to expose on LAN. |
| `port` | `6969` | HTTP port. |
| `hermes_bin` | `hermes` | Path to the Hermes CLI binary. |
| `session_idle_timeout` | `600` | Seconds before idle sessions are reaped. |
| `log_level` | `INFO` | Log level (`DEBUG`/`INFO`/`WARNING`/`ERROR`). |
| `auto_start` | `true` | Auto-start the daemon when the Hermes plugin loads. |
| `debug` | `false` | Enable verbose gateway/session logging. |
| `hermes_dashboard_url` | `auto` | Hermes dashboard base URL. Auto-detected from `HERMES_DASHBOARD_URL` env var, otherwise defaults to `http://127.0.0.1:9119`. |

```bash
hermes hermes-chat configure --port 8080 --hermes-bin /usr/local/bin/hermes --restart
hermes hermes-chat configure --debug true --restart
```

Hermes can also configure the daemon via registered tools:

- `hermes_bridge_configure` — write config settings.
- `hermes_bridge_status` — check health and config.
- `hermes_bridge_restart` — restart the daemon.
- `hermes_bridge_install_dependencies` — install missing Python packages.

## Troubleshooting

**Agent never responds / stays "thinking"**
Check the daemon log (`hermes hermes-chat logs`). A `session not found (4001)` warning means the in-memory session was lost (e.g. daemon restarted) — the daemon auto-recreates sessions on 4001, but if it persists, restart the daemon.

**`tui_gateway` unavailable**
The daemon logs the exact import error at startup and falls back to a PTY backend. Check that you're running inside the correct Hermes Python environment and that Hermes is a recent build that ships `tui_gateway`.

**`/v1/chat` returns `409 Conflict`**
An unresolved gate is pending for this chat. Resolve it via `/v1/gate/resolve` or by typing one of the option names as your next message.

**Daemon won't start on Windows**
Native Windows is not supported. Use WSL, a Linux VM, or macOS.

## Contributing

Issues and PRs welcome at [github.com/roormonger/hermes-chat](https://github.com/roormonger/hermes-chat).

## License

MIT — see [LICENSE](LICENSE).