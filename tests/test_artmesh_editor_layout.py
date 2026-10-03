from __future__ import annotations

import copy
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("LPK_DISABLE_NATIVE_PREVIEW", "1")

from PIL import Image
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
from qfluentwidgets import CheckBox, PushButton

from app.gui.ArtMeshInspector import ArtMeshInspector
from tests.test_artmesh_selection_ui import _snapshot


class ArtMeshEditorLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.paths = [self.root / "first.png", self.root / "second.png"]
        for path in self.paths:
            Image.new("RGBA", (160, 160), (60, 180, 220, 255)).save(path)
        self.widgets = []

    def tearDown(self):
        for widget in reversed(self.widgets):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temporary.cleanup()

    def editor(self):
        inspector = ArtMeshInspector()
        self.widgets.append(inspector)
        controls = QWidget()
        layout = QVBoxLayout(controls)
        layout.addWidget(CheckBox("Parent Part visible", controls))
        layout.addWidget(PushButton("Selection PSD", controls))
        inspector.set_editor_layout(controls)
        inspector.resize(435, 580)
        inspector.show()
        self.app.processEvents()
        return inspector, controls

    def test_editor_controls_are_mounted_in_right_column_and_both_splitters_resize(self):
        inspector, controls = self.editor()
        inspector.load_snapshot(_snapshot(), self.paths)
        self.app.processEvents()
        self.assertEqual(inspector.layout_mode, "editor")
        self.assertEqual(inspector.splitter.orientation(), Qt.Orientation.Horizontal)
        self.assertEqual(inspector.splitter.count(), 2)
        self.assertIs(inspector.splitter.widget(0), inspector.list_panel)
        self.assertIs(inspector.splitter.widget(1), inspector.view_splitter)
        self.assertIs(inspector.entry_list.parentWidget(), inspector.list_panel)
        self.assertIs(inspector.search_edit.parentWidget(), inspector.list_panel)
        self.assertIs(inspector.view_splitter.widget(0), inspector.atlas_frame)
        self.assertIs(inspector.view_splitter.widget(1), inspector.editor_details_scroll)
        self.assertIs(inspector.editor_details_scroll.widget(), controls)
        self.assertGreater(inspector.entry_list.height(), 350)
        self.assertLess(inspector.list_panel.width(), inspector.view_splitter.width())
        self.assertLess(inspector.list_panel.geometry().right(), inspector.view_splitter.geometry().left())
        self.assertLess(inspector.atlas_frame.geometry().bottom(), inspector.editor_details_scroll.geometry().top())
        self.assertTrue(inspector.pose_canvas.parentWidget().isHidden())
        before = inspector.list_panel.width()
        handle = inspector.splitter.handle(1)
        point = handle.rect().center()
        QTest.mousePress(handle, Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseMove(handle, point + QPoint(30, 0))
        QTest.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=point + QPoint(30, 0))
        self.app.processEvents()
        self.assertGreater(inspector.list_panel.width(), before)
        before = inspector.atlas_frame.height()
        handle = inspector.view_splitter.handle(1)
        point = handle.rect().center()
        QTest.mousePress(handle, Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseMove(handle, point + QPoint(0, -25))
        QTest.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=point + QPoint(0, -25))
        self.app.processEvents()
        self.assertLess(inspector.atlas_frame.height(), before)

    def test_single_multi_single_rebuild_keeps_one_callback_and_deferred_children_safe(self):
        inspector, controls = self.editor()
        single = copy.deepcopy(_snapshot())
        single["drawables"] = single["drawables"][:1]
        inspector.load_snapshot(single, self.paths[:1])
        self.app.processEvents()
        first = inspector.atlas_canvas
        self.assertIsNone(inspector.atlas_tabs)
        self.assertEqual(list(inspector.atlas_canvases), [0])
        self.assertFalse(inspector.overview_canvases)
        inspector.load_snapshot(_snapshot(), self.paths)
        self.app.processEvents()
        self.assertIs(inspector.atlas_canvases[0], first)
        self.assertEqual(inspector.atlas_tabs.count(), 3)
        inspector.atlas_tabs.setCurrentIndex(2)
        previous = inspector.atlas_canvas
        inspector.load_snapshot(single, self.paths[:1])
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.assertIs(inspector.atlas_canvas, previous)
        self.assertIsNone(inspector.atlas_tabs)
        self.assertEqual(list(inspector.atlas_canvases), [0])
        # Repeating the public mount must not reconnect selection gestures.
        inspector.set_editor_layout(controls)
        inspector.set_editor_layout(controls)
        events = []
        inspector.selectionIdsChanged.connect(events.append)
        canvas = inspector.atlas_canvas
        point = canvas.source_to_view((30, 30)).toPoint()
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=point)
        self.assertEqual(events, [["face"]])
        events.clear()
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, point)
        self.assertEqual(events, [[]])

    def test_long_ids_are_elided_with_tooltips_and_do_not_resize_columns(self):
        inspector, _controls = self.editor()
        snapshot = _snapshot()
        identifier = "ArtMesh_" + "very_long_drawable_identifier_" * 16
        snapshot["drawables"][0]["id"] = identifier
        inspector.load_snapshot(snapshot, self.paths)
        self.app.processEvents()
        self.assertEqual(inspector.entry_list.textElideMode(), Qt.TextElideMode.ElideRight)
        self.assertIn(identifier, inspector.entry_list.item(0).toolTip())
        self.assertEqual(inspector.entry_list.horizontalScrollBar().maximum(), 0)
        self.assertEqual((inspector.width(), inspector.height()), (435, 580))
        self.assertLess(inspector.list_panel.width(), 200)
        replacement = QWidget()
        inspector.set_editor_layout(replacement)
        self.assertIs(inspector.editor_details_scroll.widget(), replacement)
        self.assertEqual(inspector.splitter.count(), 2)
        self.assertEqual(inspector.view_splitter.count(), 2)

    def test_static_single_atlas_inspection_keeps_pose_and_existing_tabs(self):
        inspector = ArtMeshInspector()
        self.widgets.append(inspector)
        snapshot = _snapshot()
        snapshot["drawables"] = snapshot["drawables"][:1]
        inspector.load_snapshot(snapshot, self.paths[:1])
        inspector.resize(680, 580)
        inspector.show()
        self.app.processEvents()
        self.assertEqual(inspector.layout_mode, "inspection")
        self.assertFalse(inspector.pose_canvas.parentWidget().isHidden())
        self.assertEqual(inspector.atlas_tabs.count(), 2)
        self.assertFalse(hasattr(inspector, "editor_details_scroll"))


if __name__ == "__main__":
    unittest.main()
