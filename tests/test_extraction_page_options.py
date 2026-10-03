from __future__ import annotations

import importlib
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from app.core.settings_manager import SettingsManager
from app.core.extractor_thread import ExtractorThread
from app.core.spine_converter import SpineConversionOptions


class ExtractionPageOptionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_explicit_conversion_survives_disabled_compatibility_and_images_only_toggle(self):
        module = importlib.import_module("app.gui.ExtractorPage")
        with tempfile.TemporaryDirectory() as directory:
            manager = SettingsManager(Path(directory) / "settings.json")
            manager.set("last_output_path", directory)
            manager.set_spine_compatibility_mode(False)
            with patch.object(module, "SettingsManager", return_value=manager), patch(
                "app.core.config_manager.SettingsManager", return_value=manager,
            ):
                page = module.ExtractorPage()
                try:
                    self.assertFalse(page._extraction_spine_options().enabled)
                    page.spine_convert_checkbox.setChecked(True)
                    options = page._extraction_spine_options()
                    self.assertTrue(options.enabled)
                    self.assertEqual(options.target_version, "3.8.75")
                    self.assertEqual(options.output_format, "json")
                    self.assertTrue(SettingsManager(manager.settings_file).get("extractor.convert_spine_3875"))
                    page.images_only_checkbox.setChecked(True)
                    self.assertFalse(page.spine_convert_checkbox.isEnabled())
                    self.assertFalse(page._extraction_spine_options().enabled)
                    page.images_only_checkbox.setChecked(False)
                    self.assertTrue(page.spine_convert_checkbox.isEnabled())
                    self.assertTrue(page._extraction_spine_options().enabled)
                finally:
                    logging.getLogger().removeHandler(page.log_handler)
                    page.close()
                    page.deleteLater()
                    self.app.processEvents()

    def test_worker_honors_explicit_options_and_suppresses_conversion_for_textures(self):
        for enabled, images_only in ((True, False), (False, False), (True, True)):
            with self.subTest(enabled=enabled, images_only=images_only):
                options = SpineConversionOptions(enabled=enabled)
                worker = ExtractorThread([], {}, "unused", images_only, spine_conversion=options)
                with patch("app.core.extractor_thread.run_extraction_batch") as run, patch(
                    "app.core.extractor_thread._spine_conversion_snapshot", side_effect=AssertionError("unexpected fallback"),
                ):
                    worker.run()
                self.assertEqual(run.call_args.kwargs["spine_conversion"].enabled, enabled and not images_only)
