import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.settings_manager import SettingsManager


class SpineEditorSettingsTests(unittest.TestCase):
    def test_editor_options_persist_without_enabling_automatic_conversion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            settings = SettingsManager(path)
            self.assertTrue(settings.get_spine_create_project())
            self.assertFalse(settings.get_spine_conversion_enabled())
            settings.set_spine_editor_path(" D:/Programs/Spine/Spine.com ")
            settings.set_spine_create_project(False)
            restored = SettingsManager(path)
            self.assertEqual(restored.get_spine_editor_path(), "D:/Programs/Spine/Spine.com")
            self.assertFalse(restored.get_spine_create_project())
            self.assertFalse(restored.get_spine_conversion_enabled())

    def test_extraction_snapshots_editor_options(self):
        from app.core import extractor_thread
        with tempfile.TemporaryDirectory() as directory:
            settings = SettingsManager(Path(directory) / "settings.json")
            settings.set_spine_editor_path("D:/Tools/Spine.com")
            settings.set_spine_conversion_enabled(True)
            with patch.object(extractor_thread, "SettingsManager", return_value=settings):
                options = extractor_thread._spine_conversion_snapshot()
            self.assertTrue(options.enabled)
            self.assertTrue(options.create_project)
            self.assertEqual(options.editor_path, "D:/Tools/Spine.com")
