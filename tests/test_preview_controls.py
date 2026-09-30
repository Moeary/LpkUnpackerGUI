from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from app.core.settings_manager import SettingsManager
from app.gui.PreviewPage import PreviewPage
from tests.test_editor_navigation import DummySpinePreview


class FakeNativePreview(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.live2d_canvas = SimpleNamespace()
        self.meta = [{"id": "ParamAngleX", "min": -30, "max": 30, "default": 0, "value": 0}]
        self.settings = {}
        self.part_calls = []
        self.motion_times = []
        self.motion_seek_items = []
        self.failure = None
        self.playback = None

    def load_model(self, path):
        if self.failure:
            raise self.failure
        self.loaded = path

    def unload_model(self):
        pass

    def apply_settings(self, settings):
        self.settings.update(settings)

    def get_parameter_meta_list(self):
        return copy.deepcopy(self.meta)

    def set_motion_frozen(self, value):
        self.frozen = value

    def set_motion_loop(self, value):
        self.loop = value

    def set_selected_motion(self, group, index):
        self.motion = group, index

    def set_motion_time(self, motion, time):
        self.motion_times.append(time)
        self.motion_seek_items.append(copy.deepcopy(motion))
        self.meta[0]["value"] = 12.5
        self.playback = dict(motion, time=time, frozen=True, scrubbed=True)
        return {"ParamAngleX": 12.5}

    def get_motion_playback_state(self):
        return copy.deepcopy(self.playback)

    def set_part_opacity_overrides(self, values, defaults):
        self.part_calls.append((dict(values), dict(defaults)))

    def set_rendering_active(self, active):
        self.active = active

    def shutdown(self):
        pass


class FakeCoreModel:
    def drawable_snapshot(self, parameters):
        mesh = {"id": "HairMesh", "drawable_id": "HairMesh", "parent_part_id": "PartHair", "parent_part_index": 0,
                "texture_index": 0, "vertices": [[10, 10], [90, 10], [90, 90]],
                "uvs": [[0, 1], [1, 1], [1, 0]], "indices": [0, 1, 2], "opacity": 1,
                "visible": True, "render_order": 1}
        return {"canvas": {"width": 100, "height": 100}, "parts": [{"id": "PartHair", "opacity": .8}],
                "drawables": [mesh, dict(mesh, id="HairMesh2", drawable_id="HairMesh2", render_order=2)]}


class PreviewControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="preview-controls-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        settings_file = self.root / "settings.json"

        class TempSettings(SettingsManager):
            def __init__(self, *args, **kwargs):
                super().__init__(settings_file)

        self.module = importlib.import_module("app.gui.PreviewPage")
        self.mesh_module = importlib.import_module("app.gui.PreviewArtMeshPanel")
        with patch.object(self.module, "SettingsManager", TempSettings), patch.object(self.module, "SpinePreviewWidget", DummySpinePreview), patch.object(PreviewPage, "_ensure_embedded_live2d", return_value=None):
            self.page = PreviewPage()
        self.native = FakeNativePreview(self.page.preview_dock_area)
        self.page.live2d_preview = self.native
        self.page.preview_dock_layout.addWidget(self.native)
        self.page._ensure_embedded_live2d = lambda: self.native
        self.page.resize(1040, 760)
        self.page.show()
        self.app.processEvents()
        self.addCleanup(self.dispose)
        self.model = self.root / "character.model3.json"
        self.model.write_text(json.dumps({"Version": 3, "FileReferences": {"Moc": "character.moc3", "Textures": ["texture.png"]}}), encoding="utf-8")
        (self.root / "character.moc3").write_bytes(b"fake-moc")
        Image.new("RGBA", (32, 32), (40, 80, 120, 255)).save(self.root / "texture.png")
        self.motion = {"group": "Idle", "index": 0, "display": "Idle / 0", "duration": 2}
        motion_path = self.root / "idle.motion3.json"
        motion_path.write_text(json.dumps({"Meta": {"Duration": 2}, "Curves": [
            {"Target": "Parameter", "Id": "ParamAngleX", "Segments": [0, 0, 0, 2, 20]},
            {"Target": "Model", "Id": "ParamOther", "Segments": [0, 0, 0, 2, 1]},
        ]}), encoding="utf-8")
        self.motion["file"] = str(motion_path)

    def dispose(self):
        self.page.shutdown()
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()

    def load_live(self):
        with patch.object(self.module.InfoBar, "success"), patch.object(self.page, "_load_motions_from_model_json", return_value=[self.motion]):
            self.page.load_model_preview(str(self.model))
            self.page._live2d_load_timer.stop()
            self.assertTrue(self.page.preview_current_model())
        self.page._parameter_refresh_timer.stop()
        self.page._refresh_parameter_controls()
        self.app.processEvents()

    def test_live_controls_freeze_seek_parameters_are_reachable_on_right(self):
        self.load_live()
        page = self.page
        self.assertIs(page.resource_details_stack.currentWidget(), page.live2d_details)
        for widget in (page.settings_panel, page.motion_group, page.advanced_panel, page.pose_controls_card):
            self.assertTrue(page.right_sidebar.isAncestorOf(widget))
            self.assertFalse(page.left_sidebar.isAncestorOf(widget))
        page.freeze_motion_check.click()
        self.assertTrue(self.native.frozen)
        self.assertTrue(page.motion_timeline_frame.isVisible())
        self.assertEqual(page.motion_time_spin.maximum(), 2000)
        page.motion_time_spin.setValue(1250)
        self.assertEqual(self.native.motion_times[-1], 1.25)
        slider, _, scale = page.advanced_panel.advanced_param_sliders["ParamAngleX"]
        self.assertEqual(slider.value() / scale, 12.5)
        top = page.motion_time_spin.mapTo(page, QPoint())
        self.assertGreater(top.x(), page.preview_stage.x())
        self.assertLess(top.y() + page.motion_time_spin.height(), page.height())
        page.set_active(False)
        self.assertFalse(page._parameter_sync_timer.isActive())
        page.set_active(True)
        self.assertTrue(page._parameter_sync_timer.isActive())

    def test_clock_is_visible_while_playing_and_tracks_native_identity(self):
        self.load_live()
        page = self.page
        self.assertTrue(page.motion_timeline_frame.isVisible())
        self.assertFalse(page.motion_time_spin.isEnabled())
        other = dict(self.motion, group="TapBody", index=1, duration=4)
        page._motion_items.append(other)
        self.native.playback = dict(other, time=1.25, playing=True)
        page._sync_live_parameter_controls()
        self.assertEqual(page.motion_combo.currentIndex(), 0)  # selection is not proof of playback
        self.assertEqual(page.motion_time_spin.value(), 1250)
        self.assertEqual(page.motion_time_spin.maximum(), 4000)
        self.assertIn("TapBody[1]", page.motion_time_label.text())
        page.freeze_motion_check.click()
        self.assertEqual(page.motion_time_spin.value(), 1250)
        self.assertTrue(page.motion_time_spin.isEnabled())
        page._sync_live_parameter_controls()
        self.assertEqual(page.motion_time_spin.value(), 1250)
        page.freeze_motion_check.click()
        self.assertEqual(page.motion_time_spin.value(), 1250)

    def test_freeze_preserves_exact_native_values_until_that_parameter_is_edited(self):
        self.load_live()
        self.native.meta[0]["value"] = 3.14159265
        self.native.meta.append({"id": "ParamOther", "min": -30, "max": 30, "default": 0, "value": 2.71828183})
        self.page._refresh_parameter_controls()
        self.page.freeze_motion_check.click()
        self.assertEqual(self.native.settings["advanced_params"], {"ParamAngleX": 3.14159265, "ParamOther": 2.71828183})
        slider, _, scale = self.page.advanced_panel.advanced_param_sliders["ParamAngleX"]
        slider.setValue(int(12.5 * scale))
        self.assertEqual(self.native.settings["advanced_params"], {"ParamAngleX": 12.5, "ParamOther": 2.71828183})

    def test_selected_motion_without_autoplay_does_not_change_displayed_seek_identity(self):
        self.load_live()
        page = self.page
        other = dict(self.motion, group="TapBody", index=1, duration=4)
        page.auto_play_motion_check.setChecked(False)
        page._populate_motion_controls([self.motion, other])
        self.native.playback = dict(self.motion, time=.75, playing=True)
        page.motion_combo.setCurrentIndex(1)
        page._sync_live_parameter_controls()
        self.assertIn("Idle[0]", page.motion_time_label.text())
        page.freeze_motion_check.click()
        page.motion_timeline.setValue(625)
        self.assertEqual(self.native.motion_times[-1], 1.25)
        self.assertEqual(self.native.motion_seek_items[-1]["group"], "Idle")
        self.assertIn("Idle[0]", page.motion_time_label.text())

    def test_mouse_tabs_return_from_artmesh_and_inspect_current_pose_parameters(self):
        self.load_live()
        self.native.meta[0]["value"] = 3.14159265
        self.native.meta.append({"id": "ParamOther", "min": 0, "max": 1, "value": .125})
        self.page._refresh_parameter_controls()
        with patch.object(self.mesh_module, "CubismCore") as core:
            core.return_value.load_moc.return_value = FakeCoreModel()
            page = self.page
            for index in (2, 0, 1, 2):
                item = page.live2d_details.pivot.widget(page.live2d_details._keys[index])
                page.live2d_details.tab_scroll.ensureWidgetVisible(item, 8, 0)
                self.app.processEvents()
                QTest.mouseClick(item, Qt.LeftButton)
                self.app.processEvents()
                self.assertEqual(page.live2d_details.currentIndex(), index)
            panel = page.artmesh_panel
            panel.select_drawable("HairMesh2")
            self.assertEqual(panel._parameter_rows, ["ParamAngleX"])
            self.assertEqual(float(panel.parameter_table.item(0, 1).text()), 3.14159)
            panel.freeze_pose.click()
            self.assertTrue(page.freeze_motion_check.isChecked())
            self.assertEqual(panel.inspector.current_entry().drawable_id, "HairMesh2")
            panel.parameter_search.setText("Other")
            self.assertEqual(panel._parameter_rows, ["ParamOther"])
            self.assertEqual(panel.parameter_table.item(0, 1).text(), "0.125")
            self.assertEqual(panel.inspector.current_entry().drawable_id, "HairMesh2")
            panel.parameter_table.cellDoubleClicked.emit(0, 0)
            self.assertEqual(page.live2d_details.currentIndex(), 0)
            self.assertEqual(panel.inspector.current_entry().drawable_id, "HairMesh2")

    def test_artmesh_controls_use_part_id_and_leave_source_unchanged(self):
        self.load_live()
        source_files = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in self.root.iterdir() if path.name != "settings.json"}
        with patch.object(self.mesh_module, "CubismCore") as core:
            core.return_value.load_moc.return_value = FakeCoreModel()
            page = self.page
            page.live2d_details.setCurrentIndex(2)
            panel = page.artmesh_panel
            self.assertEqual(panel.inspector.current_entry().drawable_id, "HairMesh")
            panel.part_visible.click()
            self.assertEqual(self.native.part_calls[-1][0], {"PartHair": 0})
            self.assertNotIn("HairMesh", panel.part_overrides)
            page._preview_drawable_clicked("PartHair")
            self.assertEqual(panel.inspector.current_entry().drawable_id, "HairMesh")
            panel.reset_button.click()
            self.assertEqual(self.native.part_calls[-1], ({}, {"PartHair": .8}))
            page._preview_model_point_clicked(.75, .25)
            self.assertEqual(panel.inspector.current_entry().drawable_id, "HairMesh2")
            panel.part_opacity.setValue(.2)
            self.assertEqual(self.native.part_calls[-1][0], {"PartHair": .2})
        self.assertEqual(source_files, {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                       for path in self.root.iterdir() if path.name != "settings.json"})
        self.assertFalse(Path(panel.inspector.metadata_path).is_relative_to(self.root))

    def test_switching_formats_clears_parameter_and_part_overrides(self):
        self.load_live()
        page = self.page
        page.artmesh_panel.part_overrides = {"PartHair": 0}
        page.freeze_motion_check.setChecked(True)
        page._show_spine_stage()
        self.assertIs(page.resource_details_stack.currentWidget(), page.spine_details)
        self.assertTrue(page.pose_controls_card.isHidden())
        self.assertFalse(page.artmesh_panel.part_overrides)
        self.assertEqual(self.native.part_calls[-1], ({}, {}))
        self.assertFalse(page.advanced_panel.advanced_param_sliders)
        self.assertFalse(page.freeze_motion_check.isChecked())
        page._show_image_stage()
        self.assertIs(page.resource_details_stack.currentWidget(), page.image_item_list)
        self.assertTrue(page.open_editor_btn.isHidden())
        self.load_live()
        self.assertFalse(page.artmesh_panel.part_overrides)
        slider, _, scale = page.advanced_panel.advanced_param_sliders["ParamAngleX"]
        self.assertEqual(slider.value() / scale, 0)

    def test_images_keep_large_center_vertical_thumbnails_and_navigation(self):
        page = self.page
        paths = []
        for index in range(3):
            path = self.root / f"image-{index}.png"
            Image.new("RGBA", (200, 150), (index * 70, 90, 120, 255)).save(path)
            paths.append(str(path))
        page.load_image_preview(paths, str(self.root), temporary=False)
        self.app.processEvents()
        panel = page.image_preview_panel
        self.assertIs(page.resource_details_stack.currentWidget(), page.image_item_list)
        self.assertTrue(page.right_sidebar.isAncestorOf(panel.side_panel))
        y = [widget.mapTo(page, QPoint()).y() for widget in panel._list_items]
        self.assertEqual(y, sorted(set(y)))
        panel.next_btn.click()
        self.assertEqual(panel._current_index, 1)
        self.assertEqual(page.resource_combo.currentIndex(), 1)
        QTest.mouseClick(panel._list_items[2], Qt.LeftButton)
        self.assertEqual(panel._current_index, 2)
        panel.actual_btn.click()
        self.assertFalse(panel._fit_to_window)
        panel.fit_btn.click()
        self.assertTrue(panel._fit_to_window)
        self.assertFalse(panel._current_pixmap.isNull())
        self.assertTrue(page.open_editor_btn.isHidden())

    def test_renderer_failure_keeps_editor_source_and_reports_error(self):
        self.load_live()
        self.native.failure = RuntimeError("GPU failed")
        with patch.object(self.page, "show_error") as error:
            self.assertFalse(self.page.preview_current_model())
            self.assertIn("GPU failed", error.call_args.args[1])
        self.assertEqual(self.page.current_editor_source(), ("live2d", str(self.model)))
        self.assertFalse(self.page.open_editor_btn.isHidden())

    def test_render_companion_routes_complete_model_to_editor_and_mixed_image_clears_it(self):
        rendered = self.root / "character.preview.json"
        document = json.loads(self.model.read_text(encoding="utf-8"))
        document["FileReferences"]["Motions"] = {"Commands": [{"Command": "start_mtn Idle"}]}
        self.model.write_text(json.dumps(document), encoding="utf-8")
        filtered = copy.deepcopy(document)
        filtered["FileReferences"]["Motions"] = {}
        rendered.write_text(json.dumps(filtered), encoding="utf-8")
        with patch.object(self.module.InfoBar, "success"):
            self.page.load_model_preview(str(rendered), editor_model_json=str(self.model))
        self.page._live2d_load_timer.stop()
        self.assertEqual(self.page.current_editor_source(), ("live2d", str(self.model)))
        self.assertEqual(self.page.current_model_path, str(rendered))
        image = self.root / "texture.png"
        self.page._preview_items.append({"kind": "image", "path": str(image), "source_dir": str(self.root)})
        self.page.image_preview_panel.load_items(self.page._preview_items)
        self.page.activate_preview_item(1)
        self.assertEqual(self.page._resource_mode, "image")
        self.assertIsNone(self.page.current_editor_source())
        self.assertIs(self.page.resource_details_stack.currentWidget(), self.page.image_item_list)


if __name__ == "__main__":
    unittest.main()
