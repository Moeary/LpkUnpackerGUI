from __future__ import annotations

import copy
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from PySide6.QtCore import QPoint, Qt
    from app.gui.editor_timeline import AnimationTimelineEditor, sample_track
except (ImportError, OSError):
    QApplication = None


@unittest.skipIf(QApplication is None, "Qt desktop dependencies are not installed")
class TimelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widget = AnimationTimelineEditor()
        self.widget.resize(800, 380)
        self.widget.show()
        self.app.processEvents()

    def tearDown(self):
        self.widget.set_playing(False)
        self.widget.close()
        self.widget.deleteLater()
        self.app.processEvents()

    def test_empty_track_add_and_delete_last_key_are_committed_without_mutating_input(self):
        edited = []
        self.widget.framesEdited.connect(edited.append)
        frames = []
        self.widget.set_track(frames, 2, ["value"])
        self.widget.set_time(0.5)
        self.widget.add_button.click()
        self.assertEqual(edited[-1], [{"time": 0.5, "value": 0, "interpolation": "linear"}])
        self.assertEqual(frames, [])
        self.widget.delete_button.click()
        self.assertEqual(edited[-1], [])
        self.assertEqual(self.widget.table.rowCount(), 0)

    def test_seek_play_and_loop_never_emit_an_edit(self):
        edited, times = [], []
        self.widget.framesEdited.connect(edited.append)
        self.widget.timeChanged.connect(times.append)
        self.widget.set_track([], 0.1, ["angle"])
        self.widget.time_spin.setValue(0.07)
        self.widget.set_playing(True)
        QTest.qWait(160)
        self.widget.set_playing(False)
        self.assertTrue(times)
        self.assertFalse(edited)
        self.assertGreaterEqual(self.widget.current_time, 0)
        self.assertLessEqual(self.widget.current_time, 0.1)

    def test_bezier_controls_modify_curve_and_retain_unknown_frame_fields(self):
        frames = [{"time": 0, "value": 0, "interpolation": "linear", "tag": "keep"},
                  {"time": 1, "value": 10, "interpolation": "linear"}]
        original = copy.deepcopy(frames)
        edited = []
        self.widget.framesEdited.connect(edited.append)
        self.widget.set_track(frames, 1, ["value"])
        self.widget.select_frame(0)
        self.widget.interpolation_combo.setCurrentIndex(self.widget.interpolation_combo.findData("bezier"))
        for spin, value in zip(self.widget.control_spins, [0.25, 0, 0.75, 0]):
            spin.setValue(value)
        self.widget.apply_curve_button.click()
        self.assertLess(sample_track(edited[-1], "value", 0.5), 3)
        self.assertEqual(edited[-1][0]["tag"], "keep")
        self.assertEqual(frames, original)

    def test_flat_bezier_handles_are_displayed(self):
        frames = [{"time": 0, "value": 2, "interpolation": "bezier",
                   "bezier": {"value": [0.2, 5, 0.8, 5]}, "bezier_value_scale": {"value": 1}},
                  {"time": 1, "value": 2, "interpolation": "linear"}]
        self.widget.set_track(frames, 1, ["value"], editable=False)
        self.assertAlmostEqual(sample_track(frames, "value", 0.5), 5.75)
        self.widget.select_frame(0)
        self.assertFalse(self.widget.add_button.isEnabled())
        self.assertFalse(self.widget.apply_curve_button.isEnabled())
        self.assertFalse(self.widget.table.item(0, 0).flags() & Qt.ItemFlag.ItemIsEditable)

    def test_graphical_drag_commits_once_and_value_table_rejects_duplicate_times(self):
        self.widget.set_track([{"time": 0, "angle": 0}, {"time": 1, "angle": 10}], 2, ["angle"])
        edited = []
        self.widget.framesEdited.connect(edited.append)
        canvas = self.widget.canvas
        x = round(canvas._x(1))
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(x, 29))
        QTest.mouseMove(canvas, QPoint(round(canvas._x(1.5)), 29))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(round(canvas._x(1.5)), 29))
        self.assertEqual(len(edited), 1)
        self.assertAlmostEqual(edited[0][-1]["time"], 1.5, places=2)
        self.widget.table.item(1, 0).setText("0")
        self.assertEqual(len(edited), 1)
        self.assertGreater(self.widget.frames[-1]["time"], 0)

    def test_curve_cache_survives_clock_updates_and_table_is_collapsible(self):
        self.widget.set_track([{"time": 0, "value": 0}, {"time": 1, "value": 10}], 1, ["value"])
        self.widget.canvas.repaint()
        path = self.widget.canvas._cached_path
        self.assertIsNotNone(path)
        self.widget.set_time(0.5)
        self.widget.canvas.repaint()
        self.assertIs(self.widget.canvas._cached_path, path)
        self.assertTrue(self.widget.table.isHidden())
        self.widget.table_check.setChecked(True)
        self.assertFalse(self.widget.table.isHidden())


if __name__ == "__main__":
    unittest.main()
