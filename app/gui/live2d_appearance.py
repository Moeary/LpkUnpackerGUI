"""Appearance tasks and one feedback surface for the shared Live2D editor."""
from __future__ import annotations

from concurrent.futures import CancelledError
from contextlib import nullcontext
from threading import Event

from PySide6.QtCore import QSize, Qt, QThread, QSignalBlocker, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLayout, QSizePolicy, QWidget
from qfluentwidgets import CaptionLabel, Pivot, ProgressBar, PushButton, TextEdit, TransparentToolButton, FluentIcon

from app.gui.editor_workspace import EditorSplitter, EditorViewportLayout
from app.i18n import tr


APPEARANCE_TEXT = {
    "editor.appearance.tab": "外观",
    "editor.appearance.animation": "动画与参数",
    "editor.appearance.animation_hint": "动画与参数",
    "editor.appearance.parts": "部件与选区",
    "editor.appearance.parts_hint": "ArtMesh 部件与选区",
    "editor.appearance.atlas": "图集",
    "editor.appearance.psd_task": "PSD 任务",
    "editor.appearance.psd_hint": "基于当前皮肤与姿态生成 PSD；回写结果保留在皮肤目录中。",
    "editor.appearance.psd_export": "生成 PSD",
    "editor.appearance.psd_repack": "回写",
    "editor.appearance.psd_history": "历史版本",
    "editor.appearance.psd_project": "高级项目",
    "editor.appearance.preview_current": "当前编辑：{name}",
    "editor.appearance.preview_modified": "当前编辑：{name} · 工作图集有修改",
    "editor.appearance.preview_readonly": "只读预览：{name}",
    "editor.appearance.preview_preparing": "准备只读预览：{name}",
    "editor.appearance.preview_return": "返回当前编辑",
    "editor.appearance.returned": "已返回当前编辑皮肤：{name}",
    "editor.appearance.psd_existing": "已应用已有皮肤：{name}",
    "editor.appearance.psd_current": "此版本已应用：{name}",
    "editor.appearance.variant_saved": "已保存皮肤变体：{name}",
    "editor.appearance.preview_stopping": "预览任务正在结束，请稍候再切换或关闭。",
    "editor.appearance.origin": "打开来源 PSD 任务",
    "editor.appearance.origin_missing": "此皮肤的来源 PSD 任务已不可用。",
    "editor.appearance.feedback_details": "任务详情",
    "editor.appearance.feedback_ready": "打开模型后开始编辑。",
    "editor.appearance.feedback_running": "处理中",
    "editor.appearance.feedback_succeeded": "已完成",
    "editor.appearance.feedback_failed": "操作失败",
    "editor.appearance.feedback_cancelled": "已取消",
    "editor.appearance.import_model": "导入模型皮肤",
    "editor.appearance.import_textures": "导入图片皮肤",
    "editor.appearance.capture": "保存新皮肤",
    "editor.appearance.edit_psd": "用 PSD 编辑",
    "editor.appearance.save_scope": "保存工程会另存完整副本，包含动作、工作图集、皮肤目录及 PSD / ViewerEX 子工程。",
    "editor.appearance.catalog": "皮肤管理",
    "editor.appearance.catalog_drop": "拖入以导入皮肤",
    "editor.appearance.atlas_drop": "拖入以替换此图集",
    "editor.appearance.atlas_replace": "替换",
    "editor.appearance.atlas_replace_hint": "替换当前工作图集，须保持尺寸；不新增皮肤，不更换模型或动作。",
    "editor.appearance.atlas_edit": "编辑",
    "editor.appearance.atlas_edit_hint": "用图像编辑器打开当前工作图集；保存后自动更新。",
    "editor.appearance.atlas_drop_invalid": "此处只接受一张图片来替换当前图集；导入模型或多张图片请使用左侧皮肤管理。",
    "editor.appearance.atlas_replaced": "已替换工作图集 {index}；可保存为新皮肤。",
    "editor.appearance.atlas_unchanged": "工作图集内容未改变。",
    "editor.appearance.psd_generate_step": "生成",
    "editor.appearance.psd_repack_step": "回写",
    "editor.appearance.psd_history_step": "历史",
    "editor.appearance.psd_settings": "工程与设置",
}


def appearance_text(key: str, **values) -> str:
    return tr(key, default=APPEARANCE_TEXT[key], **values)


class CompactAppearanceButton(PushButton):
    """Long translations stay in tooltips without widening the task pane."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._full_text = ""
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)

    def setText(self, text):  # noqa: N802
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self._fit_text()

    def minimumSizeHint(self):  # noqa: N802
        return QSize(48, super().minimumSizeHint().height())

    def _fit_text(self):
        super().setText(self.fontMetrics().elidedText(self._full_text, Qt.ElideRight, max(32, self.width() - 26)))

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._fit_text()


class AppearanceStepPivot(Pivot):
    """Keep all three workflow steps inside the available toolbar width."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._labels = {}
        self.hBoxLayout.setSizeConstraint(QLayout.SetNoConstraint)
        self.setMinimumWidth(0)

    def setItemText(self, routeKey, text):  # noqa: N802
        self._labels[routeKey] = str(text)
        self._fit_items()

    def _fit_items(self):
        width = max(1, min(100, self.width() // max(1, len(self.items))))
        for key, item in self.items.items():
            item.setFixedWidth(width)
            item.setText(item.fontMetrics().elidedText(self._labels.get(key, ""), Qt.ElideRight, max(1, width - 24)))

    def resizeEvent(self, event):  # noqa: N802
        self._fit_items()
        super().resizeEvent(event)


class ElidedAppearanceLabel(CaptionLabel):
    def __init__(self, parent=None):
        self._full_text = ""
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)

    def setText(self, text):  # noqa: N802
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self._fit_text()

    def _fit_text(self):
        super().setText(self.fontMetrics().elidedText(self._full_text, Qt.ElideRight, max(1, self.width())))

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._fit_text()


class AppearanceTaskWorkspace(QWidget):
    """Skin import and atlas edits sit above a permanent PSD workbench."""
    taskChanged = Signal(str)

    def __init__(self, skin_catalog, atlas_page, psd_page, psd_panel, parent=None, *, settings=None):
        super().__init__(parent)
        self.skin_page, self.psd_page, self.psd_panel = atlas_page, psd_page, psd_panel
        self._task, self._context = "export", {}
        self._settings, self._initial_show, self._restoring = settings, False, False
        self._wide = False
        self._layout_states = {}
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        root = EditorViewportLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.vertical_splitter = EditorSplitter(Qt.Vertical, self)
        self.vertical_splitter.setChildrenCollapsible(False)
        self.upper_splitter = EditorSplitter(Qt.Horizontal, self.vertical_splitter)
        self.upper_splitter.setChildrenCollapsible(False)
        self.upper_splitter.setMinimumHeight(150)
        self.upper_splitter.addWidget(skin_catalog)
        self.upper_splitter.addWidget(atlas_page)
        skin_catalog.setMinimumWidth(120)
        atlas_page.setMinimumWidth(130)
        self.psd_workspace = QWidget(self.vertical_splitter)
        self.psd_workspace.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.psd_workspace.setMinimumHeight(150)
        lower = EditorViewportLayout(self.psd_workspace)
        lower.setContentsMargins(8, 0, 0, 0)
        lower.setSpacing(8)
        self.task_toolbar = QWidget(self.psd_workspace)
        row = QHBoxLayout(self.task_toolbar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.task_pivot = AppearanceStepPivot(self.task_toolbar)
        self.task_pivot.setFixedHeight(38)
        self.task_pivot.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        for task in ("export", "repack", "history"):
            self.task_pivot.addItem(task, "", onClick=lambda _checked=False, value=task: self.show_psd_task(value))
        self.task_pivot.setItemFontSize(14)
        row.addWidget(self.task_pivot, 1)
        self.settings_button = CompactAppearanceButton(self.task_toolbar)
        self.settings_button.setFixedWidth(100)
        self.settings_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.settings_button.clicked.connect(lambda: self.show_psd_task("advanced"))
        row.addWidget(self.settings_button)
        lower.addWidget(self.task_toolbar)
        self.task_hint = ElidedAppearanceLabel(self)
        self.task_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        lower.addWidget(self.task_hint)
        lower.addWidget(psd_page, 1)
        self.vertical_splitter.addWidget(self.upper_splitter)
        self.vertical_splitter.addWidget(self.psd_workspace)
        self.vertical_splitter.setStretchFactor(0, 0)
        self.vertical_splitter.setStretchFactor(1, 1)
        root.addWidget(self.vertical_splitter, 1)
        self._restore_timer = QTimer(self)
        self._restore_timer.setSingleShot(True)
        self._restore_timer.timeout.connect(self.restore_layout)
        for splitter in (self.upper_splitter, self.vertical_splitter):
            splitter.splitterMoved.connect(self.save_layout)
            splitter.resetRequested.connect(self.reset_layout)
        configure = getattr(psd_panel, "configure_task_embedding", None)
        if callable(configure):
            configure()
        changed = getattr(psd_panel, "contextChanged", None)
        if changed is not None:
            changed.connect(self.set_psd_context)
        self.retranslate_ui()
        self.show_psd_task("export", emit=False)

    @property
    def current_task(self):
        return self._task

    def show_skin_task(self, _checked=False, *, emit=True):
        self.skin_page.setFocus(Qt.OtherFocusReason)
        if emit:
            self.taskChanged.emit("skin")

    def show_psd_task(self, task=None, *, emit=True):
        if isinstance(task, int):
            task = ("export", "repack", "history", "advanced")[max(0, min(3, task))]
        self._task = task or self._task
        if self._task != "advanced":
            self.task_pivot.setCurrentItem(self._task)
        self.settings_button.setCheckable(True)
        self.settings_button.setChecked(self._task == "advanced")
        show = getattr(self.psd_panel, "show_task", None)
        if callable(show):
            show(self._task)
        self._refresh_context()
        if emit:
            self.taskChanged.emit("psd")

    def set_psd_context(self, context):
        self._context = dict(context or {})
        self._refresh_context()

    def _refresh_context(self):
        source = self._context.get("skin_source") or {}
        name = source.get("name", "") if isinstance(source, dict) else ""
        token = self._context.get("token") or ""
        text = " · ".join(str(v) for v in (name, token) if v) or appearance_text("editor.appearance.psd_hint")
        self.task_hint.setText(text)
        self.task_hint.setToolTip(text + ("\n" + str(self._context["psd_path"]) if self._context.get("psd_path") else ""))

    def retranslate_ui(self):
        for task, key in (("export", "psd_generate_step"), ("repack", "psd_repack_step"), ("history", "psd_history_step")):
            text = appearance_text("editor.appearance." + key)
            self.task_pivot.setItemText(task, text)
            self.task_pivot.widget(task).setToolTip(appearance_text("editor.appearance.psd_" + ("export" if task == "export" else task)))
        self.task_pivot._fit_items()
        self.settings_button.setText(appearance_text("editor.appearance.psd_settings"))
        self._refresh_context()

    def save_layout(self, *_args):
        if self._initial_show and not self._restoring:
            inner, outer = self.upper_splitter.sizes(), self.vertical_splitter.sizes()
            if all(inner) and all(outer):
                self._layout_states["wide" if self._wide else "narrow"] = {
                    "extent": outer[0], "ratio": inner[0] / sum(inner)}
                if self._settings:
                    self._settings.set("editor_layouts.live2d_appearance", {
                        "version": 2, "layouts": dict(self._layout_states)})

    def restore_layout(self):
        saved = self._settings.get("editor_layouts.live2d_appearance", {}) if self._settings else {}
        saved = saved if isinstance(saved, dict) else {}
        if saved.get("version") == 2 and isinstance(saved.get("layouts"), dict):
            self._layout_states = dict(saved["layouts"])
        elif saved.get("version") == 1:
            self._layout_states["narrow"] = {"extent": saved.get("upper_height", 220),
                                              "ratio": saved.get("catalog_ratio", .44)}
        self._apply_layout()

    def _apply_layout(self):
        self._wide = self.width() >= 720
        self._restoring = True
        self.vertical_splitter.setOrientation(Qt.Horizontal if self._wide else Qt.Vertical)
        self.upper_splitter.setOrientation(Qt.Vertical if self._wide else Qt.Horizontal)
        self.upper_splitter.setMinimumWidth(200 if self._wide else 0)
        self.psd_workspace.setMinimumWidth(340 if self._wide else 0)
        self.psd_workspace.layout().setContentsMargins(12 if self._wide else 0, 0, 0, 0)
        saved = self._layout_states.get("wide" if self._wide else "narrow", {})
        saved = saved if isinstance(saved, dict) else {}
        try:
            extent = max(150, int(saved.get("extent", 240 if self._wide else 204)))
            ratio = max(.25, min(.65, float(saved.get("ratio", .4 if self._wide else .44))))
        except (ValueError, TypeError):
            extent, ratio = (240, .4) if self._wide else (204, .44)
        available = (self.width() if self._wide else self.height()) - self.vertical_splitter.handleWidth()
        extent = min(extent, max(150, available - (340 if self._wide else 180)))
        self.vertical_splitter.setSizes([extent, max(150, available - extent)])
        inner = (self.height() if self._wide else self.width()) - self.upper_splitter.handleWidth()
        self.upper_splitter.setSizes([round(inner * ratio), round(inner * (1 - ratio))])
        self._restoring = False

    def reset_layout(self):
        self._layout_states.pop("wide" if self._wide else "narrow", None)
        self._apply_layout()
        self.save_layout()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if not self._initial_show:
            self._initial_show = True
            self._restore_timer.start(0)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self.task_pivot.setFixedHeight(38)
        if self._initial_show and (self.width() >= 720) != self._wide:
            # splitterMoved already records user choices. At this point Qt has
            # resized the old orientation, so saving would overwrite them.
            self._apply_layout()


class SharedTaskFeedback(QWidget):
    """One current task, with a compact status and optionally expanded details."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._state, self._expanded = {}, False
        root = EditorViewportLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)
        row = QHBoxLayout()
        self.status_label = CaptionLabel(self)
        self.status_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        row.addWidget(self.status_label, 1)
        self.details_button = TransparentToolButton(FluentIcon.DOWN, self)
        self.details_button.setFixedSize(28, 24)
        self.details_button.clicked.connect(self.toggle_details)
        row.addWidget(self.details_button)
        root.addLayout(row)
        self.progress_bar = ProgressBar(self)
        self.progress_bar.setFixedHeight(4)
        root.addWidget(self.progress_bar)
        self.details = TextEdit(self)
        self.details.setReadOnly(True)
        self.details.setMinimumSize(0, 0)
        self.details.setFixedHeight(125)
        self.details.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        root.addWidget(self.details)
        self.details.hide()
        self.retranslate_ui()
        self.set_state({"state": "idle", "message": appearance_text("editor.appearance.feedback_ready")})

    def toggle_details(self):
        self._expanded = not self._expanded
        self.details.setVisible(self._expanded and bool(self.details.toPlainText()))
        self.details_button.setIcon(FluentIcon.UP if self._expanded else FluentIcon.DOWN)

    def set_state(self, state):
        state = dict(state or {})
        if (self._state.get("busy") and self._state.get("task") != state.get("task")
                and not state.get("busy") and state.get("state") in {"succeeded", "failed", "cancelled"}):
            return
        self._state = state
        message, phase = str(state.get("message") or ""), str(state.get("phase") or "")
        if phase and phase != message:
            message = f"{phase} · {message}" if message else phase
        self.status_label.setText(message)
        self.status_label.setToolTip(message)
        details = state.get("details") or ""
        if isinstance(details, (list, tuple)):
            details = "\n".join(str(item) for item in details)
        self.details.setPlainText(str(details))
        self.details_button.setVisible(bool(details))
        self.details.setVisible(bool(details) and self._expanded)
        self.progress_bar.setVisible(bool(state.get("busy")))
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(max(0, min(100, int(state.get("progress") or 0))))

    def set_message(self, message, *, failed=False):
        if self._state.get("busy") and not failed:
            return
        self.set_state({"task": "editor", "state": "failed" if failed else "idle", "busy": False,
                        "message": str(message), "details": str(message) if failed else ""})

    def retranslate_ui(self):
        self.details_button.setToolTip(appearance_text("editor.appearance.feedback_details"))
        self.details_button.setAccessibleName(self.details_button.toolTip())


class ReadonlyPreviewPrepareThread(QThread):
    """A worker owns a fresh CPU session; only the receiver performs the GL swap."""
    prepared = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self, generation, path, parent=None, *, mod_project=None, mod_model_id=""):
        super().__init__(parent)
        self.generation, self.path = generation, str(path)
        self.mod_project, self.mod_model_id = mod_project, mod_model_id
        self.cancel_event, self.candidate = Event(), None

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        from app.core.live2d_editor_session import prepare_readonly_preview_session
        try:
            if self.cancel_event.is_set():
                return
            from app.core.live2d_editor_mod_preview import mapped_mod_skin_preview
            source = (mapped_mod_skin_preview(self.mod_project, self.mod_model_id)
                      if self.mod_project is not None else nullcontext(self.path))
            with source as path:
                candidate = prepare_readonly_preview_session(path, cancel_event=self.cancel_event)
            if self.cancel_event.is_set():
                candidate.close()
                return
            self.candidate = candidate
            self.prepared.emit(self.generation, candidate)
        except CancelledError:
            pass
        except Exception as exc:
            if not self.cancel_event.is_set():
                self.failed.emit(self.generation, str(exc))


__all__ = ["AppearanceTaskWorkspace", "SharedTaskFeedback", "ReadonlyPreviewPrepareThread",
           "CompactAppearanceButton", "APPEARANCE_TEXT", "appearance_text"]
