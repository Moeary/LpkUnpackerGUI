from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    import app.gui.SettingsPage as settings_page_module
    from app.core.settings_manager import SettingsManager
except (ImportError, OSError):  # pragma: no cover - optional desktop dependency
    QApplication = None
    settings_page_module = None
    SettingsManager = None


@unittest.skipIf(
    QApplication is None,
    "PySide6 is optional in the lightweight test environment",
)
class SettingsPageLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_settings_are_split_into_two_scrollable_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"

            class TempSettings(SettingsManager):
                def __init__(self, settings_file=None):
                    super().__init__(settings_path)

            with patch.object(settings_page_module, "SettingsManager", TempSettings):
                page = settings_page_module.SettingsPage()
            try:
                self.assertEqual(page.settings_columns_layout.count(), 2)
                self.assertEqual(
                    page.settings_scroll.horizontalScrollBarPolicy(),
                    Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
                )
                self.assertEqual(
                    {
                        child.objectName()
                        for child in page.settings_content.findChildren(type(page.general_card))
                        if child.objectName().startswith("settings")
                    },
                    {
                        "settingsGeneralCard",
                        "settingsRuntimeCard",
                        "settingsArchiveCard",
                        "settingsAssetToolsCard",
                        "settingsDownloadCard",
                        "settingsPhotoshopCard",
                        "settingsSpineCard",
                        "settingsTextureCard",
                        "settingsMcpCard",
                    },
                )
                self.assertEqual(page.settings_scroll.horizontalScrollBar().maximum(), 0)
            finally:
                page.close()
                page.deleteLater()
                self.app.processEvents()

    def test_spine_editor_and_project_creation_preferences_save_with_runtime_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"

            class TempSettings(SettingsManager):
                def __init__(self, settings_file=None):
                    super().__init__(settings_path)

            with patch.object(settings_page_module, "SettingsManager", TempSettings):
                page = settings_page_module.SettingsPage()
            try:
                page.spine_editor_edit.setText(r"D:\\Tools\\Spine.com")
                page.spine_create_project_checkbox.setChecked(False)
                self.assertTrue(page.save_runtime_settings())
                manager = SettingsManager(settings_path)
                self.assertEqual(manager.get_spine_editor_path(), r"D:\\Tools\\Spine.com")
                # The legacy project checkbox is hidden by the consolidated
                # compatibility setting.  Compatibility mode always requests
                # an independent .spine project; a hidden stale checkbox must
                # not override that policy when Runtime settings are saved.
                self.assertTrue(manager.get_spine_conversion_options()["create_project"])
            finally:
                page.close()
                page.deleteLater()
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
