from __future__ import annotations

import hashlib
import json
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError
from PIL import Image

from app.mcp_server import AnimationMCPService


ROOT = Path(__file__).resolve().parents[1]


def _data(result) -> dict:
    if result.isError:
        raise AssertionError("MCP tool failed: " + " ".join(getattr(item, "text", "") for item in result.content))
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads("".join(getattr(item, "text", "") for item in result.content))


class AnimationMCPOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="animation-mcp-output-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()

    def test_output_scope_and_existing_destinations(self) -> None:
        service = AnimationMCPService(self.workspace)
        self.assertEqual(service.output_path("new-package"), self.workspace / "new-package")
        for destination in ("..", "../outside", str(self.base / "outside"), str(self.workspace)):
            with self.subTest(destination=destination), self.assertRaises(ToolError):
                service.output_path(destination)
        existing = self.workspace / "existing"
        existing.mkdir()
        marker = existing / "keep.txt"
        marker.write_text("original", encoding="utf-8")
        with self.assertRaisesRegex(ToolError, "already exists"):
            service.output_path(str(existing))
        self.assertEqual(marker.read_text(encoding="utf-8"), "original")

    def test_output_cannot_follow_link_outside_workspace(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        link = self.workspace / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            if os.name != "nt":
                self.skipTest(f"Directory symlinks are unavailable: {exc}")
            # Windows junctions require no symlink privilege. Both explicitly
            # named targets are inside this test's own temporary directory.
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                capture_output=True, text=True, timeout=15,
            )
            if created.returncode != 0:
                self.skipTest(f"Directory links are unavailable: {created.stderr}")
        with self.assertRaisesRegex(ToolError, "inside --workspace"):
            AnimationMCPService(self.workspace).output_path("link/new-package")
        self.assertFalse((outside / "new-package").exists())

    def test_python_native_and_buffered_stdout_use_stderr(self) -> None:
        code = """
import ctypes, json, os
from app.mcp_server import _protocol_stdout
with _protocol_stdout() as protocol:
    print("python diagnostic")
    os.write(1, b"native diagnostic\\n")
    libc = ctypes.CDLL("msvcrt" if os.name == "nt" else None)
    libc.printf(b"buffered diagnostic\\n")
    protocol.write(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}) + "\\n")
"""
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"jsonrpc": "2.0", "id": 1, "result": {}})
        for diagnostic in ("python diagnostic", "native diagnostic", "buffered diagnostic"):
            self.assertIn(diagnostic, result.stderr)

    def test_cli_help_and_bad_workspace_do_not_import_gui(self) -> None:
        for module, prefix in (("app.mcp_server", []), ("app.main", ["--mcp-animation"])):
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, "-m", module, *prefix, "--help"], cwd=ROOT,
                    capture_output=True, text=True, encoding="utf-8", timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("stdio only", result.stdout)
                self.assertNotIn("QFluent", result.stdout + result.stderr)
                self.assertNotIn("Runtime log:", result.stdout)
                invalid = subprocess.run(
                    [sys.executable, "-m", module, *prefix, "--workspace", str(self.base / "missing")],
                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=15,
                )
                self.assertEqual(invalid.returncode, 2, invalid.stderr)
                self.assertEqual(invalid.stdout, "")
                self.assertIn("error:", invalid.stderr)


class AnimationMCPProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="animation-mcp-protocol-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.source = self.base / "source"
        self.source.mkdir()
        self.protocol_errors: list[Exception] = []

    @asynccontextmanager
    async def _client(self, *, application_entry: bool = False):
        async def on_message(message):
            if isinstance(message, Exception):
                self.protocol_errors.append(message)

        # cwd is intentionally unrelated to the source checkout. Client config
        # must work with an absolute PYTHONPATH, as the Settings card produces.
        args = ["-u", "-m", "app.main", "--mcp-animation"] if application_entry else ["-u", "-m", "app.mcp_server"]
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[*args, "--workspace", str(self.workspace)],
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            cwd=self.base,
        )
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as diagnostics:
            async with stdio_client(parameters, errlog=diagnostics) as (read, write):
                async with ClientSession(
                    read, write, read_timeout_seconds=timedelta(seconds=15),
                    message_handler=on_message,
                ) as client:
                    initialized = await client.initialize()
                    self.assertEqual(initialized.serverInfo.name, "LpkUnpacker Animation")
                    yield client
            diagnostics.seek(0)
            self.assertNotIn("QFluent", diagnostics.read())
        self.assertEqual(self.protocol_errors, [], "Non-protocol data appeared on stdout")

    async def test_spine_editing_and_workspace_errors_over_real_stdio(self) -> None:
        model = self.source / "skeleton.json"
        original = {
            "skeleton": {"spine": "3.8.99"},
            "bones": [{"name": "root"}, {"name": "hip", "parent": "root", "y": 100}],
            "slots": [], "skins": [], "animations": {},
        }
        model.write_text(json.dumps(original), encoding="utf-8")
        before = model.read_bytes()
        async with self._client(application_entry=True) as client:
            tools = (await client.list_tools()).tools
            self.assertEqual({tool.name for tool in tools}, {
                "inspect_model", "create_project", "inspect_project", "create_animation",
                "get_keyframes", "set_keyframes", "clone_animation", "save_package", "close_project",
            })
            for tool in tools:
                self.assertFalse(tool.annotations.destructiveHint)
                self.assertFalse(tool.annotations.openWorldHint)
                self.assertTrue(tool.description)
            inventory = _data(await client.call_tool("inspect_model", {"model_path": str(model)}))
            self.assertEqual(inventory["kind"], "spine")
            self.assertIn("hip", [bone["name"] for bone in inventory["bones"]])
            created = _data(await client.call_tool("create_project", {"model_path": str(model)}))
            project_id = created["project_id"]
            _data(await client.call_tool("create_animation", {
                "project_id": project_id, "name": "crouch", "duration": 1, "loop": False,
            }))
            arguments = {"project_id": project_id, "animation_name": "crouch", "target": "hip", "channel": "translate"}
            _data(await client.call_tool("set_keyframes", {**arguments, "keyframes": [
                {"time": 0, "x": 0, "y": 0},
                {"time": 0.5, "x": 0, "y": -50},
                {"time": 1, "x": 0, "y": 0},
            ]}))
            readback = _data(await client.call_tool("get_keyframes", arguments))
            self.assertTrue(readback["editable"])
            self.assertEqual([frame["y"] for frame in readback["keyframes"]], [0, -50, 0])
            _data(await client.call_tool("clone_animation", {
                "project_id": project_id, "source_name": "crouch", "new_name": "crouch_variant",
            }))
            _data(await client.call_tool("set_keyframes", {
                **arguments, "animation_name": "crouch_variant", "mode": "merge",
                "keyframes": [{"time": 0.25, "x": 0, "y": -25}],
            }))
            invalid = await client.call_tool("set_keyframes", {**arguments, "target": "missing", "keyframes": [{"time": 0, "x": 0, "y": 0}]})
            self.assertTrue(invalid.isError)
            for destination in ("../outside", str(self.base / "outside")):
                result = await client.call_tool("save_package", {"project_id": project_id, "output_dir": destination})
                self.assertTrue(result.isError)
                self.assertFalse((self.base / "outside").exists())
            saved = _data(await client.call_tool("save_package", {"project_id": project_id, "output_dir": "crouch-copy"}))
            saved_model = Path(saved["model_path"])
            self.assertTrue(saved_model.is_relative_to(self.workspace))
            exported = json.loads(saved_model.read_text(encoding="utf-8"))
            frames = exported["animations"]["crouch"]["bones"]["hip"]["translate"]
            self.assertEqual([frame["y"] for frame in frames], [0, -50, 0])
            variant = exported["animations"]["crouch_variant"]["bones"]["hip"]["translate"]
            self.assertEqual([frame["time"] for frame in variant], [0, 0.25, 0.5, 1])
            saved_before = saved_model.read_bytes()
            duplicate = await client.call_tool("save_package", {"project_id": project_id, "output_dir": "crouch-copy"})
            self.assertTrue(duplicate.isError)
            self.assertEqual(saved_model.read_bytes(), saved_before)
            _data(await client.call_tool("inspect_project", {"project_id": project_id}))
            _data(await client.call_tool("close_project", {"project_id": project_id}))
            closed = await client.call_tool("inspect_project", {"project_id": project_id})
            self.assertTrue(closed.isError)
        self.assertEqual(model.read_bytes(), before)

    async def test_live2d_motion_editing_over_real_stdio(self) -> None:
        moc = self.source / "character.moc3"
        # No MOC3 header: never pass these fixture bytes to a native Core DLL.
        moc.write_bytes(b"safe sidecar fixture")
        texture = self.source / "texture.png"
        Image.new("RGBA", (1, 1), (255, 255, 255, 255)).save(texture)
        model = self.source / "character.model3.json"
        model.write_text(json.dumps({
            "Version": 3, "FileReferences": {"Moc": moc.name, "Textures": [texture.name]},
        }), encoding="utf-8")
        inventory_path = self.source / "parameters.json"
        inventory_path.write_text(json.dumps({
            "format": "LpkUnpacker.Live2DParameterInventory", "version": 1,
            "moc_sha256": hashlib.sha256(moc.read_bytes()).hexdigest(),
            "parameters": [{"id": "ParamAngleX", "min": -30, "max": 30, "default": 0}],
        }), encoding="utf-8")
        source_bytes = {path: path.read_bytes() for path in self.source.iterdir()}
        async with self._client() as client:
            created = _data(await client.call_tool("create_project", {
                "model_path": str(model), "parameter_inventory_path": str(inventory_path),
            }))
            project_id = created["project_id"]
            self.assertEqual(created["model"]["kind"], "live2d")
            _data(await client.call_tool("create_animation", {"project_id": project_id, "name": "ai_nod", "duration": 1}))
            arguments = {"project_id": project_id, "animation_name": "ai_nod", "target": "ParamAngleX", "channel": "value"}
            _data(await client.call_tool("set_keyframes", {**arguments, "keyframes": [
                {"time": 0, "value": 0}, {"time": 0.5, "value": 10}, {"time": 1, "value": 0},
            ]}))
            out_of_range = await client.call_tool("set_keyframes", {**arguments, "keyframes": [{"time": 0, "value": 31}]})
            self.assertTrue(out_of_range.isError)
            invalid_schema = await client.call_tool("set_keyframes", {**arguments, "keyframes": [{"time": True, "value": 0, "script": "unsupported"}]})
            self.assertTrue(invalid_schema.isError)
            saved = _data(await client.call_tool("save_package", {"project_id": project_id, "output_dir": "nod-copy"}))
            saved_model = Path(saved["model_path"])
            copied = json.loads(saved_model.read_text(encoding="utf-8"))
            motion_path = saved_model.parent / copied["FileReferences"]["Motions"]["ai_nod"][0]["File"]
            motion = json.loads(motion_path.read_text(encoding="utf-8"))
            self.assertEqual(motion["Meta"]["Duration"], 1)
            self.assertEqual(motion["Curves"][0]["Id"], "ParamAngleX")
            self.assertEqual(motion["Curves"][0]["Segments"], [0, 0, 0, 0.5, 10, 0, 1, 0])
        for path, original_bytes in source_bytes.items():
            self.assertEqual(path.read_bytes(), original_bytes)


if __name__ == "__main__":
    unittest.main()
