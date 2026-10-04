from __future__ import annotations

import os
import hashlib
import json
import tempfile
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from tests.test_live2d_editor_session import make_model
from tests.test_live2d_mod_page_smoke import _TestSettings
from app.gui.Live2DEditorPage import Live2DEditorPage


class _Canvas(QObject):
    modelLoaded = Signal()
    drawableClicked = Signal(str)
    modelPointClicked = Signal(float, float)
    drawablesPicked = Signal(list)
    regionPicked = Signal(list, dict)
    selectionFailed = Signal(str)
    selectionCancelled = Signal()

    def setSelectionMode(self, mode):
        self.mode = mode

    def setSelectionSceneProvider(self, provider):
        self.provider = provider

    def getSelectionPose(self):
        owner = self.parent()
        return {"parameters": dict(owner.values), "parts": dict(getattr(owner, "parts", {})),
                "drawables": dict(getattr(owner, "drawables", {})), "parts_complete": False}


class _EditorTestSettings(_TestSettings):
    def get_output_root(self):
        return str(self.root / "output")

    def get_texture_viewer_mode(self):
        return "builtin"


class _Preview(QWidget):
    def __init__(self, model_path=None, parent=None, embedded=False):
        super().__init__(parent)
        self.live2d_canvas = _Canvas(self)
        self.live2d_container = QWidget(self)
        self.model_path = model_path
        self.values = {}
        self.reloads = 0

    def load_model(self, path):
        self.model_path = path
        self.reloads += 1
        self.live2d_canvas.modelLoaded.emit()

    def set_editor_mode(self, _enabled):
        pass

    def apply_settings(self, settings):
        self.values.update(settings.get("advanced_params", {}))

    def set_rendering_active(self, active):
        self.active = active

    def set_part_opacity_overrides(self, values, defaults=None):
        self.parts = dict(defaults or {}, **values)

    def set_drawable_opacity_overrides(self, values):
        self.drawables = dict(values)

    def set_motion_frozen(self, _frozen):
        pass


class _ModeCanvas(_Canvas):
    drawablesPickedWithMode = Signal(list, str)
    regionPickedWithMode = Signal(list, dict, str)


class _ModePreview(_Preview):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.live2d_canvas.deleteLater()
        self.live2d_canvas = _ModeCanvas(self)


class Live2DEditorPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="live2d-editor-ui-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.model = make_model(self.root / "source")
        settings = _EditorTestSettings(self.root)
        self.settings_patch = patch("app.gui.Live2DModPage.SettingsManager", return_value=settings)
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.psd_settings_patch = patch("app.gui.PsdReconstructionPage.SettingsManager", return_value=settings)
        self.psd_settings_patch.start()
        self.addCleanup(self.psd_settings_patch.stop)
        self.page = Live2DEditorPage()
        selection_options = patch.object(self.page, "_selection_export_options",
                                         return_value={"mode": "mesh", "atlas_layout": "packed"})
        selection_options.start()
        self.addCleanup(selection_options.stop)
        self.addCleanup(self._cleanup_page)

    def _cleanup_page(self):
        self.page.shutdown()
        self.page.close()
        self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def open(self):
        with patch("app.gui.Live2DPreviewWindow.Live2DPreviewWindow", _Preview):
            self.assertTrue(self.page.open_source(str(self.model)), self.page.status_label.text())

    def test_empty_editor_is_lazy_and_compact_mod_is_preserved(self):
        self.assertIsNone(self.page.preview)
        self.assertIsNone(self.page.session)
        self.assertFalse(self.page.save_button.isEnabled())
        self.assertTrue(self.page.mod_panel._compact)
        self.assertTrue(hasattr(self.page.mod_panel, "export_button"))

    def test_named_selection_controls_save_recall_update_and_undo(self):
        from app.gui.artmesh_selection_tools import SelectionNameDialog
        self.open()
        self.page.tabs.setCurrentWidget(self.page.artmesh_tab)
        inspector, controls = self.page.artmesh_inspector, self.page.selection_tools
        inspector.select_entries(["ArtMeshFace"])
        self.assertTrue(controls.save_button.isEnabled())
        def enter_name(dialog):
            dialog.name_edit.setText("面部")
            dialog.accept()
            return dialog.result()
        with patch.object(SelectionNameDialog, "exec", enter_name):
            controls.save_button.click()
        self.assertEqual(self.page.session.named_selections(), {"面部": ["ArtMeshFace"]})
        self.assertEqual(controls.combo.currentData(), "面部")
        self.assertIn("*", self.page.title_label.text())
        inspector.select_entries([])
        controls.combo.activated.emit(1)
        self.assertEqual(inspector.selected_drawable_ids(), ["ArtMeshFace"])
        self.assertEqual(len(self.page.session._undo), 1)
        controls.save_selection(replace=True)
        self.assertEqual(len(self.page.session._undo), 1)
        self.page.undo()
        self.assertEqual(controls.combo.count(), 1)
        self.assertFalse(self.page.session.dirty)
        self.page.redo()
        self.assertEqual(controls.combo.count(), 2)

    def test_viewport_resize_does_not_propagate_hidden_tabs_preferred_height(self):
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.addWidget(self.page)
        try:
            host.resize(1320, 900)
            host.show()
            self.app.processEvents()
            host.resize(1040, 760)
            self.app.processEvents()
            self.assertEqual((host.width(), host.height()), (1040, 760))
            self.assertFalse(host.hasHeightForWidth())
            self.assertFalse(self.page.hasHeightForWidth())
            self.assertEqual(self.page.layout().totalHeightForWidth(991), -1)
            self.assertLessEqual(self.page.minimumSizeHint().height(), 700)
        finally:
            self.page.setParent(None)
            host.close()
            host.deleteLater()

    def test_parameter_pose_key_and_seek_dirty_semantics(self):
        self.open()
        session = self.page.session
        session.create_motion("Move", 2)
        self.page._populate_motions("Move")
        self.page._parameter_spins["ParamAngleY"].setValue(-20)
        self.assertEqual(self.page.preview.values["ParamAngleY"], -20)
        self.page._key_current_pose()
        result = self.page.save_copy(str(self.root / "copy"))
        self.assertIsNotNone(result)
        self.assertFalse(session.dirty)
        self.page.timeline._seek(.5)
        self.assertEqual(self.page.preview.values["ParamAngleY"], -20)
        self.assertFalse(session.dirty)
        self.assertFalse(self.page.open_source(str(self.root / "missing.json")))
        self.assertIs(self.page.session, session)
        self.assertTrue(self.page.open_source(result["model_path"]))
        self.assertEqual(self.page._current_motion(), "Move[0]")

    def _add_second_parameter(self):
        inventory = self.model.parent / "live2d_parameter_inventory.json"
        data = json.loads(inventory.read_text(encoding="utf-8"))
        data["parameters"].append({"id": "ParamAngleX", "name": "水平角度", "min": -30, "max": 30, "default": 0})
        inventory.write_text(json.dumps(data), encoding="utf-8")

    def test_searchable_timeline_parameter_selector_syncs_table_and_motion_curves(self):
        self._add_second_parameter()
        self.open()
        page, session = self.page, self.page.session
        session.create_motion("Vertical", 2)
        session.set_keyframes("Vertical", "ParamAngleY", [{"time": 0, "value": -10}, {"time": 2, "value": 10}])
        session.create_motion("Horizontal", 2)
        session.set_keyframes("Horizontal", "ParamAngleX", [{"time": 0, "value": -20}, {"time": 2, "value": 20}])
        page._populate_motions("Vertical")
        page.timeline.set_time(.75)
        before = session._signature()
        index = page.parameter_combo.currentIndex()
        page.parameter_combo.setText("AngleX")
        self.assertEqual(page.parameter_combo.currentIndex(), index)
        self.assertEqual(page._selected_parameter, "ParamAngleY")
        page.parameter_search.setText("Vertical no match")
        QTest.keyClick(page.parameter_combo, Qt.Key_Return)
        self.assertEqual(page._selected_parameter, "ParamAngleX")
        self.assertEqual(page.parameter_table.currentRow(), page._parameter_rows["ParamAngleX"])
        self.assertEqual(page.parameter_search.text(), "")
        self.assertEqual(page.timeline.current_time, .75)
        self.assertEqual(session._signature(), before)
        page.parameter_table.setCurrentCell(page._parameter_rows["ParamAngleY"], 0)
        self.assertEqual(page.parameter_combo.currentData(), "ParamAngleY")
        page.motion_combo.setCurrentIndex(page.motion_combo.findData("Horizontal"))
        self.assertEqual(page.parameter_combo.currentData(), "ParamAngleX")
        self.assertEqual(page._selected_parameter, "ParamAngleX")
        self.assertEqual(page.timeline.frames[0]["value"], -20)
        session.create_motion("Empty", 2)
        page._populate_motions("Empty")
        self.assertEqual(page.parameter_combo.currentData(), "ParamAngleX")
        self.assertEqual(page.timeline.frames, [])

    def test_parameter_slider_gesture_has_one_history_and_spin_boundaries_keep_final_values(self):
        self._add_second_parameter()
        self.open()
        page, session = self.page, self.page.session
        page.resize(1040, 760)
        page.show()
        self.app.processEvents()
        page._parameter_drag_started()
        with patch.object(page, "_refresh_track", side_effect=AssertionError("drag rebuilt the timeline")), \
                patch.object(page, "_refresh_skin_controls", side_effect=AssertionError("drag rebuilt skins")), \
                patch.object(session, "_snapshot", side_effect=AssertionError("drag copied motions")):
            for value in (250, 300, 400, 600, 700):
                page.parameter_slider.setValue(value)
            self.assertEqual(page.preview.values["ParamAngleY"], 12)
            self.assertTrue(session.dirty)
            self.assertEqual(len(session._undo), 0)
        page._parameter_drag_finished()
        self.assertEqual(len(session._undo), 1)
        self.assertEqual(session.parameter_overrides["ParamAngleY"], 12)
        reloads = page.preview.reloads
        page.undo()
        self.assertFalse(session.dirty)
        self.assertEqual(page.preview.reloads, reloads)
        page.redo()
        self.assertEqual(page.preview.values["ParamAngleY"], 12)
        page.parameter_spin.setFocus()
        QTest.keyClick(page.parameter_spin, Qt.Key_Up)
        final = page.parameter_spin.value()
        page.parameter_combo.setCurrentIndex(page.parameter_combo.findData("ParamAngleX"))
        self.assertFalse(session.parameter_preview_pending)
        self.assertEqual(session.parameter_overrides["ParamAngleY"], final)
        page.parameter_spin.setValue(-11)
        page.set_active(False)
        self.assertEqual(session.parameter_overrides["ParamAngleX"], -11)
        self.assertFalse(session.parameter_preview_pending)
        result = page.save_copy(str(self.root / "gesture-copy"))
        self.assertIsNotNone(result)
        from app.core.live2d_editor_session import Live2DEditorSession
        reopened = Live2DEditorSession(result["model_path"])
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.parameter_overrides, session.parameter_overrides)

    def test_fluent_groove_and_handle_mouse_release_commit_one_parameter_gesture(self):
        self.open()
        self.page.resize(1040, 760)
        self.page.show()
        self.app.processEvents()
        slider, session = self.page.parameter_slider, self.page.session
        for target, final_slider_value in ((slider, 750), (slider.handle, 600)):
            count = len(session._undo)
            self.page._parameter_dragging = False
            QTest.mousePress(target, Qt.LeftButton, pos=target.rect().center())
            for value in (300, 400, 550, final_slider_value):
                slider.setValue(value)
            self.assertTrue(self.page._parameter_dragging)
            self.assertTrue(session.parameter_preview_pending)
            QTest.mouseRelease(target, Qt.LeftButton, pos=target.rect().center())
            self.assertFalse(self.page._parameter_dragging)
            self.assertFalse(session.parameter_preview_pending)
            self.assertEqual(session.parameter_overrides["ParamAngleY"], -30 + .06 * final_slider_value)
            self.assertEqual(len(session._undo), count + 1)

    def test_current_pose_selection_and_region_psd_share_effective_snapshot_without_dirty(self):
        self.open()
        page, session = self.page, self.page.session
        original = session._signature()
        page.set_artmesh_selection_mode("rectangle")
        self.assertEqual(page.preview.live2d_canvas.mode, "rectangle")
        region = {"x": 10, "y": 10, "width": 80, "height": 80,
                  "polygon": [[10, 10], [90, 10], [90, 90], [10, 90]]}
        page.preview.live2d_canvas.regionPicked.emit(["ArtMeshFace"], region)
        self.assertEqual(page.artmesh_inspector.selected_drawable_ids(), ["ArtMeshFace"])
        self.assertEqual(session._signature(), original)
        with patch.object(page.psd_panel, "export_selected_artmeshes", return_value=True) as export:
            self.assertTrue(page.export_selected_artmeshes())
        args, kwargs = export.call_args
        self.assertEqual(args[0], ["ArtMeshFace"])
        self.assertEqual(args[2], region)
        self.assertEqual(args[1]["parameters"], page.preview.values)
        self.assertEqual(args[1]["parts"]["PartFace"], .7)
        self.assertEqual(len(kwargs["mesh_data"]["drawables"]), 1)
        session.set_part_opacity("PartFace", 0)
        page._apply_preview_pose()
        scene = page._selection_scene()
        self.assertEqual(scene["snapshot"]["drawables"][0]["opacity"], 0)
        page.clear_artmesh_selection()
        self.assertEqual(page.artmesh_inspector.selected_drawable_ids(), [])
        self.assertFalse(page.export_selection_button.isEnabled())
        self.assertEqual(page.preview.live2d_canvas.mode, "none")
        page.preview.live2d_canvas.drawablesPicked.emit(["ArtMeshFace"])
        self.assertEqual(page.artmesh_inspector.current_entry().drawable_id, "ArtMeshFace")

    def _add_atlas_meshes(self):
        from PIL import Image
        path = self.model.parent / "drawables.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["parts"].append({"id": "PartEar", "index": 1, "opacity": .8})
        data["drawables"].append(dict(data["drawables"][0], id="ArtMeshEar", texture_index=1,
                                       parent_part_id="PartEar", parent_part_index=1))
        data["drawables"].append(dict(data["drawables"][0], id="ArtMeshHair"))
        path.write_text(json.dumps(data), encoding="utf-8")
        Image.new("RGBA", (32, 32), (200, 50, 100, 180)).save(self.model.parent / "ear.png")
        model = json.loads(self.model.read_text(encoding="utf-8"))
        model["FileReferences"]["Textures"].append("ear.png")
        self.model.write_text(json.dumps(model), encoding="utf-8")

    def test_preview_modes_are_consumed_once_and_share_cross_atlas_selection(self):
        self._add_atlas_meshes()
        with patch("app.gui.Live2DPreviewWindow.Live2DPreviewWindow", _ModePreview):
            self.assertTrue(self.page.open_source(str(self.model)))
        page = self.page
        canvas = page.preview.live2d_canvas
        before = page.session._signature()
        canvas.drawablesPickedWithMode.emit(["ArtMeshFace"], "replace")
        canvas.drawablesPicked.emit(["ArtMeshFace"])  # Native emits both for compatibility.
        self.assertEqual(page._selected_drawable_ids, ["ArtMeshFace"])
        page.artmesh_inspector.apply_selection(["ArtMeshEar"], mode="add")
        self.assertEqual(set(page._selected_drawable_ids), {"ArtMeshFace", "ArtMeshEar"})
        canvas.drawablesPickedWithMode.emit(["ArtMeshFace"], "toggle")
        canvas.drawablesPicked.emit(["ArtMeshFace"])
        self.assertEqual(page._selected_drawable_ids, ["ArtMeshEar"])
        canvas.drawablesPickedWithMode.emit([], "toggle")
        self.assertEqual(page._selected_drawable_ids, ["ArtMeshEar"])
        region = {"x": 10, "y": 10, "width": 80, "height": 80}
        canvas.regionPickedWithMode.emit(["ArtMeshHair"], region, "add")
        canvas.regionPicked.emit(["ArtMeshHair"], region)
        self.assertEqual(set(page._selected_drawable_ids), {"ArtMeshEar", "ArtMeshHair"})
        self.assertIsNone(page._selection_region)
        canvas.drawablesPickedWithMode.emit([], "replace")
        self.assertEqual(page._selected_drawable_ids, [])
        self.assertFalse(page.artmesh_export_button.isEnabled())
        self.assertEqual(page.session._signature(), before)

    def test_ctrl_multi_selection_visibility_changes_only_selected_meshes_and_restores_opacity(self):
        self._add_atlas_meshes()
        self.open()
        page, session = self.page, self.page.session
        page.resize(1040, 760)
        page.show()
        page.tabs.setCurrentWidget(page.artmesh_tab)
        self.app.processEvents()
        inspector = page.artmesh_inspector
        items = {inspector.entries[int(inspector.entry_list.item(row).data(Qt.UserRole))].drawable_id:
                 inspector.entry_list.item(row) for row in range(inspector.entry_list.count())}
        QTest.mouseClick(inspector.entry_list.viewport(), Qt.LeftButton, pos=inspector.entry_list.visualItemRect(items["ArtMeshFace"]).center())
        QTest.mouseClick(inspector.entry_list.viewport(), Qt.LeftButton, Qt.ControlModifier,
                         inspector.entry_list.visualItemRect(items["ArtMeshEar"]).center())
        self.assertEqual(set(page._selected_drawable_ids), {"ArtMeshFace", "ArtMeshEar"})
        session.set_drawable_opacity(["ArtMeshFace"], .3)
        session.set_drawable_opacity(["ArtMeshEar"], .6)
        page._apply_preview_pose()
        page._update_actions()
        self.assertEqual(page.drawable_opacity.value(), -.01)
        parts, undo = dict(session.part_overrides), len(session._undo)
        inspector.set_uv_preview_visible(False)
        self.app.processEvents()
        self.assertTrue(page.drawable_visible.isVisible())
        self.assertTrue(page.drawable_opacity.isVisible())
        QTest.mouseClick(page.drawable_visible, Qt.LeftButton)
        self.assertEqual(session.drawable_visibility_overrides, {"ArtMeshFace": False, "ArtMeshEar": False})
        self.assertEqual(len(session._undo), undo + 1)
        snapshot = {entry["id"]: entry for entry in page._selection_scene()["snapshot"]["drawables"]}
        self.assertEqual(snapshot["ArtMeshFace"]["opacity"], 0)
        self.assertEqual(snapshot["ArtMeshEar"]["opacity"], 0)
        self.assertGreater(snapshot["ArtMeshHair"]["opacity"], 0)
        self.assertEqual(session.part_overrides, parts)
        QTest.mouseClick(page.drawable_visible, Qt.LeftButton)
        self.assertEqual(session.drawable_visibility_overrides, {})
        self.assertEqual(page.preview.drawables, {"ArtMeshFace": .3, "ArtMeshEar": .6})
        self.assertEqual(session.drawable_opacity_overrides, {"ArtMeshFace": .3, "ArtMeshEar": .6})
        self.assertEqual(len(session._undo), undo + 2)
        page.undo()
        self.assertEqual(session.drawable_visibility_overrides, {"ArtMeshFace": False, "ArtMeshEar": False})
        page.redo()
        self.assertEqual(session.drawable_visibility_overrides, {})

    def test_hidden_selected_drawables_restore_only_export_snapshot_without_mutating_preview(self):
        from app.gui.editor_dialogs import EditorMessageBox
        self._add_atlas_meshes()
        self.open()
        page, session = self.page, self.page.session
        page.tabs.setCurrentWidget(page.artmesh_tab)
        page.artmesh_inspector.select_entries(["ArtMeshFace", "ArtMeshEar"])
        session.set_drawable_visibility(["ArtMeshFace", "ArtMeshHair"], False)
        page._apply_preview_pose()
        signature, undo = session._signature(), len(session._undo)
        with patch("app.gui.Live2DEditorPage.QMessageBox.question", return_value=EditorMessageBox.Yes), \
                patch.object(page, "open_psd_workspace", return_value=True), \
                patch.object(page.psd_panel, "export_selected_artmeshes", return_value=True) as export:
            self.assertTrue(page.export_selected_artmeshes())
        pose = export.call_args.args[1]
        self.assertEqual(pose["export_visibility_restored_drawables"], ["ArtMeshFace"])
        self.assertEqual(pose["drawables"], {"ArtMeshFace": 1, "ArtMeshHair": 0})
        snapshot = {entry["id"]: entry for entry in export.call_args.kwargs["mesh_data"]["drawables"]}
        self.assertGreater(snapshot["ArtMeshFace"]["opacity"], 0)
        self.assertEqual(snapshot["ArtMeshHair"]["opacity"], 0)
        self.assertEqual(page.preview.drawables, {"ArtMeshFace": 0, "ArtMeshHair": 0})
        self.assertEqual(session._signature(), signature)
        self.assertEqual(len(session._undo), undo)

    def test_batch_opacity_digits_fit_at_narrow_and_wide_sizes_in_all_languages(self):
        from app.i18n import get_i18n
        self.open()
        page = self.page
        page.tabs.setCurrentWidget(page.artmesh_tab)
        page.artmesh_inspector.select_entries(["ArtMeshFace"])
        page.show()
        language = get_i18n().language
        try:
            for locale in ("zh_CN", "en_US", "ja_JP"):
                get_i18n().set_language(locale)
                for width in (1040, 1320):
                    page.resize(width, 760)
                    self.app.processEvents()
                    for value in (1, .5):
                        page.drawable_opacity.setValue(value)
                        self.app.processEvents()
                        edit = page.drawable_opacity.lineEdit()
                        margins = edit.textMargins()
                        available = edit.width() - margins.left() - margins.right() - 4
                        self.assertLessEqual(edit.fontMetrics().horizontalAdvance(edit.text()), available)
                        self.assertTrue(page.drawable_opacity.isVisible())
                        self.assertTrue(page.drawable_visible.isVisible())
        finally:
            get_i18n().set_language(language)

    def test_uv_selection_exports_without_preview_and_invalidates_old_rectangle(self):
        self._add_atlas_meshes()
        self.open()
        page = self.page
        region = {"x": 10, "y": 10, "width": 80, "height": 80}
        page._native_region_picked(["ArtMeshFace"], region)
        self.assertEqual(page._selection_region, region)
        page.artmesh_inspector.apply_selection(["ArtMeshEar"], mode="add", primary_id="ArtMeshEar")
        self.assertIsNone(page._selection_region)
        self.assertEqual(page.artmesh_inspector.current_entry().drawable_id, "ArtMeshEar")
        self.assertEqual(page.preview.parts["PartFace"], .7)
        page.workspace.set_panel_visible("preview", False)
        self.assertTrue(page.artmesh_export_button.isEnabled())
        with patch.object(page.psd_panel, "export_selected_artmeshes", return_value=True) as export:
            self.assertTrue(page.export_selected_artmeshes())
        self.assertEqual(set(export.call_args.args[0]), {"ArtMeshFace", "ArtMeshEar"})
        self.assertIsNone(export.call_args.args[2])
        page._native_region_picked(["ArtMeshFace"], region)
        page._parameter_spins["ParamAngleY"].setValue(12)
        with patch.object(page.psd_panel, "export_selected_artmeshes", return_value=True) as export:
            self.assertTrue(page.export_selected_artmeshes())
        self.assertIsNone(export.call_args.args[2])  # The old rectangle no longer refers to this pose.

    def test_hidden_selected_export_only_restores_snapshot_parts_and_preserves_preview_history(self):
        from app.gui.editor_dialogs import EditorMessageBox
        self._add_atlas_meshes()
        self.open()
        page, session = self.page, self.page.session
        page.tabs.setCurrentWidget(page.artmesh_tab)
        page.artmesh_inspector.select_entries(["ArtMeshFace", "ArtMeshEar"])
        session.set_part_opacity("PartFace", 0)
        page._apply_preview_pose()
        signature, undo = session._signature(), list(session._undo)
        with patch("app.gui.Live2DEditorPage.QMessageBox.question", return_value=EditorMessageBox.Yes) as confirm, \
                patch.object(page, "open_psd_workspace", return_value=True), \
                patch.object(page.psd_panel, "export_selected_artmeshes", return_value=True) as export:
            self.assertTrue(page.export_selected_artmeshes())
        self.assertIn("PartFace", confirm.call_args.args[2])
        self.assertEqual(export.call_args.args[1]["parts"], {"PartFace": 1, "PartEar": .8})
        self.assertEqual(set(export.call_args.args[0]), {"ArtMeshFace", "ArtMeshEar"})
        self.assertGreater(export.call_args.kwargs["mesh_data"]["drawables"][0].get("opacity", 1), 0)
        self.assertEqual(page.preview.parts["PartFace"], 0)
        self.assertEqual(session.part_overrides, {"PartFace": 0})
        self.assertEqual(session._signature(), signature)
        self.assertEqual(session._undo, undo)
        self.assertEqual(page._selection_scene()["snapshot"]["drawables"][0]["opacity"], 0)

    def test_hidden_export_cancel_and_intrinsic_invisibility_do_not_silently_drop_ids(self):
        from app.gui.editor_dialogs import EditorMessageBox
        self.open()
        page, session = self.page, self.page.session
        page.tabs.setCurrentWidget(page.artmesh_tab)
        page.artmesh_inspector.select_entry("ArtMeshFace")
        session.set_part_opacity("PartFace", 0)
        page._apply_preview_pose()
        signature, history = session._signature(), len(session._undo)
        with patch("app.gui.Live2DEditorPage.QMessageBox.question", return_value=EditorMessageBox.Cancel), \
                patch.object(page, "open_psd_workspace") as opened, \
                patch.object(page.psd_panel, "export_selected_artmeshes") as export:
            self.assertFalse(page.export_selected_artmeshes())
            opened.assert_not_called()
            export.assert_not_called()
        self.assertEqual(session._signature(), signature)
        self.assertEqual(len(session._undo), history)
        self.assertEqual(page.preview.parts["PartFace"], 0)
        session.mesh_data["drawables"][0]["opacity"] = 0  # Intrinsic pose opacity, even after restoring Part.
        page._selection_cache = None
        with patch("app.gui.Live2DEditorPage.QMessageBox.question", return_value=EditorMessageBox.Yes), \
                patch.object(page.psd_panel, "export_selected_artmeshes") as export:
            self.assertFalse(page.export_selected_artmeshes())
            export.assert_not_called()
        self.assertIn("ArtMeshFace", page.status_label.text())

    def test_unchanged_pose_selection_does_not_rebuild_inspector_or_redecode_atlas(self):
        self.open()
        page = self.page
        page.tabs.setCurrentWidget(page.artmesh_tab)
        page._native_drawables_picked(["ArtMeshFace"])
        with patch.object(page.artmesh_inspector, "load_snapshot", wraps=page.artmesh_inspector.load_snapshot) as loaded:
            for _ in range(3):
                page._native_drawables_picked(["ArtMeshFace"])
            loaded.assert_not_called()
            page.preview.values["ParamAngleY"] = 5
            page._refresh_mesh()
            self.assertEqual(loaded.call_count, 1)
            page._refresh_mesh(force=True)
            self.assertEqual(loaded.call_count, 2)

    def test_local_artmesh_thumbnail_geometry_never_overlaps_part_controls(self):
        from PIL import Image
        self.open()
        self.page.resize(1040, 760)
        self.page.show()
        self.page.tabs.setCurrentWidget(self.page.artmesh_tab)
        self.page.artmesh_inspector.select_entry("ArtMeshFace")
        self.page.parent_part_controls.show()
        entry = self.page.artmesh_inspector.current_entry()
        for size in ((2000, 3), (3, 2000), (300, 300)):
            with patch.object(self.page.session, "local_artmesh_image", return_value=Image.new("RGBA", size)):
                self.page._artmesh_selected(entry)
                self.app.processEvents()
                label = self.page.artmesh_image
                parent = self.page.artmesh_details_widget
                self.assertLessEqual(label.mapTo(parent, label.rect().bottomLeft()).y(), self.page.part_visible.mapTo(parent, self.page.part_visible.rect().topLeft()).y())
                self.assertLessEqual(label.mapTo(parent, label.rect().bottomLeft()).y(), self.page.part_opacity.mapTo(parent, self.page.part_opacity.rect().topLeft()).y())
                self.assertLessEqual(label.height(), 160)
        with patch.object(self.page.session, "local_artmesh_image", return_value=None):
            self.page._artmesh_selected(entry)
            self.assertTrue(self.page.artmesh_image.pixmap().isNull())

    def test_artmesh_editor_two_columns_keep_local_controls_inside_at_three_language_sizes(self):
        from app.i18n import get_i18n
        self._add_atlas_meshes()
        self.open()
        page, inspector = self.page, self.page.artmesh_inspector
        page.tabs.setCurrentWidget(page.artmesh_tab)
        inspector.select_entry("ArtMeshFace")
        page.show()
        page.parent_part_controls.show()
        i18n, before = get_i18n(), get_i18n().language
        try:
            for language in ("zh_CN", "en_US", "ja_JP"):
                i18n.set_language(language)
                previous_width = 0
                for size in ((1040, 760), (1640, 1000), (1040, 760)):
                    with self.subTest(language=language, size=size):
                        page.resize(*size)
                        for _ in range(4):
                            self.app.processEvents()
                        self.assertEqual((page.width(), page.height()), size)
                        self.assertFalse(page.hasHeightForWidth())
                        self.assertEqual(inspector.layout_mode, "editor")
                        self.assertGreater(inspector.entry_list.height(), inspector.height() * .65)
                        self.assertLess(inspector.list_panel.width(), inspector.view_splitter.width())
                        self.assertIs(inspector.editor_details_scroll.widget(), page.artmesh_details_widget)
                        for widget in (page.local_preview_label, page.artmesh_image, page.part_visible,
                                       page.part_opacity, page.part_hint, page.artmesh_export_button):
                            self.assertTrue(inspector.isAncestorOf(widget))
                        self.assertEqual(page.artmesh_scroll.horizontalScrollBar().maximum(), 0)
                        parent = page.artmesh_details_widget
                        self.assertLessEqual(page.artmesh_image.mapTo(parent, page.artmesh_image.rect().bottomLeft()).y(), page.part_visible.mapTo(parent, page.part_visible.rect().topLeft()).y())
                        self.assertLessEqual(page.part_opacity.mapTo(parent, page.part_opacity.rect().bottomLeft()).y(), page.artmesh_export_button.mapTo(parent, page.artmesh_export_button.rect().topLeft()).y())
                        self.assertTrue(page.artmesh_export_button.isEnabled())
                        if size[0] > 1040:
                            self.assertGreater(inspector.atlas_frame.width(), previous_width)
                        previous_width = inspector.atlas_frame.width()
        finally:
            i18n.set_language(before)

    def test_artmesh_long_parent_id_keeps_full_hint_and_unavailable_part_remains_explained(self):
        path = self.model.parent / "drawables.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        identifier = "Part_" + "very_long_parent_name_" * 10
        data["parts"][0]["id"] = identifier
        data["drawables"][0]["parent_part_id"] = identifier
        path.write_text(json.dumps(data), encoding="utf-8")
        self.open()
        page = self.page
        page.resize(1040, 760)
        page.show()
        page.tabs.setCurrentWidget(page.artmesh_tab)
        page.artmesh_inspector.select_entry("ArtMeshFace")
        self.app.processEvents()
        self.assertEqual((page.width(), page.height()), (1040, 760))
        self.assertIn(identifier, page.part_hint.toolTip())
        self.assertIn(identifier, page.part_opacity.toolTip())
        entry = page.artmesh_inspector.current_entry()
        entry.raw["parent_part_id"] = ""
        entry.raw["parent_part_index"] = -1
        page._artmesh_selected(entry)
        self.assertFalse(page.part_visible.isEnabled())
        self.assertFalse(page.part_opacity.isEnabled())
        self.assertTrue(page.part_hint.toolTip())
        self.assertEqual(page.part_hint.toolTip(), page.artmesh_image.toolTip())

    def test_local_artmesh_zoom_dialog_is_owned_scrollable_and_keeps_full_crop(self):
        from app.gui.ImagePreviewPanel import ImageZoomScrollArea
        self.open()
        self.page.tabs.setCurrentWidget(self.page.artmesh_tab)
        self.page.artmesh_inspector.select_entry("ArtMeshFace")
        seen = []
        def inspect_dialog():
            dialog = self.app.activeModalWidget()
            seen.append(bool(dialog and dialog.isWindow() and dialog.parentWidget() is self.page))
            area = dialog.findChild(ImageZoomScrollArea)
            self.assertIsNotNone(area)
            area.zoomRequested.emit(2, area.rect().center())
            self.assertGreater(area.widget().width(), 0)
            self.assertFalse(area.widget().pixmap().isNull())
            dialog.accept()
        QTimer.singleShot(30, inspect_dialog)
        QTest.mouseClick(self.page.local_preview_button, Qt.LeftButton)
        self.assertEqual(seen, [True])
        self.assertIsNone(self.app.activeModalWidget())

    def test_mod_picker_background_is_frozen_and_uses_matching_viewport_geometry(self):
        from PySide6.QtGui import QImage
        self.open()
        page = self.page
        canvas = page.preview.live2d_canvas
        frame = QImage(400, 200, QImage.Format_RGBA8888)
        frame.fill(Qt.red)
        calls = []
        canvas.grabFramebuffer = lambda: calls.append("frame") or frame
        canvas.width = lambda: 200
        canvas.height = lambda: 100
        canvas.windowPointToCanvas = lambda x, y, snapshot: (x * 2 - 100, y * 2 - 50)
        scene = {"snapshot": {"canvas": {"width": 400, "height": 200}}, "texture_paths": []}
        page._preview_session = SimpleNamespace(source_path=self.model)
        page._mod_preview_model_id = "main"
        try:
            with patch.object(page.mod_panel, "model_by_id", return_value={"model_json": "main.json"}), \
                    patch.object(page.mod_panel, "resolve_project_path", return_value=self.model), \
                    patch.object(page.preview, "set_motion_frozen", side_effect=lambda frozen: calls.append(("frozen", frozen))), \
                    patch.object(page, "_selection_scene", side_effect=lambda: calls.append("scene") or scene):
                result = page._mod_pick_scene("main")
                self.assertEqual(calls, [("frozen", True), "frame", "scene"])
                self.assertIs(result["pose_frame"], frame)
                self.assertEqual(result["pose_frame_canvas_polygon"], [[-100, -50], [300, -50], [300, 150], [-100, 150]])
                self.assertEqual(result["model_path"], str(self.model.resolve()))
                self.assertEqual(result["model_id"], "main")
                self.assertFalse(page.session.dirty)
                calls.clear()
                self.assertIsNone(page._mod_pick_scene("other"))
                self.assertEqual(calls, [])
        finally:
            page._preview_session = None
            page._mod_preview_model_id = ""

    def test_watcher_external_save_reload_and_history(self):
        from PIL import Image
        self.open()
        self.page._pending_textures.add(0)
        Image.new("RGBA", (32, 32), (220, 20, 60, 255)).save(self.page.session.texture_paths[0])
        self.page._process_texture_changes()
        self.assertTrue(self.page.session.dirty)
        self.assertGreater(self.page.preview.reloads, 0)
        self.page.undo()
        self.assertFalse(self.page.session.dirty)
        self.page.redo()
        self.assertTrue(self.page.session.dirty)
        self.page.tabs.setCurrentWidget(self.page.artmesh_tab)
        self.page.artmesh_inspector.select_entry("ArtMeshFace")
        self.page.part_opacity.setValue(0)
        self.assertEqual(self.page.preview.parts["PartFace"], 0)
        self.page.undo()
        self.assertEqual(self.page.preview.parts["PartFace"], .7)

    def test_frozen_native_update_applies_parameters_and_restores_part_defaults(self):
        from app.gui.Live2DCanvas import Live2DCanvas
        calls = []
        model = SimpleNamespace(
            _model=SimpleNamespace(Update=lambda delta: calls.append(("update", delta))),
            SetPartOpacity=lambda index, value: calls.append(("part", index, value)),
            Draw=lambda: calls.append(("draw",)),
        )
        canvas = SimpleNamespace(model=model, _part_indices={"PartFace": 0},
                                 _part_opacity_defaults={}, _part_opacity_overrides={},
                                 _motion_frozen=True, _advanced_enabled=True,
                                 _apply_advanced_params=lambda: calls.append(("parameters",)),
                                 update=lambda: None)
        with patch("app.gui.Live2DCanvas.live2d.clearBuffer"):
            Live2DCanvas.setPartOpacityOverrides(canvas, {"PartFace": 0}, {"PartFace": .7})
            Live2DCanvas.on_draw(canvas)
            # live2d-py Draw updates Cubism Core itself. SDK Update(0) would
            # additionally evaluate Physics/Pose and drift a frozen pose.
            self.assertEqual(calls, [("parameters",), ("part", 0, 0), ("draw",)])
            calls.clear()
            Live2DCanvas.setPartOpacityOverrides(canvas, {})
            Live2DCanvas.on_draw(canvas)
            self.assertIn(("part", 0, .7), calls)

    def test_native_point_selection_uses_actual_artmesh_geometry(self):
        self.open()
        self.page.preview.live2d_canvas.modelPointClicked.emit(.5, .5)
        self.assertEqual(self.page.artmesh_inspector.current_entry().drawable_id, "ArtMeshFace")

    def _wait_psd_worker(self):
        limit = time.monotonic() + 15
        while self.page.psd_panel.is_busy() and time.monotonic() < limit:
            self.app.processEvents()
            time.sleep(.01)
        self.app.processEvents()
        self.assertFalse(self.page.psd_panel.is_busy(), self.page.psd_panel.log_text.toPlainText())
        self.assertEqual(self.page.psd_panel._task_state, "succeeded", self.page.psd_panel.log_text.toPlainText())
        self.assertEqual(self.page.psd_panel._task_progress, 100, self.page.psd_panel.log_text.toPlainText())
        if self.page.psd_panel._task_id == "project-preparation":
            self.assertIsNotNone(self.page.psd_panel.current_project)
        else:
            self.assertEqual(self.page.psd_panel.progress_bar.value(), 100, self.page.psd_panel.log_text.toPlainText())

    def _wait_preview_worker(self):
        limit = time.monotonic() + 10
        while (self.page._preview_workers or self.page._preview_request) and time.monotonic() < limit:
            self.app.processEvents()
            time.sleep(.005)
        self.app.processEvents()
        self.assertFalse(self.page._preview_workers, self.page.status_label.text())
        self.assertIsNone(self.page._preview_request, self.page.status_label.text())

    def test_complete_psd_workflows_fit_single_column_and_keep_original_page(self):
        self.open()
        self.assertTrue(self.page.open_psd_workspace())
        self.page.resize(1040, 760)
        self.page.show()
        self.app.processEvents()
        psd = self.page.psd_panel
        self.assertTrue(psd._compact)
        self.assertEqual(psd.workflow_tabs.count(), 4)
        self.assertFalse(hasattr(psd, "content_splitter"))
        self.assertIsNone(psd.preview_dialog)
        for index, card in enumerate((psd.export_card, psd.repack_card, psd.preview_control_frame, psd.project_frame)):
            self.page.appearance_workspace.show_psd_task(("export", "repack", "history", "advanced")[index])
            self.app.processEvents()
            self.assertTrue(card.isVisible(), str(index))
            self.assertEqual(psd._compact_scrolls[index].horizontalScrollBar().maximum(), 0)
            self.assertLessEqual(card.width(), psd._compact_scrolls[index].viewport().width())
        self.page.show_viewer_export()
        self.app.processEvents()
        self.assertTrue(self.page.mod_panel.isVisible())
        self.assertEqual((self.page.width(), self.page.height()), (1040, 760))
        self.assertFalse(self.page.hasHeightForWidth())

    def test_psd_two_exports_use_current_snapshot_preserve_prior_baseline(self):
        from PIL import Image
        self.open()
        self.page.session.create_motion("NewAction", 2)
        self.assertTrue(self.page.open_psd_workspace())
        psd = self.page.psd_panel
        schemes = []
        old_bytes = {}
        for mode in ("mesh", "atlas-components"):
            psd.mode_combo.setCurrentIndex(psd._combo_index_by_data(psd.mode_combo, mode))
            psd.start_reconstruction()
            self._wait_psd_worker()
            scheme = psd.current_project.data["pose_schemes"][-1]
            schemes.append(scheme)
            self.assertEqual(scheme["mode"], mode)
            exported = psd.current_project.project_dir / scheme["psd"]
            self.assertTrue(exported.is_file())
            snapshot = psd.current_project.project_dir / scheme["editor_snapshot"]
            self.assertIn("NewAction", json.loads(snapshot.read_text(encoding="utf-8"))["FileReferences"]["Motions"])
            old_bytes[exported] = exported.read_bytes()
            if mode == "mesh":
                replacement = self.root / "new-texture.png"
                Image.new("RGBA", (32, 32), (220, 20, 70, 255)).save(replacement)
                self.assertTrue(self.page.replace_texture(0, str(replacement)))
        self.assertEqual(len({scheme["id"] for scheme in schemes}), 2)
        self.assertEqual(Image.open(psd.current_project.project_dir / schemes[0]["source_textures"][0]).getpixel((0, 0)), (30, 60, 90, 255))
        self.assertEqual(Image.open(psd.current_project.project_dir / schemes[-1]["source_textures"][0]).getpixel((0, 0)), (220, 20, 70, 255))
        for path, data in old_bytes.items():
            self.assertEqual(path.read_bytes(), data)

    def test_psd_apply_preview_return_and_project_save_reopen(self):
        from PIL import Image
        from app.core.psd_project import create_repack_dir, record_repack
        from app.core.live2d_editor_session import Live2DEditorSession
        self.open()
        source_hash = hashlib.sha256(self.model.parent.joinpath("texture.png").read_bytes()).hexdigest()
        session = self.page.session
        session.create_motion("NewAction", 2)
        self.page._populate_motions("NewAction")
        self.page._parameter_spins["ParamAngleY"].setValue(-15)
        self.assertTrue(self.page.open_psd_workspace())
        psd = self.page.psd_panel
        version_id, directory = create_repack_dir(psd.current_project)
        texture = directory / "changed.png"
        Image.new("RGBA", (32, 32), (210, 35, 110, 255)).save(texture)
        project = record_repack(psd.current_project, version_id, psd.current_project.base_model_json,
                                None, directory, [texture], texture_outputs={0: texture})
        psd.set_current_project(project)
        self.page.show_viewer_export()
        result = self.page.save_copy(str(self.root / "before"))
        self.assertIsNotNone(result)
        self.assertFalse(session.dirty)
        model = self.page._psd_version_workspace("repack:" + version_id)
        self.page._open_psd_preview(str(model), str(project.project_file))
        self._wait_preview_worker()
        self.assertIs(self.page.session, session)
        self.assertIsNotNone(self.page._preview_session)
        self.assertFalse(session.dirty)
        psd.preview_source_combo.setCurrentIndex(psd._combo_index_by_data(psd.preview_source_combo, "repack:" + version_id))
        self.assertFalse(session.dirty)
        self.assertTrue(self.page.return_to_current_model())
        self.assertEqual(self.page.preview.model_path, str(session.model_path))
        self.assertEqual(self.page.preview.values["ParamAngleY"], -15)
        self.assertFalse(session.dirty)
        original = session.texture_paths[0].read_bytes()
        self.assertTrue(self.page.apply_psd_version("repack:" + version_id), self.page.status_label.text())
        self.assertEqual(Image.open(session.texture_paths[0]).getpixel((0, 0)), (210, 35, 110, 255))
        self.assertIn("NewAction", session.project.motions)
        self.page.undo()
        self.assertEqual(session.texture_paths[0].read_bytes(), original)
        self.page.redo()
        result = self.page.save_copy(str(self.root / "saved"))
        self.assertIsNotNone(result, self.page.status_label.text())
        self.assertEqual(Path(result["model_path"]).name, "model.json")
        reopened = Live2DEditorSession(result["model_path"])
        self.addCleanup(reopened.close)
        self.assertTrue(reopened.psd_project.project_file.is_file())
        self.assertTrue(reopened.mod_project.project_file.is_file())
        self.assertEqual(reopened.psd_project.data["repack_history"][0]["id"], version_id)
        self.assertIn("NewAction[0]", reopened.project.motions)
        self.assertEqual(Image.open(reopened.texture_paths[0]).getpixel((0, 0)), (210, 35, 110, 255))
        self.assertEqual(hashlib.sha256(self.model.parent.joinpath("texture.png").read_bytes()).hexdigest(), source_hash)

    def test_child_dirty_busy_and_failed_open_preserve_current_project(self):
        self.open()
        self.assertTrue(self.page.open_psd_workspace())
        session = self.page.session
        self.page.save_copy(str(self.root / "clean"))
        self.page.psd_panel._project_dirty = True
        with patch("app.gui.Live2DEditorPage.QMessageBox.warning", return_value=0x00400000):  # Cancel
            self.assertFalse(self.page.confirm_discard_or_save())
        self.page.psd_panel._project_dirty = False
        self.page.psd_panel._is_busy = True
        self.assertFalse(self.page.confirm_discard_or_save())
        self.assertIsNone(self.page.save_copy(str(self.root / "busy")))
        self.page.psd_panel._is_busy = False
        replacement = make_model(self.root / "other")
        original_binding = self.page._bind_subprojects
        def fail_candidate_binding():
            if self.page.session is not session:
                raise RuntimeError("broken child project")
            return original_binding()
        with patch.object(self.page, "_bind_subprojects", side_effect=fail_candidate_binding):
            self.assertFalse(self.page.open_source(str(replacement)))
        self.assertIs(self.page.session, session)
        self.assertEqual(self.page.preview.model_path, str(session.model_path))
        self.assertTrue(self.page.psd_panel.current_project.project_file.is_relative_to(session.root))
        self.assertFalse(session.dirty)
        self.assertEqual(self.page.last_open_error, "broken child project")

    def test_psd_picker_imports_recent_and_switches_owned_projects_without_writing_sources(self):
        from app.core.psd_project import create_project_from_source
        self.open()
        first = create_project_from_source(self.model, "PSD Alpha", output_root=self.root / "legacy")
        second = create_project_from_source(self.model, "PSD Beta", output_root=self.page.psd_panel.settings_manager.get_output_dir("psd_projects"))
        original_hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                           for root in (first.project_dir, second.project_dir, self.model.parent)
                           for path in root.rglob("*") if path.is_file()}
        psd = self.page.psd_panel
        psd.settings_manager.set("psd.project_files", [str(first.project_file)])
        self.assertTrue(self.page.open_psd_workspace())
        psd.workflow_tabs.setCurrentIndex(3)
        self.page.resize(1040, 760)
        self.page.show()
        self.app.processEvents()
        self.assertTrue(psd.project_combo.isVisible())
        def select(path):
            index = psd._combo_index_by_data(psd.project_combo, str(path))
            self.assertGreaterEqual(index, 0, psd.discover_project_files())
            psd.project_combo.setCurrentIndex(index)
            self.app.processEvents()
        select(first.project_file)
        owned_first = psd.current_project.project_file
        self.assertTrue(owned_first.is_relative_to(self.page.session.root / "psd"))
        self.assertNotEqual(owned_first, first.project_file)
        psd.mode_combo.setCurrentIndex(psd._combo_index_by_data(psd.mode_combo, "atlas-components"))
        psd.mark_project_dirty()
        select(second.project_file)
        owned_second = psd.current_project.project_file
        self.assertNotEqual(owned_second, owned_first)
        select(owned_first)
        self.assertEqual(psd.current_project.project_file, owned_first)
        self.assertEqual(psd.mode_combo.currentData(), "atlas-components")
        select(owned_second)
        self.assertEqual(psd.current_project.project_file, owned_second)
        self.assertEqual(psd._compact_scrolls[3].horizontalScrollBar().maximum(), 0)
        for path, digest in original_hashes.items():
            self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(), digest)
        self.assertEqual(psd.settings_manager.get("psd.project_files"), [str(first.project_file)])

    def test_action_mouse_dialog_guard_create_copy_delete_undo_and_save_reopen(self):
        from app.gui.editor_actions import ActionNameDialog
        self.open()
        self.page.resize(1040, 760)
        self.page.show()
        self.app.processEvents()
        before = self.model.read_bytes()
        failures = []
        def click_dialog(button, name=None, confirm=True, repeat=False):
            def answer():
                try:
                    dialog = self.page._action_dialog
                    self.assertIsNotNone(dialog)
                    self.assertTrue(dialog.isWindow())
                    self.assertIs(self.app.activeModalWidget(), dialog)
                    if repeat:
                        self.page._create_motion()
                        self.assertEqual(len([item for item in self.page.findChildren(ActionNameDialog) if item.isVisible()]), 1)
                    if name is not None:
                        dialog.name_edit.setText(name)
                    QTest.mouseClick(dialog.yesButton if confirm else dialog.cancelButton, Qt.LeftButton)
                except BaseException as exc:
                    failures.append(exc)
                    if self.page._action_dialog:
                        self.page._action_dialog.reject()
            QTimer.singleShot(0, answer)
            QTest.mouseClick(button, Qt.LeftButton)
            self.app.processEvents()
            self.assertFalse(failures, failures)
            self.assertIsNone(self.page._action_dialog)
        click_dialog(self.page.new_motion_button, confirm=False, repeat=True)
        self.assertFalse(self.page.session.dirty)
        click_dialog(self.page.new_motion_button, "NewPose", repeat=True)
        self.assertEqual(self.page._current_motion(), "NewPose")
        self.page.session.set_keyframes("NewPose", "ParamAngleY", [{"time": 0, "value": -10}, {"time": 2, "value": 10}])
        click_dialog(self.page.clone_motion_button, "CopyPose")
        self.assertEqual(self.page._current_motion(), "CopyPose")
        self.assertEqual(self.page.session.keyframes("NewPose", "ParamAngleY"), self.page.session.keyframes("CopyPose", "ParamAngleY"))
        click_dialog(self.page.delete_motion_button, confirm=False)
        self.assertIn("CopyPose", self.page.session.project.motions)
        click_dialog(self.page.delete_motion_button)
        self.assertNotIn("CopyPose", self.page.session.project.motions)
        self.page.undo_button.click()
        self.assertIn("CopyPose", self.page.session.project.motions)
        self.page.redo_button.click()
        self.assertNotIn("CopyPose", self.page.session.project.motions)
        saved = self.page.save_copy(str(self.root / "actions"))
        self.assertTrue(saved)
        self.assertTrue(self.page.open_source(saved["model_path"]))
        self.assertIn("NewPose[0]", self.page.session.project.motions)
        self.assertNotIn("CopyPose[0]", self.page.session.project.motions)
        self.assertEqual(self.model.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
