"""Hermes plugin package entry point.

Hermes discovers the plugin via plugin.yaml and then loads ``__init__.py``.
Because the plugin directory name contains a hyphen, we load ``plugin.py``
with importlib instead of relying on normal package-relative imports.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_plugin_path = Path(__file__).resolve().parent / "plugin.py"
_spec = importlib.util.spec_from_file_location("hermes_chat_plugin", _plugin_path)
_plugin_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_plugin_module)

register = _plugin_module.register

__all__ = ["register"]
