"""Configuration management for hermes-bridge.

The bridge reads its runtime settings from a YAML config file. When running as a
Hermes plugin the file lives inside the plugin directory
(~/.hermes/plugins/hermes-bridge/config.yaml). The standalone layout uses the
repo root config.yaml.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - optional when Hermes provides it.
    yaml = None  # type: ignore


DEFAULTS: dict[str, Any] = {
    "host": "0.0.0.0",
    "port": 6969,
    "hermes_bin": "hermes",
    "session_idle_timeout": 600,
    "gate_idle_threshold": 0.35,
    "log_level": "INFO",
    "auto_start": True,
    "debug": False,
}


@dataclass
class BridgeConfig:
    host: str
    port: int
    hermes_bin: str
    session_idle_timeout: float
    gate_idle_threshold: float
    log_level: str
    auto_start: bool
    debug: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "hermes_bin": self.hermes_bin,
            "session_idle_timeout": self.session_idle_timeout,
            "gate_idle_threshold": self.gate_idle_threshold,
            "log_level": self.log_level,
            "auto_start": self.auto_start,
            "debug": self.debug,
        }


def _plugin_dir() -> Path | None:
    """Return the Hermes plugin directory if we are running inside one."""
    path_str = os.environ.get("HERMES_PLUGIN_DIR")
    if path_str:
        return Path(path_str)
    # Best-effort inference from the repo layout.
    candidate = Path(__file__).resolve().parent.parent
    if (candidate / "plugin.yaml").exists():
        return candidate
    return None


def config_path() -> Path:
    """Return the active config file path."""
    plugin = _plugin_dir()
    if plugin is not None:
        return plugin / "config.yaml"
    return Path(__file__).resolve().parent.parent / "config.yaml"


def data_dir() -> Path:
    """Return a writable directory for PID files, logs, and the database."""
    plugin = _plugin_dir()
    if plugin is not None:
        d = plugin / "run"
    else:
        d = Path(__file__).resolve().parent.parent / "run"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_config(path: Path | None = None) -> BridgeConfig:
    """Load config from disk, merging with defaults."""
    cfg: dict[str, Any] = dict(DEFAULTS)
    target = path or config_path()
    if target.exists() and yaml is not None:
        try:
            with open(target, "r", encoding="utf-8") as f:
                cfg.update(yaml.safe_load(f) or {})
        except Exception:
            pass
    return BridgeConfig(**cfg)


def save_config(config: BridgeConfig, path: Path | None = None) -> None:
    """Persist config to disk."""
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if yaml is None:
        raise RuntimeError("PyYAML is required to write config")
    with open(target, "w", encoding="utf-8") as f:
        yaml.safe_dump(config.to_dict(), f, sort_keys=False)


def update_config(updates: dict[str, Any], path: Path | None = None) -> BridgeConfig:
    """Update specific config keys and persist."""
    cfg = load_config(path)
    data = cfg.to_dict()
    data.update(updates)
    new_cfg = BridgeConfig(**data)
    save_config(new_cfg, path)
    return new_cfg


def effective_hermes_bin(config: BridgeConfig) -> str:
    """Return the Hermes binary path, env var wins over config."""
    return os.environ.get("HERMES_BIN", config.hermes_bin)


def ensure_hermes_bin(config: BridgeConfig) -> str:
    """Return the resolved Hermes binary or raise a clear error."""
    binary = effective_hermes_bin(config)
    resolved = shutil.which(binary)
    if resolved:
        return resolved
    raise RuntimeError(
        f"Hermes binary not found: {binary!r}. "
        "Install Hermes on PATH or set HERMES_BIN."
    )
