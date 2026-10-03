from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.gui.Live2DPreviewWindow import Live2DPreviewWindow


class CameraCanvas:
    def __init__(self):
        self.generation = 3
        self.state = {"enabled": False, "scale": 1.0, "offset_x": 0.0,
                      "offset_y": 0.0, "rotation": 0.0, "model_generation": 3}
        self.calls = []

    def getContentFitState(self):
        return dict(self.state)

    def modelLoadGeneration(self):
        return self.generation

    def setModelTransform(self, scale, x, y):
        self.calls.append(("transform", scale, x, y))
        self.state.update(enabled=False, scale=scale, offset_x=x, offset_y=y)

    def setRotationAngle(self, rotation):
        self.calls.append(("rotation", rotation))
        self.state.update(enabled=False, rotation=rotation)

    def setContentFitEnabled(self, enabled):
        self.calls.append(("enabled", enabled))
        self.state["enabled"] = enabled

    def fitToContent(self, *, auto_resize):
        self.calls.append(("fit", auto_resize))
        return True

    def setBackground(self, transparent, color):
        self.calls.append(("background", transparent, color))


class PreviewWindowFitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        with patch.object(Live2DPreviewWindow, "setup_ui"):
            self.window = Live2DPreviewWindow(embedded=True)
        self.canvas = CameraCanvas()
        self.window.live2d_canvas = self.canvas

    def tearDown(self):
        self.window._released = True
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def fitted(self):
        self.canvas.state.update(enabled=True, scale=2.7184, offset_x=-.3334,
                                 offset_y=.1799, rotation=0.0)
        self.window._on_content_fit_applied(self.canvas.getContentFitState())

    def test_rounded_display_controls_preserve_actual_fit_when_background_changes(self):
        self.fitted()
        self.window.apply_settings({"model_scale": 2.72, "model_offset_x": -.33,
                                    "model_offset_y": .18, "model_rotation": 0,
                                    "transparent_bg": False, "content_fit_enabled": True})
        self.assertEqual(self.canvas.calls, [("background", False, None)])
        self.assertTrue(self.canvas.state["enabled"])
        self.assertEqual(self.canvas.state["scale"], 2.7184)

    def test_manual_transform_wins_over_stale_enabled_setting(self):
        self.fitted()
        self.window.apply_settings({"model_scale": 2.74, "content_fit_enabled": True})
        self.assertEqual(self.canvas.calls, [("transform", 2.74, -.3334, .1799)])
        self.assertFalse(self.canvas.state["enabled"])
        self.window.apply_settings({"transparent_bg": True, "content_fit_enabled": False})
        self.assertFalse(self.canvas.state["enabled"])
        self.assertEqual(self.canvas.state["scale"], 2.74)

    def test_enabling_fit_is_idempotent_and_button_requests_content_fit(self):
        self.window.apply_settings({"content_fit_enabled": True})
        self.window.apply_settings({"content_fit_enabled": True})
        self.assertEqual(self.canvas.calls, [("enabled", True)])
        self.assertTrue(self.window.fit_model())
        self.assertEqual(self.canvas.calls[-1], ("fit", True))

    def test_first_frame_notification_is_deferred_and_rechecks_load_identity(self):
        path = str(Path("ready-model.json").resolve())
        self.window.model_path = path
        ready = []
        self.window.modelReady.connect(lambda *args: ready.append(args))
        self.window._on_model_frame_ready(path, 3)
        self.assertEqual(ready, [])
        self.canvas.generation = 4
        self.app.processEvents()
        self.assertEqual(ready, [])
        self.window._on_model_frame_ready(path, 4)
        self.app.processEvents()
        self.assertEqual(ready, [(path, 4)])
        self.window._on_model_frame_ready(path, 4)
        self.window.model_path = str(Path("other-model.json").resolve())
        self.app.processEvents()
        self.assertEqual(len(ready), 1)
        self.window.model_path = path
        self.window._on_model_frame_ready(path, 4)
        self.window._released = True
        self.app.processEvents()
        self.assertEqual(len(ready), 1)


if __name__ == "__main__":
    unittest.main()
