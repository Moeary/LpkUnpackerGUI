from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from tests.test_live2d_editor_session import make_model
from tests.test_live2d_mod_page_smoke import _TestSettings
from app.gui.Live2DEditorPage import Live2DEditorPage


class _Canvas(QObject):
    modelLoaded = Signal()
    drawableClicked = Signal(str)
    modelPointClicked = Signal(float, float)


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

    def set_motion_frozen(self, _frozen):
        pass


class Live2DEditorPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="live2d-editor-ui-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.model = make_model(self.root / "source")
        self.settings_patch = patch("app.gui.Live2DModPage.SettingsManager", return_value=_TestSettings(self.root))
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.page = Live2DEditorPage()
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
            self.assertEqual(calls, [("parameters",), ("part", 0, 0), ("update", 0), ("draw",)])
            calls.clear()
            Live2DCanvas.setPartOpacityOverrides(canvas, {})
            Live2DCanvas.on_draw(canvas)
            self.assertIn(("part", 0, .7), calls)

    def test_native_point_selection_uses_actual_artmesh_geometry(self):
        self.open()
        self.page.preview.live2d_canvas.modelPointClicked.emit(.5, .5)
        self.assertEqual(self.page.artmesh_inspector.current_entry().drawable_id, "ArtMeshFace")


if __name__ == "__main__":
    unittest.main()
