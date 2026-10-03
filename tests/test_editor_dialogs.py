from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt
from PySide6.QtGui import QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QWidget
from qfluentwidgets import PushButton

from app.gui.editor_dialogs import EditorMessageBox, EditorTextDialog, ThemedEditorDialog, get_editor_text
from app.gui.theme import apply_application_theme
from app.i18n import get_i18n, tr


def contrast(first, second):
    def luminance(color):
        channels = [value / 255 for value in (color.red(), color.green(), color.blue())]
        linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in channels]
        return sum(weight * value for weight, value in zip((.2126, .7152, .0722), linear))
    a, b = sorted((luminance(first), luminance(second)))
    return (b + .05) / (a + .05)


class EditorDialogsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.language = get_i18n().language
        self.host = QWidget()
        self.host.resize(720, 480)
        self.host.show()
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.close()
            dialog.deleteLater()
        self.host.close()
        self.host.deleteLater()
        get_i18n().set_language(self.language)
        apply_application_theme("light")
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def box(self):
        box = EditorMessageBox(self.host)
        self.dialogs.append(box)
        box.setWindowTitle("Keep current edits")
        box.setText("Save the edited project before opening another model?")
        box.setStandardButtons(box.Save | box.Discard | box.Cancel)
        return box

    def test_real_owned_modal_fluent_buttons_and_open_window_theme_switch(self):
        box = self.box()
        box.show()
        for theme in ("dark", "light", "dark"):
            with self.subTest(theme=theme):
                apply_application_theme(theme, self.host)
                self.app.processEvents()
                self.assertTrue(box.isWindow())
                self.assertEqual(box.windowModality(), Qt.WindowModal)
                self.assertIs(box.parentWidget(), self.host)
                self.assertIs(self.app.activeModalWidget(), box)
                self.assertFalse(hasattr(box, "windowMask"))
                self.assertTrue(all(isinstance(button, PushButton) for button in box.buttons()))
                palette = box.palette()
                background = palette.color(QPalette.Window)
                self.assertGreater(contrast(background, palette.color(QPalette.WindowText)), 4.5)
                self.assertGreater(contrast(background, palette.color(QPalette.Disabled, QPalette.ButtonText)), 3)
                box.button(box.Save).setEnabled(False)
                image = box.grab().toImage()
                self.assertEqual(image.pixelColor(3, 3).name(), background.name())
                control_image = box.button(box.Save).grab().toImage()
                disabled_text = "#a4afbf" if theme == "dark" else "#596575"
                self.assertTrue(any(control_image.pixelColor(x, y).name() == disabled_text
                                    for y in range(control_image.height()) for x in range(control_image.width())))

    def test_all_languages_update_visible_buttons_and_keep_scene_specific_save_label(self):
        box = self.box()
        box.show()
        translated_labels = set()
        for language in ("zh_CN", "en_US", "ja_JP"):
            get_i18n().set_language(language)
            self.app.processEvents()
            self.assertEqual(box.button(box.Save).text(), tr("editor.dialogs.save", default="保存"))
            self.assertEqual(box.button(box.Discard).text(), tr("editor.dialogs.discard", default="不保存"))
            self.assertEqual(box.button(box.Cancel).text(), tr("editor.dialogs.cancel", default="取消"))
            translated_labels.add(tuple(button.text() for button in box.buttons()))
        self.assertEqual(len(translated_labels), 3)
        box.setButtonText(box.Save, "Export model copy")
        get_i18n().set_language("zh_CN")
        self.assertEqual(box.button(box.Save).text(), "Export model copy")

    def test_escape_close_and_default_discard_request_are_cancel(self):
        box = self.box()
        box.setDefaultButton(box.Discard)
        box.show()
        self.app.processEvents()
        self.assertTrue(box.button(box.Cancel).isDefault())
        self.assertFalse(box.button(box.Discard).isDefault())
        QTest.keyClick(box, Qt.Key_Escape)
        self.assertEqual(box.result(), box.Cancel)
        box.show()
        self.app.processEvents()
        box.close()
        self.assertEqual(box.result(), box.Cancel)

    def test_static_save_return_code_and_cancel_reentrant_modal(self):
        errors = []
        def save():
            try:
                box = self.app.activeModalWidget()
                self.assertIsInstance(box, EditorMessageBox)
                QTest.mouseClick(box.button(box.Save), Qt.LeftButton)
            except BaseException as error:
                errors.append(error)
                self.app.activeModalWidget().reject()
        QTimer.singleShot(0, save)
        answer = EditorMessageBox.question(self.host, "Save", "Save edits?",
                                          EditorMessageBox.Save | EditorMessageBox.Discard | EditorMessageBox.Cancel,
                                          EditorMessageBox.Save)
        self.assertFalse(errors, errors)
        self.assertEqual(answer, EditorMessageBox.Save)
        outer = ThemedEditorDialog(self.host)
        self.dialogs.append(outer)
        def nested():
            try:
                answer = EditorMessageBox.warning(self.host, "Nested", "Unsaved",
                                                 EditorMessageBox.Save | EditorMessageBox.Discard | EditorMessageBox.Cancel)
                self.assertEqual(answer, EditorMessageBox.Cancel)
                self.assertIs(self.app.activeModalWidget(), outer)
                self.assertIs(self.host._editor_modal_dialog, outer)
            except BaseException as error:
                errors.append(error)
            finally:
                outer.reject()
        QTimer.singleShot(0, nested)
        self.assertEqual(outer.exec(), QDialog.Rejected)
        self.assertFalse(errors, errors)
        self.assertIsNone(self.host._editor_modal_dialog)

    def test_text_dialog_returns_cancel_without_modifying_caller_value(self):
        def cancel():
            dialog = self.app.activeModalWidget()
            self.assertIsInstance(dialog, EditorTextDialog)
            dialog.name_edit.setText("Changed name")
            QTest.mouseClick(dialog.cancelButton, Qt.LeftButton)
        QTimer.singleShot(0, cancel)
        text, accepted = get_editor_text(self.host, "Project name", "Name", text="Original")
        self.assertEqual(text, "Changed name")
        self.assertFalse(accepted)

    def test_text_dialog_invalid_return_stays_open_and_valid_return_accepts_once(self):
        class ValidatedText(EditorTextDialog):
            def validate(self):
                return self.name_edit.text() == "valid"
        dialog = ValidatedText("New project", "Name", self.host)
        self.dialogs.append(dialog)
        accepted = []
        dialog.accepted.connect(lambda: accepted.append(True))
        dialog.show()
        self.app.processEvents()
        QTest.keyClick(dialog.name_edit, Qt.Key_Return)
        self.app.processEvents()
        self.assertTrue(dialog.isVisible())
        self.assertEqual(accepted, [])
        dialog.name_edit.setText("valid")
        QTest.keyClick(dialog.name_edit, Qt.Key_Return)
        self.app.processEvents()
        self.assertFalse(dialog.isVisible())
        self.assertEqual(accepted, [True])


if __name__ == "__main__":
    unittest.main()
