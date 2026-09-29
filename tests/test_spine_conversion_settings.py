import json
import tempfile
import unittest
from pathlib import Path

from app.core.settings_manager import SettingsManager


class SpineConversionSettingsTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory(prefix="lpk-spine-settings-")
        self.settings_path = Path(self._temp_dir.name) / "settings.json"

    def tearDown(self):
        self._temp_dir.cleanup()

    def test_spine_conversion_defaults_are_opt_in_and_target_375(self):
        manager = SettingsManager(self.settings_path)

        self.assertFalse(manager.get_spine_conversion_enabled())
        self.assertEqual(manager.get_spine_conversion_target_version(), "3.8.75")
        self.assertEqual(manager.get_spine_conversion_output_format(), "json")

    def test_spine_conversion_settings_persist_and_reload_from_explicit_file(self):
        manager = SettingsManager(self.settings_path)

        manager.set_spine_conversion_enabled(True)
        manager.set_spine_conversion_target_version("4.0.64")
        manager.set_spine_conversion_output_format("skel")

        reloaded = SettingsManager(self.settings_path)
        self.assertTrue(reloaded.get_spine_conversion_enabled())
        self.assertEqual(reloaded.get_spine_conversion_target_version(), "4.0.64")
        self.assertEqual(reloaded.get_spine_conversion_output_format(), "skel")

    def test_spine_conversion_output_format_rejects_unknown_values(self):
        manager = SettingsManager(self.settings_path)

        manager.set_spine_conversion_output_format("same")

        self.assertEqual(manager.get_spine_conversion_output_format(), "json")

    def test_invalid_target_is_rejected_without_writing(self):
        manager = SettingsManager(self.settings_path)
        before = json.loads(self.settings_path.read_text(encoding="utf-8"))

        with self.assertRaises(ValueError):
            manager.set_spine_conversion_target_version("4.3")

        after = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(after, before)
        self.assertEqual(manager.get_spine_conversion_target_version(), "3.8.75")

    def test_existing_invalid_target_is_preserved_for_converter_validation(self):
        self.settings_path.write_text(
            json.dumps({"spine_conversion": {"target_version": "4.3.1"}}),
            encoding="utf-8",
        )
        manager = SettingsManager(self.settings_path)

        self.assertEqual(manager.get_spine_conversion_target_version(), "4.3.1")
        with self.assertRaises(ValueError):
            manager.set_spine_conversion_target_version("4.3.1")

    def test_two_managers_interleaved_writes_preserve_each_single_key(self):
        first = SettingsManager(self.settings_path)
        second = SettingsManager(self.settings_path)

        first.set_spine_conversion_target_version("3.8.99")
        second.set_spine_converter_path(r"C:\tools\SpineSkeletonDataConverter.exe")
        first.set_spine_conversion_enabled(True)
        second.set_spine_runtime_dir(r"C:\tools\spine\3.8.75")

        reloaded = SettingsManager(self.settings_path)
        self.assertTrue(reloaded.get_spine_conversion_enabled())
        self.assertEqual(reloaded.get_spine_conversion_target_version(), "3.8.99")
        self.assertEqual(
            reloaded.get_spine_converter_path(),
            r"C:\tools\SpineSkeletonDataConverter.exe",
        )
        self.assertEqual(
            reloaded.get_spine_runtime_dir(),
            r"C:\tools\spine\3.8.75",
        )

    def test_remember_paths_false_keeps_set_in_memory_without_persisting(self):
        self.settings_path.write_text(
            json.dumps({"remember_paths": False}),
            encoding="utf-8",
        )
        manager = SettingsManager(self.settings_path)
        before = self.settings_path.read_text(encoding="utf-8")

        manager.set_spine_conversion_enabled(True)

        after = self.settings_path.read_text(encoding="utf-8")
        self.assertEqual(after, before)
        self.assertTrue(manager.get_spine_conversion_enabled())


if __name__ == "__main__":
    unittest.main()
