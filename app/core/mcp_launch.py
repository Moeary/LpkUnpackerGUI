"""Client launch configuration for the local animation editing service."""
from __future__ import annotations

import os
import json
from pathlib import Path
import socket
import sys
from typing import Literal

from app.paths import ASSETS_DIR, PROJECT_ROOT


DEFAULT_MCP_PORT = 8765
MCP_HOST = "127.0.0.1"
ANIMATION_GUIDE_URI = "lpk-animation://guide"
MCPTransport = Literal["stdio", "streamable-http"]


def validate_mcp_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("MCP port must be an integer between 1 and 65535")
    return port


def animation_mcp_endpoint(port: int = DEFAULT_MCP_PORT) -> str:
    return f"http://{MCP_HOST}:{validate_mcp_port(port)}/mcp"


def _workspace(workspace: str | Path) -> Path:
    if not str(workspace).strip():
        raise ValueError("Choose an existing MCP output workspace")
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("MCP workspace must be an existing directory")
    return root


def bind_animation_mcp_port(port: int) -> socket.socket:
    """Reserve the loopback listener without a check-then-bind race."""
    port = validate_mcp_port(port)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind((MCP_HOST, port))
        listener.listen(128)
        listener.setblocking(False)
        return listener
    except OSError as exc:
        listener.close()
        raise ValueError(f"Cannot listen on {MCP_HOST}:{port}: port is in use or unavailable ({exc})") from exc


def animation_mcp_launch(
    workspace: str | Path, *, transport: MCPTransport = "stdio",
    port: int = DEFAULT_MCP_PORT, managed: bool = False,
) -> dict:
    """Return the actual interpreter/EXE command for a source or packaged app."""
    root = _workspace(workspace)
    if transport not in ("stdio", "streamable-http"):
        raise ValueError("MCP transport must be stdio or streamable-http")
    validate_mcp_port(port)
    packaged = bool(getattr(sys, "frozen", False) or "__compiled__" in globals())
    executable = Path(sys.executable)
    if not packaged and executable.name.lower() == "pythonw.exe":
        console_python = executable.with_name("python.exe")
        if console_python.is_file():
            executable = console_python
    server = {"command": str(executable), "args": []}
    if packaged:
        server["args"] = ["--mcp-animation", "--workspace", str(root)]
    else:
        server["args"] = ["-m", "app.mcp_server", "--workspace", str(root)]
        paths = [str(PROJECT_ROOT)]
        if os.environ.get("PYTHONPATH"):
            paths.append(os.environ["PYTHONPATH"])
        server["env"] = {"PYTHONPATH": os.pathsep.join(paths)}
    if transport == "streamable-http":
        server["args"].extend(["--transport", transport, "--port", str(port)])
        if managed:
            server["args"].append("--managed")
    elif managed:
        raise ValueError("The application manages HTTP only; stdio belongs to its MCP client")
    return server


def animation_mcp_config(
    workspace: str | Path, *, transport: MCPTransport = "stdio", port: int = DEFAULT_MCP_PORT,
) -> dict:
    launch = animation_mcp_launch(workspace, transport=transport, port=port)
    server = {"type": "http", "url": animation_mcp_endpoint(port)} if transport == "streamable-http" else launch
    return {"mcpServers": {"live2d-spine-animation": server}}


def animation_mcp_guide(
    workspace: str | Path, *, transport: MCPTransport = "stdio", port: int = DEFAULT_MCP_PORT,
    tool_schemas: list[dict] | None = None,
) -> str:
    """Render portable Markdown with actual connection paths and live schemas."""
    root = _workspace(workspace)
    configuration = animation_mcp_config(root, transport=transport, port=port)
    if tool_schemas is None:
        import anyio
        from app.mcp_server import create_server

        server = create_server(root, transport=transport, port=port)

        async def schemas():
            return [tool.model_dump(mode="json", exclude_none=True) for tool in await server.list_tools()]

        tool_schemas = anyio.run(schemas)
    connection = {
        "transport": transport,
        "endpoint": animation_mcp_endpoint(port) if transport == "streamable-http" else None,
        "output_workspace": str(root),
        "guide_resource": ANIMATION_GUIDE_URI,
        "launch": animation_mcp_launch(root, transport=transport, port=port),
    }
    template = (ASSETS_DIR / "docs" / "animation-ai-guide.md").read_text(encoding="utf-8")
    return template.replace(
        "{{GUIDE_STATUS}}", "This generated guide contains the selected connection, absolute output workspace, actual launch paths and registered tool schemas. The server must be running before an HTTP client can connect.",
    ).replace(
        "{{CONNECTION}}", json.dumps(connection, ensure_ascii=False, indent=2),
    ).replace(
        "{{CONFIGURATION}}", json.dumps(configuration, ensure_ascii=False, indent=2),
    ).replace(
        "{{TOOL_SCHEMAS}}", json.dumps(tool_schemas, ensure_ascii=False, indent=2),
    )
