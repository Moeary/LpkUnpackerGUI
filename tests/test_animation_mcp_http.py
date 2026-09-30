from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.core.mcp_launch import ANIMATION_GUIDE_URI, animation_mcp_endpoint


ROOT = Path(__file__).resolve().parents[1]


def available_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def data(result) -> dict:
    if result.isError:
        raise AssertionError("MCP tool failed: " + " ".join(getattr(item, "text", "") for item in result.content))
    return result.structuredContent or json.loads("".join(item.text for item in result.content))


class AnimationMCPHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mcp-http-actual-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.workspace = self.base / "outputs"
        self.workspace.mkdir()
        self.source = self.base / "source"
        self.source.mkdir()
        self.process = None
        self.stdout = tempfile.TemporaryFile()
        self.stderr = tempfile.TemporaryFile()
        self.addCleanup(self.stdout.close)
        self.addCleanup(self.stderr.close)
        self.addCleanup(self._ensure_stopped)

    def _diagnostics(self) -> str:
        self.stderr.seek(0)
        return self.stderr.read().decode("utf-8", errors="replace")

    def _start(self, port: int):
        self.port = port
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-m", "app.main", "--mcp-animation", "--workspace", str(self.workspace),
             "--transport", "streamable-http", "--port", str(port), "--managed"],
            cwd=self.base, env={**os.environ, "PYTHONPATH": str(ROOT)},
            stdin=subprocess.PIPE, stdout=self.stdout, stderr=self.stderr,
        )

    async def _ready(self):
        for _ in range(400):
            if "LPK_MCP_READY " in self._diagnostics():
                return
            if self.process.poll() is not None:
                self.fail(self._diagnostics())
            await asyncio.sleep(0.025)
        self.fail("HTTP process did not become ready: " + self._diagnostics())

    def _ensure_stopped(self):
        if self.process is None:
            return
        if self.process.poll() is None:
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()

    @asynccontextmanager
    async def _client(self):
        async with httpx.AsyncClient(timeout=10, trust_env=False) as http_client:
            async with streamable_http_client(animation_mcp_endpoint(self.port), http_client=http_client) as (read, write, _):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=10)) as client:
                    initialized = await client.initialize()
                    self.assertEqual(initialized.serverInfo.name, "LpkUnpacker Animation")
                    yield client

    async def test_real_http_resource_edit_save_and_managed_stop(self):
        model = self.source / "skeleton.json"
        model.write_text(json.dumps({
            "skeleton": {"spine": "3.8.75"},
            "bones": [{"name": "root"}, {"name": "hip", "parent": "root", "y": 100}],
            "slots": [], "skins": [], "animations": {},
        }), encoding="utf-8")
        original_bytes = model.read_bytes()
        self._start(available_port())
        await self._ready()
        async with self._client() as client:
            tools = (await client.list_tools()).tools
            self.assertEqual(len(tools), 9)
            resources = (await client.list_resources()).resources
            self.assertEqual([str(resource.uri) for resource in resources], [ANIMATION_GUIDE_URI])
            self.assertEqual(resources[0].mimeType, "text/markdown")
            guide = (await client.read_resource(ANIMATION_GUIDE_URI)).contents[0].text
            self.assertNotIn("{{", guide)
            self.assertIn(animation_mcp_endpoint(self.port), guide)
            sections = guide.split("```json\n")
            connection = json.loads(sections[1].split("```", 1)[0])
            self.assertEqual(connection["output_workspace"], str(self.workspace.resolve()))
            self.assertEqual(connection["transport"], "streamable-http")
            live_schemas = json.loads(sections[3].split("```", 1)[0])
            self.assertEqual(live_schemas, [tool.model_dump(mode="json", exclude_none=True) for tool in tools])
            self.assertEqual(data(await client.call_tool("inspect_model", {"model_path": str(model)}))["kind"], "spine")
            project = data(await client.call_tool("create_project", {"model_path": str(model)}))["project_id"]
            data(await client.call_tool("create_animation", {"project_id": project, "name": "crouch", "duration": 1}))
            track = {"project_id": project, "animation_name": "crouch", "target": "hip", "channel": "translate"}
            data(await client.call_tool("set_keyframes", {**track, "keyframes": [
                {"time": 0, "x": 0, "y": 0}, {"time": 0.5, "x": 0, "y": -40}, {"time": 1, "x": 0, "y": 0},
            ]}))
            self.assertEqual(data(await client.call_tool("get_keyframes", track))["keyframes"][1]["y"], -40)
            rejected = await client.call_tool("save_package", {"project_id": project, "output_dir": "../escaped"})
            self.assertTrue(rejected.isError)
            self.assertFalse((self.base / "escaped").exists())
            saved = data(await client.call_tool("save_package", {"project_id": project, "output_dir": "crouch-http"}))
            exported = Path(saved["model_path"])
            self.assertTrue(exported.is_relative_to(self.workspace))
            output_bytes = exported.read_bytes()
            duplicate = await client.call_tool("save_package", {"project_id": project, "output_dir": "crouch-http"})
            self.assertTrue(duplicate.isError)
            self.assertEqual(exported.read_bytes(), output_bytes)
            data(await client.call_tool("close_project", {"project_id": project}))
        self.assertEqual(model.read_bytes(), original_bytes)
        self.process.stdin.write(b"stop\n")
        self.process.stdin.flush()
        self.process.stdin.close()
        self.assertEqual(await asyncio.to_thread(self.process.wait, timeout=5), 0, self._diagnostics())
        # The server releases the port, including after a real MCP session.
        with socket.socket() as released:
            released.bind(("127.0.0.1", self.port))
        self.stdout.seek(0)
        self.assertEqual(self.stdout.read(), b"")
        self.assertNotIn("QFluent", self._diagnostics())

    async def test_managed_parent_pipe_eof_releases_listener(self):
        self._start(available_port())
        await self._ready()
        self.process.stdin.close()
        self.assertEqual(await asyncio.to_thread(self.process.wait, timeout=5), 0, self._diagnostics())
        with socket.socket() as released:
            released.bind(("127.0.0.1", self.port))

    async def test_occupied_port_is_a_clear_startup_error(self):
        with socket.socket() as occupying:
            occupying.bind(("127.0.0.1", 0))
            occupying.listen()
            port = occupying.getsockname()[1]
            self._start(port)
            self.assertEqual(await asyncio.to_thread(self.process.wait, timeout=10), 2)
            self.assertIn(f"127.0.0.1:{port}", self._diagnostics())
            self.assertIn("port is in use or unavailable", self._diagnostics())
            self.assertNotIn("LPK_MCP_READY", self._diagnostics())

    async def test_http_rejects_nonlocal_host_and_origin(self):
        self._start(available_port())
        await self._ready()
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"},
            }}
            for header in ({"Host": "untrusted.example"}, {"Origin": "https://untrusted.example"}):
                result = await client.post(animation_mcp_endpoint(self.port), json=payload,
                    headers={"Accept": "application/json, text/event-stream", **header})
                self.assertIn(result.status_code, (403, 421))


if __name__ == "__main__":
    unittest.main()
