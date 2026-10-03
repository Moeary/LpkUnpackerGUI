"""Local stdio / loopback HTTP MCP for independent animation packages."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import logging
import os
import sys
import threading
import uuid
import socket
from pathlib import Path
from typing import Annotated, Any, Iterator, Literal, Sequence, TextIO

import anyio
from mcp.server.stdio import stdio_server
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import Annotations, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from app.core.mcp_launch import (
    ANIMATION_GUIDE_URI, DEFAULT_MCP_PORT, MCP_HOST,
    animation_mcp_endpoint, animation_mcp_guide, bind_animation_mcp_port, validate_mcp_port,
)


JsonObject = dict[str, Any]
FiniteNumber = Annotated[float, Field(strict=True, allow_inf_nan=False)]


class AnimationKeyframe(BaseModel):
    """A parameter value or a Spine bone transform at one time in seconds."""

    model_config = ConfigDict(extra="forbid")

    time: Annotated[FiniteNumber, Field(ge=0, description="Time in seconds, within the animation duration.")]
    value: Annotated[FiniteNumber | None, Field(description="Live2D parameter value.")] = None
    x: Annotated[FiniteNumber | None, Field(description="Spine translate or scale X value.")] = None
    y: Annotated[FiniteNumber | None, Field(description="Spine translate or scale Y value.")] = None
    angle: Annotated[FiniteNumber | None, Field(description="Spine rotation angle in degrees.")] = None
    interpolation: Annotated[
        Literal["linear", "stepped"],
        Field(description="Interpolation from this frame to the next frame."),
    ] = "linear"


class AnimationMCPService:
    """Keep editable projects in memory and restrict all output to one directory."""

    MAX_PROJECTS = 32

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("--workspace must be an existing directory")
        self._projects: dict[str, Any] = {}
        self._lock = threading.RLock()

    def input_path(self, path: str) -> Path:
        if not path.strip():
            raise ToolError("Input path must not be empty")
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        return candidate.resolve(strict=True)

    def output_path(self, output_dir: str) -> Path:
        if not output_dir.strip():
            raise ToolError("output_dir must name a new directory inside --workspace")
        candidate = Path(output_dir).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        # lexists also detects dangling links, which must never be reused.
        if os.path.lexists(candidate):
            raise ToolError("Output already exists; choose a new directory name")
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise ToolError("Output must remain inside --workspace, including resolved links") from exc
        return resolved

    def project(self, project_id: str) -> Any:
        try:
            return self._projects[project_id]
        except KeyError as exc:
            raise ToolError("Unknown project_id; call create_project in this server session first") from exc

    def summary(self, project_id: str) -> JsonObject:
        return {"project_id": project_id, "model": self.project(project_id).inspect()}


def _tool_annotations(*, read_only: bool = False, idempotent: bool = False) -> ToolAnnotations:
    return ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=False,
        idempotentHint=idempotent,
        openWorldHint=False,
    )


def create_server(
    workspace: str | Path, *, log_level: str = "WARNING",
    transport: Literal["stdio", "streamable-http"] = "stdio", port: int = DEFAULT_MCP_PORT,
) -> FastMCP:
    """Build the same tools and guide for both local transports."""
    from app.core.animation_editing import (
        AnimationEditingError,
        create_animation_project,
        inspect_animation_model,
    )

    service = AnimationMCPService(workspace)
    if transport not in ("stdio", "streamable-http"):
        raise ValueError("MCP transport must be stdio or streamable-http")
    validate_mcp_port(port)
    server = FastMCP(
        "LpkUnpacker Animation",
        instructions=(
            "Edit Live2D parameter motions and Spine bone animations using local files. "
            "Inspect the model, create an in-memory project, create an animation, set its "
            "keyframes, and save an independent package. Source files are not modified. "
            "All outputs must be new directories inside the configured workspace. "
            "Relative input and output paths are resolved from that workspace. "
            "Project IDs are valid only for this server process. "
            f"Read the Markdown resource {ANIMATION_GUIDE_URI} for the current connection and schemas."
        ),
        log_level=log_level,
        host=MCP_HOST,
        port=port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=False,
    )

    def invoke(operation: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return operation(*args, **kwargs)
        except (AnimationEditingError, OSError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        description=(
            "Read a Live2D model3.json or a Spine JSON, skel or model wrapper and report parameters, "
            "bones, animations and supported timelines. Optional parameter inventory "
            "must match the Live2D MOC hash; input paths are read only."
        ),
        annotations=_tool_annotations(read_only=True, idempotent=True),
    )
    def inspect_model(
        model_path: Annotated[str, Field(description="Absolute model path, or a path relative to the workspace.")],
        parameter_inventory_path: Annotated[
            str | None,
            Field(description="Optional Live2D parameter inventory JSON file matching this model's MOC hash."),
        ] = None,
    ) -> JsonObject:
        return invoke(
            inspect_animation_model,
            invoke(service.input_path, model_path),
            parameter_inventory_path=(
                invoke(service.input_path, parameter_inventory_path)
                if parameter_inventory_path is not None else None
            ),
        )

    @server.tool(
        description="Load a model into an editable in-memory project; return its project_id and model inventory.",
        annotations=_tool_annotations(),
    )
    def create_project(
        model_path: Annotated[str, Field(description="Absolute model path, or a path relative to the workspace.")],
        parameter_inventory_path: Annotated[
            str | None,
            Field(description="Optional Live2D parameter inventory JSON file matching this model's MOC hash."),
        ] = None,
    ) -> JsonObject:
        with service._lock:
            if len(service._projects) >= service.MAX_PROJECTS:
                raise ToolError("Too many open projects; use close_project before creating another")
            project = invoke(
                create_animation_project,
                invoke(service.input_path, model_path),
                parameter_inventory_path=(
                    invoke(service.input_path, parameter_inventory_path)
                    if parameter_inventory_path is not None else None
                ),
            )
            model = invoke(project.inspect)
            project_id = uuid.uuid4().hex
            service._projects[project_id] = project
            return {"project_id": project_id, "model": model}

    @server.tool(
        description="Inspect an open project's current model inventory and animation timelines.",
        annotations=_tool_annotations(read_only=True, idempotent=True),
    )
    def inspect_project(
        project_id: Annotated[str, Field(description="Project ID returned by create_project in this session.")],
    ) -> JsonObject:
        with service._lock:
            return invoke(service.summary, project_id)

    @server.tool(
        description="Create a named animation in memory with a duration in seconds, looping flag and frame rate.",
        annotations=_tool_annotations(),
    )
    def create_animation(
        project_id: Annotated[str, Field(description="Project ID returned by create_project.")],
        name: Annotated[str, Field(description="New animation name. Existing animation names cannot be overwritten.")],
        duration: Annotated[FiniteNumber, Field(gt=0, description="Animation duration in seconds.")],
        loop: Annotated[bool, Field(strict=True, description="Whether the new animation should loop.")] = False,
        fps: Annotated[FiniteNumber, Field(gt=0, description="Sampling frame rate for the animation.")] = 30,
    ) -> JsonObject:
        with service._lock:
            result = invoke(service.project(project_id).create_animation, name, duration=duration, loop=loop, fps=fps)
            return {"project_id": project_id, "animation": result}

    @server.tool(
        description=(
            "Clone an existing animation under a new name in memory, preserving its other timelines. "
            "Use the inventory's exact animation name; existing Live2D names are Group[index]."
        ),
        annotations=_tool_annotations(),
    )
    def clone_animation(
        project_id: Annotated[str, Field(description="Project ID returned by create_project.")],
        source_name: Annotated[str, Field(description="Exact existing animation name from inspect_project.")],
        new_name: Annotated[str, Field(description="New animation name; existing names cannot be overwritten.")],
    ) -> JsonObject:
        with service._lock:
            result = invoke(service.project(project_id).clone_animation, source_name, new_name)
            return {"project_id": project_id, "animation": result}

    @server.tool(
        description=(
            "Read one timeline's keyframes and editable flag. Existing Bezier or inverse-stepped "
            "timelines require explicit replace; merge preserves existing linear or stepped frames."
        ),
        annotations=_tool_annotations(read_only=True, idempotent=True),
    )
    def get_keyframes(
        project_id: Annotated[str, Field(description="Project ID returned by create_project.")],
        animation_name: Annotated[str, Field(description="Exact animation name from inspect_project.")],
        target: Annotated[str, Field(description="Live2D parameter ID or Spine bone name from model inventory.")],
        channel: Annotated[
            Literal["value", "translate", "rotate", "scale"],
            Field(description="Live2D uses value; Spine uses translate, rotate or scale."),
        ],
    ) -> JsonObject:
        with service._lock:
            return {"project_id": project_id, **invoke(
                service.project(project_id).get_keyframes, animation_name, target, channel,
            )}

    @server.tool(
        description=(
            "Replace or merge one animation timeline's keyframes in memory. Live2D: target is a parameter ID, "
            "channel=value, frames have time/value. Spine: target is a bone name; translate/scale "
            "frames have time/x/y, rotate frames have time/angle. Times are seconds; rotation is degrees. "
            "Spine translation and rotation are offsets from setup pose; scale multiplies setup scale. "
            "Interpolation is linear or stepped. Inspect the model before choosing targets."
        ),
        annotations=_tool_annotations(),
    )
    def set_keyframes(
        project_id: Annotated[str, Field(description="Project ID returned by create_project.")],
        animation_name: Annotated[str, Field(description="Name of an animation in this project.")],
        target: Annotated[str, Field(description="Live2D parameter ID or Spine bone name from model inventory.")],
        channel: Annotated[
            Literal["value", "translate", "rotate", "scale"],
            Field(description="Live2D uses value; Spine uses translate, rotate or scale."),
        ],
        keyframes: Annotated[list[AnimationKeyframe], Field(min_length=1, description="Keyframes in increasing time order.")],
        mode: Annotated[
            Literal["replace", "merge"],
            Field(description="Replace this timeline, or merge by time with existing editable frames. Other timelines are retained."),
        ] = "replace",
    ) -> JsonObject:
        with service._lock:
            result = invoke(
                service.project(project_id).set_keyframes,
                animation_name, target, channel,
                [frame.model_dump(exclude_none=True) for frame in keyframes],
                mode=mode,
            )
            return {"project_id": project_id, **result}

    @server.tool(
        description=(
            "Save an independent playable animation package to a NEW directory inside --workspace. "
            "Include model dependencies and an edit manifest. Existing destinations and source files "
            "are never overwritten. Absolute destinations must also be inside the workspace."
        ),
        annotations=_tool_annotations(),
    )
    def save_package(
        project_id: Annotated[str, Field(description="Project ID returned by create_project.")],
        output_dir: Annotated[str, Field(description="New package directory, preferably relative to the configured workspace.")],
    ) -> JsonObject:
        with service._lock:
            return invoke(service.project(project_id).save_copy, service.output_path(output_dir))

    @server.tool(
        description="Release an in-memory project. Saved packages remain on disk; unsaved edits are discarded.",
        annotations=_tool_annotations(),
    )
    def close_project(
        project_id: Annotated[str, Field(description="Project ID returned by create_project.")],
    ) -> JsonObject:
        with service._lock:
            service.project(project_id)
            del service._projects[project_id]
            return {"project_id": project_id, "closed": True}

    @server.resource(
        ANIMATION_GUIDE_URI,
        name="animation-ai-guide",
        title="Live2D / Spine animation editing guide",
        description="Current connection, output workspace boundary, workflows, runtime limits and live tool schemas.",
        mime_type="text/markdown",
        annotations=Annotations(audience=["assistant"], priority=1.0),
    )
    async def animation_ai_guide() -> str:
        tools = [tool.model_dump(mode="json", exclude_none=True) for tool in await server.list_tools()]
        return animation_mcp_guide(service.workspace, transport=transport, port=port, tool_schemas=tools)

    return server


@contextmanager
def _protocol_stdout() -> Iterator[TextIO]:
    """Keep the protocol pipe private while native and Python diagnostics use stderr."""
    original_stdout = sys.stdout
    original_stdout.flush()
    protocol_fd = os.dup(original_stdout.fileno())
    protocol_output = os.fdopen(protocol_fd, "w", encoding="utf-8", newline="\n", buffering=1)
    try:
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
            kernel32.SetStdHandle.restype = wintypes.BOOL
            if not kernel32.SetStdHandle(-11 & 0xFFFFFFFF, msvcrt.get_osfhandle(sys.stderr.fileno())):
                raise ctypes.WinError(ctypes.get_last_error())
        os.dup2(sys.stderr.fileno(), original_stdout.fileno())
        sys.stdout = sys.stderr
        yield protocol_output
    finally:
        sys.stdout.flush()
        # The CLI process exits after serving. Keep native stdout redirected
        # until exit so any remaining CRT buffers cannot corrupt the pipe.
        protocol_output.close()


async def _run_stdio(server: FastMCP, protocol_output: TextIO) -> None:
    # This is the SDK's FastMCP.run_stdio_async loop with an explicit protocol
    # stream. The private descriptor prevents C printf and stray Python prints
    # from sharing the JSON-RPC pipe; v1 FastMCP's default wrapper does not.
    async with stdio_server(stdout=anyio.wrap_file(protocol_output)) as (read_stream, write_stream):
        await server._mcp_server.run(
            read_stream, write_stream, server._mcp_server.create_initialization_options(),
        )


async def _run_http(server: FastMCP, listener: socket.socket, managed: bool) -> None:
    """Serve the SDK ASGI app using one reserved IPv4 loopback listener."""
    import uvicorn

    configuration = uvicorn.Config(
        server.streamable_http_app(), host=MCP_HOST, port=server.settings.port,
        log_level=server.settings.log_level.lower(), access_log=False,
        loop="asyncio", http="h11", ws="none", lifespan="on",
        timeout_graceful_shutdown=2,
    )
    http_server = uvicorn.Server(configuration)
    if managed:
        # The GUI owns this stdin pipe. EOF also stops the child when its
        # parent exits unexpectedly; raw reads avoid buffered-I/O daemon locks.
        stdin_fd = sys.stdin.fileno()

        def control() -> None:
            pending = b""
            try:
                while not http_server.should_exit:
                    chunk = os.read(stdin_fd, 256)
                    if not chunk:
                        break
                    pending = (pending + chunk)[-512:]
                    if any(line.strip().lower() == b"stop" for line in pending.split(b"\n")):
                        break
                http_server.should_exit = True
            except OSError:
                http_server.should_exit = True

        threading.Thread(target=control, name="mcp-parent-control", daemon=True).start()

    async def announce_ready() -> None:
        while not http_server.started and not http_server.should_exit:
            await anyio.sleep(0.025)
        if http_server.started:
            print(f"LPK_MCP_READY {animation_mcp_endpoint(server.settings.port)}", file=sys.stderr, flush=True)

    async with anyio.create_task_group() as group:
        group.start_soon(announce_ready)
        try:
            await http_server.serve(sockets=[listener])
        finally:
            group.cancel_scope.cancel()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Local Live2D/Spine animation MCP (stdio or Streamable HTTP on 127.0.0.1).",
    )
    parser.add_argument("--workspace", required=True, help="Existing writable directory containing all saved animation packages.")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="WARNING", help="Diagnostic logging on stderr (default: WARNING).")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio", help="stdio (client-owned, default) or local Streamable HTTP.")
    parser.add_argument("--port", type=int, default=DEFAULT_MCP_PORT, help=f"Loopback HTTP port, 1..65535 (default: {DEFAULT_MCP_PORT}); unused for stdio.")
    parser.add_argument("--managed", action="store_true", help="HTTP child process stops on stdin 'stop' or EOF; used by the GUI.")
    args = parser.parse_args(argv)
    try:
        validate_mcp_port(args.port)
    except ValueError as exc:
        parser.error(str(exc))
    if args.managed and args.transport != "streamable-http":
        parser.error("--managed requires --transport streamable-http")
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    with _protocol_stdout() as protocol_output:
        try:
            server = create_server(args.workspace, log_level=args.log_level, transport=args.transport, port=args.port)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        if args.transport == "stdio":
            anyio.run(_run_stdio, server, protocol_output)
        else:
            try:
                listener = bind_animation_mcp_port(args.port)
            except ValueError as exc:
                parser.error(str(exc))
            try:
                anyio.run(_run_http, server, listener, args.managed)
            except KeyboardInterrupt:
                return 0
            finally:
                listener.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
