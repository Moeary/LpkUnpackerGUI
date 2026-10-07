"""Compact editor preference cards integrated into the existing settings pages."""
from PySide6.QtGui import QKeySequence, QPainter
from PySide6.QtWidgets import QFormLayout, QGridLayout, QHBoxLayout, QKeySequenceEdit, QSizePolicy, QStyle, QStyleOption, QVBoxLayout
from qfluentwidgets import CardWidget, CaptionLabel, CheckBox, PrimaryPushButton, PushButton, SpinBox, SubtitleLabel, isDarkTheme, qconfig

from app.gui.editor_shortcuts import SPECS, bindings, label, preferences
from app.i18n import get_i18n


class _KeyEditor(QKeySequenceEdit):
    def __init__(self, sequence, parent):
        super().__init__(sequence, parent)
        self.setObjectName("editorKey")
        qconfig.themeChangedFinished.connect(self._sync_theme)
        self._sync_theme()

    def _sync_theme(self, *_args):
        background, foreground, border = ("#2b2b2b", "#f3f3f3", "#5a5a5a") if isDarkTheme() else ("#ffffff", "#202020", "#b8bec7")
        self.setStyleSheet(
            f'QKeySequenceEdit#editorKey {{ background: {background}; color: {foreground}; border: 1px solid {border}; border-radius: 5px; padding: 4px; }}'
            f'QKeySequenceEdit#editorKey QLineEdit {{ background: transparent; color: {foreground}; border: none; }}'
        )

    def paintEvent(self, event):
        # QKeySequenceEdit does not paint QWidget stylesheet backgrounds itself.
        option = QStyleOption()
        option.initFrom(self)
        painter = QPainter(self)
        self.style().drawPrimitive(QStyle.PE_Widget, option, painter, self)
        painter.end()
        super().paintEvent(event)


class ShortcutSettingsCard(CardWidget):
    def __init__(self, kind, settings, parent=None):
        super().__init__(parent)
        self.kind, self.settings = kind, settings
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        self.title = SubtitleLabel(self)
        self.hint = CaptionLabel(self)
        self.hint.setWordWrap(True)
        layout.addWidget(self.title)
        layout.addWidget(self.hint)
        form = QGridLayout()
        form.setColumnStretch(0, 1)
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(8)
        self.edits, self.labels = {}, {}
        values = bindings(settings, kind)
        for row_index, (action, _name, _default) in enumerate(SPECS[kind]):
            name = CaptionLabel(self)
            name.setWordWrap(True)
            name.setMinimumWidth(0)
            name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            edit = _KeyEditor(QKeySequence(values[action]), self)
            edit.setMaximumSequenceLength(1)
            edit.setClearButtonEnabled(True)
            edit.setFixedWidth(185)
            edit.setMinimumHeight(30)
            self.edits[action], self.labels[action] = edit, name
            form.addWidget(name, row_index, 0)
            form.addWidget(edit, row_index, 1)
            edit.keySequenceChanged.connect(self.validate)
        layout.addLayout(form)
        self.status = CaptionLabel(self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self.reset = PushButton(self)
        self.apply = PrimaryPushButton(self)
        self.reset.clicked.connect(self.defaults)
        self.apply.clicked.connect(self.save)
        row.addWidget(self.reset)
        row.addStretch(1)
        row.addWidget(self.apply)
        layout.addLayout(row)
        get_i18n().languageChanged.connect(self.retranslate_ui)
        self.retranslate_ui()

    def retranslate_ui(self, *_args):
        self.title.setText(("Live2D" if self.kind == "live2d" else "Spine") + " · " + label("title", "快捷键"))
        self.hint.setText(label("hint", "点击键位后按下组合键；清空可禁用。仅在对应编辑器生效，输入框保留文字编辑快捷键。"))
        for action, fallback, default in SPECS[self.kind]:
            self.labels[action].setText(label(action, fallback))
            self.edits[action].setAccessibleName(label(action, fallback))
            self.edits[action].setToolTip(label("default", "默认：") + QKeySequence(default).toString())
        self.reset.setText(label("reset", "恢复默认"))
        self.apply.setText(label("apply", "应用键位"))
        self.validate()

    def values(self):
        return {action: edit.keySequence().toString(QKeySequence.PortableText) for action, edit in self.edits.items()}

    def validate(self, *_args):
        occupied = {}
        conflict = None
        for action, value in self.values().items():
            if value and value in occupied:
                conflict = (occupied[value], action, value)
                break
            if value:
                occupied[value] = action
        self.apply.setEnabled(conflict is None)
        if conflict:
            first, second, key = conflict
            self.status.setText(label("conflict", "键位冲突：") + f"{key} · {self.labels[first].text()} / {self.labels[second].text()}")
        else:
            self.status.setText(label("pending", "修改后点击应用；两个编辑器可使用相同键位。"))
        return conflict is None

    def defaults(self):
        for action, _name, default in SPECS[self.kind]:
            self.edits[action].setKeySequence(QKeySequence(default))
        self.validate()

    def save(self):
        if not self.validate():
            return
        if not self.settings.set_editor_preference("shortcuts." + self.kind, self.values()):
            self.status.setText(label("save_failed", "设置保存失败，请检查配置文件是否可写。"))
            return
        preferences.changed.emit(self.settings.settings_file)
        self.status.setText(label("applied", "键位已应用"))


class RecoverySettingsCard(CardWidget):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        self.title = SubtitleLabel(self)
        self.hint = CaptionLabel(self)
        self.hint.setWordWrap(True)
        self.enabled = CheckBox(self)
        self.enabled.setChecked(settings.get("editor.recovery.enabled", True))
        self.interval = SpinBox(self)
        self.interval.setRange(15, 600)
        self.interval.setValue(settings.get("editor.recovery.interval", 60))
        self.keep = SpinBox(self)
        self.keep.setRange(1, 10)
        self.keep.setValue(settings.get("editor.recovery.keep", 3))
        for widget in (self.title, self.hint, self.enabled):
            layout.addWidget(widget)
        form = QFormLayout()
        self.interval_label, self.keep_label = CaptionLabel(self), CaptionLabel(self)
        form.addRow(self.interval_label, self.interval)
        form.addRow(self.keep_label, self.keep)
        layout.addLayout(form)
        self.enabled.toggled.connect(lambda value: self.save("enabled", value))
        self.interval.valueChanged.connect(lambda value: self.save("interval", value))
        self.keep.valueChanged.connect(lambda value: self.save("keep", value))
        get_i18n().languageChanged.connect(self.retranslate_ui)
        self.retranslate_ui()

    def save(self, key, value):
        if not self.settings.set_editor_preference("recovery." + key, value):
            self.hint.setText(label("save_failed", "设置保存失败，请检查配置文件是否可写。"))

    def retranslate_ui(self, *_args):
        self.title.setText(label("recovery_title", "工程恢复"))
        self.hint.setText(label("recovery_hint", "后台备份未保存的 Live2D / Spine 工程；任务忙碌时顺延。恢复点不是正式保存，异常退出后可从编辑器恢复。"))
        self.enabled.setText(label("recovery_enabled", "自动建立恢复点"))
        self.interval_label.setText(label("interval", "间隔（秒）"))
        self.keep_label.setText(label("keep", "每个工程保留份数"))
