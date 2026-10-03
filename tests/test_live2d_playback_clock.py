from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.gui.Live2DCanvas import Live2DCanvas


class FakeModel:
    def __init__(self):
        self._lastFrame = 100.0
        self.step = .025
        self.starts = []
        self.pending = None
        self.finished = False
        self._model = Mock()
        self.SetParameterValue = Mock()
        self.Draw = Mock()

    def StartMotion(self, group, index, priority, onStartMotionHandler, onFinishMotionHandler):
        self.starts.append((group, index, onStartMotionHandler, onFinishMotionHandler))
        self.pending = self.starts[-1]

    def Update(self):
        self._lastFrame += self.step
        if self.pending:
            group, index, started, _finished = self.pending
            self.pending = None
            self.finished = False
            started(group, index)

    def IsMotionFinished(self):
        return self.finished


class CanvasHarness:
    # Exercise production motion methods without constructing an OpenGL window.
    _clear_motion_clock = Live2DCanvas._clear_motion_clock
    _reset_update_timestamp = Live2DCanvas._reset_update_timestamp
    _update_model_motion = Live2DCanvas._update_model_motion
    _start_native_motion = Live2DCanvas._start_native_motion
    getMotionPlaybackState = Live2DCanvas.getMotionPlaybackState
    setMotionFrozen = Live2DCanvas.setMotionFrozen
    setMotionTime = Live2DCanvas.setMotionTime
    setAdvancedParams = Live2DCanvas.setAdvancedParams
    _apply_advanced_params = Live2DCanvas._apply_advanced_params
    _restart_loop_motion_if_finished = Live2DCanvas._restart_loop_motion_if_finished
    on_draw = Live2DCanvas.on_draw

    def __init__(self):
        self.model = FakeModel()
        self._motion_request_serial = 0
        self._motion_playback = self._motion_scrub = None
        self._updating_motion = self._motion_started_during_update = False
        self._motion_frozen = self._motion_loop_enabled = False
        self._last_played_motion = None
        self._advanced_enabled = False
        self._advanced_params = {}
        self._part_opacity_overrides = {}
        self.update = Mock()
        self.playMotion = Mock()
        self.getParameterMetaList = Mock(return_value=[{"id": "ParamAngleX", "value": 12.125}])

    def findMotion(self, group, index):
        return {"group": group, "index": index, "duration": 2}


class Live2DPlaybackClockTests(unittest.TestCase):
    def test_progress_uses_native_update_delta_and_waits_for_native_start(self):
        canvas = CanvasHarness()
        canvas._start_native_motion("Idle", 0)
        self.assertIsNone(canvas.getMotionPlaybackState())
        canvas._update_model_motion()
        self.assertEqual(canvas.getMotionPlaybackState()["time"], 0)
        canvas._update_model_motion()
        self.assertAlmostEqual(canvas.getMotionPlaybackState()["time"], .025)
        canvas.model.step = .6
        canvas._update_model_motion()
        self.assertAlmostEqual(canvas.getMotionPlaybackState()["time"], .125)

    def test_freeze_keeps_native_clock_and_exact_frame_without_restarting(self):
        canvas = CanvasHarness()
        canvas._start_native_motion("Idle", 0)
        canvas._update_model_motion()
        canvas._update_model_motion()
        previous = canvas.getMotionPlaybackState()["time"]
        canvas.setMotionFrozen(True)
        with patch("app.gui.Live2DCanvas.live2d.clearBuffer"):
            canvas.on_draw()
            canvas.on_draw()
        self.assertEqual(canvas.getMotionPlaybackState()["time"], previous)
        canvas.model._model.Update.assert_not_called()
        with patch("app.gui.Live2DCanvas.time.time", return_value=200):
            canvas.setMotionFrozen(False)
        self.assertEqual(canvas.model._lastFrame, 200)
        canvas.playMotion.assert_not_called()
        canvas._update_model_motion()
        self.assertAlmostEqual(canvas.getMotionPlaybackState()["time"], previous + .025)

    def test_finished_old_motion_cannot_overwrite_current_identity_and_loop_restarts(self):
        canvas = CanvasHarness()
        canvas._start_native_motion("Idle", 0)
        canvas._update_model_motion()
        old_finished = canvas.model.starts[-1][3]
        canvas._start_native_motion("TapBody", 1)
        canvas._update_model_motion()
        old_finished("Idle", 0)
        self.assertEqual(canvas.getMotionPlaybackState()["group"], "TapBody")
        self.assertTrue(canvas.getMotionPlaybackState()["playing"])
        canvas.model.starts[-1][3]("TapBody", 1)
        self.assertEqual(canvas.getMotionPlaybackState()["time"], 2)
        canvas.model.finished = True
        canvas._motion_loop_enabled = True
        canvas._restart_loop_motion_if_finished()
        canvas._update_model_motion()
        self.assertEqual(canvas.getMotionPlaybackState()["time"], 0)
        canvas._clear_motion_clock()
        canvas.model.starts[-1][2]("TapBody", 1)
        self.assertIsNone(canvas.getMotionPlaybackState())

    def test_frozen_scrub_is_separate_from_native_clock_and_replay_is_explicit(self):
        canvas = CanvasHarness()
        canvas._start_native_motion("Idle", 0)
        canvas._update_model_motion()
        canvas._update_model_motion()
        canvas.setMotionFrozen(True)
        with patch("app.gui.Live2DCanvas.evaluate_motion_parameters", return_value={"ParamAngleX": 12.125}):
            values = canvas.setMotionTime({"group": "Idle", "index": 0, "duration": 2}, 1.2)
        self.assertEqual(values, {"ParamAngleX": 12.125})
        self.assertAlmostEqual(canvas.getMotionPlaybackState()["time"], 1.2)
        self.assertAlmostEqual(canvas._motion_playback["time"], .025)
        self.assertTrue(canvas.getMotionPlaybackState()["scrubbed"])
        canvas.setMotionFrozen(False)
        canvas.playMotion.assert_called_once_with("Idle", 0)

    def test_frozen_parameters_are_drawn_without_rerunning_sdk_motion_or_physics(self):
        canvas = CanvasHarness()
        canvas.setMotionFrozen(True)
        canvas.setAdvancedParams(True, {"ParamAngleX": 12.125})
        with patch("app.gui.Live2DCanvas.live2d.clearBuffer"):
            canvas.on_draw()
            canvas.on_draw()
            canvas.model._model.Update.assert_not_called()
            canvas.setAdvancedParams(True, {"ParamAngleX": -8.5})
            canvas.on_draw()
            canvas.on_draw()
        canvas.model._model.Update.assert_not_called()
        self.assertEqual(canvas.model.Draw.call_count, 4)
        self.assertEqual(canvas.model.SetParameterValue.call_args.args, ("ParamAngleX", -8.5))


if __name__ == "__main__":
    unittest.main()
