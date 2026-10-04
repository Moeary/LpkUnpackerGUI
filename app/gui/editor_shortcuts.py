"""Editable single-chord shortcuts scoped to the visible editor and its canvas."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QApplication, QAbstractSpinBox, QKeySequenceEdit, QLineEdit, QPlainTextEdit,
    QTextEdit, QWidget,
)

from app.core.settings_manager import SettingsManager
from app.i18n import tr


COMMON = [
    ("undo", "撤销（选区区域撤销选区）", "Ctrl+Z"),
    ("redo", "重做（选区区域重做选区）", "Ctrl+Shift+Z"),
    ("redo_alt", "重做（备用键位）", "Ctrl+Y"),
    ("save", "保存工程副本", "Ctrl+S"),
    ("play", "播放 / 暂停", "Space"),
    ("key", "当前轨道添加关键帧", "K"),
    ("delete_key", "删除选中的关键帧", "Delete"),
    ("new", "新建动作", "Ctrl+N"),
    ("clone", "复制当前动作", "Ctrl+D"),
]
SPECS = {
    "live2d": COMMON + [
        ("point", "ArtMesh 点选", "V"), ("box", "ArtMesh 框选", "B"),
        ("clear", "清空选区", "Esc"), ("named", "保存命名选区", "Ctrl+Alt+S"),
        ("uv", "共享 UV 高亮", "U"), ("psd", "导出选区 PSD", "Ctrl+Shift+E"),
        ("selection_undo", "撤销选区（任意编辑区域）", "Ctrl+Alt+Z"),
        ("selection_redo", "重做选区（任意编辑区域）", "Ctrl+Alt+Shift+Z"),
    ],
    "spine": COMMON + [
        ("bones", "骨骼面板", "1"), ("slots", "Slot 图层面板", "2"),
        ("atlas", "图集面板", "3"), ("slot_key", "当前 Slot 设置关键帧", "Shift+K"),
        ("slot_clear", "删除当前 Slot 关键帧", "Ctrl+Delete"),
        ("up", "Slot 图层上移", "Alt+Up"), ("down", "Slot 图层下移", "Alt+Down"),
    ],
}


def label(action, fallback):
    return tr("editor.shortcuts." + action, default=fallback)


def bindings(settings, kind):
    overrides = settings.get("editor.shortcuts." + kind, {})
    return {action: str(overrides.get(action, default)) for action, _name, default in SPECS[kind]}


class _Preferences(QObject):
    changed = Signal(str)


preferences = _Preferences()


class EditorShortcutRouter(QObject):
    def __init__(self, page, kind, settings=None):
        super().__init__(page)
        self.page, self.kind = page, kind
        self.settings = settings or SettingsManager()
        self.reload()
        preferences.changed.connect(self.reload)
        QApplication.instance().installEventFilter(self)
        page.timeline.external_shortcuts = True
        page.sourceOpened.connect(self._bind_canvas)

    def _bind_canvas(self, *_args):
        canvas = self._canvas()
        if canvas is not None:
            canvas.external_selection_shortcuts = True

    def reload(self, settings_file=None):
        if settings_file and settings_file != self.settings.settings_file:
            return
        self.settings.settings = self.settings.load_settings()
        self.keys = {QKeySequence(value)[0].toCombined(): action
                     for action, value in bindings(self.settings, self.kind).items()
                     if value and not QKeySequence(value).isEmpty()}

    def _canvas(self):
        preview = self.page.preview
        return getattr(preview, "live2d_canvas" if self.kind == "live2d" else "canvas", None)

    def _belongs(self, target):
        return target is self._canvas() or (isinstance(target, QWidget) and
                                           (target is self.page or self.page.isAncestorOf(target)))

    @staticmethod
    def _typing(target):
        while isinstance(target, QWidget):
            if isinstance(target, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QKeySequenceEdit)):
                return True
            target = target.parentWidget()
        return False

    def selection_context(self, target):
        if self.kind != "live2d":
            return False
        return target is self._canvas() or target is self.page.artmesh_tab or (
            isinstance(target, QWidget) and self.page.artmesh_tab.isAncestorOf(target))

    def eventFilter(self, target, event):
        if event.type() not in (QEvent.ShortcutOverride, QEvent.KeyPress):
            return False
        app = QApplication.instance()
        if not self.page.isVisible() or app.activeModalWidget() or app.activePopupWidget():
            return False
        if not self._belongs(target):
            return False
        action = self.keys.get(event.keyCombination().toCombined())
        if action is None or (self._typing(target) and action != "save"):
            return False
        event.accept()
        if event.type() == QEvent.KeyPress and not event.isAutoRepeat():
            self.dispatch(action, target)
        return True

    def dispatch(self, action, target=None):
        page = self.page
        if action == "redo_alt":
            action = "redo"
        if not page.session or (self.kind == "spine" and page.loading):
            return
        if self.kind == "live2d" and (page._projects_busy() or page._preview_workers):
            return
        if action in ("undo", "redo"):
            if self.selection_context(target):
                getattr(page.artmesh_inspector, action + "_selection")()
            else:
                getattr(page, action)()
            return
        buttons = {"save": page.save_button, "play": page.timeline.play_button,
                   "new": page.new_motion_button if self.kind == "live2d" else page.new_button,
                   "clone": page.clone_motion_button if self.kind == "live2d" else page.clone_button}
        if action in buttons:
            buttons[action].click()
        elif action in ("key", "delete_key"):
            if page.timeline.isVisible() and page.timeline.isEnabled():
                getattr(page.timeline, "add_keyframe" if action == "key" else "delete_selected")()
        elif self.kind == "live2d":
            if action in ("point", "box"):
                page.tabs.setCurrentWidget(page.artmesh_tab)
                page.set_artmesh_selection_mode("point" if action == "point" else "rectangle")
            elif action == "clear":
                page.clear_artmesh_selection()
            elif action == "named":
                page.selection_tools.save_button.click()
            elif action == "uv":
                page.selection_tools.shared_check.click()
            elif action == "psd":
                page.export_selection_button.click()
            elif action in ("selection_undo", "selection_redo"):
                getattr(page.artmesh_inspector, action.split("_")[1] + "_selection")()
        else:
            if action in ("bones", "slots", "atlas"):
                page.tabs.setCurrentIndex(("bones", "slots", "atlas").index(action))
            elif action == "slot_key":
                if page.animation_name and page.slot_inspector.name:
                    page.slot_inspector.key_check.setChecked(True)
                    page._apply_slot()
            elif action == "slot_clear":
                if page.slot_inspector.name:
                    page._delete_slot_key()
            elif action in ("up", "down"):
                page._move_slot(-1 if action == "up" else 1)
