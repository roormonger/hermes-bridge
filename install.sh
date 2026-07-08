#!/usr/bin/env bash
# One-line installer for hermes-bridge.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/roormonger/hermes-bridge/main/install.sh | bash
#
# The installer:
#   - clones or updates ~/.hermes-bridge
#   - creates a Python venv and installs requirements
#   - searches common Hermes installation locations
#   - installs a `hermes-bridge` launcher in ~/.local/bin
#   - optionally installs a systemd user service on Linux

set -euo pipefail

REPO_URL="https://github.com/roormonger/hermes-bridge.git"
INSTALL_DIR="${HERMES_BRIDGE_DIR:-$HOME/.hermes-bridge}"
BIN_DIR="${HERMES_BRIDGE_BIN_DIR:-$HOME/.local/bin}"
SERVICE_NAME="hermes-bridge"

PYTHON_MIN="3.10"

# --- helpers -----------------------------------------------------------------

info() { printf '\033[1;34m[info]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ok]\033[0m   %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m[err]\033[0m  %s\n' "$*" >&2; exit 1; }

cmd_exists() { command -v "$1" >/dev/null 2>&1; }

version_ge() {
    # returns 0 if $1 >= $2
    printf '%s\n%s\n' "$2" "$1" | sort -r -V -C
}

# --- preflight ---------------------------------------------------------------

if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
    err "hermes-bridge requires Linux, macOS, or WSL. It cannot run natively on Windows."
fi

if ! cmd_exists python3 && ! cmd_exists python; then
    err "Python is required. Install Python ${PYTHON_MIN}+ and try again."
fi

PYTHON="$(command -v python3 2>/dev/null || command -v python 2>/dev/null)"
PY_VERSION="$($PYTHON --version 2>&1 | awk '{print $2}')"
if ! version_ge "$PY_VERSION" "$PYTHON_MIN"; then
    err "Python ${PYTHON_MIN}+ is required. Found ${PY_VERSION}."
fi
ok "Python ${PY_VERSION} found"

if ! cmd_exists git; then
    err "git is required. Install git and try again."
fi

# --- install / update source -------------------------------------------------

if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Updating existing installation in ${INSTALL_DIR}"
    git -C "$INSTALL_DIR" fetch origin
    git -C "$INSTALL_DIR" reset --hard "origin/$(git -C "$INSTALL_DIR" rev-parse --abbrev-ref HEAD)"
else
    info "Cloning hermes-bridge into ${INSTALL_DIR}"
    rm -rf "$INSTALL_DIR"
    git clone "$REPO_URL" "$INSTALL_DIR"
fi
ok "Source ready at ${INSTALL_DIR}"

# --- virtualenv + dependencies ----------------------------------------------

if [[ ! -d "$INSTALL_DIR/.venv" ]]; then
    info "Creating Python virtual environment"
    "$PYTHON" -m venv "$INSTALL_DIR/.venv"
fi

info "Installing Python dependencies"
"$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
ok "Dependencies installed"

# --- discover Hermes ---------------------------------------------------------

HERMES_CANDIDATES=(
    "$HERMES_BIN"
    "hermes"
    "$HOME/.local/bin/hermes"
    "$HOME/.hermes/hermes-agent/venv/bin/hermes"
    "/opt/homebrew/bin/hermes"
    "/usr/local/bin/hermes"
    "/usr/bin/hermes"
)

HERMES_FOUND=""
for candidate in "${HERMES_CANDIDATES[@]}"; do
    if [[ -n "$candidate" ]] && cmd_exists "$candidate"; then
        HERMES_FOUND="$candidate"
        break
    fi
done

if [[ -n "$HERMES_FOUND" ]]; then
    ok "Hermes found: ${HERMES_FOUND}"
else
    warn "Hermes not found on PATH or in common install locations."
    warn "Install Hermes Agent, then either put 'hermes' on PATH or set HERMES_BIN before running the bridge."
fi

# --- install launcher --------------------------------------------------------

mkdir -p "$BIN_DIR"

cat > "$BIN_DIR/hermes-bridge" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export PATH="\${PATH}:${INSTALL_DIR}/.venv/bin"
${HERMES_FOUND:+export HERMES_BIN="$HERMES_FOUND"}
cd "$INSTALL_DIR"
exec uvicorn bridge.main:app "\$@"
EOF

chmod +x "$BIN_DIR/hermes-bridge"
ok "Launcher installed to ${BIN_DIR}/hermes-bridge"

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    warn "${BIN_DIR} is not on your PATH. Add this to your shell profile:"
    warn "    export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# --- optional systemd user service -------------------------------------------

if [[ "$OSTYPE" == "linux"* ]] && cmd_exists systemctl; then
    printf '\n'
    read -r -p "Install systemd user service so hermes-bridge starts on login? [y/N] " reply </dev/tty || true
    if [[ "$reply" =~ ^[Yy]$ ]]; then
        mkdir -p "$HOME/.config/systemd/user"
        cat > "$HOME/.config/systemd/user/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=hermes-bridge FastAPI service
After=network.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
Environment="PATH=${INSTALL_DIR}/.venv/bin:/usr/local/bin:/usr/bin"
${HERMES_FOUND:+Environment="HERMES_BIN=${HERMES_FOUND}"}
ExecStart=${INSTALL_DIR}/.venv/bin/uvicorn bridge.main:app --host 0.0.0.0 --port 8000
Restart=on-failure

[Install]
WantedBy=default.target
EOF
        systemctl --user daemon-reload
        systemctl --user enable "$SERVICE_NAME"
        ok "Systemd user service installed. Run: systemctl --user start ${SERVICE_NAME}"
    fi
fi

# --- summary -----------------------------------------------------------------

printf '\n'
ok "hermes-bridge is installed."
info "Start it with:"
printf '    \033[1mhermes-bridge --host 0.0.0.0 --port 8000\033[0m\n'
info "Verify:"
printf '    \033[1mcurl http://localhost:8000/healthz\033[0m\n'
if [[ -z "$HERMES_FOUND" ]]; then
    warn "Hermes was not auto-detected. Install it and set HERMES_BIN if needed."
fi
