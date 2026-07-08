"""Dashboard backend API for hermes-bridge.

Mounted by Hermes under ``/api/plugins/hermes-bridge/``. All routes here run
inside the Hermes dashboard server process and manipulate the bridge daemon via
``bridge.daemon``.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from bridge.config import load_config, update_config
from bridge.daemon import is_running, logs, restart, start, status, stop
from bridge.dependencies import check_dependencies, install_dependencies

router = APIRouter()


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
    return status()


@router.post("/start")
async def post_start() -> dict:
    return start()


@router.post("/stop")
async def post_stop() -> dict:
    return stop()


@router.post("/restart")
async def post_restart() -> dict:
    return restart()


@router.get("/config")
async def get_config() -> dict:
    return load_config().to_dict()


@router.post("/config")
async def post_config(body: ConfigUpdate) -> dict:
    updates = {k: v for k, v in body.model_dump().items() if v is not None and k != "restart"}
    cfg = update_config(updates)
    result = {"status": "configured", "config": cfg.to_dict()}
    if body.restart:
        result.update(restart())
    return result


@router.get("/logs")
async def get_logs(tail: int = 100) -> dict:
    return {"lines": tail, "log": logs(tail)}


@router.get("/deps")
async def get_deps() -> dict:
    missing = check_dependencies()
    return {"missing": missing, "ok": not missing}


@router.post("/install-deps")
async def post_install_deps() -> dict:
    return install_dependencies(auto=True)
