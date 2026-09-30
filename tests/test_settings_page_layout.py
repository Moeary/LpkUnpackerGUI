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

    def test_categories_show_one_scrollable_column_without_clipped_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"

            class TempSettings(SettingsManager):
                def __init__(self, settings_file=None):
                    super().__init__(settings_path)

            with patch.object(settings_page_module, "SettingsManager", TempSettings):
                page = settings_page_module.SettingsPage()
            try:
                page.show()
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
                        "settingsCubismCard",
                        "settingsDownloadCard",
                        "settingsPhotoshopCard",
                        "settingsSpineCard",
                        "settingsTextureCard",
                        "settingsMcpCard",
                        "settingsMcpConnectionCard",
                        "settingsMcpGuideCard",
                    },
                )
                controls = {
                    "general": (page.font_family_combo, page.output_root_edit),
                    "live2d": (page.cubism_core_edit, page.photoshop_edit),
                    "spine": (page.spine_runtime_version_combo, page.spine_editor_edit),
                    "resources": (page.archive_tool_edit, page.assetstudio_tool_edit),
                    "ai": (page.mcp_port_spin, page.mcp_config_button, page.mcp_save_guide_button),
                    "other": (page.texture_viewer_combo,),
                }
                for width, height in ((1320, 900), (1040, 760)):
                    page.resize(width, height)
                    for category, fields in controls.items():
                        with self.subTest(width=width, category=category):
                            self.assertTrue(page.select_settings_category(category))
                            self.app.processEvents()
                            self.app.processEvents()
                            self.assertLess(page.category_list.geometry().right(), page.settings_scroll.geometry().left())
                            self.assertEqual(page.settings_scroll.horizontalScrollBar().maximum(), 0)
                            selected_cards = dict(page._settings_categories)[category]
                            for _key, cards in page._settings_categories:
                                for card in cards:
                                    self.assertEqual(card.isVisibleTo(page), card in selected_cards)
                            for field in fields:
                                self.assertTrue(field.isVisibleTo(page), field.objectName())
                                self.assertGreater(field.width(), 90)
                                origin = field.mapTo(page.settings_content, field.rect().topLeft())
                                self.assertGreaterEqual(origin.x(), 0)
                                self.assertLessEqual(origin.x() + field.width(), page.settings_content.width())
                                page.settings_scroll.ensureWidgetVisible(field)
                                self.app.processEvents()
                                origin = field.mapTo(page.settings_scroll.viewport(), field.rect().topLeft())
                                self.assertGreaterEqual(origin.y(), 0)
                                self.assertLessEqual(origin.y() + field.height(), page.settings_scroll.viewport().height())
                self.assertTrue(page.theme_combo.isHidden())
                self.assertFalse(page.cubism_core_edit.isVisibleTo(page))
                page.select_settings_category("resources")
                self.app.processEvents()
                self.assertTrue(page.assetstudio_tool_edit.isVisibleTo(page))
                self.assertFalse(page.cubism_core_edit.isVisibleTo(page))
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
