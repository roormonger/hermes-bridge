"""Generate Open WebUI function-export JSON files from the .py plugin sources."""

from __future__ import annotations

import json
from pathlib import Path


BASE = Path(__file__).resolve().parent


def build_export(filename: str, func_id: str, name: str, func_type: str, description: str) -> None:
    content = (BASE / filename).read_text(encoding="utf-8")
    export = [
        {
            "id": func_id,
            "user_id": "",
            "name": name,
            "type": func_type,
            "content": content,
            "meta": {"description": description, "manifest": {}},
            "is_active": False,
            "is_global": False,
            "updated_at": 0,
            "created_at": 0,
        }
    ]
    out_path = BASE / filename.replace(".py", ".json")
    out_path.write_text(json.dumps(export, indent=2), encoding="utf-8")
    print(f"Created {out_path}")


if __name__ == "__main__":
    build_export(
        "pipe_plugin.py",
        "hermes_bridge_pipe",
        "Hermes Bridge",
        "pipe",
        "Routes chat turns through hermes-bridge, translating Hermes TUI decision gates into Open WebUI UI elements.",
    )
    build_export(
        "action_plugin.py",
        "hermes_gate_resolver",
        "Hermes Gate Resolver",
        "action",
        "Resolves a pending Hermes TUI decision gate via the hermes-bridge API.",
    )
