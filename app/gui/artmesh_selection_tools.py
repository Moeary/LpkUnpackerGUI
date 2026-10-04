"""Compact saved selections and cancellable, read-only UV impact analysis."""
from __future__ import annotations

from threading import Event

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget, QSizePolicy
from qfluentwidgets import CaptionLabel, CheckBox, ComboBox, FluentIcon, RoundMenu, TransparentToolButton

from app.core.psd_selection import selection_uv_constraints
from app.gui.editor_dialogs import EditorTextDialog, EditorMessageBox
from app.gui.live2d_appearance import CompactAppearanceButton, ElidedAppearanceLabel
from app.i18n import get_i18n, tr


TEXT = {
    "saved": "命名选区", "empty": "选择已保存的选区…", "save": "保存选区",
    "save_hint": "将当前选中的 ArtMesh 保存到此工程", "name": "选区名称",
    "rename": "重命名", "update": "用当前选区更新", "delete": "删除选区",
    "delete_question": "删除选区“{name}”？模型部件不受影响，可撤销。",
    "invalid": "请输入 1–80 个字符的名称，且不能与已有选区重名。",
    "more": "管理此命名选区", "shared": "共享 UV 高亮",
    "hint": "按图集原始像素检查，包含隐藏部件与透明区域；表示潜在影响，不代表已经修改。",
    "idle": "选中部件后，开启共享 UV 检查",
    "empty_selection": "请先选择部件", "busy": "正在检查共享 UV…",
    "result": "关联 {count} 个部件 · 橙色虚线",
    "none": "未发现与未选部件共享的图集像素",
    "failed": "UV 检查失败（悬停查看原因）", "include": "加入选区",
    "include_hint": "将所有潜在关联部件加入当前选区；不会修改模型或贴图",
    "missing": "已载入 {count} 个部件；{missing} 个 ID 在当前模型中缺失。",
    "legend": "黄色实线：已选部件；橙色虚线：共享图集像素的未选部件。",
}


def selection_text(key, **values):
    return tr("editor.selection." + key, default=TEXT[key], **values)


class _CompactCheckBox(CheckBox):
    def __init__(self, parent):
        super().__init__(parent)
        self._full_text = ""
        self.setMinimumWidth(90)
        self.setMaximumWidth(150)

    def setText(self, text):
        self._full_text = text
        self._fit_text()

    def _fit_text(self):
        super().setText(self.fontMetrics().elidedText(self._full_text, Qt.ElideRight, max(32, self.width() - 38)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_text()

    def sizeHint(self):
        return QSize(min(150, max(90, self.fontMetrics().horizontalAdvance(self._full_text) + 38)),
                     super().sizeHint().height())

    def minimumSizeHint(self):
        return QSize(90, super().minimumSizeHint().height())


class SelectionNameDialog(EditorTextDialog):
    def __init__(self, title, names, parent, initial=""):
        super().__init__(title, selection_text("name"), parent, initial_text=initial)
        self.names = {name.casefold() for name in names}
        self.name_edit.setMaxLength(80)
        self.error = CaptionLabel(self)
        self.error.setWordWrap(True)
        self.error.hide()
        self.viewLayout.addWidget(self.error)

    def accept(self):
        name = self.name_edit.text().strip()
        if not name or name.casefold() in self.names:
            self.error.setText(selection_text("invalid"))
            self.error.show()
            self.name_edit.setFocus()
            return
        super().accept()


class _Signals(QObject):
    completed = Signal(object, object, str)


class _ImpactJob(QRunnable):
    def __init__(self, key, drawables, ids, sizes):
        super().__init__()
        self.key, self.drawables, self.ids, self.sizes = key, drawables, ids, sizes
        self.cancelled = Event()
        self.signals = _Signals()

    def run(self):
        try:
            import cv2
            _protected, shared = selection_uv_constraints(
                cv2, self.drawables, self.ids, self.sizes, cancelled=self.cancelled.is_set)
            self.signals.completed.emit(self.key, shared, "")
        except InterruptedError:
            self.signals.completed.emit(self.key, None, "")
        except Exception as exc:
            self.signals.completed.emit(self.key, None, str(exc))


class ArtMeshSelectionTools(QWidget):
    """No model/Core objects cross the worker boundary; old results are ignored."""
    changed = Signal()
    highlightsChanged = Signal(list)
    message = Signal(str)
    failed = Signal(str)

    def __init__(self, inspector, parent=None):
        super().__init__(parent)
        self.inspector = inspector
        self.session = None
        self.editable = False
        self._sets = {}
        self._context = None
        self._key = None
        self._cached_key = None
        self._cached_result = []
        self._job = None
        self._related = []
        self._closed = False
        self._state = "idle"
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(5)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.combo = ComboBox(self)
        self.combo.setMinimumWidth(0)
        self.combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.save_button = CompactAppearanceButton(self)
        self.save_button.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.save_button.setMinimumWidth(70)
        self.save_button.setMaximumWidth(110)
        self.more_button = TransparentToolButton(FluentIcon.MORE, self)
        self.more_button.setFixedSize(30, 30)
        self.undo_button = TransparentToolButton(FluentIcon.RETURN, self)
        self.redo_button = TransparentToolButton(FluentIcon.ROTATE, self)
        for button in (self.undo_button, self.redo_button):
            button.setFixedSize(26, 30)
        self.undo_button.clicked.connect(inspector.undo_selection)
        self.redo_button.clicked.connect(inspector.redo_selection)
        inspector.selectionIdsChanged.connect(self._history_changed)
        for widget in (self.combo, self.save_button, self.undo_button, self.redo_button, self.more_button):
            row.addWidget(widget, 1 if widget is self.combo else 0)
        layout.addLayout(row)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.shared_check = _CompactCheckBox(self)
        self.summary = ElidedAppearanceLabel(self)
        self.summary.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.include_button = CompactAppearanceButton(self)
        self.include_button.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.include_button.setMinimumWidth(70)
        self.include_button.setMaximumWidth(110)
        row.addWidget(self.shared_check)
        row.addWidget(self.summary, 1)
        row.addWidget(self.include_button)
        layout.addLayout(row)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self._analyze)
        self.combo.activated.connect(self._apply)
        self.save_button.clicked.connect(self.save_selection)
        self.more_button.clicked.connect(self._menu)
        self.shared_check.toggled.connect(self.invalidate)
        self.include_button.clicked.connect(self.include_related)
        inspector.selectionIdsChanged.connect(self.invalidate)
        get_i18n().languageChanged.connect(self.retranslate_ui)
        self.retranslate_ui()
        self.refresh(None, False)

    def _history_changed(self, *_args):
        self.undo_button.setEnabled(bool(self.inspector.selection_history.past))
        self.redo_button.setEnabled(bool(self.inspector.selection_history.future))

    def retranslate_ui(self, *_args):
        self.undo_button.setToolTip(tr("editor.shortcuts.selection_undo", default="撤销选区"))
        self.redo_button.setToolTip(tr("editor.shortcuts.selection_redo", default="重做选区"))
        self._history_changed()
        self.combo.setAccessibleName(selection_text("saved"))
        self.save_button.setText(selection_text("save"))
        self.save_button.setToolTip(selection_text("save_hint"))
        self.more_button.setToolTip(selection_text("more"))
        self.shared_check.setText(selection_text("shared"))
        self.shared_check.setToolTip(selection_text("hint"))
        self.include_button.setText(selection_text("include"))
        self.include_button.setToolTip(selection_text("include_hint"))
        for widget in (self.save_button, self.more_button, self.shared_check, self.include_button):
            widget.setAccessibleName(widget.toolTip() or widget.text())
        if self.combo.count():
            self.combo.setItemText(0, selection_text("empty"))
        self._show_state()

    def refresh(self, session, editable, *, context=None):
        changed_context = self.session is not session or self._context is not context
        self.session, self.editable, self._context = session, bool(editable), context
        sets = session.named_selections() if session else {}
        if changed_context or sets != self._sets or not self.combo.count():
            name = self.combo.currentData() if not changed_context else None
            self._sets = sets
            with QSignalBlocker(self.combo):
                self.combo.clear()
                self.combo.addItem(selection_text("empty"), userData=None)
                for key, ids in sets.items():
                    self.combo.addItem(f"{key} · {len(ids)}", userData=key)
                self.combo.setCurrentIndex(max(0, self.combo.findData(name)))
        self.combo.setEnabled(bool(session and editable and sets))
        name = self.combo.currentData()
        self.combo.setToolTip((name + "\n" + "\n".join(sets[name])) if name in sets else selection_text("empty"))
        self.save_button.setEnabled(bool(session and editable and self.inspector.selected_drawable_ids()))
        self.more_button.setEnabled(bool(session and editable and self.combo.currentData()))
        self.shared_check.setEnabled(bool(session))
        self.include_button.setEnabled(bool(self._related))
        if changed_context:
            self._cached_key = None
            self.invalidate()

    def _apply(self, _index):
        name = self.combo.currentData()
        if not self.editable or name not in self._sets:
            return
        ids = self._sets[name]
        available = {entry.drawable_id for entry in self.inspector.entries}
        self.inspector.select_entries([identifier for identifier in ids if identifier in available])
        missing = len(set(ids) - available)
        if missing:
            self.message.emit(selection_text("missing", count=len(ids) - missing, missing=missing))
        self.more_button.setEnabled(True)

    def save_selection(self, *, replace=False):
        if not self.session or not self.editable:
            return
        ids = self.inspector.selected_drawable_ids()
        if not ids:
            return
        name = self.combo.currentData() if replace else None
        if name is None:
            dialog = SelectionNameDialog(selection_text("save"), self._sets, self)
            try:
                if not dialog.exec():
                    return
                name = dialog.name_edit.text().strip()
            finally:
                dialog.deleteLater()
        try:
            self.session.save_named_selection(name, ids, replace=replace)
            self.changed.emit()
            self.refresh(self.session, self.editable, context=self._context)
            with QSignalBlocker(self.combo):
                self.combo.setCurrentIndex(self.combo.findData(name))
            self.more_button.setEnabled(True)
        except Exception as exc:
            self.failed.emit(str(exc))

    def _menu(self):
        name = self.combo.currentData()
        if not self.editable or name not in self._sets:
            return
        menu = RoundMenu(parent=self)
        for key, callback in (("update", lambda: self.save_selection(replace=True)),
                              ("rename", self.rename_selection), ("delete", self.delete_selection)):
            action = QAction(selection_text(key), menu)
            action.setEnabled(key != "update" or bool(self.inspector.selected_drawable_ids()))
            action.triggered.connect(callback)
            menu.addAction(action)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.exec(self.more_button.mapToGlobal(self.more_button.rect().bottomLeft()))

    def rename_selection(self):
        name = self.combo.currentData()
        if not self.editable or name not in self._sets:
            return
        dialog = SelectionNameDialog(selection_text("rename"), [n for n in self._sets if n != name], self, name)
        if dialog.exec():
            try:
                new_name = dialog.name_edit.text().strip()
                self.session.rename_named_selection(name, new_name)
                self.changed.emit()
                self.refresh(self.session, self.editable, context=self._context)
                with QSignalBlocker(self.combo):
                    self.combo.setCurrentIndex(self.combo.findData(new_name))
                self.more_button.setEnabled(True)
            except Exception as exc:
                self.failed.emit(str(exc))
        dialog.deleteLater()

    def delete_selection(self):
        name = self.combo.currentData()
        if not self.editable or name not in self._sets:
            return
        if EditorMessageBox.question(self.window(), selection_text("delete"),
                                     selection_text("delete_question", name=name),
                                     EditorMessageBox.Yes | EditorMessageBox.Cancel,
                                     EditorMessageBox.Cancel) != EditorMessageBox.Yes:
            return
        try:
            self.session.delete_named_selection(name)
            self.changed.emit()
            self.refresh(self.session, self.editable, context=self._context)
        except Exception as exc:
            self.failed.emit(str(exc))

    def invalidate(self, *_args):
        key = self._request_key()
        if key is not None and key == self._key:
            return  # pose-only refreshes must not restart a UV-only analysis
        self._key = key
        if self._job:
            self._job.cancelled.set()
        self._set_related([])
        self._state = ("idle" if not self.shared_check.isChecked() else
                       "busy" if self.inspector.selected_drawable_ids() else "empty_selection")
        self._show_state()
        if not self._closed:
            self.timer.start()

    def _request_key(self):
        ids = self.inspector.selected_drawable_ids()
        if not self.session or not self.shared_check.isChecked() or not ids:
            return None
        geometry = tuple((e.drawable_id, e.texture_index, tuple(e.uvs), tuple(e.indices))
                         for e in self.inspector.entries)
        return (id(self._context or self.session), tuple(sorted(self.inspector.texture_sizes.items())),
                geometry, tuple(ids))

    def _analyze(self):
        if self._closed or not self.session or not self.shared_check.isChecked():
            return
        ids = self.inspector.selected_drawable_ids()
        if not ids:
            return
        entries = self.inspector.entries
        sizes = self.inspector.texture_sizes
        key = self._request_key()
        self._key = key
        selected_pages = {e.texture_index for e in entries if e.drawable_id in ids}
        if any(index not in sizes or self.inspector._texture_pixmap(index).isNull()
               for index in selected_pages):
            self._accept(key, None, "Missing atlas image or dimensions")
            return
        if key == self._cached_key:
            self._accept(key, self._cached_result, "")
            return
        if self._job:
            return  # coalesce rapid gestures; completion starts the latest one
        drawables = [{"id": e.drawable_id, "texture_index": e.texture_index,
                      "uvs": [list(p) for p in e.uvs], "indices": list(e.indices)} for e in entries]
        texture_sizes = [sizes.get(i, (1, 1)) for i in range(max(sizes, default=-1) + 1)]
        self._job = _ImpactJob(key, drawables, ids, texture_sizes)
        self._job.signals.completed.connect(self._completed)
        QThreadPool.globalInstance().start(self._job)

    def _completed(self, key, result, error):
        self._job = None
        if self._closed:
            return
        if key == self._key and (result is not None or error):
            self._accept(key, result, error)
        else:
            self.timer.start()

    def _accept(self, key, result, error):
        if error:
            self._state = "failed"
            self._show_state(error)
            return
        self._cached_key, self._cached_result = key, result
        self._set_related([item["unselected_id"] for item in result])
        self._state = "result" if self._related else "none"
        self._show_state()

    def _set_related(self, ids):
        self._related = list(dict.fromkeys(ids))
        self.inspector.set_shared_highlights(self._related)
        self.include_button.setEnabled(bool(self._related))
        self.highlightsChanged.emit(self._related)

    def _show_state(self, detail=""):
        text = selection_text(self._state, count=len(self._related))
        self.summary.setText(text)
        self.summary.setToolTip(detail or (text + "\n" + selection_text("legend") +
                                          ("\n" + "\n".join(self._related) if self._related else "")))

    def include_related(self):
        self.inspector.select_entries(self.inspector.selected_drawable_ids() + self._related)

    def shutdown(self):
        self._closed = True
        self.timer.stop()
        if self._job:
            self._job.cancelled.set()
        self._set_related([])
