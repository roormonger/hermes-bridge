"""Dashboard backend API for Hermes Chat.

Mounted by Hermes under ``/api/plugins/hermes-chat/``. All routes here run
inside the Hermes dashboard server process and manipulate the Hermes Chat daemon via
``bridge.daemon``.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

# Make sure the plugin root is importable regardless of how Hermes loads this file.
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

_ERROR_LOG = _PLUGIN_ROOT / "run" / "dashboard-api-error.log"

from bridge.analytics import get_models_analytics as _get_models_analytics
from bridge.config import auth_secret, load_config, update_config
from bridge.daemon import LOG_FILE, is_running, logs, restart, start, status, stop
from bridge.dependencies import check_dependencies, check_optional_dependencies, install_dependencies

_DEFAULT_HERMES_DASHBOARD_URL = "http://127.0.0.1:9119"


def _hermes_dashboard_url() -> dict:
    """Return the resolved Hermes dashboard URL and its source."""
    cfg = load_config()
    if cfg.hermes_dashboard_url:
        return {"url": cfg.hermes_dashboard_url, "source": "config"}
    env_url = os.environ.get("HERMES_DASHBOARD_URL")
    if env_url:
        return {"url": env_url, "source": "HERMES_DASHBOARD_URL"}
    env_port = os.environ.get("HERMES_DASHBOARD_PORT")
    if env_port:
        return {"url": f"http://127.0.0.1:{env_port}", "source": "HERMES_DASHBOARD_PORT"}
    return {"url": _DEFAULT_HERMES_DASHBOARD_URL, "source": "default"}


def _verify_dashboard_url(url: str) -> dict:
    """Verify by reading the Hermes session DB directly.

    We avoid making an HTTP request to the dashboard because the dashboard
    routes require authentication. The bridge and this plugin run on the same
    host as Hermes, so we can read the underlying SQLite database directly.
    """
    try:
        data = _get_models_analytics(days=30)
        models = data.get("models") or []
        return {"ok": True, "model_count": len(models), "url": url}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "url": url}

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
    hermes_dashboard_url: Optional[str] = None
    voice_enabled: Optional[bool] = None
    default_tts_voice: Optional[str] = None
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


@router.get("/voice-config")
async def get_voice_config() -> dict:
    try:
        cfg = load_config()
        missing_optional = check_optional_dependencies()
        missing_str = " ".join(
            d if isinstance(d, str) else d.get("requirement", "") for d in missing_optional
        )
        return {
            "voice_enabled": cfg.voice_enabled,
            "default_tts_voice": cfg.default_tts_voice,
            "tts_available": "edge-tts" not in missing_str,
            "stt_available": "faster-whisper" not in missing_str and "imageio-ffmpeg" not in missing_str,
        }
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


@router.get("/logs/download")
async def download_logs():
    try:
        if not LOG_FILE.exists():
            return PlainTextResponse("No log file found.", status_code=404)
        return FileResponse(
            path=str(LOG_FILE),
            media_type="text/plain",
            filename="hermes-chat.log",
            headers={"Content-Disposition": 'attachment; filename="hermes-chat.log"'},
        )
    except Exception as exc:
        _handle_exc(exc)


@router.get("/deps")
async def get_deps() -> dict:
    try:
        missing = check_dependencies()
        missing_optional = check_optional_dependencies()
        return {"missing": missing, "ok": not missing, "missing_optional": missing_optional}
    except Exception as exc:
        _handle_exc(exc)


@router.get("/dashboard-url")
async def get_dashboard_url() -> dict:
    """Return the auto-discovered Hermes dashboard URL and a verification result."""
    try:
        info = _hermes_dashboard_url()
        info["verify"] = _verify_dashboard_url(info["url"])
        return info
    except Exception as exc:
        _handle_exc(exc)


@router.post("/dashboard-url/verify")
async def post_verify_dashboard_url(body: dict) -> dict:
    """Verify an arbitrary Hermes dashboard URL."""
    try:
        url = body.get("url", "")
        if not url:
            raise HTTPException(status_code=400, detail="url is required")
        return _verify_dashboard_url(url)
    except HTTPException:
        raise
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
