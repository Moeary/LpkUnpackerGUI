from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("LPK_DISABLE_NATIVE_PREVIEW", "1")

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from PySide6.QtWidgets import QApplication

from app.gui.Live2DCanvas import Live2DCanvas
from app.gui.live2d_content_fit import alpha_content_bounds, content_fit_transform
from app.gui.live2d_selection import ModelCoordinates, PreviewTransform, SelectionScene


class ContentFitMathTests(unittest.TestCase):
    def test_native_alpha_bounds_ignore_transparent_geometry_and_rgb_background(self):
        pixels = np.full((100, 200, 4), 240, dtype=np.uint8)
        pixels[:, :, 3] = 0
        pixels[10:80, 50:150, 3] = 255
        pixels[80, 149, 3] = 1  # preserve even a faint rendered silhouette edge
        self.assertEqual(alpha_content_bounds(pixels), (.25, .1, .75, .81))
        pixels[:, :, 3] = 0
        self.assertIsNone(alpha_content_bounds(pixels))
        with self.assertRaises(ValueError):
            alpha_content_bounds(np.zeros((0, 0, 4), dtype=np.uint8))

    def test_fit_keeps_entire_visible_bounds_centered_at_all_viewport_aspects(self):
        bounds = (.2, .12, .65, .9)
        for width, height in ((400, 400), (900, 300), (300, 800)):
            fitted = content_fit_transform(bounds, (width, height), 1.5)
            transform = PreviewTransform(width, height, 900, 600, fitted.scale,
                                         fitted.offset_x, fitted.offset_y)
            bottom_left = transform.clip_to_window((bounds[0] * 2 - 1, bounds[1] * 2 - 1))
            top_right = transform.clip_to_window((bounds[2] * 2 - 1, bounds[3] * 2 - 1))
            self.assertAlmostEqual((bottom_left[0] + top_right[0]) / 2, width / 2)
            self.assertAlmostEqual((bottom_left[1] + top_right[1]) / 2, height / 2)
            self.assertAlmostEqual(max((top_right[0] - bottom_left[0]) / width,
                                       (bottom_left[1] - top_right[1]) / height), .96)
            self.assertGreaterEqual(bottom_left[0], width * .02 - 1e-8)
            self.assertGreaterEqual(top_right[1], height * .02 - 1e-8)
            self.assertFalse(fitted.range_limited)

    def test_small_off_center_content_respects_ranges_without_clipping_pan(self):
        bounds = (.8, .8, .9, .9)
        fitted = content_fit_transform(bounds, (400, 400), 1.)
        self.assertTrue(fitted.range_limited)
        self.assertLessEqual(fitted.scale, 4.)
        self.assertLessEqual(max(abs(fitted.offset_x), abs(fitted.offset_y)), 1.)
        transform = PreviewTransform(400, 400, 400, 400, fitted.scale, fitted.offset_x, fitted.offset_y)
        center = transform.clip_to_window((.85 * 2 - 1, .85 * 2 - 1))
        np.testing.assert_allclose(center, (200, 200))
        for invalid in ((0, 0, 0, 1), (-1, 0, .2, 1), (0, 0, 1, float("nan"))):
            with self.assertRaises(ValueError):
                content_fit_transform(invalid, (400, 400), 1.)

    def test_sdk_coordinates_and_point_region_hits_remain_consistent_after_fit(self):
        matrix = np.eye(4)
        matrix[0, 0], matrix[1, 1] = .005, .007
        matrix[0, 3], matrix[1, 3] = -.3, .2
        canvas = {"origin_x": 800, "origin_y": 600, "pixels_per_unit": 2}
        snapshot = {"drawables": [{"id": "visible-face", "vertices": [[840, 560], [940, 560], [890, 640]],
                                    "indices": [0, 1, 2]}]}
        for width, height, dpr in ((500, 500, 1), (900, 300, 1.75), (300, 800, 2)):
            fitted = content_fit_transform((.15, .2, .75, .85), (width, height), 1.5)
            transform = PreviewTransform(width, height, 600 * dpr, 400 * dpr,
                                         fitted.scale, fitted.offset_x, fitted.offset_y)
            coordinates = ModelCoordinates(transform, matrix.flatten(order="F"), canvas)
            target = (890, 585)
            window = coordinates.canvas_to_window(target)
            point = coordinates.window_to_canvas(window)
            np.testing.assert_allclose(point, target, atol=1e-7)
            self.assertEqual(SelectionScene(snapshot).hit_point(point), ["visible-face"])
            region = coordinates.window_rect((window[0] - 3, window[1] - 3),
                                             (window[0] + 3, window[1] + 3))
            self.assertEqual(SelectionScene(snapshot).hit_region(region), ["visible-face"])


class ContentFitCanvasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.canvas = Live2DCanvas()
        self.canvas.resize(400, 300)
        self.canvas.model = SimpleNamespace()
        self.canvas.model_path = "model-a.json"
        self.canvas._active_model_path = "model-a.json"
        self.canvas._model_load_generation = 1
        self.canvas._active_model_generation = 1
        self.canvas._fbo_width, self.canvas._fbo_height = 200, 100
        self.pixels = np.zeros((100, 200, 4), dtype=np.uint8)
        self.pixels[10:90, 60:140, 3] = 255
        self.canvas._read_content_fit_pixels = Mock(return_value=self.pixels)
        self.addCleanup(self.canvas.close)
        self.addCleanup(self.canvas.unloadModel)

    def test_request_is_deferred_until_actual_draw_and_readback_occurs_once(self):
        self.canvas.makeCurrent = Mock()
        self.assertTrue(self.canvas.fitToContent())
        self.canvas.makeCurrent.assert_not_called()
        self.canvas._read_content_fit_pixels.assert_not_called()
        self.canvas._canvas_framebuffer = 10
        self.canvas.on_draw = Mock(return_value=False)
        with patch("app.gui.Live2DCanvas.GL.glGetIntegerv", return_value=0), \
             patch("app.gui.Live2DCanvas.GL.glBindFramebuffer"), \
             patch("app.gui.Live2DCanvas.GL.glViewport"), \
             patch("app.gui.Live2DCanvas.GL.glClearColor"), \
             patch("app.gui.Live2DCanvas.GL.glClear"):
            self.canvas._ADPOpenGLCanvas__draw_on_canvas()
            self.canvas._read_content_fit_pixels.assert_not_called()
            sequence = []
            self.canvas.on_draw.side_effect = lambda: sequence.append("draw") or True
            self.canvas._read_content_fit_pixels.side_effect = lambda: sequence.append("sample") or self.pixels
            self.canvas._ADPOpenGLCanvas__draw_on_canvas()
            self.canvas._ADPOpenGLCanvas__draw_on_canvas()
        self.assertEqual(sequence, ["draw", "sample", "draw"])
        state = self.canvas.getContentFitState()
        self.assertEqual(state["readback_count"], 1)
        self.assertEqual(state["status"], "fit")
        self.assertEqual(state["bounds"], [.3, .1, .7, .9])
        self.assertTrue(state["enabled"])

    def test_resize_uses_normalized_cache_without_readback_or_cumulative_drift(self):
        self.canvas.fitToContent()
        self.canvas.on_canvas_rendered()
        initial = self.canvas.getContentFitState()
        self.canvas._desired_canvas_size = Mock(return_value=(200, 100))
        self.canvas._canvas_framebuffer = None  # skip actual GPU work in this CPU test
        for width, height in ((800, 250), (250, 800), (400, 300)):
            self.canvas.resize(width, height)
            self.canvas.paintGL()
        final = self.canvas.getContentFitState()
        self.assertEqual(final["readback_count"], 1)
        self.assertEqual(final["bounds"], initial["bounds"])
        self.assertEqual(final["scale"], initial["scale"])
        self.assertEqual(final["offset_x"], initial["offset_x"])
        self.assertEqual(final["offset_y"], initial["offset_y"])

    def test_manual_transform_or_rotation_exits_fit_and_disable_retains_camera(self):
        events = []
        self.canvas.fitModeChanged.connect(events.append)
        self.canvas.fitToContent()
        self.canvas.on_canvas_rendered()
        before = self.canvas.getContentFitState()
        self.canvas.setContentFitEnabled(False)
        disabled = self.canvas.getContentFitState()
        self.assertEqual((disabled["scale"], disabled["offset_x"], disabled["offset_y"]),
                         (before["scale"], before["offset_x"], before["offset_y"]))
        self.canvas.setContentFitEnabled(True)
        self.assertEqual(self.canvas.getContentFitState()["status"], "fit")
        self.canvas.setModelTransform(1.7, .13, -.22)
        self.assertFalse(self.canvas.getContentFitState()["enabled"])
        self.assertEqual(self.canvas.getContentFitState()["offset_y"], -.22)
        self.canvas.fitToContent(refresh=False)
        self.assertEqual(self.canvas.getContentFitState()["status"], "fit")
        self.canvas.setRotationAngle(18)
        self.assertFalse(self.canvas.getContentFitState()["enabled"])
        self.assertEqual(self.canvas.getContentFitState()["rotation"], 18)
        self.assertEqual(events, [True, False, True, False, True, False])

    def test_explicit_fit_refreshes_alpha_but_not_model_pose_or_source(self):
        self.canvas.model.Update = Mock()
        self.canvas.fitToContent(auto_resize=False)
        self.canvas.on_canvas_rendered()
        self.pixels[:, :, 3] = 0
        self.pixels[30:80, 90:170, 3] = 255
        self.canvas.fitToContent(auto_resize=False)
        self.canvas.on_canvas_rendered()
        state = self.canvas.getContentFitState()
        self.assertFalse(state["enabled"])
        self.assertEqual(state["readback_count"], 2)
        self.assertEqual(state["bounds"], [.45, .3, .85, .8])
        self.canvas.model.Update.assert_not_called()

    def test_empty_frames_retry_only_three_times_then_fallback_without_loop(self):
        self.pixels[:, :, 3] = 0
        self.canvas.fitToContent()
        self.canvas.on_canvas_rendered()
        self.assertTrue(self.canvas.getContentFitState()["pending"])
        self.canvas.on_canvas_rendered()
        self.canvas.on_canvas_rendered()
        self.canvas.on_canvas_rendered()
        state = self.canvas.getContentFitState()
        self.assertEqual(state["readback_count"], 3)
        self.assertFalse(state["pending"])
        self.assertEqual(state["status"], "empty-fallback")
        self.assertEqual(state["bounds"], [0, 0, 1, 1])
        self.canvas.setContentFitEnabled(False)
        self.canvas.setContentFitEnabled(True)
        self.assertEqual(self.canvas.getContentFitState()["status"], "empty-fallback")
        self.assertEqual(self.canvas.getContentFitState()["readback_count"], 3)

    def test_nonempty_second_frame_completes_and_manual_change_cancels_pending(self):
        self.canvas._read_content_fit_pixels.side_effect = [np.zeros_like(self.pixels), self.pixels]
        self.canvas.fitToContent()
        self.canvas.on_canvas_rendered()
        self.canvas.on_canvas_rendered()
        self.assertEqual(self.canvas.getContentFitState()["status"], "fit")
        self.assertEqual(self.canvas.getContentFitState()["readback_count"], 2)
        self.canvas.fitToContent()
        self.canvas.setModelTransform(1., 0., 0.)
        self.canvas.on_canvas_rendered()
        self.assertEqual(self.canvas.getContentFitState()["readback_count"], 2)

    def test_model_unload_invalidates_bounds_and_first_draw_signal_has_load_identity(self):
        ready = []
        self.canvas.modelFrameReady.connect(lambda path, generation: ready.append((path, generation)))
        self.canvas.fitToContent()
        self.canvas.on_canvas_rendered()
        self.canvas.on_canvas_rendered()
        self.assertEqual(ready, [("model-a.json", 1)])
        self.canvas.unloadModel()
        self.assertEqual(self.canvas.modelLoadGeneration(), 2)
        self.assertIsNone(self.canvas.getContentFitState()["bounds"])
        self.assertEqual(self.canvas.getContentFitState()["readback_count"], 0)
        self.assertFalse(self.canvas.fitToContent())
        self.canvas.loadModel("model-b.json")
        self.assertEqual(self.canvas.modelLoadGeneration(), 3)
        self.canvas.model = SimpleNamespace()
        self.canvas._active_model_generation = 3
        self.canvas._active_model_path = "model-b.json"
        self.canvas.on_canvas_rendered()
        self.assertEqual(ready[-1], ("model-b.json", 3))

    def test_failed_fit_readback_is_reported_once_without_destroying_manual_transform(self):
        states = []
        self.canvas.contentFitApplied.connect(states.append)
        self.canvas._read_content_fit_pixels.side_effect = RuntimeError("read failed")
        self.canvas.fitToContent()
        self.canvas.on_canvas_rendered()
        self.canvas.on_canvas_rendered()
        self.assertEqual(self.canvas.getContentFitState()["readback_count"], 1)
        self.assertEqual(states[0]["status"], "readback-error")
        self.assertEqual(states[0]["error"], "read failed")


if __name__ == "__main__":
    unittest.main()
