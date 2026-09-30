from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QPoint
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
from app.core.settings_manager import SettingsManager
from app.gui.editor_timeline import AnimationTimelineEditor
from app.gui.editor_workspace import EditorTabs, EditorWorkspace


class EditorWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def workspace(self, settings=False):
        timeline = AnimationTimelineEditor()
        timeline.set_track([{"time": 0, "value": 0}, {"time": 1, "value": 10}], 1, ["value"])
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(timeline)
        workspace = EditorWorkspace(QWidget(), QWidget(), host, settings_key="test", settings=settings)
        self.widgets.append(workspace)
        workspace.resize(1000, 650)
        workspace.show()
        self.app.processEvents()
        self.app.processEvents()
        return workspace, timeline

    def test_every_panel_recovers_and_preserves_split_proportions(self):
        workspace, _ = self.workspace()
        workspace.horizontal_splitter.setSizes([650, 300])
        workspace.vertical_splitter.setSizes([330, 270])
        self.app.processEvents()
        before = workspace.horizontal_splitter.sizes()
        changes = []
        workspace.panelVisibilityChanged.connect(lambda panel, visible: changes.append((panel, visible)))
        for panel in ("preview", "details", "timeline"):
            workspace._panels[panel].hide_button.click()
        self.assertEqual(workspace._body.currentIndex(), 1)
        self.assertTrue(workspace.layout_toolbar.isVisible())
        for panel in ("preview", "details", "timeline"):
            workspace._toggles[panel].click()
            self.assertTrue(workspace.is_panel_visible(panel))
        self.app.processEvents()
        after = workspace.horizontal_splitter.sizes()
        self.assertAlmostEqual(before[0] / sum(before), after[0] / sum(after), delta=.03)
        self.assertEqual(len(changes), 6)
        workspace.reset_button.click()
        self.assertTrue(all(workspace.is_panel_visible(panel) for panel in workspace._panels))
        self.assertEqual(changes[-3:], [("preview", True), ("details", True), ("timeline", True)])

    def test_splitter_drag_to_zero_has_a_reachable_restore_button(self):
        workspace, _ = self.workspace()
        workspace.horizontal_splitter.setSizes([0, 950])
        workspace._splitter_moved(0, 1)
        self.assertFalse(workspace.is_panel_visible("preview"))
        self.assertTrue(workspace.preview_toggle.isVisible())
        workspace.preview_toggle.click()
        self.assertTrue(workspace.is_panel_visible("preview"))
        self.assertGreater(workspace.horizontal_splitter.sizes()[0], 0)
        workspace.vertical_splitter.setSizes([600, 0])
        workspace._splitter_moved(0, 1)
        self.assertFalse(workspace.is_panel_visible("timeline"))
        workspace.timeline_toggle.click()
        self.assertGreater(workspace.vertical_splitter.sizes()[1], 0)

    def test_portable_settings_restore_visibility_and_emit_renderer_state(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SettingsManager(Path(directory) / "settings.json")
            workspace, _ = self.workspace(settings)
            workspace.set_panel_visible("preview", False)
            workspace.set_panel_visible("timeline", False)
            restored, _ = self.workspace(SettingsManager(Path(directory) / "settings.json"))
            changes = []
            restored.panelVisibilityChanged.connect(lambda panel, visible: changes.append((panel, visible)))
            restored.restore_layout()
            self.assertEqual(changes, [("preview", False), ("details", True), ("timeline", False)])
            self.assertFalse(restored.preview_panel.isVisible())
            self.assertTrue(restored.details_panel.isVisible())
            restored.reset_layout()
            self.assertTrue(all(restored.is_panel_visible(panel) for panel in restored._panels))
            self.assertTrue(Path(directory, "settings.json").is_file())

    def test_default_toolbar_is_visible_and_small_or_expanded_timeline_can_scroll(self):
        workspace, timeline = self.workspace()
        scroll = workspace.timeline_panel.scroll
        self.assertEqual(scroll.verticalScrollBar().maximum(), 0)
        bottom = timeline.add_button.mapTo(scroll.viewport(), QPoint(0, timeline.add_button.height())).y()
        self.assertLessEqual(bottom, scroll.viewport().height())
        self.assertLess(timeline.add_button.geometry().bottom(), timeline.canvas.y())
        timeline.table_check.setChecked(True)
        timeline.curve_controls_check.setChecked(True)
        workspace.vertical_splitter.setSizes([600, 110])
        self.app.processEvents()
        self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
        self.assertLess(timeline.add_button.geometry().bottom(), timeline.canvas.y())

    def test_right_panel_has_full_height_and_old_layout_is_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SettingsManager(Path(directory) / "settings.json")
            settings.set("editor_layouts.test", {"sizes-h": [800, 200], "sizes-v": [200, 700],
                                                "visible": {"preview": False, "details": False, "timeline": False}})
            workspace, _ = self.workspace(settings)
            self.assertTrue(all(workspace.is_panel_visible(panel) for panel in workspace._panels))
            self.assertIs(workspace.horizontal_splitter.widget(0), workspace.vertical_splitter)
            self.assertIs(workspace.horizontal_splitter.widget(1), workspace.details_panel)
            self.assertEqual(workspace.details_panel.height(), workspace.vertical_splitter.height())
            self.assertGreaterEqual(workspace.timeline_panel.height(), 180)
            self.assertLessEqual(workspace.timeline_panel.height(), 210)
            horizontal = workspace.horizontal_splitter.sizes()
            self.assertAlmostEqual(horizontal[0] / sum(horizontal), .55, delta=.035)
            saved = settings.get("editor_layouts.test")
            self.assertEqual(saved["version"], 2)
            self.assertEqual(saved["layout"], "left-preview-timeline")

    def test_narrow_fluent_tabs_scroll_to_all_four_items(self):
        tabs = EditorTabs()
        self.widgets.append(tabs)
        labels = ["Parameters", "ArtMesh parts", "Textures", "MOD management"]
        for label in labels:
            tabs.addTab(QWidget(), label)
        tabs.resize(250, 350)
        tabs.show()
        self.app.processEvents()
        tabs.setCurrentIndex(3)
        self.app.processEvents()
        self.assertEqual(tabs.currentWidget(), tabs.widget(3))
        self.assertTrue(tabs.forward_button.isVisible())
        self.assertGreater(tabs.tab_scroll.horizontalScrollBar().value(), 0)
        last = tabs.pivot.widget(tabs._keys[3])
        right = last.mapTo(tabs.tab_scroll.viewport(), QPoint(last.width(), 0)).x()
        self.assertLessEqual(right, tabs.tab_scroll.viewport().width() + 8)
        tabs.setTabText(3, "MOD 管理")
        self.assertEqual(tabs.tabText(3), "MOD 管理")


if __name__ == "__main__":
    unittest.main()
