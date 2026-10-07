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
    from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
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
        from app.gui.editor_actions import ActionNameDialog
        dialog = ActionNameDialog("New", self.page)
        dialog.name_edit.setText("Reach")
        with patch.object(self.page.preview, "set_animation", side_effect=native_set_animation), \
             patch.object(self.page.preview, "open_plan", side_effect=native_open), \
             patch.object(self.page.preview, "set_time") as set_time, \
             patch("app.gui.SpineEditorPage.ActionNameDialog", return_value=dialog), \
             patch.object(dialog, "exec", return_value=QDialog.Accepted):
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

    def test_action_mouse_create_copy_confirm_delete_and_undo_persist(self):
        self.open()
        original = self.model.read_bytes()
        failures = []
        def run_dialog(button, name=None, accepted=True):
            def answer():
                try:
                    dialog = self.page._action_dialog
                    self.assertIs(self.app.activeModalWidget(), dialog)
                    self.assertTrue(dialog.isWindow())
                    self.page._new_animation()
                    self.assertIs(self.page._action_dialog, dialog)
                    if name is not None:
                        dialog.name_edit.setText(name)
                    QTest.mouseClick(dialog.yesButton if accepted else dialog.cancelButton, Qt.LeftButton)
                except BaseException as exc:
                    failures.append(exc)
                    if self.page._action_dialog:
                        self.page._action_dialog.reject()
            QTimer.singleShot(0, answer)
            QTest.mouseClick(button, Qt.LeftButton)
            self.app.processEvents()
            self.assertFalse(failures, failures)
        run_dialog(self.page.new_button, accepted=False)
        self.assertFalse(self.page.session.dirty)
        run_dialog(self.page.new_button, "Reach")
        self.assertEqual(self.page.animation_name, "Reach")
        run_dialog(self.page.clone_button, "ReachCopy")
        self.assertEqual(self.page.animation_name, "ReachCopy")
        run_dialog(self.page.delete_animation_button, accepted=False)
        self.assertIn("ReachCopy", self.page.session.animation_names)
        run_dialog(self.page.delete_animation_button)
        self.assertNotIn("ReachCopy", self.page.session.animation_names)
        self.page.undo_button.click()
        self.assertIn("ReachCopy", self.page.session.animation_names)
        self.page.redo_button.click()
        self.assertNotIn("ReachCopy", self.page.session.animation_names)
        output = self.root / "saved-actions"
        with patch("app.gui.SpineEditorPage.QFileDialog.getSaveFileName", return_value=(str(output), "")):
            self.assertTrue(self.page.export_copy())
        self.page.open_source(str(output / "model.json"))
        self.wait(lambda: self.page.session.original_source == output / "model.json" and not self.page.loading)
        self.assertIn("Reach", self.page.session.animation_names)
        self.assertNotIn("ReachCopy", self.page.session.animation_names)
        self.assertEqual(self.model.read_bytes(), original)

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

    def test_real_ctrl_z_ctrl_y_preserve_selected_expanded_scrolled_filtered_structure(self):
        document = json.loads(self.model.read_text(encoding="utf-8"))
        for branch in range(16):
            document["bones"].append({"name": f"branch-{branch}", "parent": "root"})
            for leaf in range(5):
                document["bones"].append({"name": f"leaf-{branch}-{leaf}", "parent": f"branch-{branch}"})
        self.model.write_text(json.dumps(document), encoding="utf-8")
        original = self.model.read_bytes()
        self.open()
        tree = self.page.tree
        self.page.search_edit.setText("leaf")
        tree.expandAll()
        items = {item.data(0, Qt.UserRole): item for item in self.page._structure_items()}
        items[("bone", "branch-2")].setExpanded(False)
        target = items[("bone", "leaf-13-3")]
        tree.setCurrentItem(target)
        tree.scrollToItem(target)
        tree.setFocus()
        self.app.processEvents()
        self.assertGreater(tree.verticalScrollBar().value(), 0)
        expected = self.page._structure_view_state()
        self.page._transform_edited("leaf-13-3", {"x": 25})
        self.assertEqual(self.page.session.bone("leaf-13-3")["x"], 25)
        for key, x in ((Qt.Key_Z, 0), (Qt.Key_Y, 25), (Qt.Key_Z, 0)):
            with self.subTest(shortcut=key, x=x):
                QTest.keyClick(tree, key, Qt.ControlModifier)
                self.app.processEvents()
                self.assertEqual(self.page.session.bone("leaf-13-3").get("x", 0), x)
                self.assertEqual(self.page._structure_view_state(), expected)
                self.assertEqual(self.page._selection, ("bone", "leaf-13-3"))
                self.assertEqual(self.page.search_edit.text(), "leaf")
                hidden = {item.data(0, Qt.UserRole) for item in self.page._structure_items() if item.isHidden()}
                self.assertIn(("bone", "hip"), hidden)
                self.assertNotIn(("bone", "leaf-13-3"), hidden)
        self.assertEqual(self.model.read_bytes(), original)

    def test_refresh_keeps_existing_nodes_and_falls_back_when_selection_was_removed(self):
        self.open()
        self.page.tree.setCurrentItem(self.page.tree.topLevelItem(0).child(0))
        self.page.session.document["bones"] = [{"name": "root"}]
        self.page.session.document["animations"] = {}
        self.page._populate()
        self.assertEqual(self.page._selection, ("bone", "root"))
        self.assertEqual(self.page.tree.currentItem().data(0, Qt.UserRole), ("bone", "root"))

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

    def open_with_parts(self):
        from PIL import Image
        Image.new("RGBA", (8, 8), (255, 100, 0, 255)).save(self.source / "page.png")
        region = "{}\n  rotate: false\n  xy: {},0\n  size: 4,4\n  orig: 4,4\n  offset: 0,0\n  index: -1\n"
        (self.source / "model.atlas").write_text(
            "page.png\nsize: 8,8\nformat: RGBA8888\nfilter: Linear,Linear\nrepeat: none\n"
            + region.format("arm", 0) + region.format("face", 4), encoding="utf-8")
        self.model.write_text(json.dumps({"skeleton": {"spine": "3.8.75"},
            "bones": [{"name": "root"}, {"name": "hip", "parent": "root"}],
            "slots": [{"name": "back", "bone": "hip", "attachment": "arm"},
                      {"name": "front", "bone": "hip", "attachment": "face"},
                      {"name": "spare", "bone": "root"}],
            "skins": [{"name": "default", "attachments": {
                "back": {"arm": {"width": 4, "height": 4}},
                "front": {"face": {"width": 4, "height": 4}},
                "spare": {"face_alt": {"path": "face", "width": 4, "height": 4}}}}],
            "animations": {}}), encoding="utf-8")
        self.open()

    def test_preview_pick_selects_the_part_in_the_active_tab(self):
        from app.core.spine_pick import PickResult
        self.open_with_parts()
        region_id = next(self.page.atlas_list.item(i).data(Qt.UserRole)[1] for i in range(self.page.atlas_list.count())
                         if self.page.atlas_list.item(i).text() == "face")
        face = PickResult(region_id=region_id, name="face", page=0, pixel=(5, 1))

        self.page.tabs.setCurrentIndex(2)
        self.page.preview.regionPicked.emit(face)
        self.assertEqual(self.page.atlas_list.currentItem().text(), "face")
        self.assertEqual(self.page._selection[0], "atlas")

        # "spare" can show the same region but has no attachment set up, so
        # the slot actually drawing it ("front") is chosen.
        self.page.tabs.setCurrentIndex(1)
        self.page.preview.regionPicked.emit(face)
        self.assertEqual(self.page._selection, ("slot", "front"))

        self.page.tabs.setCurrentIndex(0)
        self.page.preview.regionPicked.emit(face)
        self.assertEqual(self.page.tree.currentItem().data(0, Qt.UserRole), ("slot", "front"))
        self.assertIn("face", self.page.status_label.text())

        self.page.preview.regionPicked.emit(None)
        self.assertEqual(self.page._selection, ("slot", "front"))  # a miss keeps the selection

    def test_psd_menu_exports_and_writes_back_through_history(self):
        from PIL import Image
        from psd_tools import PSDImage
        from psd_tools.api.layers import PixelLayer
        self.open_with_parts()
        self.assertTrue(self.page.psd_button.isEnabled())
        output = self.root / "psd"
        self.assertTrue(self.page.export_atlas_psd(str(output)))
        psd_path = output / "spine_atlas.psd"
        psd = PSDImage.open(psd_path)
        face_id = next(self.page.atlas_list.item(i).data(Qt.UserRole)[1] for i in range(self.page.atlas_list.count())
                       if self.page.atlas_list.item(i).text() == "face")
        group = next(layer for layer in psd if layer.is_group() and layer.name == face_id)
        base = next(iter(group))
        group.remove(base)
        group.append(PixelLayer.frompil(Image.new("RGBA", base.size, (0, 0, 255, 255)), psd,
                                        name="base", top=base.top, left=base.left))
        psd.save(psd_path)
        self.assertTrue(self.page.import_atlas_psd(str(psd_path)))
        self.assertTrue(self.page.session.dirty)
        self.assertIn("face", self.page.status_label.text())
        self.assertTrue(self.page.undo_button.isEnabled())
        self.page.undo()
        self.assertFalse(self.page.session.dirty)


if __name__ == "__main__":
    unittest.main()
