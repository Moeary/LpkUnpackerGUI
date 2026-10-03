from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("LPK_DISABLE_NATIVE_PREVIEW", "1")

import numpy as np
from PIL import Image
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from qfluentwidgets import qconfig

from app.core.animation_editing import AnimationEditingError
from app.core.live2d_editor_session import Live2DEditorSession, prepare_readonly_preview_session
from app.core.live2d_editor_export import Live2DExportSnapshotRequest
from app.gui.ArtMeshInspector import ArtMeshInspector
from app.gui.Live2DCanvas import Live2DCanvas
from app.gui.theme import apply_application_theme
from tests.test_artmesh_selection_ui import _snapshot
from tests.test_live2d_skins import hashes, two_atlas_model


class DrawableAppearanceStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = two_atlas_model(self.root / "source")
        self.mesh = _snapshot()
        self.mesh["drawables"][0]["opacity"] = .8
        (self.source.parent / "drawables.json").write_text(json.dumps(self.mesh), encoding="utf-8")
        self.before = hashes(self.source.parent)
        self.session = Live2DEditorSession(self.source)
        self.addCleanup(self.session.close)

    def test_no_parent_batch_hide_restore_retains_opacity_and_is_single_undo(self):
        session = self.session
        self.assertFalse(session.mesh_data.get("parts"))
        revision = session.skin_state_snapshot()["revision"]
        session.set_drawable_opacity(["face", "ear"], .5)
        count = len(session._undo)
        session.set_drawable_visibility(["face", "ear", "face"], False)
        self.assertEqual(len(session._undo), count + 1)
        self.assertEqual(session.drawable_opacity_multipliers(), {"face": 0, "ear": 0})
        session.set_drawable_visibility(["face", "ear"], False)
        self.assertEqual(len(session._undo), count + 1)
        session.set_drawable_visibility(["face", "ear"], True)
        self.assertEqual(session.drawable_opacity_multipliers(), {"face": .5, "ear": .5})
        session.undo()
        self.assertEqual(session.drawable_opacity_multipliers(), {"face": 0, "ear": 0})
        session.undo()
        self.assertEqual(session.drawable_opacity_multipliers(), {"face": .5, "ear": .5})
        session.undo()
        self.assertEqual(session.drawable_opacity_multipliers(), {})
        self.assertFalse(session.dirty)
        session.redo()
        session.redo()
        self.assertEqual(session.drawable_opacity_multipliers(), {"face": 0, "ear": 0})
        self.assertEqual(session.part_overrides, {})
        self.assertEqual(revision, session.skin_state_snapshot()["revision"])
        self.assertEqual(self.before, hashes(self.source.parent))

    def test_sibling_parent_is_untouched_and_invalid_batch_leaves_everything_unchanged(self):
        session = self.session
        for drawable in session.mesh_data["drawables"]:
            drawable["parent_part_id"] = "SameParent"
        session.part_overrides = {"SameParent": .7}
        session.set_drawable_visibility(["face"], False)
        pose = session._pose_history()
        count = len(session._undo)
        for value in (float("nan"), float("inf"), -.1, 1.1, "not a number"):
            with self.assertRaises(AnimationEditingError):
                session.set_drawable_opacity(["face", "ear"], value)
        with self.assertRaises(AnimationEditingError):
            session.set_drawable_visibility(["ear", "missing"], False)
        self.assertEqual(session._pose_history(), pose)
        self.assertEqual(len(session._undo), count)
        self.assertEqual(session.part_overrides, {"SameParent": .7})
        mesh = session.exact_pose_mesh({}, {"SameParent": .7})
        self.assertEqual(mesh["drawables"][0]["opacity"], 0)
        self.assertEqual(mesh["drawables"][1]["opacity"], 1)

    def test_exact_snapshot_restoration_is_detached_and_preserves_mask_source_opacity(self):
        session = self.session
        session.mesh_data["drawables"][1]["masks"] = [0]
        original = copy.deepcopy(session.mesh_data)
        session.set_drawable_opacity(["face"], .5)
        session.set_drawable_visibility(["face"], False)
        shown = session.exact_pose_mesh({}, {})
        self.assertEqual(shown["drawables"][0]["opacity"], 0)
        self.assertEqual(shown["drawables"][0]["source_pose_opacity"], .8)
        self.assertEqual(shown["drawables"][1]["masks"], [0])
        neutral = session.exact_pose_mesh({}, {}, {})
        self.assertEqual(neutral["drawables"][0]["opacity"], .8)
        self.assertEqual(session.mesh_data, original)
        self.assertEqual(session.drawable_opacity_multipliers(), {"face": 0})

    def test_asset_and_pose_history_interoperate_and_observe_direct_map_replacement(self):
        session = self.session
        session.set_drawable_visibility(["face"], False)
        session.create_motion("Motion", 2)
        session.set_drawable_opacity(["ear"], .25)
        session.undo()
        self.assertEqual(session.drawable_opacity_multipliers(), {"face": 0})
        session.undo()
        self.assertNotIn("Motion", session.project.motions)
        self.assertEqual(session.drawable_opacity_multipliers(), {"face": 0})
        session.undo()
        self.assertFalse(session.dirty)
        session.drawable_opacity_overrides = {"ear": .5}
        self.assertTrue(session.dirty)
        session._saved_signature = session._signature()
        session.drawable_opacity_overrides["ear"] = .75
        self.assertTrue(session.dirty)

    def test_save_move_reopen_capture_and_readonly_keep_editor_only_visibility(self):
        session = self.session
        session.set_drawable_opacity(["face"], .5)
        session.set_drawable_visibility(["face", "ear"], False)
        count = len(session._undo)
        session.preview_parameter("ParamAngleY", 5)
        request = session.capture_export_snapshot()
        self.addCleanup(request.close)
        self.assertEqual(len(session._undo), count)
        self.assertTrue(session.parameter_preview_pending)
        payload = request.to_worker_payload()
        self.assertEqual(payload["_drawable_visibility"], {"face": False, "ear": False})
        exported = request.write(self.root / "snapshot")
        restored = Live2DEditorSession(exported)
        self.addCleanup(restored.close)
        self.assertEqual(restored.drawable_opacity_multipliers(), {"face": 0, "ear": 0})
        self.assertEqual(restored.drawable_opacity_overrides, {"face": .5})
        self.assertEqual(restored._texture_data, session._texture_data)
        self.assertEqual((restored.root / "model.moc3").read_bytes(), (session.root / "model.moc3").read_bytes())
        sidecar = json.loads(exported.with_name("model.drawables.json").read_text(encoding="utf-8"))
        self.assertEqual(sidecar["drawables"][0]["opacity"], .8)
        session.save_copy(self.root / "saved")
        (self.root / "saved").rename(self.root / "moved")
        readonly = prepare_readonly_preview_session(self.root / "moved/model.json")
        self.addCleanup(readonly.close)
        self.assertFalse(readonly.dirty)
        readonly.set_drawable_visibility(["face", "ear"], True)
        self.assertEqual(readonly.drawable_opacity_multipliers(), {"face": .5})
        self.assertEqual(session.drawable_opacity_multipliers(), {"face": 0, "ear": 0})
        self.assertEqual(self.before, hashes(self.source.parent))


class DrawableSelectionLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.paths = [self.root / "a.png", self.root / "b.png"]
        for path in self.paths:
            Image.new("RGBA", (100, 100), "cyan").save(path)
        self.widgets = []

    def tearDown(self):
        for widget in reversed(self.widgets):
            if isinstance(widget, Live2DCanvas):
                widget.model = None
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temporary.cleanup()

    def inspector(self):
        inspector = ArtMeshInspector()
        self.widgets.append(inspector)
        inspector.set_editor_layout(QWidget())
        inspector.resize(435, 580)
        inspector.load_snapshot(_snapshot(), self.paths)
        inspector.show()
        self.app.processEvents()
        return inspector

    def test_ctrl_list_toggle_and_uv_toggle_emit_the_same_selection_operations(self):
        inspector = self.inspector()
        events = []
        inspector.selectionIdsChanged.connect(events.append)
        viewport = inspector.entry_list.viewport()
        for index, modifiers in ((0, Qt.NoModifier), (1, Qt.ControlModifier), (0, Qt.ControlModifier)):
            point = inspector.entry_list.visualItemRect(inspector.entry_list.item(index)).center()
            QTest.mouseClick(viewport, Qt.LeftButton, modifiers, point)
        self.assertEqual(events, [["face"], ["face", "ear"], ["ear"]])
        inspector.select_entry("face")
        inspector.atlas_tabs.setCurrentIndex(2)
        canvas = inspector.atlas_canvases[1]
        point = canvas.source_to_view((20, 20)).toPoint()
        QTest.mouseClick(canvas, Qt.LeftButton, Qt.ControlModifier, point)
        self.assertEqual(set(inspector.selected_drawable_ids()), {"face", "ear"})

    def test_native_ctrl_click_and_rectangle_have_explicit_toggle_and_add_modes(self):
        canvas = Live2DCanvas()
        self.widgets.append(canvas)
        canvas.resize(400, 400)
        canvas._fbo_width = canvas._fbo_height = 400
        canvas.model = SimpleNamespace(_model=SimpleNamespace(GetMvp=lambda: np.eye(4).flatten(order="F")))
        canvas.setSelectionSceneProvider(lambda: {"snapshot": _snapshot(), "texture_paths": self.paths})
        canvas.setEditorInteraction(True)
        canvas.setSelectionMode("none")
        points, regions = [], []
        canvas.drawablesPickedWithMode.connect(lambda ids, mode: points.append((ids, mode)))
        canvas.regionPickedWithMode.connect(lambda ids, region, mode: regions.append((ids, mode)))
        QTest.mouseClick(canvas, Qt.LeftButton, Qt.ControlModifier, QPoint(80, 80))
        self.assertEqual(points, [(["face", "ear"], "toggle")])
        canvas.setSelectionMode("rectangle")
        QTest.mousePress(canvas, Qt.LeftButton, Qt.ControlModifier, QPoint(20, 20))
        QTest.mouseRelease(canvas, Qt.LeftButton, pos=QPoint(120, 120))
        self.assertEqual(regions, [(["face", "ear"], "add")])

    def test_low_uv_width_collapse_and_restore_keep_multiselect(self):
        inspector = self.inspector()
        inspector.select_entries(["face", "ear"])
        inspector.splitter.setSizes([400, 30])
        self.app.processEvents()
        self.assertLess(inspector.view_splitter.width(), 100)
        inspector.set_uv_preview_visible(False)
        self.assertEqual(inspector.splitter.sizes()[1], 0)
        self.assertGreater(inspector.list_panel.width(), 400)
        self.assertEqual(inspector.selected_drawable_ids(), ["face", "ear"])
        QTest.mouseClick(inspector.uv_toggle_button, Qt.LeftButton)
        self.assertTrue(inspector.uv_preview_visible())
        self.assertEqual(inspector.selected_drawable_ids(), ["face", "ear"])

    def test_dark_and_light_list_uv_surfaces_use_the_same_theme_palette(self):
        inspector = self.inspector()
        previous = qconfig.theme
        try:
            for theme in ("dark", "light"):
                apply_application_theme(theme, inspector)
                self.app.processEvents()
                expected = inspector.palette().window().color()
                canvas = inspector.atlas_canvas
                self.assertEqual(canvas.grab().toImage().pixelColor(1, 1), expected)
                view = inspector.entry_list.viewport()
                self.assertEqual(view.grab().toImage().pixelColor(10, view.height() - 10), expected)
        finally:
            apply_application_theme(previous)

    def test_native_capability_is_honest_and_hidden_overrides_are_validated_before_mutation(self):
        canvas = Live2DCanvas()
        self.widgets.append(canvas)
        canvas.model = SimpleNamespace()
        self.assertFalse(canvas.supportsDrawableOpacityOverrides())
        with self.assertRaises(RuntimeError):
            canvas.setDrawableOpacityOverrides({"face": 0})
        self.assertEqual(canvas._drawable_opacity_overrides, {})
        setter = Mock()
        canvas.model = SimpleNamespace(_model=SimpleNamespace(SetDrawableOpacityOverrides=setter),
                                       GetParamIds=lambda: [])
        canvas._drawable_indices = {"face": 0, "ear": 1}
        canvas.setDrawableOpacityOverrides({"face": 0, "ear": .5})
        setter.assert_called_once_with({0: 0, 1: .5})
        self.assertEqual(canvas.getSelectionPose()["drawables"], {"face": 0, "ear": .5})
        with self.assertRaises(ValueError):
            canvas.setDrawableOpacityOverrides({"missing": .5})
        self.assertEqual(canvas._drawable_opacity_overrides, {"face": 0, "ear": .5})

    def test_native_fallback_picking_omits_colour_hidden_drawables(self):
        canvas = Live2DCanvas()
        self.widgets.append(canvas)
        canvas.resize(400, 400)
        canvas._fbo_width = canvas._fbo_height = 400
        canvas.model = SimpleNamespace(_model=SimpleNamespace(HitDrawable=Mock(return_value=["face", "ear"])))
        canvas._drawable_opacity_overrides = {"face": 0, "ear": .5}
        self.assertEqual(canvas.pickDrawablesAt(80, 80, "toggle"), ["ear"])


if __name__ == "__main__":
    unittest.main()
