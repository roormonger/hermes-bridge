"""Background daemon lifecycle for hermes-bridge.

Provides start, stop, status, and a self-healing watchdog. The daemon runs
uvicorn as a subprocess and polls /healthz; if health fails it restarts the
subprocess automatically.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import BridgeConfig, data_dir, effective_hermes_bin, load_config, _plugin_dir

PID_FILE = data_dir() / "hermes-bridge.pid"
LOCK_FILE = data_dir() / "hermes-bridge.start.lock"
LOG_FILE = data_dir() / "hermes-bridge.log"
WATCHDOG_INTERVAL = 5
WATCHDOG_FAIL_THRESHOLD = 3


def _start_lock_held_by_alive_process() -> bool:
    if not LOCK_FILE.exists():
        return False
    try:
        pid = int(LOCK_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _acquire_start_lock() -> bool:
    if _start_lock_held_by_alive_process():
        return False
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def _release_start_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def _read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except ValueError:
        return None


def is_running() -> bool:
    pid = _read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False


def status() -> dict:
    pid = _read_pid()
    running = is_running()
    healthy = False
    config = load_config()
    if running:
        try:
            url = f"http://{config.host}:{config.port}/healthz"
            with urllib.request.urlopen(url, timeout=2) as resp:
                healthy = resp.status == 200
        except Exception:
            healthy = False
    return {"running": running, "pid": pid, "healthy": healthy, "config": config.to_dict()}


def _start_uvicorn(config: BridgeConfig) -> subprocess.Popen:
    env = os.environ.copy()
    env["HERMES_BIN"] = effective_hermes_bin(config)
    plugin_dir = _plugin_dir()
    if plugin_dir is not None:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(plugin_dir) + (os.pathsep + existing if existing else "")

    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "bridge.main:app",
        "--host",
        config.host,
        "--port",
        str(config.port),
        "--log-level",
        config.log_level.lower(),
    ]
    log = open(LOG_FILE, "a", encoding="utf-8")
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": subprocess.STDOUT,
        "env": env,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    elif os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    return subprocess.Popen(cmd, **kwargs)


def _watchdog(config: BridgeConfig, proc: subprocess.Popen) -> None:
    """Monitor the uvicorn child and trigger a restart if it becomes unhealthy."""
    consecutive_failures = 0
    while proc.poll() is None:
        time.sleep(WATCHDOG_INTERVAL)
        if proc.poll() is not None:
            break
        healthy = False
        try:
            url = f"http://{config.host}:{config.port}/healthz"
            with urllib.request.urlopen(url, timeout=2) as resp:
                healthy = resp.status == 200
        except (urllib.error.URLError, Exception):
            healthy = False

        if healthy:
            consecutive_failures = 0
            continue

        consecutive_failures += 1
        if consecutive_failures >= WATCHDOG_FAIL_THRESHOLD:
            try:
                proc.terminate()
            except Exception:
                pass
            break


def _daemon_loop() -> None:
    """Main daemon loop: keep uvicorn running and heal on failure."""
    PID_FILE.write_text(str(os.getpid()))
    config = load_config()

    while True:
        proc = _start_uvicorn(config)
        watcher = threading.Thread(target=_watchdog, args=(config, proc), daemon=True)
        watcher.start()
        proc.wait()
        if proc.returncode == 0:
            break
        # Unexpected exit: reload config in case the user changed it, then restart.
        config = load_config()

    PID_FILE.unlink(missing_ok=True)


def start() -> dict:
    """Start the detached daemon if not already running."""
    if is_running():
        return {"status": "already_running", **status()}

    if not _acquire_start_lock():
        return {"status": "already_running", **status()}

    try:
        cmd = [sys.executable, "-m", "bridge.daemon", "_run"]
        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "posix":
            kwargs["start_new_session"] = True
        elif os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        subprocess.Popen(cmd, **kwargs)
        # Give the daemon a moment to write its PID file.
        for _ in range(10):
            time.sleep(0.1)
            if is_running():
                break
        return {"status": "started", **status()}
    finally:
        _release_start_lock()


def stop() -> dict:
    """Stop the daemon."""
    pid = _read_pid()
    if pid is None:
        return {"status": "not_running", "running": False, "pid": None, "healthy": False}

    try:
        if os.name == "posix":
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass

    PID_FILE.unlink(missing_ok=True)
    return {"status": "stopped", "running": False, "pid": None, "healthy": False}


def restart() -> dict:
    """Restart the daemon."""
    stop()
    # Wait for the old process to exit.
    for _ in range(20):
        if not is_running():
            break
        time.sleep(0.1)
    return start()


def logs(tail: int = 50) -> str:
    """Return the last *tail* lines of the daemon log."""
    if not LOG_FILE.exists():
        return "No logs yet."
    lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[-tail:])


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "_run":
        _daemon_loop()
    else:
        # Allow direct invocation for debugging: python -m bridge.daemon start|stop|status
        if len(sys.argv) <= 1:
            print("Usage: python -m bridge.daemon start|stop|restart|status|logs")
            sys.exit(1)
        action = sys.argv[1]
        if action == "start":
            print(start())
        elif action == "stop":
            print(stop())
        elif action == "restart":
            print(restart())
        elif action == "status":
            print(status())
        elif action == "logs":
            print(logs())
        else:
            print(f"Unknown action: {action}")
            sys.exit(1)
