"""Theme-aware, owned modal windows for editor actions.

These are real Qt.Dialog windows rather than MessageBoxBase child masks. Native
Live2D/Spine QWindow surfaces therefore stay below the dialog, and closing a
confirmation window never means discarding work.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QMessageBox as QtMessageBox, QVBoxLayout
from qfluentwidgets import BodyLabel, LineEdit, PrimaryPushButton, PushButton, SubtitleLabel, Theme, isDarkTheme, qconfig, setCustomStyleSheet

from app.gui.theme import palette_for_theme
from app.i18n import get_i18n, tr


DIALOG_TEXT = {
    "editor.dialogs.save": "保存",
    "editor.dialogs.discard": "不保存",
    "editor.dialogs.cancel": "取消",
    "editor.dialogs.confirm": "确定",
    "editor.dialogs.yes": "是",
    "editor.dialogs.no": "否",
    "editor.dialogs.close": "关闭",
}


def _text(key):
    return tr(key, default=DIALOG_TEXT[key])


def _style_dialog_button(button):
    # Fluent's stock light primary disabled style uses white on light gray.
    # Keep the actual Fluent control and give only its disabled state adequate
    # contrast, including buttons disabled after the dialog is already shown.
    setCustomStyleSheet(
        button,
        "QPushButton:disabled { color: #596575; background-color: #e3e9f0; border: 1px solid #c7d0dc; }",
        "QPushButton:disabled { color: #a0a0a0; background-color: #3b3b3b; border: 1px solid #5a5a5a; }",
    )


def exec_editor_dialog(dialog: QDialog) -> int:
    """Keep one editor modal per owner window; reject reentrant requests."""
    owner = dialog.parentWidget()
    active = getattr(owner, "_editor_modal_dialog", None) if owner else None
    if active is not None and active is not dialog:
        active.raise_()
        active.activateWindow()
        dialog.reject()
        return dialog.result()
    popup = QApplication.activePopupWidget()
    if popup:
        popup.close()
    if owner:
        owner._editor_modal_dialog = dialog
    try:
        return QDialog.exec(dialog)
    finally:
        if owner and getattr(owner, "_editor_modal_dialog", None) is dialog:
            owner._editor_modal_dialog = None


class ThemedEditorDialog(QDialog):
    """Fluent content/buttons in an owned WindowModal top-level QDialog.

    ``viewLayout`` and ``yesButton``/``cancelButton`` let existing editor forms
    migrate without replacing their validated form logic.
    """

    def __init__(self, parent=None):
        super().__init__(parent.window() if parent else None, Qt.WindowType.Dialog)
        self.setObjectName("editorFluentDialog")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumWidth(340)
        self.setMaximumWidth(620)
        self.setAutoFillBackground(True)
        self.widget = self
        self.main_layout = QVBoxLayout(self)
        self.vBoxLayout = self.main_layout
        self.main_layout.setContentsMargins(22, 18, 22, 18)
        self.main_layout.setSpacing(16)
        self.title_label = SubtitleLabel(self)
        self.title_label.setWordWrap(True)
        self.title_label.hide()
        self.main_layout.addWidget(self.title_label)
        self.viewLayout = QVBoxLayout()
        self.viewLayout.setContentsMargins(0, 0, 0, 0)
        self.viewLayout.setSpacing(10)
        self.content_layout = self.viewLayout
        self.main_layout.addLayout(self.viewLayout)
        self.buttonLayout = QHBoxLayout()
        self.buttonLayout.setContentsMargins(0, 2, 0, 0)
        self.buttonLayout.setSpacing(8)
        self.buttonLayout.addStretch(1)
        self.cancelButton = PushButton(_text("editor.dialogs.cancel"), self)
        self.yesButton = PrimaryPushButton(_text("editor.dialogs.confirm"), self)
        self._base_yes_button, self._base_cancel_button = self.yesButton, self.cancelButton
        for button in (self.cancelButton, self.yesButton):
            button.setMinimumWidth(88)
            _style_dialog_button(button)
            self.buttonLayout.addWidget(button)
        self.main_layout.addLayout(self.buttonLayout)
        self.cancelButton.setDefault(True)
        self.cancelButton.clicked.connect(self.reject)
        self.yesButton.clicked.connect(self._on_confirm)
        qconfig.themeChangedFinished.connect(self._sync_theme)
        get_i18n().languageChanged.connect(self.retranslate_ui)
        self._sync_theme()

    def setWindowTitle(self, title):  # noqa: N802
        super().setWindowTitle(title)
        if hasattr(self, "title_label"):
            self.title_label.setText(title)
            self.title_label.setVisible(bool(title))

    def _sync_theme(self):
        self.setPalette(palette_for_theme(Theme.DARK if isDarkTheme() else Theme.LIGHT))
        # Windows' native QDialog style otherwise paints white despite its
        # dark QPalette. Only the surface needs QSS; all controls are Fluent.
        self.setStyleSheet("QDialog#editorFluentDialog { background: palette(window); color: palette(window-text); }")
        self.update()

    def retranslate_ui(self, *_args):
        self.cancelButton.setText(_text("editor.dialogs.cancel"))
        self.yesButton.setText(_text("editor.dialogs.confirm"))

    def _on_confirm(self):
        if self.validate():
            self.accept()

    def validate(self):
        return True

    def bind_submit_edit(self, edit):
        """Validate Return once without Qt falling through to default Cancel."""
        edit.setProperty("editorSubmitField", True)
        edit.installEventFilter(self)

    def eventFilter(self, watched, event):  # noqa: N802
        if (event.type() == QEvent.Type.KeyPress and watched.property("editorSubmitField")
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)):
            self._on_confirm()
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def hideYesButton(self):  # noqa: N802
        self.yesButton.hide()

    def hideCancelButton(self):  # noqa: N802
        self.cancelButton.hide()

    def exec(self):
        return exec_editor_dialog(self)


class EditorMessageBox(ThemedEditorDialog):
    """Small QMessageBox-compatible adapter for existing editor prompts."""

    StandardButton = QtMessageBox.StandardButton
    Icon = QtMessageBox.Icon
    Save, Discard, Cancel = QtMessageBox.Save, QtMessageBox.Discard, QtMessageBox.Cancel
    Yes, No, Ok, Close = QtMessageBox.Yes, QtMessageBox.No, QtMessageBox.Ok, QtMessageBox.Close
    NoButton = QtMessageBox.NoButton
    _BUTTON_KEYS = {
        Save: "editor.dialogs.save", Discard: "editor.dialogs.discard", Cancel: "editor.dialogs.cancel",
        Yes: "editor.dialogs.yes", No: "editor.dialogs.no", Ok: "editor.dialogs.confirm", Close: "editor.dialogs.close",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.message_label = BodyLabel(self)
        self.message_label.setTextFormat(Qt.TextFormat.PlainText)
        self.message_label.setWordWrap(True)
        self.viewLayout.addWidget(self.message_label)
        self.informative_label = BodyLabel(self)
        self.informative_label.setTextFormat(Qt.TextFormat.PlainText)
        self.informative_label.setWordWrap(True)
        self.informative_label.hide()
        self.viewLayout.addWidget(self.informative_label)
        self._buttons = {}
        self._custom_button_text = {}
        self._default = self.Cancel
        self._choice = self.Cancel
        self.setStandardButtons(self.Ok)

    def setText(self, text):  # noqa: N802
        self.message_label.setText(str(text))

    def setInformativeText(self, text):  # noqa: N802
        self.informative_label.setText(str(text))
        self.informative_label.setVisible(bool(text))

    def setIcon(self, icon):  # noqa: N802
        self._icon = icon

    def setStandardButtons(self, buttons):  # noqa: N802
        for button in (self.yesButton, self.cancelButton, *self._buttons.values()):
            self.buttonLayout.removeWidget(button)
            button.hide()
        for button in self._buttons.values():
            button.deleteLater()
        self._buttons = {}
        primary = next((key for key in (self.Save, self.Yes, self.Ok) if buttons & key), None)
        for key in (self.Save, self.Discard, self.Yes, self.No, self.Ok, self.Close, self.Cancel):
            if not buttons & key:
                continue
            control = PrimaryPushButton(self) if key == primary else PushButton(self)
            control.setMinimumWidth(88)
            _style_dialog_button(control)
            control.setText(self._custom_button_text.get(key, _text(self._BUTTON_KEYS[key])))
            control.clicked.connect(lambda _checked=False, choice=key: self._choose(choice))
            self._buttons[key] = control
            self.buttonLayout.addWidget(control)
            control.show()
        self.yesButton = self._buttons.get(primary, self._base_yes_button)
        self.cancelButton = self._buttons.get(self.Cancel, self._buttons.get(self.No, self._base_cancel_button))
        self.setDefaultButton(self._default)

    def setDefaultButton(self, button):  # noqa: N802
        # Enter must never silently discard an unsaved document.
        if button == self.Discard or button not in self._buttons:
            button = next((key for key in (self.Cancel, self.No, self.Save, self.Ok, self.Close, self.Yes)
                           if key in self._buttons), self.Cancel)
        self._default = button
        for key, control in self._buttons.items():
            control.setDefault(key == button)
            control.setAutoDefault(key == button)

    def button(self, button):
        return self._buttons.get(button)

    def buttons(self):
        return list(self._buttons.values())

    def setButtonText(self, button, text):  # noqa: N802
        self._custom_button_text[button] = str(text)
        if button in self._buttons:
            self._buttons[button].setText(str(text))

    def _choose(self, choice):
        self._choice = choice
        self.done(int(choice))

    def reject(self):
        self._choose(self.Cancel)

    def retranslate_ui(self, *_args):
        if not hasattr(self, "_buttons"):
            return
        for key, button in self._buttons.items():
            button.setText(self._custom_button_text.get(key, _text(self._BUTTON_KEYS[key])))

    @classmethod
    def _ask(cls, parent, title, text, buttons, defaultButton, button_texts=None):
        box = cls(parent)
        box.setWindowTitle(title)
        box.setText(text)
        box.setStandardButtons(buttons)
        box.setDefaultButton(defaultButton)
        for button, label in (button_texts or {}).items():
            box.setButtonText(button, label)
        try:
            return cls.StandardButton(box.exec())
        finally:
            box.deleteLater()

    @classmethod
    def question(cls, parent, title, text, buttons=Yes | No, defaultButton=NoButton, *, button_texts=None):
        return cls._ask(parent, title, text, buttons, defaultButton, button_texts)

    @classmethod
    def warning(cls, parent, title, text, buttons=Ok, defaultButton=NoButton, *, button_texts=None):
        return cls._ask(parent, title, text, buttons, defaultButton, button_texts)

    information = warning
    critical = warning


class EditorTextDialog(ThemedEditorDialog):
    def __init__(self, title, label, parent=None, initial_text=""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.prompt_label = BodyLabel(label, self)
        self.prompt_label.setWordWrap(True)
        self.viewLayout.addWidget(self.prompt_label)
        self.name_edit = LineEdit(self)
        self.name_edit.setText(initial_text)
        self.name_edit.selectAll()
        self.viewLayout.addWidget(self.name_edit)
        self.bind_submit_edit(self.name_edit)
        self.name_edit.returnPressed.connect(self._on_confirm)

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.name_edit.setFocus()


def get_editor_text(parent, title, label, text=""):
    dialog = EditorTextDialog(title, label, parent, initial_text=text)
    try:
        accepted = dialog.exec() == QDialog.Accepted
        return dialog.name_edit.text(), accepted
    finally:
        dialog.deleteLater()


__all__ = ["DIALOG_TEXT", "ThemedEditorDialog", "EditorMessageBox", "EditorTextDialog", "get_editor_text", "exec_editor_dialog"]
