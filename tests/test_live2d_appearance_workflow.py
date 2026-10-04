"""Behavioral regressions for the common appearance and preview workspace."""
from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from threading import Event, get_ident
from unittest.mock import patch

from PIL import Image
from PySide6.QtTest import QTest
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from app.core.live2d_editor_session import prepare_readonly_preview_session
from app.core.psd_project import create_repack_dir, record_repack
from app.gui.live2d_appearance import AppearanceTaskWorkspace, SharedTaskFeedback, appearance_text
from app.core.settings_manager import SettingsManager
from tests import test_live2d_editor_page as fixtures
from tests.test_live2d_editor_session import make_model


class Live2DAppearanceWorkflowTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.Live2DEditorPageTests.setUpClass.__func__)
    setUp = fixtures.Live2DEditorPageTests.setUp
    _cleanup_page = fixtures.Live2DEditorPageTests._cleanup_page
    open = fixtures.Live2DEditorPageTests.open
    _wait_preview_worker = fixtures.Live2DEditorPageTests._wait_preview_worker

    def _version(self):
        self.assertTrue(self.page.open_psd_workspace())
        project = self.page.psd_panel.current_project
        version_id, directory = create_repack_dir(project)
        texture = directory / "result.png"
        Image.new("RGBA", (32, 32), (240, 20, 90, 255)).save(texture)
        project = record_repack(project, version_id, project.base_model_json, None, directory, [texture],
                                texture_outputs={0: texture})
        self.page.psd_panel.set_current_project(project)
        return "repack:" + version_id

    def _preview(self, path=None):
        path = path or make_model(self.root / "readonly")
        self.assertTrue(self.page._open_workspace_preview(str(path), "History"))
        self._wait_preview_worker()
        self.assertIsNotNone(self.page._preview_session)
        return path

    def test_appearance_keeps_skin_atlas_and_psd_visible_across_all_steps(self):
        self.open()
        page = self.page
        self.assertEqual(page.tabs.count(), 4)
        self.assertTrue(page.open_psd_workspace())
        page.resize(1040, 760)
        page.show()
        self.app.processEvents()
        for task in ("export", "repack", "history", "advanced"):
            page.appearance_workspace.show_psd_task(task)
            self.app.processEvents()
            self.assertTrue(page.skin_controls.isVisible())
            self.assertTrue(page.texture_tab.isVisible())
            self.assertTrue(page.psd_panel.isVisible())
            self.assertFalse(page.psd_panel.task_selector.isVisible())
            self.assertFalse(page.psd_panel.log_frame.isVisible())
            toolbar = page.appearance_workspace
            self.assertFalse(hasattr(toolbar, "return_atlas_button"))
            self.assertFalse(hasattr(page.skin_controls, "psd_button"))
            self.assertLess(page.skin_controls.width(), toolbar.upper_splitter.width())
            self.assertLess(page.texture_tab.width(), toolbar.upper_splitter.width())
            self.assertLess(toolbar.upper_splitter.height(), toolbar.psd_workspace.height())
        self.assertIsNone(page.psd_panel.preview_dialog)
        self.assertFalse(hasattr(page.psd_panel, "content_splitter"))
        page.appearance_workspace.show_skin_task()
        self.assertTrue(page.skin_controls.isVisible())
        self.assertTrue(page.texture_tab.isVisible())
        self.assertTrue(page.psd_panel.isVisible())
        self.assertFalse(page.workspace.is_panel_visible("timeline"))

    def test_appearance_splitter_drag_is_restored_without_rewriting_layout(self):
        settings = SettingsManager(self.root / "appearance-settings.json")
        first = AppearanceTaskWorkspace(QWidget(), QWidget(), QWidget(), QWidget(), settings=settings)
        second = AppearanceTaskWorkspace(QWidget(), QWidget(), QWidget(), QWidget(), settings=settings)
        self.addCleanup(first.deleteLater)
        self.addCleanup(second.deleteLater)
        first.resize(420, 650)
        first.show()
        self.app.processEvents()
        first.vertical_splitter.moveSplitter(275, 1)
        first.upper_splitter.moveSplitter(150, 1)
        saved = settings.get("editor_layouts.live2d_appearance")
        self.assertEqual(saved["layouts"]["narrow"]["extent"], first.upper_splitter.height())
        first.hide()
        second.resize(420, 650)
        with patch.object(settings, "set", wraps=settings.set) as write:
            second.show()
            for _ in range(3):
                self.app.processEvents()
            write.assert_not_called()
        self.assertAlmostEqual(second.upper_splitter.height(), saved["layouts"]["narrow"]["extent"], delta=2)
        self.assertAlmostEqual(second.upper_splitter.sizes()[0] / sum(second.upper_splitter.sizes()), saved["layouts"]["narrow"]["ratio"], delta=.01)

    def test_skin_import_and_current_atlas_drop_are_distinct_and_never_hide_psd(self):
        from app.gui.Live2DModPage import TextureMappingDialog
        self.open()
        self.page.open_psd_workspace()
        self.page.resize(1040, 760)
        self.page.show()
        self.app.processEvents()
        image = self.root / "imported.png"
        Image.new("RGBA", (32, 32), (20, 80, 160, 255)).save(image)
        session, moc = self.page.session, self.page.session.project.document["FileReferences"]["Moc"]

        def drop(surface, paths):
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
            enter = QDragEnterEvent(surface.rect().center(), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
            event = QDropEvent(QPointF(surface.rect().center()), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
            QApplication.sendEvent(surface, enter)
            QApplication.sendEvent(surface, event)
            self.assertTrue(event.isAccepted())

        def map_confirm(dialog):
            combo = dialog.table.cellWidget(0, 2)
            combo.setCurrentIndex(combo.findData("0"))
            return QDialog.Accepted

        with patch.object(self.page, "_skin_name_dialog", return_value="Imported"), patch.object(TextureMappingDialog, "exec", map_confirm):
            drop(self.page.skin_controls.scroll.viewport(), [image])
        self.assertEqual(len(session.list_skins()), 2)
        active = session.active_skin_id
        replacement = self.root / "replacement.png"
        Image.new("RGBA", (32, 32), (170, 50, 20, 255)).save(replacement)
        drop(self.page.texture_scroll.viewport(), [replacement])
        self.assertEqual(len(session.list_skins()), 2)
        self.assertEqual(session.active_skin_id, active)
        self.assertTrue(session.skin_modified)
        self.assertEqual(Image.open(session.texture_paths[0]).getpixel((0, 0)), (170, 50, 20, 255))
        data, history = session.texture_paths[0].read_bytes(), len(session._undo)
        drop(self.page.texture_scroll.viewport(), [image, replacement])
        self.assertEqual(session.texture_paths[0].read_bytes(), data)
        self.assertEqual(len(session._undo), history)
        self.assertIn(appearance_text("editor.appearance.atlas_drop_invalid"), self.page.status_label.text())
        self.assertIs(self.page.session, session)
        self.assertEqual(session.project.document["FileReferences"]["Moc"], moc)
        self.assertTrue(self.page.psd_panel.isVisible())

    def test_appearance_changes_orientation_without_losing_task_or_split_sizes(self):
        settings = SettingsManager(self.root / "adaptive-appearance.json")
        workspace = AppearanceTaskWorkspace(QWidget(), QWidget(), QWidget(), QWidget(), settings=settings)
        self.addCleanup(workspace.deleteLater)
        workspace.resize(420, 650)
        workspace.show()
        self.app.processEvents()
        workspace.show_psd_task("history")
        workspace.vertical_splitter.moveSplitter(240, 1)
        narrow_height = workspace.upper_splitter.height()
        workspace.resize(960, 650)
        self.app.processEvents()
        self.assertEqual(workspace.vertical_splitter.orientation(), Qt.Horizontal)
        self.assertEqual(workspace.upper_splitter.orientation(), Qt.Vertical)
        self.assertEqual(workspace.current_task, "history")
        self.assertGreater(workspace.psd_workspace.width(), workspace.upper_splitter.width())
        workspace.vertical_splitter.moveSplitter(270, 1)
        wide_width = workspace.upper_splitter.width()
        workspace.resize(420, 650)
        self.app.processEvents()
        self.assertAlmostEqual(workspace.upper_splitter.height(), narrow_height, delta=2)
        workspace.resize(960, 650)
        self.app.processEvents()
        self.assertAlmostEqual(workspace.upper_splitter.width(), wide_width, delta=2)
        workspace.hide()

    def test_psd_result_reuse_skips_workspace_and_undo_and_variant_retains_reverse_link(self):
        self.open()
        token = self._version()
        record = self.page.save_psd_version_as_skin(token)
        self.assertIsNotNone(record, self.page.status_label.text())
        session = self.page.session
        count, undo_count = len(session.list_skins()), len(session._undo)
        with patch.object(self.page, "_psd_version_workspace", side_effect=AssertionError("should not prepare")):
            self.page._psd_skin_ready(token)
            self.assertTrue(self.page.apply_psd_version(token))
            self.assertEqual(len(session.list_skins()), count)
            self.assertEqual(len(session._undo), undo_count)
            self.assertTrue(self.page.apply_skin("original"))
            self.assertTrue(self.page.apply_psd_version(token))
            variant = self.page.save_psd_version_as_skin(token, "Variant", variant=True)
        self.assertEqual(len(session.list_skins()), count + 1)
        self.assertEqual(session.active_skin_id, record["id"])
        self.assertEqual(session.get_skin_origin(variant["id"]), session.get_skin_origin(record["id"]))
        projects = list((session.root / "psd").rglob("project.live2dpsd.json"))
        self.assertTrue(self.page.open_skin_psd_origin(variant["id"]))
        self.assertEqual(projects, list((session.root / "psd").rglob("project.live2dpsd.json")))
        self.assertEqual(self.page.psd_panel.preview_source_token(), token)

    def test_skin_polling_uses_snapshot_without_rebuilding_controls(self):
        self.open()
        with patch.object(self.page.skin_controls.combo, "clear", wraps=self.page.skin_controls.combo.clear) as clear:
            for _ in range(12):
                self.page._update_actions()
            self.assertEqual(clear.call_count, 0)
            self.page.capture_skin("Saved")
            self.assertEqual(clear.call_count, 1)

    def test_repack_apply_save_then_export_step_keeps_navigation_and_action_synced(self):
        self.open()
        token = self._version()
        page, workspace = self.page, self.page.appearance_workspace
        page.show()
        workspace.show_psd_task("repack")
        self.assertTrue(page.apply_psd_version(token))
        self.assertIsNotNone(page.save_copy(str(self.root / "step-sync-copy")))
        workspace.show_psd_task("export")
        self.app.processEvents()
        self.assertEqual(workspace.current_task, "export")
        self.assertEqual(workspace.task_pivot.currentRouteKey(), "export")
        self.assertTrue(workspace.task_pivot.widget("export").isSelected)
        self.assertEqual(page.psd_panel.workflow_tabs.currentIndex(), 0)
        self.assertEqual(page.psd_panel.workflow, "export")
        self.assertTrue(page.psd_panel.export_card.isVisible())

    def test_psd_defaults_settings_and_project_ui_choices_survive_rebinding(self):
        from app.core.psd_project import load_project
        self.open()
        self.assertTrue(self.page.open_psd_workspace())
        panel = self.page.psd_panel
        self.assertEqual(panel.export_name_edit.currentData(), "default")
        self.assertEqual(panel.mode_combo.currentData(), "mesh")
        self.assertEqual(panel.mesh_canvas_spin.value(), 2048)
        panel.mode_combo.setCurrentIndex(panel.mode_combo.findData("atlas-components"))
        panel.mesh_canvas_spin.setValue(1024)
        panel.mesh_canvas_spin.editingFinished.emit()
        panel.texture_name_edit.setText("Remembered atlas")
        self.page.appearance_workspace.show_psd_task("repack")
        panel.save_current_project()
        saved = load_project(panel.current_project.project_file)
        self.assertEqual(saved.data["ui_state"]["export_mode"], "atlas-components")
        self.assertEqual(saved.data["ui_state"]["workflow"], "repack")
        self.assertEqual(saved.data["ui_state"]["texture_name"], "Remembered atlas")
        panel.bind_project(saved)
        self.assertEqual(panel.mode_combo.currentData(), "atlas-components")
        self.assertEqual(panel.texture_name_edit.text(), "Remembered atlas")
        self.assertEqual(panel.settings_manager.get("psd.resource_limits.mesh_max_dimension"), 1024)

    def test_single_atlas_burst_reads_only_changed_index_and_unrelated_directory_event_is_noop(self):
        document = json.loads(self.model.read_text(encoding="utf-8"))
        document["FileReferences"]["Textures"].append("second.png")
        self.model.write_text(json.dumps(document), encoding="utf-8")
        Image.new("RGBA", (32, 32), (1, 2, 3, 255)).save(self.model.parent / "second.png")
        self.open()
        page, session = self.page, self.page.session
        first = session.texture_paths[0]
        before = page.preview.reloads
        with patch.object(session, "accept_texture_change", wraps=session.accept_texture_change) as accept:
            temporary = first.parent / "atomic-save.png"
            Image.new("RGBA", (32, 32), (90, 80, 70, 255)).save(temporary)
            os.replace(temporary, first)
            for path in (first, first.parent, first, first.parent):
                page._queue_texture_reload(str(path))
            self.assertEqual(page._pending_textures, {0})
            QTest.qWait(420)
            self.assertEqual([call.args[0] for call in accept.call_args_list], [0])
            self.assertEqual(page.preview.reloads, before + 1)
            self.assertIn(str(first), page.texture_watcher.files())
            accept.reset_mock()
            (first.parent / "unrelated.txt").write_text("ignore", encoding="utf-8")
            page._queue_texture_reload(str(first.parent))
            QTest.qWait(400)
            self.assertEqual(accept.call_count, 0)
            self.assertEqual(page.preview.reloads, before + 1)

    def test_partial_save_retries_then_restores_last_good_working_texture(self):
        self.open()
        page, session = self.page, self.page.session
        path = session.texture_paths[0]
        last_good = path.read_bytes()
        path.write_bytes(b"incomplete save")
        page._queue_texture_reload(str(path))
        page._texture_timer.stop()
        for _ in range(4):
            page._process_texture_changes()
            page._texture_timer.stop()
        self.assertEqual(path.read_bytes(), last_good)
        self.assertEqual(session._texture_data[0], last_good)
        self.assertFalse(page._pending_textures)
        self.assertFalse(session.skin_modified)

    def test_preview_return_noop_error_and_change_each_load_current_once_and_keep_pose(self):
        self.open()
        page, session = self.page, self.page.session
        session.create_motion("Current", 2)
        page._populate_motions("Current")
        page._parameter_spins["ParamAngleY"].setValue(-17)
        page._flush_parameter_edit()
        preview = make_model(self.root / "other")
        invalid = self.root / "invalid.png"
        invalid.write_bytes(b"not an image")
        replacement = self.root / "replacement.png"
        Image.new("RGBA", (32, 32), (1, 100, 80, 255)).save(replacement)
        for operation in (lambda: page.replace_texture(0, str(session.texture_paths[0])),
                          lambda: page.apply_skin(session.active_skin_id),
                          lambda: page.replace_texture(0, str(invalid)),
                          lambda: page.replace_texture(0, str(replacement))):
            self._preview(preview)
            before = page.preview.reloads
            operation()
            self.assertEqual(page.preview.reloads, before + 1)
            self.assertIsNone(page._preview_session)
            self.assertEqual(page.preview.model_path, str(session.model_path))
            self.assertEqual(page._current_motion(), "Current")
            self.assertEqual(page.preview.values["ParamAngleY"], -17)
            self.assertFalse(page._native_return_pending)

    def test_latest_preview_waits_for_cancelled_worker_skips_intermediate_copy_and_swaps_on_gui_thread(self):
        self.open()
        a, b, c = [make_model(self.root / name) for name in ("A", "B", "C")]
        entered, release = Event(), Event()
        prepared, created = [], []
        gui_thread = get_ident()
        calls = []
        original_load = self.page.preview.load_model
        def load(path):
            calls.append(get_ident())
            original_load(path)
        def prepare(path, *, cancel_event=None):
            prepared.append(Path(path))
            if Path(path) == a:
                entered.set()
                release.wait(3)
            candidate = prepare_readonly_preview_session(path)
            created.append(candidate)
            return candidate
        self.addCleanup(release.set)
        with patch("app.core.live2d_editor_session.prepare_readonly_preview_session", side_effect=prepare), \
                patch.object(self.page.preview, "load_model", side_effect=load):
            self.page._open_workspace_preview(str(a), "A")
            self.assertTrue(entered.wait(2))
            self.page._open_workspace_preview(str(b), "B")
            self.page._open_workspace_preview(str(c), "C")
            self.assertEqual(len(self.page._preview_workers), 1)
            release.set()
            self._wait_preview_worker()
        self.assertEqual(prepared, [a, c])
        self.assertEqual(self.page._preview_session.source_path, c.resolve())
        self.assertEqual(calls, [gui_thread])
        self.assertFalse(created[0].root.exists())
        self.assertFalse(self.page.task_feedback._state.get("busy"))

    def test_close_and_source_switch_remain_responsive_until_preview_worker_finishes(self):
        self.open()
        path = make_model(self.root / "slow")
        entered, release = Event(), Event()
        original_session = self.page.session
        def prepare(source, *, cancel_event=None):
            entered.set()
            release.wait(3)
            return prepare_readonly_preview_session(source)
        self.addCleanup(release.set)
        with patch("app.core.live2d_editor_session.prepare_readonly_preview_session", side_effect=prepare):
            self.page._open_workspace_preview(str(path), "Slow")
            self.assertTrue(entered.wait(2))
            started = time.monotonic()
            self.assertFalse(self.page.shutdown())
            self.assertLess(time.monotonic() - started, .4)
            self.assertIs(self.page.session, original_session)
            self.assertFalse(self.page.open_source(str(path)))
            self.assertIs(self.page.session, original_session)
            release.set()
            self._wait_preview_worker()
        self.assertTrue(self.page.shutdown())
        self.assertIsNone(self.page.session)

    def test_feedback_keeps_current_running_task_against_delayed_completion(self):
        feedback = SharedTaskFeedback()
        try:
            feedback.set_state({"task": "viewer", "busy": True, "state": "running", "message": "Exporting",
                                "details": "one\ntwo"})
            feedback.set_state({"task": "psd", "busy": False, "state": "succeeded", "message": "Old PSD done"})
            self.assertEqual(feedback.status_label.text(), "Exporting")
            feedback.details_button.click()
            self.assertFalse(feedback.details.isHidden())
            self.assertEqual(feedback.details.toPlainText(), "one\ntwo")
            feedback.set_state({"task": "viewer", "busy": False, "state": "failed", "message": "Failed",
                                "details": "complete traceback"})
            self.assertEqual(feedback.details.toPlainText(), "complete traceback")
        finally:
            feedback.close()
            feedback.deleteLater()

    def test_preview_swap_and_return_failure_restore_previous_readonly_context(self):
        self.open()
        self._preview()
        previous = self.page._preview_session
        previous_context = self.page._preview_context
        other = make_model(self.root / "failed-preview")
        def load(path):
            self.page.preview.model_path = path
        with patch.object(self.page.preview, "load_model", side_effect=load), \
                patch.object(self.page, "_native_model_ready", side_effect=[RuntimeError("swap failed"), None]):
            self.page._open_workspace_preview(str(other), "Failed history")
            self._wait_preview_worker()
        self.assertIs(self.page._preview_session, previous)
        self.assertEqual(self.page._preview_context, previous_context)
        self.assertEqual(self.page.preview_context_label.text(), previous_context)
        self.assertTrue(previous.root.exists())
        with patch.object(self.page.preview, "load_model", side_effect=RuntimeError("return failed")):
            self.assertFalse(self.page.return_to_current_model())
        self.assertIs(self.page._preview_session, previous)
        self.assertEqual(self.page.preview_context_label.text(), previous_context)
        self.assertTrue(self.page.return_to_current_model())


if __name__ == "__main__":
    unittest.main()
