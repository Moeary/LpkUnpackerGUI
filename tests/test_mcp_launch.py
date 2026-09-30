import tempfile
import json
import socket
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.mcp_launch import (
    animation_mcp_config, animation_mcp_endpoint, animation_mcp_guide,
    animation_mcp_launch, bind_animation_mcp_port, validate_mcp_port,
)


class McpLaunchTests(unittest.TestCase):
    def test_source_configuration_does_not_depend_on_client_directory(self):
        with tempfile.TemporaryDirectory(prefix="mcp workspace ") as directory:
            with patch("sys.frozen", False, create=True):
                config = animation_mcp_config(directory)["mcpServers"]["live2d-spine-animation"]
            self.assertEqual(config["args"][:2], ["-m", "app.mcp_server"])
            self.assertEqual(config["args"][-1], str(Path(directory).resolve()))
            self.assertIn("PYTHONPATH", config["env"])

    def test_packaged_configuration_uses_service_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("sys.frozen", True, create=True):
                config = animation_mcp_config(directory)["mcpServers"]["live2d-spine-animation"]
            self.assertEqual(config["args"][0], "--mcp-animation")
            self.assertNotIn("env", config)

    def test_missing_workspace_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                animation_mcp_config(Path(directory) / "absent")

    def test_http_configuration_and_managed_launch_use_the_actual_port(self):
        with tempfile.TemporaryDirectory() as directory:
            endpoint = animation_mcp_endpoint(8931)
            config = animation_mcp_config(directory, transport="streamable-http", port=8931)
            self.assertEqual(config["mcpServers"]["live2d-spine-animation"], {"type": "http", "url": endpoint})
            for packaged in (False, True):
                with self.subTest(packaged=packaged), patch("sys.frozen", packaged, create=True):
                    launch = animation_mcp_launch(directory, transport="streamable-http", port=8931, managed=True)
                    self.assertEqual(launch["args"][-5:], ["--transport", "streamable-http", "--port", "8931", "--managed"])
                    self.assertEqual(launch["args"][0], "--mcp-animation" if packaged else "-m")
            with self.assertRaisesRegex(ValueError, "stdio belongs"):
                animation_mcp_launch(directory, managed=True)

    def test_port_validation_and_exclusive_loopback_binding(self):
        for value in (0, 65536, -1, True, 3.5, "8765"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_mcp_port(value)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            with self.assertRaisesRegex(ValueError, "port is in use"):
                bind_animation_mcp_port(port)
        with bind_animation_mcp_port(port) as listener:
            self.assertEqual(listener.getsockname(), ("127.0.0.1", port))

    def test_generated_guide_contains_real_paths_and_registered_tool_schemas(self):
        with tempfile.TemporaryDirectory(prefix="AI output ") as directory:
            guide = animation_mcp_guide(directory, transport="streamable-http", port=8932)
            self.assertNotIn("{{", guide)
            self.assertIn(animation_mcp_endpoint(8932), guide)
            blocks = guide.split("```json\n")
            connection = json.loads(blocks[1].split("```", 1)[0])
            schemas = json.loads(blocks[3].split("```", 1)[0])
            self.assertEqual(connection["output_workspace"], str(Path(directory).resolve()))
            self.assertEqual(len(schemas), 9)
            self.assertEqual({tool["name"] for tool in schemas}, {
                "inspect_model", "create_project", "inspect_project", "create_animation",
                "get_keyframes", "set_keyframes", "clone_animation", "save_package", "close_project",
            })
            set_frames = next(tool for tool in schemas if tool["name"] == "set_keyframes")
            self.assertIn("keyframes", set_frames["inputSchema"]["properties"])
            self.assertFalse(set_frames["annotations"]["destructiveHint"])
