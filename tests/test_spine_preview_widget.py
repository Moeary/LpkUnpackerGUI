from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("LPK_DISABLE_NATIVE_PREVIEW", "1")

try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QCoreApplication, QEvent, QThread, Signal

    import app.gui.PreviewPage as preview_page_module
    from app.gui.PreviewPage import PreviewPage
    from app.gui.SpinePreviewWidget import SpinePreviewWidget
    from app.gui.theme import palette_for_theme
except (ImportError, OSError):  # pragma: no cover - optional desktop dependency
    QApplication = None
    QCoreApplication = None
    QEvent = None
    QThread = None
    Signal = None
    preview_page_module = None
    PreviewPage = None
    SpinePreviewWidget = None
    palette_for_theme = None


@unittest.skipIf(
    QApplication is None,
    "PySide6 is optional in the lightweight test environment",
)
class SpinePreviewWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _wait(self, predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("timed out waiting for native Spine preview")

    def test_controls_bound_clock_and_preserve_animation_items(self):
        controls = preview_page_module.SpineAnimationControls()
        state = dict(previewState="ready", mode="native", animationOptions=["normal"],
                     selectedAnimation="normal", time=24.81, timeMax=16.97, loop=True)
        controls.set_state(state)
        self.assertEqual(controls.time_value.text(), "7.84 / 16.97")
        with patch.object(controls.animation_combo, "clear", wraps=controls.animation_combo.clear) as clear:
            controls.set_state(dict(state, loop=False))
            clear.assert_not_called()
        self.assertEqual(controls.time_value.text(), "16.97 / 16.97")
        controls.set_state(dict(state, time=float("inf")))
        self.assertEqual(controls.time_value.text(), "0.00 / 0.00")
        controls.deleteLater()

    def test_native_missing_bridge_reports_error_and_clear_stops_loading(self):
        widget = SpinePreviewWidget()
        failed = []
        widget.previewFailed.connect(failed.append)
        widget.show()
        asset = type("Asset", (), {
            "skeleton_path": Path("missing.skel"),
            "atlas_paths": (Path("missing.atlas"),),
            "skeleton_format": "binary",
            "family": "3.8",
        })()
        plan = type("Plan", (), {"asset": asset, "runtime": None, "mode": "runtime"})()
        widget.open_plan(plan)
        self.assertTrue(failed)
        self.assertEqual(widget.last_state.get("previewState"), "error")
        self.assertNotEqual(widget.last_state.get("previewState"), "loading")

        widget.clear_preview()
        self.app.processEvents()
        self.assertEqual(widget.current_url, "")
        self.assertFalse(widget.isVisible())
        widget.shutdown()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def test_native_controls_are_safe_before_a_plan_is_loaded(self):
        widget = SpinePreviewWidget()
        widget.show()
        widget.set_skin("default")
        widget.set_animation("idle", True)
        widget.set_paused(True)
        widget.set_loop(False)
        widget.set_time(0.25)
        widget.reset_pose()
        self.assertEqual(widget.last_state, {})
        widget.shutdown()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def test_transparent_stage_follows_current_palette(self):
        widget = SpinePreviewWidget()
        original_palette = self.app.palette()
        try:
            self.app.setPalette(palette_for_theme("dark"))
            widget.set_view_settings({"transparent_bg": True, "bg_color": "#ff00ff"})
            self.assertEqual(widget._background_color().name(), "#272c34")

            self.app.setPalette(palette_for_theme("light"))
            self.assertEqual(widget._background_color().name(), "#ffffff")

            widget.set_view_settings({"transparent_bg": False, "bg_color": "#ff00ff"})
            self.assertEqual(widget._background_color().name(), "#ff00ff")
        finally:
            self.app.setPalette(original_palette)
            widget.shutdown()
            widget.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            self.app.processEvents()

    def test_preview_page_surfaces_native_error_without_active_plan(self):
        page = PreviewPage()
        try:
            page._on_spine_preview_error("synthetic native failure")
            self.assertIn("synthetic native failure", page.preview_placeholder.text())
            self.assertFalse(page.spine_preview.isVisible())
            self.assertFalse(page._spine_mode)
        finally:
            page.close_preview_window()
            page._destroy_embedded_spine()
            page._destroy_embedded_live2d()
            page.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            self.app.processEvents()

    def test_spine_mode_keeps_three_columns_and_native_controls(self):
        page = PreviewPage()
        try:
            page.right_sidebar.setVisible(True)
            page.settings_panel.setVisible(True)
            page._set_spine_mode(True)
            self.assertTrue(page._spine_mode)
            self.assertFalse(page.right_sidebar.isHidden())
            self.assertFalse(page.settings_panel.isHidden())
            self.assertFalse(page.spine_controls.isHidden())
            self.assertTrue(page.spine_runtime_edit.isHidden())
            self.assertTrue(page.spine_runtime_btn.isHidden())
            self.assertTrue(page.motion_group.isHidden())
            self.assertTrue(page.pose_controls_card.isHidden())
            self.assertTrue(page.advanced_panel.isHidden())
            self.assertFalse(page.settings_panel.window_group.isHidden())
            self.assertTrue(page.settings_panel.interaction_group.isHidden())
            self.assertTrue(page.image_limit_label.isHidden())
            self.assertTrue(page.image_limit_spinbox.isHidden())

            page._set_spine_mode(False)
            self.assertFalse(page._spine_mode)
            self.assertTrue(page.spine_controls.isHidden())
            self.assertFalse(page.settings_panel.window_group.isHidden())
            self.assertFalse(page.settings_panel.interaction_group.isHidden())
            self.assertFalse(page.image_limit_label.isHidden())
            self.assertFalse(page.image_limit_spinbox.isHidden())
        finally:
            page.close_preview_window()
            page._destroy_embedded_spine()
            page._destroy_embedded_live2d()
            page.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            self.app.processEvents()

    def test_spine_import_switch_clears_finished_worker_and_cancels_active_one(self):
        """A rapid switch must tolerate a deleted QThread wrapper and close cleanly."""

        class MemorySettings:
            def __init__(self):
                self.values = {}

            def get(self, key, default=None):
                return self.values.get(key, default)

            def set(self, key, value):
                self.values[key] = value

            def get_temp_dir(self):
                return os.getcwd()

            def get_output_dir(self, _kind):
                return os.getcwd()

        class FakeSpineImportThread(QThread):
            previewReady = Signal(object, str)
            failed = Signal(str, str)

            def __init__(self, source_path, temp_root, runtime_root="", parent=None):
                super().__init__(parent)
                self.source_path = source_path
                self.result = None

            def run(self):
                if self.source_path == "first":
                    return
                while not self.isInterruptionRequested():
                    time.sleep(0.005)

        with patch.object(preview_page_module, "SettingsManager", MemorySettings), \
                patch.object(preview_page_module, "SpinePreviewImportThread", FakeSpineImportThread):
            page = PreviewPage()
            try:
                page.start_spine_preview_import("first")
                self._wait(lambda: not page._spine_preview_workers, timeout=2.0)
                self.assertIsNone(page._spine_preview_thread)

                page.start_spine_preview_import("second")
                self._wait(
                    lambda: page._spine_preview_thread is not None
                    and page._spine_preview_thread.isRunning(),
                    timeout=2.0,
                )
                generation = page._spine_preview_generation
                page.close_preview_window()
                self.assertGreater(page._spine_preview_generation, generation)
                self._wait(lambda: not page._spine_preview_workers, timeout=2.0)
                self.assertIsNone(page._spine_preview_thread)
            finally:
                page.close_preview_window()
                page._destroy_embedded_spine()
                page._destroy_embedded_live2d()
                page.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                self.app.processEvents()

    def test_spine_import_reads_runtime_updated_by_settings_page(self):
        """A running PreviewPage observes a SettingsPage runtime change."""

        root = Path(tempfile.mkdtemp(prefix="spine-preview-settings-test-"))
        settings_file = root / "settings.json"
        old_runtime = root / "runtime-old"
        new_runtime = root / "runtime-new"
        captured_runtimes = []

        class FileSettings:
            def __init__(self, settings_file=None):
                self.settings_file = str(settings_file or settings_file_path)
                if os.path.exists(self.settings_file):
                    with open(self.settings_file, "r", encoding="utf-8") as handle:
                        self.settings = json.load(handle)
                else:
                    self.settings = {
                        "remember_paths": True,
                        "preview": {
                            "image_limit": 48,
                            "spine_runtime_dir": str(old_runtime),
                            "ui_state": {},
                        },
                        "tools": {"spine_converter_path": "old-converter"},
                    }
                    self.save_settings()

            def get(self, key, default=None):
                value = self.settings
                try:
                    for part in str(key).split("."):
                        value = value[part]
                    return value
                except (KeyError, TypeError):
                    return default

            def set(self, key, value):
                target = self.settings
                parts = str(key).split(".")
                for part in parts[:-1]:
                    target = target.setdefault(part, {})
                target[parts[-1]] = value
                if self.get("remember_paths", True):
                    self.save_settings()

            def save_settings(self):
                Path(self.settings_file).parent.mkdir(parents=True, exist_ok=True)
                with open(self.settings_file, "w", encoding="utf-8") as handle:
                    json.dump(self.settings, handle)

            def get_temp_dir(self):
                path = root / "temp"
                path.mkdir(parents=True, exist_ok=True)
                return str(path)

            def get_output_dir(self, _kind):
                path = root / "output"
                path.mkdir(parents=True, exist_ok=True)
                return str(path)

        settings_file_path = str(settings_file)

        class CapturingSpineImportThread(QThread):
            previewReady = Signal(object, str)
            failed = Signal(str, str)

            def __init__(self, source_path, temp_root, runtime_root="", parent=None):
                super().__init__(parent)
                self.runtime_root = runtime_root
                captured_runtimes.append(runtime_root)

            def run(self):
                return

        with patch.object(preview_page_module, "SettingsManager", FileSettings), \
                patch.object(preview_page_module, "SpinePreviewImportThread", CapturingSpineImportThread):
            page = PreviewPage()
            try:
                external_settings = FileSettings(settings_file)
                external_settings.set("preview.spine_runtime_dir", str(new_runtime))
                external_settings.set("tools.spine_converter_path", "new-converter")

                page.start_spine_preview_import("source")
                self._wait(lambda: not page._spine_preview_workers, timeout=2.0)
                self.assertEqual(captured_runtimes[-1], str(new_runtime))

                page.settings_panel.opacity_slider.setValue(73)
                page._save_preview_ui_state()
                with open(settings_file, "r", encoding="utf-8") as handle:
                    saved = json.load(handle)
                self.assertEqual(saved["preview"]["spine_runtime_dir"], str(new_runtime))
                self.assertEqual(saved["tools"]["spine_converter_path"], "new-converter")
                self.assertEqual(saved["preview"]["ui_state"]["opacity"], 73)
            finally:
                page.close_preview_window()
                page._destroy_embedded_spine()
                page._destroy_embedded_live2d()
                page.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                self.app.processEvents()
                shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
