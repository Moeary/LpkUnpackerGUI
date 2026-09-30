"""Client launch configuration for the local animation editing service."""
from __future__ import annotations

import os
from pathlib import Path
import sys

from app.paths import PROJECT_ROOT


def animation_mcp_config(workspace: str | Path) -> dict:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("MCP workspace must be an existing directory")
    server = {"command": sys.executable, "args": []}
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        server["args"] = ["--mcp-animation", "--workspace", str(root)]
    else:
        server["args"] = ["-m", "app.mcp_server", "--workspace", str(root)]
        paths = [str(PROJECT_ROOT)]
        if os.environ.get("PYTHONPATH"):
            paths.append(os.environ["PYTHONPATH"])
        server["env"] = {"PYTHONPATH": os.pathsep.join(paths)}
    return {"mcpServers": {"live2d-spine-animation": server}}
