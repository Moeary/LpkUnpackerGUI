from __future__ import annotations

import importlib
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Signal
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QFrame, QWidget

from app.core.settings_manager import SettingsManager
from app.gui.PreviewPage import PreviewPage
from app.gui.theme import apply_application_theme


class DummySpinePreview(QWidget):
    documentLoaded = Signal(str)
    previewReady = Signal(str)
    previewFailed = Signal(str)
    stateChanged = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._last_state = {}
        self._model = None
        self.pause_requests = []

    def set_paused(self, paused):
        self.pause_requests.append(paused)

    def clear_preview(self):
        self._model = None

    def shutdown(self):
        self.clear_preview()

    def open_plan(self, plan):
        self.plan = plan

    def set_view_settings(self, settings):
        self.settings = settings


class PreviewEditorNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="editor-navigation-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        settings_file = self.root / "settings.json"

        class TempSettings(SettingsManager):
            def __init__(self, settings_file_override=None, **kwargs):
                super().__init__(settings_file)

        module = importlib.import_module("app.gui.PreviewPage")
        with patch.object(module, "SettingsManager", TempSettings), patch.object(module, "SpinePreviewWidget", DummySpinePreview), patch.object(PreviewPage, "_ensure_embedded_live2d", return_value=None):
            self.page = PreviewPage()
        self.addCleanup(self._dispose_page)

    def _dispose_page(self):
        self.page.shutdown()
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()

    def test_live2d_button_uses_resolved_model_and_hides_for_images(self):
        model = self.root / "resolved" / "character.model3.json"
        model.parent.mkdir()
        model.write_text('{"Version":3,"FileReferences":{"Moc":"character.moc3"}}', encoding="utf-8")
        spy = QSignalSpy(self.page.editorRequested)
        module = importlib.import_module("app.gui.PreviewPage")
        with patch.object(module.QTimer, "singleShot"), patch.object(module.InfoBar, "success"):
            self.page.load_model_preview(str(model), str(self.root / "original.lpk"))
        self.assertEqual(self.page.current_editor_source(), ("live2d", str(model)))
        self.assertFalse(self.page.open_editor_btn.isHidden())
        self.page.open_editor_btn.click()
        self.assertEqual(spy.count(), 1)
        self.assertEqual(spy.at(0), ["live2d", str(model)])
        self.page._show_image_stage()
        self.assertTrue(self.page.open_editor_btn.isHidden())
        self.assertIsNone(self.page.current_editor_source())

    def test_spine_button_preserves_resolved_wrapper_references(self):
        wrapper = self.root / "model0.json"
        skeleton = self.root / "skeleton.json"
        wrapper.write_text('{"skeleton":"skeleton.json"}', encoding="utf-8")
        skeleton.write_text('{"skeleton":{"spine":"3.8.75"},"bones":[{"name":"root"}]}', encoding="utf-8")
        plan = SimpleNamespace(
            asset=SimpleNamespace(model_config_path=wrapper, skeleton_path=skeleton, spine_version="3.8.75"),
            mode="native", reason="", warnings=(),
        )
        self.page._open_spine_native_preview(plan)
        self.assertEqual(self.page.current_editor_source(), ("spine", str(wrapper)))
        self.page._show_spine_stage()
        self.assertTrue(self.page.pose_controls_card.isHidden())
        self.assertTrue(self.page.advanced_panel.isHidden())
        self.page.close_preview_window()
        self.assertTrue(self.page.open_editor_btn.isHidden())

    def test_editor_lease_keeps_archive_resources_until_copy_finishes(self):
        managed = self.root / "managed-preview"
        managed.mkdir()
        model = managed / "character.model3.json"
        model.write_text("{}", encoding="utf-8")
        self.page._model_preview_temp_dirs.append(str(managed))
        self.page._archive_preview_temp_dirs.append(str(managed))
        token = self.page.acquire_editor_source(str(model))
        self.assertIsNotNone(token)
        self.page._cleanup_model_preview_temp_dirs()
        self.page._cleanup_archive_preview_temp_dirs()
        self.assertTrue(model.is_file())
        copied = self.root / "editor-session.json"
        copied.write_bytes(model.read_bytes())
        self.page.release_editor_source(token)
        self.assertFalse(managed.exists())
        self.assertEqual(copied.read_bytes(), b"{}")

    def test_model_reference_cleanup_never_deletes_original(self):
        original = self.root / "original.model3.json"
        original.write_text("original model", encoding="utf-8")
        self.page._temp_model_json_path = str(original)
        self.page._cleanup_temp_model_json()
        self.assertEqual(original.read_text(encoding="utf-8"), "original model")


class DummyPreviewPage(QFrame):
    editorRequested = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("previewPage")
        self.active = False
        self.can_close = True
        self.prepare_count = self.shutdown_count = 0
        self.leases = set()

    def set_active(self, active):
        self.active = active

    def acquire_editor_source(self, path):
        token = str(len(self.leases)) + path
        self.leases.add(token)
        return token

    def release_editor_source(self, token):
        self.leases.discard(token)

    def prepare_shutdown(self):
        self.prepare_count += 1
        return self.can_close

    def shutdown(self):
        self.shutdown_count += 1
        return self.can_close


class DummyLive2DEditor(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("live2dEditorPage")
        self.mod_panel = QFrame(self)
        self.mod_panel.setObjectName("live2dModPage")
        self.active = False
        self.confirm_ok = self.accept_source = True
        self.opened = []
        self.shutdown_count = self.confirm_count = 0

    def open_source(self, path):
        self.opened.append(path)
        return self.accept_source

    def set_active(self, active):
        self.active = active

    def confirm_discard_or_save(self):
        self.confirm_count += 1
        return self.confirm_ok

    def shutdown(self):
        self.shutdown_count += 1


class DummySpineEditor(DummyLive2DEditor):
    sourceOpened = Signal(str)
    sourceFailed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("spineEditorPage")


class DummySettingsPage(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self.synced_theme = None
        self.reload_count = 0
        self.mcp_shutdown_count = 0

    def shutdown_animation_mcp(self):
        self.mcp_shutdown_count += 1
        return True

    def sync_theme_selection(self, theme):
        self.synced_theme = theme


def _page_factory(name):
    class Page(QFrame):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setObjectName(name)
    return Page


class MainWindowEditorNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="editor-main-window-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings_file = self.root / "settings.json"
        settings_file = self.settings_file

        class TempSettings(SettingsManager):
            def __init__(self, *args, **kwargs):
                super().__init__(settings_file)

        self.module = importlib.import_module("app.gui.MainWindow")
        with patch.multiple(
            self.module, SettingsManager=TempSettings,
            ExtractorPage=_page_factory("extractorPage"),
            PreviewPage=DummyPreviewPage,
            Live2DEditorPage=DummyLive2DEditor, SpineEditorPage=DummySpineEditor,
            PsdReconstructionPage=_page_factory("psdReconstructionPage"),
            SpineConverterPage=_page_factory("spineConverterPage"),
            SettingsPage=DummySettingsPage,
        ):
            self.window = self.module.MainWindow()
        self.window.resize(1320, 900)
        self.window.show()
        self.app.processEvents()
        self.addCleanup(self._dispose_window)

    def _dispose_window(self):
        self.window.live2dEditorPage.confirm_ok = self.window.spineEditorPage.confirm_ok = True
        self.window.previewPage.can_close = True
        self.window.close()
        self.window.deleteLater()
        apply_application_theme("light")
        self.app.processEvents()

    def test_editors_are_unique_routes_and_theme_button_sits_above_settings(self):
        window = self.window
        self.assertIs(window.live2dModPage, window.live2dEditorPage.mod_panel)
        self.assertEqual(window.stackedWidget.indexOf(window.live2dModPage), -1)
        self.assertIsNotNone(window.navigationInterface.widget("live2dEditorPage"))
        self.assertIsNotNone(window.navigationInterface.widget("spineEditorPage"))
        self.assertNotIn("live2dModPage", window.navigationInterface.panel.items)
        theme = window.theme_toggle_button.mapTo(window, QPoint(0, 0))
        settings = window.navigationInterface.widget("settingsPage").mapTo(window, QPoint(0, 0))
        self.assertLess(theme.y(), settings.y())
        window.switchTo(window.live2dEditorPage)
        self.app.processEvents()
        self.assertTrue(window.live2dEditorPage.active)
        self.assertFalse(window.previewPage.active)
        self.assertFalse(window.spineEditorPage.active)

    def test_cancelled_open_keeps_page_and_releases_only_new_lease(self):
        model = self.root / "resolved.json"
        model.write_text("{}", encoding="utf-8")
        current = self.window.stackedWidget.currentWidget()
        self.window.spineEditorPage.accept_source = False
        self.assertFalse(self.window.open_model_editor("spine", str(model)))
        self.assertIs(self.window.stackedWidget.currentWidget(), current)
        self.assertEqual(self.window.previewPage.leases, set())
        self.window.spineEditorPage.accept_source = True
        self.assertTrue(self.window.open_model_editor("spine", str(model)))
        self.assertEqual(len(self.window.previewPage.leases), 1)
        self.window.spineEditorPage.sourceOpened.emit(str(model))
        self.assertEqual(self.window.previewPage.leases, set())

    def test_theme_switch_persists_without_reloading_or_changing_editor(self):
        window = self.window
        window.switchTo(window.live2dEditorPage)
        window.live2dEditorPage.dirty = True
        window.on_theme_changed("light")
        window.toggle_theme()
        self.assertEqual(SettingsManager(self.settings_file).get("theme"), "dark")
        self.assertEqual(window.settingsPage.synced_theme, "dark")
        self.assertEqual(window.settingsPage.reload_count, 0)
        self.assertTrue(window.live2dEditorPage.dirty)
        self.assertIs(window.stackedWidget.currentWidget(), window.live2dEditorPage)

    def test_cancelled_close_does_not_shutdown_any_editor(self):
        window = self.window
        window.spineEditorPage.confirm_ok = False
        self.assertFalse(window.close())
        self.assertEqual(window.live2dEditorPage.shutdown_count, 0)
        self.assertEqual(window.spineEditorPage.shutdown_count, 0)
        self.assertEqual(window.previewPage.shutdown_count, 0)
        self.assertEqual(window.settingsPage.mcp_shutdown_count, 0)
        self.assertTrue(window.isVisible())
        window.spineEditorPage.confirm_ok = True
        window.previewPage.can_close = False
        confirms = window.live2dEditorPage.confirm_count
        self.assertFalse(window.close())
        self.assertEqual(window.live2dEditorPage.confirm_count, confirms)
        self.assertEqual(window.live2dEditorPage.shutdown_count, 0)

    def test_accepted_window_close_stops_application_owned_mcp(self):
        self.assertTrue(self.window.close())
        self.assertEqual(self.window.settingsPage.mcp_shutdown_count, 1)


if __name__ == "__main__":
    unittest.main()
