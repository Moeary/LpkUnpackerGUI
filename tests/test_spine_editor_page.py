from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("LPK_DISABLE_NATIVE_PREVIEW", "1")

try:
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication, QMessageBox
    from app.gui.SpineEditorPage import SpineEditorPage
    from app.core.editor_session import SpineEditorSession
except (ImportError, OSError):
    QApplication = None


@unittest.skipIf(QApplication is None, "Qt desktop dependencies are not installed")
class SpineEditorPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.model = self.source / "model.json"
        self.model.write_text(json.dumps({"skeleton": {"spine": "3.8.75"},
            "bones": [{"name": "root"}, {"name": "hip", "parent": "root"}],
            "slots": [], "skins": [], "animations": {"Idle": {"bones": {"hip": {"rotate": [
                {"time": 0, "angle": 0}, {"time": 2, "angle": 10}]}}}}}), encoding="utf-8")
        self.page = SpineEditorPage(layout_settings=False)
        self.page.resize(1040, 760)
        self.page.show()
        self.app.processEvents()

    def tearDown(self):
        self.page.shutdown()
        self.page.close()
        self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.temporary.cleanup()

    def wait(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("Spine editor operation timed out")

    def open(self):
        self.assertTrue(self.page.open_source(str(self.model)))
        self.wait(lambda: self.page.session is not None and not self.page.loading)

    def test_empty_page_does_not_load_a_model_and_fits_small_window(self):
        self.assertIsNone(self.page.session)
        self.assertFalse(self.page.save_button.isEnabled())
        self.assertEqual(self.page.preview.current_url, "")
        self.assertFalse(self.page.timeline.timer.isActive())
        self.assertLessEqual(self.page.minimumSizeHint().width(), 1040)
        self.assertLessEqual(self.page.minimumSizeHint().height(), 760)

    def test_open_is_async_and_failed_second_source_preserves_existing_session(self):
        real = SpineEditorSession.open
        def delayed(path):
            time.sleep(0.12)
            return real(path)
        opened, failed = [], []
        self.page.sourceOpened.connect(opened.append)
        self.page.sourceFailed.connect(failed.append)
        with patch("app.gui.SpineEditorPage.SpineEditorSession.open", side_effect=delayed):
            start = time.monotonic()
            self.assertTrue(self.page.open_source(str(self.model)))
            self.assertLess(time.monotonic() - start, 0.1)
            self.wait(lambda: bool(opened))
        old = self.page.session
        self.assertTrue(self.page.open_source(str(self.root / "missing.json")))
        self.wait(lambda: bool(failed))
        self.assertTrue(self.page.last_open_error)
        self.assertIs(self.page.session, old)
        self.assertTrue(old.project.model_path.is_file())
        self.page.open_source(str(self.model))
        self.assertEqual(self.page.last_open_error, "")
        self.assertEqual(len(opened), 2)

    def test_timeline_edits_undo_redo_and_export_reopen_source_unchanged(self):
        before = self.model.read_bytes()
        self.open()
        # Use the existing-trajectory dropdown as a user would.
        index = self.page.track_combo.findData(("hip", "rotate"))
        self.page.track_combo.setCurrentIndex(index)
        self.page.timeline.set_time(1)
        self.page.timeline.add_button.click()
        self.assertTrue(self.page.session.dirty)
        frames = self.page.session.get_bone_track("Idle", "hip", "rotate")["keyframes"]
        self.assertEqual(len(frames), 3)
        self.page.undo_button.click()
        self.assertFalse(self.page.session.dirty)
        self.page.redo_button.click()
        self.assertTrue(self.page.session.dirty)
        self.assertEqual(self.page.timeline.current_time, 1)
        output = self.root / "saved"
        with patch("app.gui.SpineEditorPage.QFileDialog.getSaveFileName", return_value=(str(output), "")):
            self.assertTrue(self.page.export_copy())
        self.assertFalse(self.page.session.dirty)
        self.assertEqual(self.model.read_bytes(), before)
        self.page.open_source(str(output / "model.json"))
        self.wait(lambda: self.page.session.original_source == output / "model.json")
        self.assertEqual(len(self.page.session.get_bone_track("Idle", "hip", "rotate")["keyframes"]), 3)

    def test_play_and_seek_do_not_dirty_and_hiding_pauses(self):
        self.open()
        self.page.timeline.time_spin.setValue(0.5)
        self.page.timeline.set_playing(True)
        self.assertFalse(self.page.session.dirty)
        self.page.hide()
        self.app.processEvents()
        self.assertFalse(self.page.timeline.is_playing)
        self.assertFalse(self.page.session.dirty)

    def test_new_animation_waits_for_snapshot_and_genuine_reload_errors_remain_visible(self):
        self.open()
        loaded_names = {"Idle"}
        requested, errors = [], []
        self.page.preview.previewFailed.connect(errors.append)

        def native_set_animation(name, loop):
            requested.append(name)
            if name not in loaded_names:
                self.page.preview.previewFailed.emit(f"Spine animation is unavailable: {name}")

        def native_open(plan):
            loaded_names.update(self.page.session.animation_names)

        plan = SimpleNamespace(dynamic=True, asset=SimpleNamespace(atlas_paths=("model.atlas",)))
        with patch.object(self.page.preview, "set_animation", side_effect=native_set_animation), \
             patch.object(self.page.preview, "open_plan", side_effect=native_open), \
             patch.object(self.page.preview, "set_time") as set_time, \
             patch("app.gui.SpineEditorPage.QInputDialog.getText", return_value=("Reach", True)):
            self.page.new_button.click()
            self.assertEqual(self.page.animation_name, "Reach")
            self.assertTrue(self.page._pending_preview)
            self.assertEqual(requested, [])
            self.assertEqual(errors, [])

            # The debounce has expired, but the snapshot worker has not loaded
            # the updated native model. Changing selection must still defer.
            self.page._preview_timer.stop()
            self.page._pending_preview = False
            with patch.object(self.page, "_preview_worker", object()):
                self.page.animation_combo.setCurrentIndex(self.page.animation_combo.findData("Idle"))
                self.page.animation_combo.setCurrentIndex(self.page.animation_combo.findData("Reach"))
                self.page.timeline.set_time(.75)
                self.assertEqual(requested, [])
                self.page._load_preview(plan)
            self.assertEqual(requested, ["Reach"])
            set_time.assert_called_with(.75)
            self.assertEqual(errors, [])

            # If a loaded runtime really lacks the requested animation, its
            # error must still be reported rather than filtered by its text.
            loaded_names.remove("Reach")
            with patch.object(self.page.preview, "open_plan"):
                self.page._load_preview(plan)
            self.assertEqual(errors, ["Spine animation is unavailable: Reach"])
            self.assertIn("Reach", self.page.status_label.text())

    def test_discard_save_cancellation_keeps_document_and_source(self):
        self.open()
        self.page.session.set_bone_transform("hip", {"y": 10})
        with patch("app.gui.SpineEditorPage.QMessageBox.question", return_value=QMessageBox.StandardButton.Cancel):
            self.assertFalse(self.page.confirm_discard_or_save())
        with patch("app.gui.SpineEditorPage.QMessageBox.question", return_value=QMessageBox.StandardButton.Save), \
             patch("app.gui.SpineEditorPage.QFileDialog.getSaveFileName", return_value=("", "")):
            self.assertFalse(self.page.confirm_discard_or_save())
        self.assertTrue(self.page.session.dirty)
        self.assertEqual(self.page.session.bone("hip")["y"], 10)

    def test_shutdown_waits_for_copy_worker_and_releases_private_source(self):
        real = SpineEditorSession.open
        def delayed(path):
            time.sleep(0.06)
            return real(path)
        with patch("app.gui.SpineEditorPage.SpineEditorSession.open", side_effect=delayed):
            self.page.open_source(str(self.model))
            self.page.shutdown()
        self.assertTrue(all(not worker.isRunning() for worker in self.page._workers))
        self.assertIsNone(self.page.session)


if __name__ == "__main__":
    unittest.main()
