import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.mcp_launch import animation_mcp_config


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
