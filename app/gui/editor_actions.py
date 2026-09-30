"""Searchable action selectors and native modal dialogs for both editors."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QDialog, QHBoxLayout, QVBoxLayout, QApplication
from qfluentwidgets import (
    BodyLabel, CaptionLabel, DoubleSpinBox, EditableComboBox, LineEdit,
    PrimaryPushButton, PushButton, RoundMenu, isDarkTheme,
)

from app.core.animation_editing import _name
from app.i18n import tr


ACTION_TEXT = {
    "editor.actions.search": "搜索并选择动作",
    "editor.actions.name": "动作名称",
    "editor.actions.duration": "时长（秒）",
    "editor.actions.empty_name": "请输入动作名称。",
    "editor.actions.duplicate_name": "动作名称已存在，请换一个名称。",
    "editor.actions.invalid_name": "动作名称不能包含路径符号或控制字符。",
    "editor.actions.delete_title": "删除动作",
    "editor.actions.delete_message": "删除动作“{name}”？可通过撤销恢复。",
    "editor.actions.new": "新建动作",
    "editor.actions.copy": "复制动作",
    "editor.actions.delete": "删除动作",
    "editor.actions.keyframe": "在当前时间添加当前轨道的关键帧（不是新建动作）",
}


def action_text(key, **values):
    return tr(key, default=ACTION_TEXT[key], **values)


def close_editor_popup():
    popup = QApplication.activePopupWidget()
    if popup:
        popup.close()


class ActionComboBox(EditableComboBox):
    """Typing filters known actions; Return never invents a combo item."""

    def __init__(self, parent=None):
        self._selected_index = -1
        super().__init__(parent)
        self.setMinimumWidth(110)
        self.setMaximumWidth(200)
        self.setClearButtonEnabled(True)
        self.setMaxVisibleItems(12)
        self.retranslate_ui()

    def minimumSizeHint(self):  # noqa: N802
        result = super().minimumSizeHint()
        return QSize(110, result.height())

    def sizeHint(self):  # noqa: N802
        result = super().sizeHint()
        return QSize(150, result.height())

    def addItem(self, text, userData=None):  # noqa: N802
        return super().addItem(text, userData=userData)

    def clear(self):
        self._selected_index = -1
        super().clear()

    def currentIndex(self):  # noqa: N802
        return self._selected_index

    def setCurrentIndex(self, index):  # noqa: N802
        if not 0 <= index < self.count():
            return
        previous = self._selected_index
        self._selected_index = self._currentIndex = index
        self.setText(self.items[index].text)
        if previous != index:
            self.currentIndexChanged.emit(index)

    def _onComboTextChanged(self, text):
        # A query must not pause/reset/change the selected model action.
        self.currentTextChanged.emit(text)

    def _onReturnPressed(self):
        matches = self.matching_indices()
        exact = self.findText(self.text())
        if exact >= 0:
            self.setCurrentIndex(exact)
        elif len(matches) == 1:
            self.setCurrentIndex(matches[0])
        else:
            self._showComboMenu()

    def matching_indices(self):
        query = self.text().strip().casefold()
        if self.currentIndex() >= 0 and self.text() == self.items[self.currentIndex()].text:
            query = ""
        return [index for index, item in enumerate(self.items) if query in item.text.casefold()]

    def _showComboMenu(self):
        if self.dropMenu:
            self._closeComboMenu()
        matches = self.matching_indices()
        if not matches:
            return
        menu = RoundMenu(parent=self)
        menu.setMaxVisibleItems(12)
        for index in matches:
            action = QAction(self.items[index].text, menu)
            action.triggered.connect(lambda _checked=False, i=index: self.setCurrentIndex(i))
            menu.addAction(action)
        menu.closedSignal.connect(self._onDropMenuClosed)
        self.dropMenu = menu
        menu.exec(self.mapToGlobal(self.rect().bottomLeft()))

    def focusOutEvent(self, event):  # noqa: N802
        if self.currentIndex() >= 0 and not self.dropMenu:
            self.setText(self.items[self.currentIndex()].text)
        super().focusOutEvent(event)

    def retranslate_ui(self):
        self.setPlaceholderText(action_text("editor.actions.search"))
        self.setToolTip(action_text("editor.actions.search"))
        self.setAccessibleName(action_text("editor.actions.search"))


class ActionNameDialog(QDialog):
    """An owned top-level window, above native QWindow render surfaces."""

    def __init__(self, title, parent, duration=True, existing_names=(), initial_name=""):
        super().__init__(parent, Qt.WindowType.Dialog)
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.existing_names = set(existing_names)
        self.setMinimumWidth(340)
        self.setMaximumWidth(480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(BodyLabel(action_text("editor.actions.name"), self))
        self.name_edit = LineEdit(self)
        self.name_edit.setText(initial_name)
        self.name_edit.selectAll()
        layout.addWidget(self.name_edit)
        self.duration_spin = DoubleSpinBox(self)
        self.duration_spin.setRange(.01, 3600)
        self.duration_spin.setDecimals(3)
        self.duration_spin.setValue(3)
        if duration:
            layout.addWidget(BodyLabel(action_text("editor.actions.duration"), self))
            layout.addWidget(self.duration_spin)
        else:
            self.duration_spin.hide()
        self.error_label = CaptionLabel(self)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #e67d73;")
        self.error_label.hide()
        layout.addWidget(self.error_label)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancelButton = PushButton(tr("common.cancel", default="取消"), self)
        self.yesButton = PrimaryPushButton(tr("common.confirm", default="确定"), self)
        buttons.addWidget(self.cancelButton)
        buttons.addWidget(self.yesButton)
        layout.addLayout(buttons)
        self.cancelButton.clicked.connect(self.reject)
        self.yesButton.clicked.connect(self._submit)
        self.name_edit.returnPressed.connect(self._submit)
        self.setStyleSheet(f"ActionNameDialog {{ background: {'#242629' if isDarkTheme() else '#fafafa'}; }}")

    def validate(self):
        name = self.name_edit.text().strip()
        error = ""
        if not name:
            error = action_text("editor.actions.empty_name")
        elif name in self.existing_names:
            error = action_text("editor.actions.duplicate_name")
        else:
            try:
                _name(name)
            except ValueError:
                error = action_text("editor.actions.invalid_name")
        self.error_label.setText(error)
        self.error_label.setVisible(bool(error))
        if error:
            self.name_edit.setFocus()
        return not error

    def _submit(self):
        if self.validate():
            self.accept()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.name_edit.setFocus()


class ActionDeleteDialog(QDialog):
    def __init__(self, name, parent):
        super().__init__(parent, Qt.WindowType.Dialog)
        self.setWindowTitle(action_text("editor.actions.delete_title"))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumWidth(340)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        label = BodyLabel(action_text("editor.actions.delete_message", name=name), self)
        label.setWordWrap(True)
        layout.addWidget(label)
        row = QHBoxLayout()
        row.addStretch(1)
        self.cancelButton = PushButton(tr("common.cancel", default="取消"), self)
        self.yesButton = PrimaryPushButton(action_text("editor.actions.delete"), self)
        row.addWidget(self.cancelButton)
        row.addWidget(self.yesButton)
        layout.addLayout(row)
        self.cancelButton.clicked.connect(self.reject)
        self.yesButton.clicked.connect(self.accept)
        self.cancelButton.setDefault(True)
        self.setStyleSheet(f"ActionDeleteDialog {{ background: {'#242629' if isDarkTheme() else '#fafafa'}; }}")
