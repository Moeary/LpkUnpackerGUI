from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from app.gui.SpineAtlasPage import SpineAtlasPage
except ImportError:  # pragma: no cover - the lightweight system env omits Qt.
    QApplication = None
    SpineAtlasPage = None


@unittest.skipIf(QApplication is None, "PySide6 is optional in the lightweight test environment")
class SpineAtlasGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="spine-gui-", dir=Path.cwd()))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_offscreen_extract_worker_finishes_and_is_released(self):
        image = Image.new("RGBA", (8, 8), (10, 20, 30, 255))
        image.save(self.root / "page.png")
        (self.root / "main.atlas").write_text(
            "page.png\nsize: 8, 8\nformat: RGBA8888\npart\nbounds: 0, 0, 2, 2\n",
            encoding="utf-8",
        )
        page = SpineAtlasPage()
        page._set_atlas_path(self.root / "main.atlas")
        page.output_edit.setText(str(self.root / "export"))
        page._extract()
        deadline = time.monotonic() + 10
        while page._worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertIsNone(page._worker)
        self.assertIsNotNone(page._result)
        self.assertTrue((self.root / "export" / "regions" / "p00_part_i-1.png").is_file())
        page.close()
