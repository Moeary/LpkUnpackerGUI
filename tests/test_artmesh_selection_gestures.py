from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("LPK_DISABLE_NATIVE_PREVIEW", "1")

import numpy as np
from PIL import Image
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.gui.ArtMeshInspector import ArtMeshInspector, ArtMeshPickDialog
from app.gui.Live2DCanvas import Live2DCanvas
from tests.test_artmesh_selection_ui import _snapshot


class ArtMeshSelectionGestureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.paths = [self.root / "wide.png", self.root / "tall.png"]
        Image.new("RGBA", (200, 100), (240, 180, 120, 255)).save(self.paths[0])
        Image.new("RGBA", (100, 200), (120, 180, 240, 255)).save(self.paths[1])
        self.snapshot = _snapshot()
        self.snapshot["drawables"].append({"id": "hand", "texture_index": 0,
            "vertices": [[75, 75], [100, 75], [75, 100]],
            "uvs": [[.75, .25], [1, .25], [.75, 0]], "indices": [0, 1, 2], "render_order": 8})
        self.widgets = []

    def tearDown(self):
        for widget in reversed(self.widgets):
            if isinstance(widget, Live2DCanvas):
                widget.model = None
            widget.close()
            widget.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def inspector(self):
        widget = ArtMeshInspector()
        self.widgets.append(widget)
        widget.resize(720, 680)
        widget.load_snapshot(self.snapshot, self.paths)
        widget.show()
        self.app.processEvents()
        return widget

    def atlas(self, inspector, page):
        QTest.mouseClick(inspector.atlas_tabs.pivot.widget(f"editor-tab-{page + 1}"), Qt.LeftButton)
        self.app.processEvents()
        return inspector.atlas_canvases[page]

    def click(self, canvas, source, *, shift=False):
        QTest.mouseClick(canvas, Qt.LeftButton, Qt.ShiftModifier if shift else Qt.NoModifier,
                         pos=canvas.source_to_view(source).toPoint())
        self.app.processEvents()

    def drag(self, canvas, first, last, *, shift=False):
        modifier = Qt.ShiftModifier if shift else Qt.NoModifier
        first = canvas.source_to_view(first).toPoint()
        last = canvas.source_to_view(last).toPoint()
        QTest.mousePress(canvas, Qt.LeftButton, modifier, pos=first)
        QTest.mouseMove(canvas, last)
        QTest.mouseRelease(canvas, Qt.LeftButton, modifier, pos=last)
        self.app.processEvents()

    def test_uv_click_shift_toggle_and_blank_keep_selection_across_atlas_pages(self):
        inspector = self.inspector()
        first, second = self.atlas(inspector, 0), self.atlas(inspector, 1)
        self.atlas(inspector, 0)
        self.click(first, (40, 20))
        self.assertEqual(inspector.selected_drawable_ids(), ["face"])
        self.atlas(inspector, 1)
        self.click(second, (20, 40), shift=True)
        self.assertEqual(set(inspector.selected_drawable_ids()), {"face", "ear"})
        self.assertEqual(inspector.current_entry().drawable_id, "ear")
        self.click(second, (90, 180), shift=True)
        self.assertEqual(set(inspector.selected_drawable_ids()), {"face", "ear"})
        self.assertEqual(inspector.current_entry().drawable_id, "ear")
        self.click(second, (20, 40), shift=True)
        self.assertEqual(inspector.selected_drawable_ids(), ["face"])
        self.click(second, (90, 180))
        self.assertEqual(inspector.selected_drawable_ids(), [])

    def test_list_shift_click_toggles_one_row_instead_of_selecting_a_range(self):
        inspector = self.inspector()
        view = inspector.entry_list.viewport()
        for row, shift in ((0, False), (2, True)):
            point = inspector.entry_list.visualItemRect(inspector.entry_list.item(row)).center()
            QTest.mouseClick(view, Qt.LeftButton, Qt.ShiftModifier if shift else Qt.NoModifier, pos=point)
        self.assertEqual(set(inspector.selected_drawable_ids()), {"face", "hand"})
        self.assertFalse(inspector.entry_list.item(1).isSelected())
        QTest.mouseClick(view, Qt.LeftButton, Qt.ShiftModifier,
                         pos=inspector.entry_list.visualItemRect(inspector.entry_list.item(0)).center())
        self.assertEqual(inspector.selected_drawable_ids(), ["hand"])
        blank = QPoint(20, view.height() - 4)
        self.assertIsNone(inspector.entry_list.itemAt(blank))
        QTest.mouseClick(view, Qt.LeftButton, Qt.ShiftModifier, pos=blank)
        self.assertEqual(inspector.selected_drawable_ids(), ["hand"])
        QTest.mouseClick(view, Qt.LeftButton, pos=blank)
        self.assertEqual(inspector.selected_drawable_ids(), [])

    def test_shift_overlap_candidate_refines_only_the_current_gesture(self):
        inspector = self.inspector()
        inspector.select_entries(["face", "hand"])
        inspector.set_pick_candidates(["face", "ear"], selection_mode="toggle")
        self.assertEqual(inspector.selected_drawable_ids(), ["hand"])
        inspector.candidate_combo.setCurrentIndex(1)
        self.assertEqual(set(inspector.selected_drawable_ids()), {"face", "ear", "hand"})
        inspector.candidate_combo.setCurrentIndex(0)
        self.assertEqual(inspector.selected_drawable_ids(), ["hand"])

    def test_uv_rectangle_replaces_shift_rectangle_adds_and_point_drag_does_not_pick(self):
        inspector = self.inspector()
        first = self.atlas(inspector, 0)
        self.drag(first, (10, 10), (50, 30))
        self.assertEqual(inspector.selected_drawable_ids(), ["face"])
        second = self.atlas(inspector, 1)
        self.drag(second, (5, 10), (25, 50), shift=True)
        self.assertEqual(set(inspector.selected_drawable_ids()), {"face", "ear"})
        self.assertEqual(inspector.overview_canvases[0].selected_indices, {0, 1})
        self.assertEqual(inspector.overview_canvases[1].selected_indices, {0, 1})
        self.drag(second, (70, 140), (95, 190))
        self.assertEqual(inspector.selected_drawable_ids(), [])
        inspector.select_entry("ear")
        inspector.set_selection_mode("point")
        self.drag(second, (20, 40), (90, 180))
        self.assertEqual(inspector.selected_drawable_ids(), ["ear"])

    def test_viewer_trigger_picker_stays_single_select_and_drag_cannot_pick_many(self):
        dialog = ArtMeshPickDialog(self.snapshot, self.paths, {"face", "ear", "hand"})
        self.widgets.append(dialog)
        dialog.show()
        self.app.processEvents()
        inspector = dialog.inspector
        inspector.select_entry("face")
        second = self.atlas(inspector, 1)
        self.click(second, (20, 40), shift=True)
        self.assertEqual(inspector.selected_drawable_ids(), ["ear"])
        self.assertEqual(dialog.selected_id, "ear")
        self.drag(second, (10, 10), (90, 190), shift=True)
        self.assertEqual(inspector.selected_drawable_ids(), ["ear"])
        self.assertTrue(dialog.validate())

    def test_native_modifiers_emit_once_on_release_and_editor_neutral_mode_never_taps(self):
        canvas = Live2DCanvas()
        self.widgets.append(canvas)
        canvas.resize(400, 400)
        canvas._fbo_width = canvas._fbo_height = 400
        canvas.model = SimpleNamespace(_model=SimpleNamespace(GetMvp=lambda: np.eye(4).flatten(order="F")))
        canvas.setSelectionSceneProvider(lambda: {"snapshot": self.snapshot, "texture_paths": self.paths})
        canvas.playDefaultTapMotion = Mock()
        points, regions = [], []
        canvas.drawablesPickedWithMode.connect(lambda ids, mode: points.append((ids, mode)))
        canvas.regionPickedWithMode.connect(lambda ids, region, mode: regions.append((ids, region, mode)))
        canvas.setEditorInteraction(True)
        canvas.setSelectionMode("none")
        QTest.mousePress(canvas, Qt.LeftButton, Qt.ShiftModifier, pos=QPoint(80, 80))
        self.assertFalse(points)
        QTest.mouseRelease(canvas, Qt.LeftButton, pos=QPoint(80, 80))
        self.assertEqual(points, [(["face", "ear"], "toggle")])
        QTest.mousePress(canvas, Qt.LeftButton, pos=QPoint(80, 80))
        QTest.mouseMove(canvas, QPoint(300, 300))
        QTest.mouseRelease(canvas, Qt.LeftButton, pos=QPoint(300, 300))
        self.assertEqual(len(points), 1)
        canvas.setSelectionMode("rectangle")
        QTest.mousePress(canvas, Qt.LeftButton, Qt.ShiftModifier, pos=QPoint(20, 20))
        QTest.mouseRelease(canvas, Qt.LeftButton, pos=QPoint(120, 120))
        self.assertEqual(regions[-1][2], "add")
        self.assertEqual(set(regions[-1][0]), {"face", "ear"})
        canvas.playDefaultTapMotion.assert_not_called()
        canvas.setEditorInteraction(False)
        QTest.mouseClick(canvas, Qt.LeftButton, pos=QPoint(80, 80))
        canvas.playDefaultTapMotion.assert_called_once()

    def test_parameter_reads_cache_only_indices_and_clear_on_same_count_model_change_unload(self):
        canvas = Live2DCanvas()
        self.widgets.append(canvas)
        values = [1.25, 2.5]
        first = SimpleNamespace(GetParamIds=Mock(return_value=["x", "y"]),
            GetParameterValue=Mock(side_effect=lambda index: values[index]), Update=Mock())
        canvas.model = first
        self.assertEqual(canvas.getParameterValues(["y", "absent"]), {"y": 2.5})
        values[1] = -99.75  # physics may exceed authoring min/max; reads are raw.
        self.assertEqual(canvas.getParameterValues(["y"]), {"y": -99.75})
        first.GetParamIds.assert_called_once()
        first.Update.assert_not_called()
        second = SimpleNamespace(GetParamIds=Mock(return_value=["other", "x"]),
            GetParameter=Mock(side_effect=lambda index: SimpleNamespace(value=[3.5, 8.25][index])), Update=Mock())
        canvas.model = second
        self.assertEqual(canvas.getParameterValues(["x", "y", "other"]), {"x": 8.25, "other": 3.5})
        second.GetParamIds.assert_called_once()
        second.Update.assert_not_called()
        unavailable = SimpleNamespace(GetParamIds=Mock(side_effect=[RuntimeError("model is not ready"), ["new"]]),
                                      GetParameterValue=Mock(return_value=6.5))
        canvas.model = unavailable
        with self.assertRaisesRegex(RuntimeError, "not ready"):
            canvas.getParameterValues(["x", "new"])
        self.assertEqual(canvas._parameter_value_indices, {})
        self.assertEqual(canvas.getParameterValues(["x", "new"]), {"new": 6.5})
        canvas.unloadModel()
        self.assertEqual(canvas.getParameterValues(["x", "y", "other"]), {})
        self.assertEqual(canvas._parameter_value_indices, {})

    def test_uv_geometry_cache_invalidates_for_new_snapshot_texture_dimensions_and_eligible_ids(self):
        inspector = self.inspector()
        self.assertEqual(inspector.hit_atlas(0, 40, 20), 0)
        original_cache = inspector._atlas_geometry_cache[0]
        self.assertEqual(inspector.hit_atlas(0, 40, 20), 0)
        self.assertIs(inspector._atlas_geometry_cache[0], original_cache)
        inspector.set_allowed_drawable_ids({"hand"})
        self.assertIsNone(inspector.hit_atlas(0, 40, 20))
        inspector.set_allowed_drawable_ids(None)
        self.snapshot["drawables"][0]["uvs"] = [[.75, .25], [1, .25], [.75, 0]]
        inspector.load_snapshot(self.snapshot, self.paths)
        self.assertIsNone(inspector.hit_atlas(0, 40, 20))
        inspector.texture_sizes[0] = (400, 200)
        self.assertIsNotNone(inspector.hit_atlas(0, 320, 160))
        self.assertEqual(inspector._atlas_geometry_cache[0][0], (400, 200))


if __name__ == "__main__":
    unittest.main()
