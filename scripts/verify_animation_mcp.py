"""Exercise real model editing through an actual MCP stdio client session.

Writes independent packages and a protocol report below --workspace. Source
models are read-only. Use render_animation_frames.py to review Spine results.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def amount(time: float) -> float:
    if time < 0.8:
        x = time / 0.8
    elif time <= 1.6:
        x = 1.0
    else:
        x = max(0.0, (2.4 - time) / 0.8)
    return x * x * (3 - 2 * x)


async def verify(args):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, PYTHONPATH=str(ROOT))
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.main", "--mcp-animation", "--workspace", str(workspace)],
        env=environment,
    )
    report = {"workspace": str(workspace), "models": {}, "protocol_calls": []}
    times = [round(i * 0.1, 2) for i in range(25)]
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as client:
            initialized = await client.initialize()
            report["server"] = initialized.serverInfo.model_dump()
            report["tools"] = [tool.name for tool in (await client.list_tools()).tools]

            async def call(name, arguments):
                result = await client.call_tool(name, arguments)
                if result.isError:
                    raise AssertionError(f"{name}: {result.content}")
                content = result.structuredContent
                if content is None:
                    content = json.loads("".join(item.text for item in result.content if hasattr(item, "text")))
                report["protocol_calls"].append(name)
                return content

            for label, source in (("belfast", args.spine), ("spineboy", args.reference_spine), ("live2d", args.live2d)):
                if source is None:
                    continue
                source = source.resolve()
                before = digest(source)
                source_files = {path: digest(path) for path in source.parent.iterdir() if path.is_file()}
                inventory = await call("inspect_model", {"model_path": str(source)})
                (workspace / f"{label}_inventory.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
                created = await call("create_project", {"model_path": str(source)})
                project_id = created["project_id"]
                animation = "ai_nod" if label == "live2d" else "ai_crouch"
                await call("create_animation", {"project_id": project_id, "name": animation, "duration": 2.4, "loop": False, "fps": 30})
                if label == "belfast":
                    tracks = [(bone, "translate", {"x": x, "y": y}) for bone, x, y in (
                        ("Y", 0, 420), ("bone4", 417, 308), ("bone3", 300, 0),
                        ("bone6", -300, 0), ("bone5", 242, 90),
                    )]
                elif label == "spineboy":
                    tracks = [("hip", "translate", {"x": -25, "y": -100}), ("torso", "rotate", {"angle": -12})]
                else:
                    tracks = [("ParamAngleY", "value", {"value": -12}), ("ParamBodyAngleY", "value", {"value": 5})]
                for target, channel, values in tracks:
                    keyframes = [{"time": t, **{key: value * amount(t) for key, value in values.items()}} for t in times]
                    await call("set_keyframes", {"project_id": project_id, "animation_name": animation,
                                                 "target": target, "channel": channel, "keyframes": keyframes, "mode": "replace"})
                    await call("get_keyframes", {"project_id": project_id, "animation_name": animation,
                                                 "target": target, "channel": channel})
                await call("clone_animation", {"project_id": project_id, "source_name": animation,
                                               "new_name": animation + "_copy"})
                output = workspace / f"{label}_{animation}"
                saved = await call("save_package", {"project_id": project_id, "output_dir": str(output)})
                await call("close_project", {"project_id": project_id})
                assert digest(source) == before, "Source model changed during copy editing"
                assert all(path.is_file() and digest(path) == expected for path, expected in source_files.items()), "Source resource changed"
                report["models"][label] = {"source": str(source), "source_sha256": before, "source_unchanged": True,
                                            "source_resource_hashes": {path.name: value for path, value in source_files.items()},
                                            "animation": animation, "output": saved}
                (workspace / "acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--spine", type=Path)
    parser.add_argument("--reference-spine", type=Path)
    parser.add_argument("--live2d", type=Path)
    args = parser.parse_args()
    if not any((args.spine, args.reference_spine, args.live2d)):
        parser.error("Supply at least one real model")
    asyncio.run(verify(args))


if __name__ == "__main__":
    main()
