from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import paths


class PackagedPathTests(unittest.TestCase):
    def test_source_checkout_uses_bundle_root_and_interpreter(self):
        with patch.object(paths, "_COMPILED", None), patch("sys.frozen", False, create=True):
            self.assertFalse(paths.is_packaged())
            self.assertEqual(paths.project_root(), paths.BUNDLE_ROOT)
            self.assertEqual(paths.app_executable(), Path(sys.executable))

    def test_nuitka_onefile_keeps_state_beside_outer_exe(self):
        # Nuitka onefile: __file__/sys.executable live in a temporary
        # extraction directory; containing_dir/original_argv0 name the EXE.
        outer = Path(r"C:\Apps\LpkUnpacker")
        compiled = SimpleNamespace(
            containing_dir=str(outer),
            original_argv0=str(outer / "LpkUnpackerGUI.exe"),
        )
        with patch.object(paths, "_COMPILED", compiled):
            self.assertTrue(paths.is_packaged())
            self.assertEqual(paths.project_root(), outer.resolve())
            self.assertEqual(paths.app_executable(), (outer / "LpkUnpackerGUI.exe").resolve())

    @unittest.skipUnless(sys.platform == "win32", "Windows process image lookup")
    def test_packaged_self_executable_is_the_real_process_image(self):
        # Nuitka sets sys.executable to a python.exe that does not exist.
        with patch.object(paths, "_COMPILED", SimpleNamespace(containing_dir=None, original_argv0=None)), \
             patch.object(sys, "executable", r"C:\missing\python.exe"):
            image = paths.self_executable()
            self.assertTrue(image.is_file())
            self.assertNotEqual(image, Path(r"C:\missing\python.exe"))
            # Without original_argv0 (standalone), MCP configs use the image too.
            self.assertEqual(paths.app_executable(), image)

    def test_bundled_assets_are_read_from_the_bundle(self):
        self.assertEqual(paths.ASSETS_DIR, paths.BUNDLE_ROOT / "assets")
        self.assertTrue((paths.ASSETS_DIR / "app" / "icon.ico").is_file())


if __name__ == "__main__":
    unittest.main()
