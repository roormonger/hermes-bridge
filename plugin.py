"""Hermes plugin entry point for hermes-chat.

This file is loaded by Hermes when the plugin is discovered. It registers:
  - A CLI command tree under `hermes hermes-chat ...`
  - Tools that let Hermes inspect/configure the running daemon
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Make sure the plugin root is importable even if Hermes does not add it to
# sys.path automatically.
_PLUGIN_ROOT = Path(__file__).resolve().parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from bridge.config import auth_secret, load_config, update_config
from bridge.daemon import is_running, logs, restart, start, status, stop
from bridge.dependencies import check_dependencies, install_dependencies


def _setup_argparse(subparser):
    subs = subparser.add_subparsers(dest="hermes_bridge_command")
    subs.add_parser("start", help="Start the Hermes Chat daemon")
    subs.add_parser("stop", help="Stop the Hermes Chat daemon")
    subs.add_parser("restart", help="Restart the Hermes Chat daemon")
    subs.add_parser("status", help="Show Hermes Chat daemon status")
    subs.add_parser("install-deps", help="Install missing Hermes Chat Python dependencies")
    logs_parser = subs.add_parser("logs", help="Show recent Hermes Chat logs")
    logs_parser.add_argument("--tail", type=int, default=50, help="Number of log lines")

    test_parser = subs.add_parser("test-gates", help="Run gate-detection dev tests")
    test_parser.add_argument("-v", "--verbose", action="store_true", help="Print each passing test")

    cfg_parser = subs.add_parser("configure", help="Write bridge config values")
    cfg_parser.add_argument("--port", type=int, help="HTTP port")
    cfg_parser.add_argument("--host", type=str, help="HTTP host")
    cfg_parser.add_argument("--hermes-bin", type=str, help="Path to the hermes binary")
    cfg_parser.add_argument(
        "--session-idle-timeout", type=float, help="Seconds before an idle session is reaped"
    )
    cfg_parser.add_argument(
        "--gate-idle-threshold", type=float, help="Seconds to wait for gate prompts"
    )
    cfg_parser.add_argument("--log-level", type=str, help="Log level (DEBUG/INFO/WARNING/ERROR)")
    cfg_parser.add_argument(
        "--debug", type=lambda x: x.lower() in ("1", "true", "yes"), help="Enable verbose PTY/gate-detection logging"
    )
    cfg_parser.add_argument(
        "--auto-start", type=lambda x: x.lower() in ("1", "true", "yes"), help="Auto-start daemon"
    )
    cfg_parser.add_argument(
        "--restart", action="store_true", help="Restart daemon after writing config"
    )

    users_parser = subs.add_parser("users", help="Manage chat UI users")
    users_subs = users_parser.add_subparsers(dest="hermes_bridge_users_command")
    users_subs.add_parser("list", help="List chat UI users")
    add_parser = users_subs.add_parser("add", help="Add a chat UI user")
    add_parser.add_argument("--username", required=True, help="Username for the new user")
    add_parser.add_argument("--password", required=True, help="Password for the new user")
    del_parser = users_subs.add_parser("delete", help="Delete a chat UI user")
    del_parser.add_argument("--user-id", required=True, help="User ID to delete")


def _handle_cli(args) -> None:
    cmd = getattr(args, "hermes_bridge_command", None)
    if cmd is None:
        print("Usage: hermes hermes-chat {start|stop|restart|status|logs|configure|test-gates}")
        return

    if cmd == "start":
        print(json.dumps(_start_with_deps(), indent=2))
    elif cmd == "stop":
        print(json.dumps(stop(), indent=2))
    elif cmd == "restart":
        print(json.dumps(_restart_with_deps(), indent=2))
    elif cmd == "status":
        print(json.dumps(status(), indent=2))
    elif cmd == "install-deps":
        print(json.dumps(install_dependencies(auto=True), indent=2))
    elif cmd == "logs":
        print(logs(getattr(args, "tail", 50)))
    elif cmd == "test-gates":
        try:
            from scripts.test_gate_detection import run_tests
        except ImportError as exc:
            print(json.dumps({"error": f"Cannot import gate-detection tests: {exc}"}, indent=2))
            return
        failures = run_tests(verbose=getattr(args, "verbose", False))
        print(json.dumps({"failures": failures, "ok": failures == 0}, indent=2))
    elif cmd == "users":
        print(_handle_users_cli(args))
    elif cmd == "configure":
        updates: dict[str, Any] = {}
        if args.port is not None:
            updates["port"] = args.port
        if args.host is not None:
            updates["host"] = args.host
        if args.hermes_bin is not None:
            updates["hermes_bin"] = args.hermes_bin
        if args.session_idle_timeout is not None:
            updates["session_idle_timeout"] = args.session_idle_timeout
        if args.gate_idle_threshold is not None:
            updates["gate_idle_threshold"] = args.gate_idle_threshold
        if args.log_level is not None:
            updates["log_level"] = args.log_level.upper()
        if hasattr(args, "debug") and args.debug is not None:
            updates["debug"] = args.debug
        if hasattr(args, "auto_start") and args.auto_start is not None:
            updates["auto_start"] = args.auto_start
        cfg = update_config(updates)
        print(json.dumps({"status": "configured", "config": cfg.to_dict()}, indent=2))
        if args.restart:
            print(json.dumps(restart(), indent=2))


# --------------------------------------------------------------------------- #
# Hermes tools
# --------------------------------------------------------------------------- #

def _schema(name: str, description: str, params: dict, required: list[str] | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": params, "required": required if required is not None else list(params.keys())},
    }


def register(ctx) -> None:
    """Called by Hermes during plugin discovery."""
    import traceback

    _PLUGIN_ROOT = Path(__file__).resolve().parent
    _ERROR_LOG = _PLUGIN_ROOT / "run" / "register-error.log"

    try:
        _do_register(ctx)
    except Exception:
        _ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        _ERROR_LOG.write_text(traceback.format_exc(), encoding="utf-8")
        raise


def _do_register(ctx) -> None:
    """Actual registration logic."""
    ctx.register_cli_command(
        name="hermes-chat",
        help="Manage the Hermes Chat daemon",
        setup_fn=_setup_argparse,
        handler_fn=_handle_cli,
    )

    ctx.register_tool(
        name="hermes_bridge_configure",
        toolset="hermes_bridge",
        schema=_schema(
            "hermes_bridge_configure",
            "Change Hermes Chat runtime configuration. Only server-side settings can be changed; Open WebUI settings must be edited in Open WebUI.",
            {
                "port": {"type": "integer", "description": "HTTP port for the bridge server"},
                "host": {"type": "string", "description": "HTTP host for the bridge server"},
                "hermes_bin": {"type": "string", "description": "Absolute path to the hermes CLI binary"},
                "session_idle_timeout": {"type": "number", "description": "Seconds before idle Hermes PTY sessions are torn down"},
                "gate_idle_threshold": {"type": "number", "description": "Seconds to wait for a gate prompt to settle"},
                "log_level": {"type": "string", "description": "Server log level (DEBUG/INFO/WARNING/ERROR)"},
                "debug": {"type": "boolean", "description": "Enable verbose PTY/gate-detection logging"},
                "restart": {"type": "boolean", "description": "Restart the daemon to apply changes"},
            },
        ),
        handler=_tool_configure,
    )

    ctx.register_tool(
        name="hermes_bridge_status",
        toolset="hermes_bridge",
        schema=_schema(
            "hermes_bridge_status",
            "Check whether the Hermes Chat daemon is running and healthy.",
            {},
        ),
        handler=_tool_status,
    )

    ctx.register_tool(
        name="hermes_bridge_restart",
        toolset="hermes_bridge",
        schema=_schema(
            "hermes_bridge_restart",
            "Restart the Hermes Chat daemon to pick up configuration changes.",
            {},
        ),
        handler=_tool_restart,
    )

    ctx.register_tool(
        name="hermes_bridge_install_dependencies",
        toolset="hermes_bridge",
        schema=_schema(
            "hermes_bridge_install_dependencies",
            "Install the Python packages required by Hermes Chat into the Hermes environment if any are missing.",
            {},
        ),
        handler=_tool_install_dependencies,
    )

    ctx.register_tool(
        name="hermes_bridge_users",
        toolset="hermes_bridge",
        schema=_schema(
            "hermes_bridge_users",
            "Manage users for the standalone Hermes chat UI. The Hermes admin is the chat admin. Use action=list to see users, action=add to create a user (requires username and password), action=delete to remove a user (requires user_id).",
            {
                "action": {
                    "type": "string",
                    "enum": ["list", "add", "delete"],
                    "description": "Action to perform: list users, add a user, or delete a user",
                },
                "username": {"type": "string", "description": "Username for add action"},
                "password": {"type": "string", "description": "Password for add action"},
                "user_id": {"type": "string", "description": "User ID for delete action"},
            },
            required=["action"],
        ),
        handler=_tool_users,
    )

    # Auto-install missing dependencies on every plugin load so the first
    # `hermes plugins install` just works without a separate install-deps step.
    missing = check_dependencies()
    if missing:
        print(f"[hermes-chat] Installing missing dependencies: {', '.join(missing)} ...")
        result = install_dependencies(auto=True)
        if result.get("status") == "error":
            print(
                f"[hermes-chat] Dependency install failed: {result.get('message')}\n"
                "Run `hermes hermes-chat install-deps` to retry manually."
            )
            missing = check_dependencies()  # re-check in case partial install succeeded
        else:
            print("[hermes-chat] Dependencies installed successfully.")
            missing = []

    # Auto-start on plugin load if configured. This is best-effort; Hermes
    # plugins are loaded during CLI startup, so the daemon survives beyond
    # the hermes command because we spawn it detached.
    try:
        cfg = load_config()
        if cfg.auto_start and not is_running() and not missing:
            start()
    except Exception:
        pass


def _tool_configure(args: dict) -> str:
    updates: dict[str, Any] = {}
    for key in [
        "port",
        "host",
        "hermes_bin",
        "session_idle_timeout",
        "gate_idle_threshold",
        "log_level",
    ]:
        if key in args and args[key] is not None:
            updates[key] = args[key]
    cfg = update_config(updates)
    result = {"status": "configured", "config": cfg.to_dict()}
    if args.get("restart"):
        result["restart"] = restart()
    return json.dumps(result, indent=2)


def _tool_status(_args: dict) -> str:
    return json.dumps(status(), indent=2)


def _tool_restart(_args: dict) -> str:
    return json.dumps(_restart_with_deps(), indent=2)


def _tool_install_dependencies(_args: dict) -> str:
    return json.dumps(install_dependencies(auto=True), indent=2)


def _user_store() -> Any:
    """Return a UserStore instance (lazy import to avoid bcrypt on load)."""
    try:
        from bridge.users import UserStore
    except ImportError as exc:
        raise RuntimeError(
            "Auth dependencies (bcrypt/pyjwt) are not installed. "
            "Run `hermes hermes-chat install-deps` and try again."
        ) from exc

    return UserStore(secret=auth_secret())


def _handle_users_cli(args) -> str:
    """CLI handler for `hermes hermes-bridge users ...`."""
    sub = getattr(args, "hermes_bridge_users_command", None)
    store = _user_store()
    if sub == "list":
        users = store.list_users()
        return json.dumps({"users": users}, indent=2)
    if sub == "add":
        user = store.create_user(args.username, args.password)
        return json.dumps({"status": "created", "user": user}, indent=2)
    if sub == "delete":
        deleted = store.delete_user(args.user_id)
        return json.dumps({"status": "deleted" if deleted else "not_found", "user_id": args.user_id}, indent=2)
    return json.dumps({"error": "Usage: hermes hermes-bridge users {list|add|delete}"}, indent=2)


def _tool_users(args: dict) -> str:
    """Hermes tool handler for managing chat UI users."""
    action = args.get("action")
    store = _user_store()
    try:
        if action == "list":
            users = store.list_users()
            return json.dumps({"users": users}, indent=2)
        if action == "add":
            user = store.create_user(args["username"], args["password"])
            return json.dumps({"status": "created", "user": user}, indent=2)
        if action == "delete":
            deleted = store.delete_user(args["user_id"])
            return json.dumps({"status": "deleted" if deleted else "not_found", "user_id": args["user_id"]}, indent=2)
        return json.dumps({"error": f"Unknown action: {action}"}, indent=2)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, indent=2)


def _start_with_deps() -> dict:
    missing = check_dependencies()
    if missing:
        return {
            "status": "missing_dependencies",
            "missing": missing,
            "message": "Run `hermes hermes-chat install-deps` before starting the daemon.",
        }
    return start()


def _restart_with_deps() -> dict:
    missing = check_dependencies()
    if missing:
        return {
            "status": "missing_dependencies",
            "missing": missing,
            "message": "Run `hermes hermes-chat install-deps` before restarting the daemon.",
        }
    return restart()
