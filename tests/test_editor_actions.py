from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from app.gui.editor_actions import ActionComboBox, ActionNameDialog


class EditorActionControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_search_query_preserves_selection_never_adds_unknown_and_mouse_maps_results(self):
        combo = ActionComboBox()
        combo.show()
        try:
            for index in range(149):
                combo.addItem(f"Command{index:03d}", f"motion-{index}")
            combo.setCurrentIndex(0)
            spy = QSignalSpy(combo.currentIndexChanged)
            combo.setFocus()
            combo.selectAll()
            QTest.keyClicks(combo, "117")
            self.assertEqual(combo.currentData(), "motion-0")
            self.assertEqual(spy.count(), 0)
            QTest.keyClick(combo, Qt.Key_Return)
            self.assertEqual(combo.currentData(), "motion-117")
            combo.setText("not-an-action")
            QTest.keyClick(combo, Qt.Key_Return)
            self.assertEqual(combo.currentData(), "motion-117")
            self.assertEqual(combo.count(), 149)
            combo.setText("00")
            QTest.mouseClick(combo.dropButton, Qt.LeftButton)
            self.app.processEvents()
            self.assertEqual(len(combo.dropMenu.actions()), 11)  # 000..009 and 100
            menu = combo.dropMenu
            item = menu.view.item(7)
            QTest.mouseClick(menu.view.viewport(), Qt.LeftButton, pos=menu.view.visualItemRect(item).center())
            self.app.processEvents()
            self.assertEqual(combo.currentData(), "motion-7")
            self.assertEqual(combo.currentText(), "Command007")
        finally:
            if combo.dropMenu:
                combo.dropMenu.close()
            combo.close()
            combo.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_native_dialog_empty_duplicate_invalid_remain_editable_and_cancel_has_no_mask(self):
        host = QWidget()
        host.show()
        dialog = ActionNameDialog("New action", host, existing_names={"Idle"})
        dialog.show()
        self.app.processEvents()
        try:
            self.assertTrue(dialog.isWindow())
            self.assertIs(self.app.activeModalWidget(), dialog)
            self.assertFalse(hasattr(dialog, "windowMask"))
            for name in ("", "Idle", "bad/name"):
                dialog.name_edit.setText(name)
                QTest.mouseClick(dialog.yesButton, Qt.LeftButton)
                self.app.processEvents()
                self.assertTrue(dialog.isVisible())
                self.assertTrue(dialog.error_label.isVisible())
            dialog.name_edit.setText("NewPose")
            QTest.mouseClick(dialog.yesButton, Qt.LeftButton)
            self.assertEqual(dialog.result(), QDialog.Accepted)
            self.assertFalse(dialog.isVisible())
        finally:
            dialog.close()
            dialog.deleteLater()
            host.close()
            host.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
