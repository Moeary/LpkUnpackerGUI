from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.core.spine_native import find_native_library


class SpineNativeDiscoveryTests(unittest.TestCase):
    def test_discovery_requires_matching_manifest_family(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = root / "spine_bridge.dll"
            bridge.write_bytes(b"test placeholder")
            (root / "spine_native.json").write_text(
                json.dumps({"runtimeFamily": "4.0"}), encoding="utf-8"
            )
            self.assertIsNone(find_native_library(root, "3.8"))
            self.assertEqual(find_native_library(root, "4.0"), bridge)

    def test_common_versioned_install_layout_is_discovered(self):
        # This mirrors the downloader's versioned layout without writing into
        # the repository's runtime tree.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "3.8.75"
            root.mkdir(parents=True)
            bridge = root / "spine_bridge.dll"
            bridge.write_bytes(b"test placeholder")
            (root / "spine_native.json").write_text(
                json.dumps({"runtimeFamily": "3.8", "version": "3.8.75"}),
                encoding="utf-8",
            )
            self.assertEqual(find_native_library(root, "3.8"), bridge)


if __name__ == "__main__":
    unittest.main()
