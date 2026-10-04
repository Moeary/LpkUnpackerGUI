from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("LPK_DISABLE_NATIVE_PREVIEW", "1")

import numpy as np
from PIL import Image
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout

from app.gui.ArtMeshInspector import ArtMeshInspector, ArtMeshPickDialog
from app.gui.Live2DCanvas import Live2DCanvas
from app.gui.Live2DModPage import Live2DModPage
from app.core.live2dviewer_mod_project import Live2DViewerModProject


def _snapshot():
    return {"coordinate_space": "canvas-pixels-y-down",
            "canvas": {"width": 100, "height": 100, "origin_x": 50, "origin_y": 50, "pixels_per_unit": 50},
            "drawables": [
                {"id": "face", "texture_index": 0, "vertices": [[0, 0], [100, 0], [0, 100]],
                 "uvs": [[0, 1], [1, 1], [0, 0]], "indices": [0, 1, 2], "render_order": 5},
                {"id": "ear", "texture_index": 1, "vertices": [[0, 0], [100, 0], [0, 100]],
                 "uvs": [[0, 1], [1, 1], [0, 0]], "indices": [0, 1, 2], "render_order": 3},
            ]}


class ArtMeshSelectionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.paths = [self.root / "wide.png", self.root / "tall.png"]
        Image.new("RGBA", (200, 100), (240, 180, 120, 255)).save(self.paths[0])
        Image.new("RGBA", (100, 200), (120, 180, 240, 255)).save(self.paths[1])
        self.widgets = []

    def tearDown(self):
        for widget in reversed(self.widgets):
            widget.close()
            widget.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def test_multiple_atlas_tabs_mouse_click_uv_selection_and_highlights_are_visible(self):
        host = QWidget()
        host.resize(410, 590)
        layout = QVBoxLayout(host)
        inspector = ArtMeshInspector(parent=host)
        layout.addWidget(inspector)
        inspector.load_snapshot(_snapshot(), self.paths)
        self.widgets.append(host)
        host.show()
        self.app.processEvents()
        self.assertEqual(inspector.atlas_tabs.count(), 3)
        inspector.select_entries(["face", "ear"], primary_id="ear")
        self.assertEqual(set(inspector.selected_drawable_ids()), {"face", "ear"})
        for canvas in inspector.overview_canvases.values():
            self.assertEqual(canvas.selected_indices, {0, 1})
            self.assertFalse(canvas.pixmap.isNull())
        # Click actual Fluent tab controls back and forth, not setCurrentIndex.
        for index in (2, 0, 1, 2):
            QTest.mouseClick(inspector.atlas_tabs.pivot.widget(f"editor-tab-{index}"), Qt.MouseButton.LeftButton)
            self.app.processEvents()
            self.assertEqual(inspector.atlas_tabs.currentIndex(), index)
        canvas = inspector.atlas_canvases[1]
        point = canvas.source_to_view((25, 50)).toPoint()
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=point)
        self.app.processEvents()
        self.assertEqual(inspector.current_entry().drawable_id, "ear")
        self.assertEqual(inspector.entry_list.currentRow(), 1)
        self.assertTrue(inspector.entry_list.visualItemRect(inspector.entry_list.item(1)).intersects(inspector.entry_list.viewport().rect()))
        self.assertEqual(inspector.texture_combo.currentData(), 1)
        self.assertEqual(inspector.overview_scroll.horizontalScrollBar().maximum(), 0)
        self.assertLessEqual(inspector.minimumSizeHint().width(), 410)

    def test_native_highlights_follow_transform_keep_roles_and_clear_without_scene_access(self):
        canvas = Live2DCanvas()
        self.widgets.append(canvas)
        canvas.resize(400, 400)
        canvas._fbo_width = canvas._fbo_height = 400
        canvas.model = SimpleNamespace(_model=SimpleNamespace(GetMvp=lambda: np.eye(4).flatten(order="F")))
        snapshot = _snapshot()
        snapshot["drawables"][0].update(vertices=[[0, 0], [100, 0], [100, 100], [0, 100]],
                                          indices=[0, 1, 2, 0, 2, 3])
        provider = Mock(return_value={"snapshot": snapshot, "texture_paths": self.paths})
        canvas.setSelectionSceneProvider(provider)
        canvas.setSelectionHighlights(["face"], ["face", "ear"])
        selected, related = canvas._selection_highlight_lines()
        self.assertEqual(len(selected), 4)  # no internal triangulation diagonal
        self.assertEqual(len(related), 3)
        for line in selected:
            self.assertTrue(0 <= line.x1() <= 400 and 0 <= line.y1() <= 400)
        canvas.setSelectionHighlights([], [])
        provider.reset_mock()
        self.assertEqual(canvas._selection_highlight_lines(), ([], []))
        provider.assert_not_called()

    def test_overlap_candidates_can_choose_back_layer_and_refresh_keeps_multiselection(self):
        inspector = ArtMeshInspector()
        self.widgets.append(inspector)
        inspector.load_snapshot(_snapshot(), self.paths)
        inspector.set_pick_candidates(["face", "ear"])
        self.assertFalse(inspector.candidate_combo.isHidden())
        inspector.candidate_combo.setCurrentIndex(1)
        self.assertEqual(inspector.current_entry().drawable_id, "ear")
        inspector.select_entries(["face", "ear"])
        inspector.load_snapshot(_snapshot(), self.paths)
        self.assertEqual(set(inspector.selected_drawable_ids()), {"face", "ear"})

    def test_pose_zoom_and_pan_keep_click_coordinates_and_search_is_cleared_for_new_hit(self):
        inspector = ArtMeshInspector()
        self.widgets.append(inspector)
        inspector.resize(680, 580)
        inspector.load_snapshot(_snapshot(), self.paths)
        inspector.show()
        self.app.processEvents()
        canvas = inspector.pose_canvas
        source = (20., 20.)
        anchor = canvas.source_to_view(source)
        event = QWheelEvent(anchor, canvas.mapToGlobal(anchor.toPoint()), QPoint(), QPoint(0, 240),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(canvas, event)
        self.assertGreater(canvas.zoom_factor, 1)
        np.testing.assert_allclose(canvas.view_to_source(anchor), source, atol=1e-8)
        QTest.mousePress(canvas, Qt.MouseButton.MiddleButton, pos=QPoint(40, 40))
        QTest.mouseMove(canvas, QPoint(65, 50))
        QTest.mouseRelease(canvas, Qt.MouseButton.MiddleButton, pos=QPoint(65, 50))
        self.assertGreater(canvas.pan_offset.x(), 0)
        point = canvas.source_to_view(source)
        np.testing.assert_allclose(canvas.view_to_source(point), source, atol=1e-8)
        inspector.search_edit.setText("ear")
        self.assertTrue(inspector.entry_list.item(0).isHidden())
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=point.toPoint())
        self.assertEqual(inspector.current_entry().drawable_id, "face")
        self.assertEqual(inspector.search_edit.text(), "")
        self.assertFalse(inspector.entry_list.item(0).isHidden())
        QTest.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
        self.assertEqual(canvas.zoom_factor, 1)
        self.assertEqual(canvas.pan_offset, QPointF())

    def test_trigger_dialog_rejects_ineligible_ids_and_is_a_real_owned_modal(self):
        parent = QWidget()
        self.widgets.append(parent)
        dialog = ArtMeshPickDialog(_snapshot(), self.paths, {"ear"}, parent)
        self.widgets.append(dialog)
        self.assertEqual(dialog.windowModality(), Qt.WindowModality.WindowModal)
        self.assertTrue(dialog.isWindow())
        self.assertTrue(dialog.inspector.entry_list.item(0).isHidden())
        dialog.inspector.select_entry("face")
        self.assertFalse(dialog.validate())
        self.assertFalse(dialog.yesButton.isEnabled())
        dialog.inspector.select_entry("ear")
        self.assertTrue(dialog.validate())
        self.assertEqual(dialog.selected_id, "ear")

    def test_pose_frame_maps_letterbox_without_stretch_and_zoom_keeps_real_mesh_picking(self):
        # The frame has a different aspect from the model canvas. Its viewport
        # also includes letterbox coordinates outside the Core canvas.
        frame = QImage(120, 80, QImage.Format.Format_RGBA8888)
        frame.fill(QColor(20, 190, 60))
        for x in range(60, 120):
            for y in range(80):
                frame.setPixelColor(x, y, QColor(30, 50, 210))
        frame.setDevicePixelRatio(1.75)
        polygon = [[-10, -20], [110, -20], [110, 60], [-10, 60]]
        dialog = ArtMeshPickDialog(_snapshot(), self.paths, {"face", "ear"},
                                   pose_frame=frame, pose_frame_canvas_polygon=polygon)
        self.widgets.append(dialog)
        dialog.show()
        self.app.processEvents()
        canvas = dialog.inspector.pose_canvas
        self.assertGreaterEqual(canvas.height(), 220)
        self.assertEqual(canvas.pixmap.devicePixelRatio(), 1)

        def color_at(source):
            point = canvas.source_to_view(source).toPoint()
            return canvas.grab().toImage().pixelColor(point)

        # These points straddle the native frame's center, not the stretched
        # canvas center. Unselected triangle edges must not obscure the image.
        self.assertEqual(color_at((25, 25)), QColor(20, 190, 60))
        self.assertEqual(color_at((70, 20)), QColor(30, 50, 210))
        self.assertEqual(color_at((60, 40)), QColor(30, 50, 210))
        source = (20., 20.)
        anchor = canvas.source_to_view(source)
        wheel = QWheelEvent(anchor, canvas.mapToGlobal(anchor.toPoint()), QPoint(), QPoint(0, 240),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(canvas, wheel)
        QTest.mousePress(canvas, Qt.MouseButton.MiddleButton, pos=QPoint(40, 40))
        QTest.mouseMove(canvas, QPoint(65, 50))
        QTest.mouseRelease(canvas, Qt.MouseButton.MiddleButton, pos=QPoint(65, 50))
        self.app.processEvents()
        self.assertEqual(color_at((70, 20)), QColor(30, 50, 210))
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=canvas.source_to_view(source).toPoint())
        self.assertEqual(dialog.selected_id, "face")
        self.assertTrue(dialog.validate())
        self.assertFalse(canvas.pixmap.isNull())
        self.assertEqual(canvas.image_polygon, [tuple(point) for point in polygon])
        # Model switches clear a retained preview instead of showing the prior
        # model underneath a new snapshot's geometry.
        dialog.inspector.load_snapshot(_snapshot(), self.paths)
        self.assertTrue(dialog.inspector.pose_pixmap.isNull())
        self.assertIsNone(dialog.inspector.pose_image_polygon)

    def test_pose_frame_rejects_wrong_or_degenerate_coordinate_contract(self):
        inspector = ArtMeshInspector()
        self.widgets.append(inspector)
        frame = QImage(30, 20, QImage.Format.Format_RGBA8888)
        frame.fill(Qt.GlobalColor.white)
        for polygon in (None, [[0, 0]] * 4,
                        [[0, 0], [100, 0], [70, 100], [0, 100]],
                        [[0, 0], [float("nan"), 0], [100, 100], [0, 100]]):
            with self.assertRaises(ValueError):
                inspector.set_pose_preview(frame, polygon)

    def test_native_window_mouse_point_rectangle_and_escape_emit_current_pose_ids_without_disk_io(self):
        canvas = Live2DCanvas()
        self.widgets.append(canvas)
        canvas.resize(400, 400)
        canvas._fbo_width = canvas._fbo_height = 400
        canvas.model = SimpleNamespace(_model=SimpleNamespace(GetMvp=lambda: np.eye(4).flatten(order="F")))
        provider = Mock(return_value={"snapshot": _snapshot(), "texture_paths": self.paths})
        canvas.setSelectionSceneProvider(provider)
        points, regions, cancelled, errors = [], [], [], []
        canvas.drawablesPicked.connect(points.append)
        canvas.regionPicked.connect(lambda ids, region: regions.append((ids, region)))
        canvas.selectionCancelled.connect(lambda: cancelled.append(True))
        canvas.selectionFailed.connect(errors.append)
        canvas.setSelectionMode("point")
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(80, 80))
        self.assertEqual(points[-1], ["face", "ear"])
        canvas.setSelectionMode("rectangle")
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(120, 120))
        self.assertEqual(set(regions[-1][0]), {"face", "ear"})
        self.assertEqual(regions[-1][1]["coordinate_space"], "canvas-pixels-y-down")
        QTest.keyClick(canvas, Qt.Key.Key_Escape)
        self.assertEqual(cancelled, [True])
        self.assertFalse(errors)
        canvas.model = None

    def test_viewer_picker_checks_exact_project_model_identity_before_showing_dialog(self):
        with patch("app.gui.Live2DModPage.SettingsManager") as settings:
            settings.return_value.get_setting.side_effect = lambda key, default=None: default
            page = Live2DModPage(compact=True)
        self.widgets.append(page)
        main = self.root / "model.json"
        main.write_text("{}", encoding="utf-8")
        page.current_project = Live2DViewerModProject(self.root, self.root / "project.json", {
            "models": [{"id": "main", "model_json": "model.json"}],
            "artmesh_areas": [{"id": "ear"}, {"id": "face", "motion": "tap"}],
        })
        page.show_warning = Mock()
        page.set_artmesh_pick_provider(lambda _: {"model_id": "main", "model_path": self.root / "other.json",
                                                  "snapshot": _snapshot(), "texture_paths": self.paths})
        with patch("app.gui.Live2DModPage.ArtMeshPickDialog") as dialog:
            self.assertFalse(page.open_artmesh_picker())
            dialog.assert_not_called()
        page.show_warning.assert_called_once()

        frame = QImage(30, 20, QImage.Format.Format_RGBA8888)
        frame.fill(Qt.GlobalColor.white)
        polygon = [[0, 0], [100, 0], [100, 100], [0, 100]]
        page.set_artmesh_pick_provider(lambda _: {"model_id": "main", "model_path": main,
            "snapshot": _snapshot(), "texture_paths": self.paths,
            "pose_frame": frame, "pose_frame_canvas_polygon": polygon})
        with patch("app.gui.Live2DModPage.ArtMeshPickDialog") as dialog:
            dialog.return_value.exec.return_value = 0
            self.assertFalse(page.open_artmesh_picker())
            self.assertEqual(dialog.call_args.kwargs["pose_frame"], frame)
            self.assertEqual(dialog.call_args.kwargs["pose_frame_canvas_polygon"], polygon)


if __name__ == "__main__":
    unittest.main()
