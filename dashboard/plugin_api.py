"""Dashboard backend API for hermes-bridge.

Mounted by Hermes under ``/api/plugins/hermes-bridge/``. All routes here run
inside the Hermes dashboard server process and manipulate the bridge daemon via
``bridge.daemon``.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# Make sure the plugin root is importable regardless of how Hermes loads this file.
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

_ERROR_LOG = _PLUGIN_ROOT / "run" / "dashboard-api-error.log"

from bridge.config import auth_secret, load_config, update_config
from bridge.daemon import is_running, logs, restart, start, status, stop
from bridge.dependencies import check_dependencies, install_dependencies

router = APIRouter()


def _handle_exc(exc: Exception) -> None:
    """Log the full traceback and raise a 500 with the error summary."""
    _ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    _ERROR_LOG.write_text(traceback.format_exc(), encoding="utf-8")
    raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


class ConfigUpdate(BaseModel):
    host: Optional[str] = None
    port: Optional[int] = None
    hermes_bin: Optional[str] = None
    session_idle_timeout: Optional[float] = None
    gate_idle_threshold: Optional[float] = None
    log_level: Optional[str] = None
    debug: Optional[bool] = None
    auto_start: Optional[bool] = None
    restart: bool = False


@router.get("/status")
async def get_status() -> dict:
    try:
        return status()
    except Exception as exc:
        _handle_exc(exc)


@router.post("/start")
async def post_start() -> dict:
    try:
        return start()
    except Exception as exc:
        _handle_exc(exc)


@router.post("/stop")
async def post_stop() -> dict:
    try:
        return stop()
    except Exception as exc:
        _handle_exc(exc)


@router.post("/restart")
async def post_restart() -> dict:
    try:
        return restart()
    except Exception as exc:
        _handle_exc(exc)


@router.get("/config")
async def get_config() -> dict:
    try:
        return load_config().to_dict()
    except Exception as exc:
        _handle_exc(exc)


@router.post("/config")
async def post_config(body: ConfigUpdate) -> dict:
    try:
        updates = {k: v for k, v in body.model_dump().items() if v is not None and k != "restart"}
        cfg = update_config(updates)
        result = {"status": "configured", "config": cfg.to_dict()}
        if body.restart:
            result.update(restart())
        return result
    except Exception as exc:
        _handle_exc(exc)


@router.get("/logs")
async def get_logs(tail: int = 100) -> dict:
    try:
        return {"lines": tail, "log": logs(tail)}
    except Exception as exc:
        _handle_exc(exc)


@router.get("/deps")
async def get_deps() -> dict:
    try:
        missing = check_dependencies()
        return {"missing": missing, "ok": not missing}
    except Exception as exc:
        _handle_exc(exc)


@router.post("/install-deps")
async def post_install_deps() -> dict:
    try:
        return install_dependencies(auto=True)
    except Exception as exc:
        _handle_exc(exc)


class CreateUserRequest(BaseModel):
    username: str
    password: str


def _user_store() -> UserStore:
    """Lazy-load UserStore so missing bcrypt doesn't break dashboard import."""
    from bridge.users import UserStore

    return UserStore(secret=auth_secret())


@router.get("/users")
async def get_users() -> dict:
    try:
        store = _user_store()
        return {"users": store.list_users()}
    except Exception as exc:
        _handle_exc(exc)


@router.post("/users")
async def post_user(body: CreateUserRequest) -> dict:
    try:
        store = _user_store()
        user = store.create_user(body.username, body.password)
        return {"status": "created", "user": user}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        _handle_exc(exc)


@router.delete("/users/{user_id}")
async def delete_user(user_id: str) -> dict:
    try:
        store = _user_store()
        deleted = store.delete_user(user_id)
        return {"status": "deleted" if deleted else "not_found", "user_id": user_id}
    except Exception as exc:
        _handle_exc(exc)
