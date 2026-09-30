"""Live2D runtime editing with native preview and the original MOD workflow."""
from __future__ import annotations

import copy
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QPoint, QProcess, QSignalBlocker, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFileDialog,
    QFrame, QHBoxLayout, QHeaderView, QLabel,
    QMessageBox, QSizePolicy, QTableWidgetItem, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, CheckBox, ComboBox, DoubleSpinBox,
    FluentIcon, LineEdit, MessageBoxBase, PrimaryPushButton,
    PushButton, RoundMenu, ScrollArea, Slider, SubtitleLabel, TableWidget,
    TransparentToolButton,
)

from app.core.live2d_editor_session import Live2DEditorSession
from app.gui.ArtMeshInspector import ArtMeshInspector
from app.gui.live2d_editor_panels import ProjectBoundModPage
from app.gui.PsdReconstructionPage import PsdReconstructionPage
from app.gui.editor_timeline import AnimationTimelineEditor
from app.gui.editor_workspace import EditorViewportLayout, EditorWorkspace, FluentEditorTabs
from app.i18n import get_i18n, tr


LIVE2D_EDITOR_TEXT = {
    "editor.live2d.title": "Live2D 编辑器",
    "editor.live2d.open": "打开模型",
    "editor.live2d.save": "保存完整副本",
    "editor.live2d.undo": "撤销",
    "editor.live2d.redo": "重做",
    "editor.live2d.empty": "打开 Live2D 模型，或从统一预览进入编辑器",
    "editor.live2d.source_hint": "编辑独立工作副本；运行时模型不含可恢复的 Cubism .cmo3 编辑源",
    "editor.live2d.parameters": "参数",
    "editor.live2d.parts": "ArtMesh 部件",
    "editor.live2d.textures": "贴图",
    "editor.live2d.mod": "MOD 管理",
    "editor.live2d.use_for_mod": "将当前编辑用于 MOD",
    "editor.live2d.mod_hint": "将当前模型、动作与贴图的独立快照导入本工程的 MOD 子工程；原模型保持不变。",
    "editor.live2d.search": "搜索参数名称 / ID",
    "editor.live2d.parameter": "参数 / ID",
    "editor.live2d.value": "数值",
    "editor.live2d.range": "范围",
    "editor.live2d.parameter_hint": "修改数值会立即预览；加入关键帧后保存到动作。",
    "editor.live2d.motion": "动作",
    "editor.live2d.no_motion": "无动作；可新建动作",
    "editor.live2d.new_motion": "新建",
    "editor.live2d.clone_motion": "克隆",
    "editor.live2d.motion_name": "动作名称",
    "editor.live2d.duration": "时长（秒）",
    "editor.live2d.key_pose": "当前参数设关键帧",
    "editor.live2d.track_empty": "选择参数以编辑动作轨道",
    "editor.live2d.replace_texture": "替换贴图",
    "editor.live2d.choose_editor": "选择编辑器",
    "editor.live2d.edit_texture": "编辑工作贴图",
    "editor.live2d.texture_hint": "外部编辑器保存工作贴图后自动更新模型；贴图尺寸须保持不变。",
    "editor.live2d.default_editor": "使用系统默认图像程序",
    "editor.live2d.choose_editor_title": "选择外部图像编辑器",
    "editor.live2d.visible": "所属部件可见",
    "editor.live2d.opacity": "所属部件透明度（预览）",
    "editor.live2d.part_hint": "所属部件 {part} · 影响 {count} 个 ArtMesh；预览状态保存在副本清单中，不修改 MOC。",
    "editor.live2d.part_unavailable": "此 ArtMesh 未关联可设置透明度的 Part；主模型透明度控制不可用。",
    "editor.live2d.local_preview": "选中 ArtMesh 的贴图区域",
    "editor.live2d.save_parent": "选择完整副本的父目录",
    "editor.live2d.saved": "完整副本已保存：{path}",
    "editor.live2d.unsaved_title": "Live2D 编辑尚未保存",
    "editor.live2d.unsaved_message": "动作、参数预览或工作贴图有修改。保存完整副本后继续？",
    "editor.live2d.busy": "MOD 任务仍在处理中，请待任务完成后关闭。",
    "editor.live2d.texture_reloaded": "贴图已重新载入，主模型预览已更新。",
    "editor.live2d.error": "操作未完成：{error}",
    "editor.live2d.modified": "已修改",
    "editor.live2d.clean": "已保存 / 未修改",
    "editor.live2d.help": "编辑说明",
    "editor.live2d.selected_parameter": "选中参数",
    "editor.live2d.mod_preview": "MOD 皮肤：{name}",
    "editor.live2d.psd": "PSD 工作台",
    "editor.live2d.return_model": "返回当前模型",
    "editor.live2d.psd_snapshot": "PSD 快照 / 回写预览：{name}",
    "editor.live2d.project_busy": "PSD 或 MOD 任务仍在处理中，请待任务完成后再保存、切换或关闭。",
    "editor.live2d.psd_applied": "回写版本贴图已应用到当前模型；可撤销，动作保持不变。",
    "editor.live2d.project_open": "打开模型 / 工程",
    "editor.live2d.project_save": "保存工程副本",
    "editor.live2d.project_unsaved": "当前模型、PSD 或 MOD 工程有修改。保存完整工程副本后继续？",
    "editor.live2d.psd_missing_version": "先选择一个有效的回写或复合版本。",
    "editor.live2d.return_failed": "当前编辑模型预览恢复失败，操作已停止。",
    "editor.live2d.open_short": "打开",
    "editor.live2d.save_short": "保存",
}


def _text(key: str, **values) -> str:
    return tr(key, default=LIVE2D_EDITOR_TEXT[key], **values)


class _MotionDialog(MessageBoxBase):
    def __init__(self, title: str, parent, duration: bool = True):
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel(title, self.widget))
        self.viewLayout.addWidget(BodyLabel(_text("editor.live2d.motion_name"), self.widget))
        self.name_edit = LineEdit(self.widget)
        self.viewLayout.addWidget(self.name_edit)
        self.duration_spin = DoubleSpinBox(self.widget)
        self.duration_spin.setRange(.01, 3600)
        self.duration_spin.setDecimals(3)
        self.duration_spin.setValue(3)
        if duration:
            self.viewLayout.addWidget(BodyLabel(_text("editor.live2d.duration"), self.widget))
            self.viewLayout.addWidget(self.duration_spin)
        else:
            self.duration_spin.hide()
        self.widget.setMinimumWidth(320)
        self.yesButton.setText(tr("common.confirm", default="确定"))
        self.cancelButton.setText(tr("common.cancel", default="取消"))

    def validate(self):
        return bool(self.name_edit.text().strip())


class _ParameterValue:
    """Small compatibility facade; only the selected parameter owns a widget."""
    def __init__(self, owner, parameter):
        self.owner = owner
        self.parameter = parameter
        self._value = float(parameter["default"])

    def value(self):
        return self._value

    def minimum(self):
        return float(self.parameter["min"])

    def maximum(self):
        return float(self.parameter["max"])

    def setValue(self, value):  # noqa: N802
        value = max(self.minimum(), min(self.maximum(), float(value)))
        if value != self._value:
            self._value = value
            self.owner._parameter_value_changed(str(self.parameter["id"]), value)


class Live2DEditorPage(QFrame):
    sourceOpened = Signal(str)
    sourceFailed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("live2dEditorPage")
        self.i18n = get_i18n()
        self.session: Live2DEditorSession | None = None
        self.preview = None
        self._preview_session = None
        self._preview_context = ""
        self._binding_projects = False
        self.last_open_error = ""
        self._refreshing = False
        self._active = False
        self._manual_pose = False
        self._selected_parameter = ""
        self._parameter_spins: dict[str, _ParameterValue] = {}
        self._mod_preview_model_id = ""
        self._external_editor = ""
        self.last_saved_copy: dict | None = None
        self._pending_textures: set[int] = set()
        self._texture_attempts: dict[int, int] = {}
        self.texture_watcher = QFileSystemWatcher(self)
        self.texture_watcher.fileChanged.connect(self._queue_texture_reload)
        self.texture_watcher.directoryChanged.connect(self._queue_texture_reload)
        self._texture_timer = QTimer(self)
        self._texture_timer.setSingleShot(True)
        self._texture_timer.setInterval(350)
        self._texture_timer.timeout.connect(self._process_texture_changes)
        self._mesh_timer = QTimer(self)
        self._mesh_timer.setSingleShot(True)
        self._mesh_timer.setInterval(200)
        self._mesh_timer.timeout.connect(self._refresh_mesh)
        self._build_ui()
        self.retranslate_ui()
        self.i18n.languageChanged.connect(self.retranslate_ui)
        self._update_actions()

    def _build_ui(self):
        root = EditorViewportLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(7)
        header = QHBoxLayout()
        self.title_label = SubtitleLabel(self)
        self.title_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.title_label.setMinimumWidth(100)
        self.open_button = PushButton(FluentIcon.FOLDER, "", self)
        self.undo_button = TransparentToolButton(FluentIcon.CANCEL, self)
        self.redo_button = TransparentToolButton(FluentIcon.SYNC, self)
        self.save_button = PrimaryPushButton(FluentIcon.SAVE, "", self)
        self.help_button = TransparentToolButton(FluentIcon.INFO, self)
        self.help_button.clicked.connect(self._show_help)
        self.open_button.clicked.connect(self._choose_source)
        self.undo_button.clicked.connect(self.undo)
        self.redo_button.clicked.connect(self.redo)
        self.save_button.clicked.connect(self._choose_save_copy)
        header.addWidget(self.title_label, 1)
        self.source_label = CaptionLabel(self)
        self.source_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.source_label.setMinimumWidth(100)
        self.source_label.setMaximumWidth(160)
        self.source_label.hide()
        header.addWidget(self.source_label)
        for button in (self.open_button, self.undo_button, self.redo_button, self.save_button, self.help_button):
            header.addWidget(button)
        root.addLayout(header)
        self.source_hint = CaptionLabel(self)
        self.source_hint.hide()
        self.stage = QWidget(self)
        self.stage.setMinimumSize(220, 120)
        self.stage_layout = QVBoxLayout(self.stage)
        self.stage_layout.setContentsMargins(0, 0, 0, 0)
        self.stage_layout.setSpacing(0)
        self.preview_toolbar = QWidget(self.stage)
        self.preview_toolbar.setFixedHeight(32)
        preview_toolbar_layout = QHBoxLayout(self.preview_toolbar)
        preview_toolbar_layout.setContentsMargins(6, 0, 6, 0)
        self.preview_context_label = CaptionLabel(self.preview_toolbar)
        self.preview_context_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.return_model_button = PushButton(self.preview_toolbar)
        self.return_model_button.clicked.connect(self.return_to_current_model)
        preview_toolbar_layout.addWidget(self.preview_context_label, 1)
        preview_toolbar_layout.addWidget(self.return_model_button)
        self.preview_toolbar.hide()
        self.stage_layout.addWidget(self.preview_toolbar)
        self.empty_label = BodyLabel(self.stage)
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.stage_layout.addWidget(self.empty_label)
        self.tabs = FluentEditorTabs(self)
        self.tabs.setMinimumWidth(280)
        self.tabs.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.tabs.currentChanged.connect(self._tab_changed)
        self._build_parameter_tab()
        self._build_artmesh_tab()
        self._build_texture_tab()
        self.psd_tab = QWidget(self.tabs)
        psd_layout = EditorViewportLayout(self.psd_tab)
        psd_layout.setContentsMargins(4, 4, 4, 4)
        self.psd_panel = PsdReconstructionPage(self.psd_tab, compact=True)
        self.psd_panel.managed_project_root_provider = lambda: self.session.root / "psd" if self.session else None
        self.psd_panel.projectRequested.connect(lambda path: self.open_psd_workspace(project_file=path))
        self.psd_panel.newProjectRequested.connect(self._new_psd_project)
        self.psd_panel.sourceRequested.connect(lambda path: self.open_psd_workspace(model_path=path))
        self.psd_panel.capturePoseRequested.connect(self._capture_psd_pose)
        self.psd_panel.repackApplyRequested.connect(self.apply_psd_version)
        self.psd_panel.repackModRequested.connect(self.send_psd_version_to_mod)
        self.psd_panel.projectChanged.connect(self._psd_project_changed)
        self.psd_panel.unifiedPreviewRequested.connect(self._open_psd_preview)
        self.psd_panel.previewClosed.connect(self.return_to_current_model)
        self.psd_panel.export_snapshot_provider = self._psd_export_snapshot
        self.psd_panel.preview_command_handler = self._psd_preview_command
        psd_layout.addWidget(self.psd_panel)
        self.tabs.addTab(self.psd_tab, "")
        self.mod_tab = QWidget(self.tabs)
        mod_layout = QVBoxLayout(self.mod_tab)
        mod_layout.setContentsMargins(4, 4, 4, 4)
        self.use_for_mod_button = PrimaryPushButton(self.mod_tab)
        self.use_for_mod_button.clicked.connect(self._use_edited_for_mod)
        mod_layout.addWidget(self.use_for_mod_button)
        self.mod_hint = CaptionLabel(self.mod_tab)
        self.mod_hint.hide()
        self.mod_panel = ProjectBoundModPage(lambda: self.session, self.mod_tab)
        self.mod_panel.previewModelRequested.connect(self._open_mod_preview)
        self.mod_panel.triggerSelectionChanged.connect(self._mod_trigger_selected)
        self.mod_panel.projectChanged.connect(self._mod_project_changed)
        mod_layout.addWidget(self.mod_panel, 1)
        self.tabs.addTab(self.mod_tab, "")
        timeline_host = QWidget(self)
        timeline_layout = EditorViewportLayout(timeline_host)
        timeline_layout.setContentsMargins(0, 0, 0, 0)
        timeline_layout.setSpacing(4)
        row = QHBoxLayout()
        self.motion_label = CaptionLabel(timeline_host)
        self.motion_label.hide()
        self.motion_combo = ComboBox(timeline_host)
        self.motion_combo.setMinimumWidth(80)
        self.motion_combo.setMaximumWidth(125)
        self.motion_combo.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.motion_combo.setFixedWidth(110)
        self.motion_combo.currentIndexChanged.connect(self._motion_changed)
        self.new_motion_button = TransparentToolButton(FluentIcon.ADD, timeline_host)
        self.clone_motion_button = TransparentToolButton(FluentIcon.COPY, timeline_host)
        self.key_pose_button = TransparentToolButton(FluentIcon.ACCEPT, timeline_host)
        for button in (self.new_motion_button, self.clone_motion_button, self.key_pose_button):
            button.setFixedSize(28, 28)
        self.new_motion_button.clicked.connect(self._create_motion)
        self.clone_motion_button.clicked.connect(self._clone_motion)
        self.key_pose_button.clicked.connect(self._key_current_pose)
        row.addWidget(self.motion_combo)
        self.track_label = CaptionLabel(timeline_host)
        self.track_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row.addWidget(self.track_label, 1)
        self.motion_options_button = TransparentToolButton(FluentIcon.MORE, timeline_host)
        self.motion_options_button.setFixedSize(26, 28)
        row.addWidget(self.motion_options_button)
        row.addSpacing(28)
        timeline_layout.addLayout(row)
        self.motion_options_menu = RoundMenu(parent=self)
        options = QWidget(self.motion_options_menu)
        options.setFixedSize(220, 42)
        commands = QHBoxLayout(options)
        commands.setContentsMargins(6, 4, 6, 4)
        for button in (self.new_motion_button, self.clone_motion_button, self.key_pose_button):
            commands.addWidget(button)
        commands.addStretch(1)
        self.motion_options_menu.addWidget(options, selectable=False)
        self.motion_options_button.clicked.connect(lambda: self.motion_options_menu.exec(self.motion_options_button.mapToGlobal(QPoint(0, self.motion_options_button.height()))))
        self.timeline = AnimationTimelineEditor(timeline_host)
        self.timeline.canvas.setMinimumHeight(80)
        self.timeline.timeChanged.connect(self._seek)
        self.timeline.framesEdited.connect(self._frames_edited)
        self.timeline.playStateChanged.connect(self._play_state_changed)
        timeline_layout.addWidget(self.timeline, 1)
        self.workspace = EditorWorkspace(self.stage, self.tabs, timeline_host, self,
                                         settings_key="live2d", settings=self.mod_panel.settings_manager)
        self.workspace.move_toolbar_to(header)
        self.main_splitter = self.workspace.horizontal_splitter
        self.vertical_splitter = self.workspace.vertical_splitter
        self.workspace.panelVisibilityChanged.connect(self._panel_visibility_changed)
        root.addWidget(self.workspace, 1)
        self.status_label = CaptionLabel(self)
        self.status_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        root.addWidget(self.status_label)
        for sequence, action in ((QKeySequence.Undo, self.undo), (QKeySequence.Redo, self.redo), (QKeySequence.Save, self._choose_save_copy)):
            shortcut = QShortcut(sequence, self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(action)
        self._apply_control_style()

    def _apply_control_style(self):
        # Interactive controls use their Fluent styles. Only image surfaces
        # need a page-specific background.
        self.setStyleSheet("""
            QLabel#editorImage { background: rgba(127,127,127,0.06);
                border-radius: 8px; padding: 6px; }
        """)

    def _show_help(self):
        dialog = MessageBoxBase(self.window())
        dialog.viewLayout.addWidget(SubtitleLabel(_text("editor.live2d.help"), dialog.widget))
        label = BodyLabel(_text("editor.live2d.source_hint"), dialog.widget)
        label.setWordWrap(True)
        dialog.viewLayout.addWidget(label)
        dialog.widget.setMinimumWidth(360)
        dialog.cancelButton.hide()
        dialog.yesButton.setText(tr("common.close", default="关闭"))
        dialog.exec()

    def _panel_visibility_changed(self, panel: str, visible: bool):
        if panel == "timeline" and not visible:
            self.timeline.set_playing(False)
        if panel == "preview" and self.preview:
            self.preview.set_rendering_active(self._active and visible)

    def _build_parameter_tab(self):
        self.parameter_tab = QWidget(self.tabs)
        layout = QVBoxLayout(self.parameter_tab)
        layout.setContentsMargins(8, 8, 8, 8)
        self.parameter_search = LineEdit(self.parameter_tab)
        self.parameter_search.textChanged.connect(self._filter_parameters)
        layout.addWidget(self.parameter_search)
        self.parameter_table = TableWidget(self.parameter_tab)
        self.parameter_table.setColumnCount(3)
        self.parameter_table.setBorderVisible(False)
        self.parameter_table.setAlternatingRowColors(True)
        self.parameter_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.parameter_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.parameter_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.parameter_table.verticalHeader().hide()
        self.parameter_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.parameter_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.parameter_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.parameter_table.setColumnWidth(1, 88)
        self.parameter_table.currentCellChanged.connect(self._parameter_selected)
        layout.addWidget(self.parameter_table, 1)
        self.parameter_editor = CardWidget(self.parameter_tab)
        edit_layout = QVBoxLayout(self.parameter_editor)
        edit_layout.setContentsMargins(12, 10, 12, 10)
        edit_layout.setSpacing(7)
        self.parameter_name = BodyLabel(self.parameter_editor)
        self.parameter_name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        edit_layout.addWidget(self.parameter_name)
        controls = QHBoxLayout()
        self.parameter_slider = Slider(Qt.Horizontal, self.parameter_editor)
        self.parameter_slider.setRange(0, 1000)
        self.parameter_slider.valueChanged.connect(self._parameter_slider_changed)
        controls.addWidget(self.parameter_slider, 1)
        self.parameter_spin = DoubleSpinBox(self.parameter_editor)
        self.parameter_spin.setDecimals(4)
        self.parameter_spin.setMinimumWidth(148)
        self.parameter_spin.setMaximumWidth(168)
        self.parameter_spin.valueChanged.connect(self._selected_parameter_value_changed)
        controls.addWidget(self.parameter_spin)
        edit_layout.addLayout(controls)
        layout.addWidget(self.parameter_editor)
        self.parameter_hint = CaptionLabel(self.parameter_tab)
        self.parameter_hint.setWordWrap(True)
        self.parameter_hint.setMaximumHeight(38)
        layout.addWidget(self.parameter_hint)
        self.tabs.addTab(self.parameter_tab, "")

    def _build_artmesh_tab(self):
        self.artmesh_tab = QWidget(self.tabs)
        layout = QVBoxLayout(self.artmesh_tab)
        layout.setContentsMargins(4, 4, 4, 4)
        self.artmesh_inspector = ArtMeshInspector(parent=self.artmesh_tab)
        inspector = self.artmesh_inspector
        for widget in (inspector.metadata_edit, inspector.open_button, inspector.mode_label, inspector.shared_label):
            widget.hide()
        inspector.pose_canvas.parentWidget().hide()
        inspector.splitter.setOrientation(Qt.Vertical)
        inspector.entry_list.setMinimumHeight(75)
        inspector.entry_list.setMaximumHeight(135)
        inspector.atlas_canvas.setMinimumSize(220, 135)
        inspector.details.setMaximumHeight(64)
        inspector.main_layout.setContentsMargins(4, 4, 4, 4)
        inspector.details.hide()
        inspector.status_label.hide()
        inspector.details.textChanged.connect(lambda: inspector.setToolTip(inspector.details.toPlainText()))
        inspector.refresh_button.clicked.disconnect()
        inspector.refresh_button.clicked.connect(self._refresh_mesh)
        inspector.selectionChanged.connect(self._artmesh_selected)
        layout.addWidget(inspector, 1)
        self.local_preview_label = CaptionLabel(self.artmesh_tab)
        layout.addWidget(self.local_preview_label)
        self.artmesh_image = QLabel(self.artmesh_tab)
        self.artmesh_image.setObjectName("editorImage")
        self.artmesh_image.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.artmesh_image.setAlignment(Qt.AlignCenter)
        self.artmesh_image.setMinimumHeight(60)
        self.artmesh_image.setMaximumHeight(90)
        layout.addWidget(self.artmesh_image)
        opacity_row = QHBoxLayout()
        self.part_visible = CheckBox(self.artmesh_tab)
        self.part_opacity = DoubleSpinBox(self.artmesh_tab)
        self.part_opacity.setMaximumWidth(120)
        self.part_opacity.setRange(0, 1)
        self.part_opacity.setSingleStep(.05)
        self.part_opacity.setDecimals(2)
        self.part_opacity.setValue(1)
        self.part_visible.setChecked(True)
        self.part_visible.toggled.connect(self._part_visibility_changed)
        self.part_opacity.valueChanged.connect(self._part_opacity_changed)
        opacity_row.addWidget(self.part_visible, 1)
        opacity_row.addWidget(self.part_opacity)
        layout.addLayout(opacity_row)
        self.part_hint = CaptionLabel(self.artmesh_tab)
        self.part_hint.setWordWrap(True)
        layout.addWidget(self.part_hint)
        # The inspector retains a real atlas and local preview. Its content can
        # scroll independently so small windows do not inherit its full height.
        content = QWidget(self.artmesh_tab)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                content_layout.addWidget(item.widget())
            elif item.layout():
                content_layout.addLayout(item.layout())
            else:
                content_layout.addItem(item)
        self.artmesh_scroll = ScrollArea(self.artmesh_tab)
        self.artmesh_scroll.setWidgetResizable(True)
        self.artmesh_scroll.setFrameShape(QFrame.NoFrame)
        self.artmesh_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.artmesh_scroll.setWidget(content)
        self.artmesh_scroll.enableTransparentBackground()
        layout.addWidget(self.artmesh_scroll)
        self.tabs.addTab(self.artmesh_tab, "")

    def _build_texture_tab(self):
        self.texture_tab = QWidget(self.tabs)
        layout = QVBoxLayout(self.texture_tab)
        layout.setContentsMargins(8, 8, 8, 8)
        self.texture_combo = ComboBox(self.texture_tab)
        self.texture_combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.texture_combo.currentIndexChanged.connect(self._refresh_texture_image)
        layout.addWidget(self.texture_combo)
        self.texture_image = QLabel(self.texture_tab)
        self.texture_image.setObjectName("editorImage")
        self.texture_image.setAlignment(Qt.AlignCenter)
        self.texture_image.setMinimumSize(190, 150)
        self.texture_image.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        layout.addWidget(self.texture_image, 1)
        actions = QHBoxLayout()
        self.replace_texture_button = PushButton(self.texture_tab)
        self.choose_editor_button = PushButton(self.texture_tab)
        self.replace_texture_button.clicked.connect(self._choose_texture_replacement)
        self.choose_editor_button.clicked.connect(self._choose_external_editor)
        actions.addWidget(self.replace_texture_button)
        actions.addWidget(self.choose_editor_button)
        layout.addLayout(actions)
        self.editor_label = CaptionLabel(self.texture_tab)
        self.editor_label.setWordWrap(True)
        layout.addWidget(self.editor_label)
        self.edit_texture_button = PrimaryPushButton(self.texture_tab)
        self.edit_texture_button.clicked.connect(self._edit_external_texture)
        layout.addWidget(self.edit_texture_button)
        self.texture_hint = CaptionLabel(self.texture_tab)
        self.texture_hint.setWordWrap(True)
        layout.addWidget(self.texture_hint)
        content = QWidget(self.texture_tab)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                content_layout.addWidget(item.widget())
            elif item.layout():
                content_layout.addLayout(item.layout())
            else:
                content_layout.addItem(item)
        self.texture_scroll = ScrollArea(self.texture_tab)
        self.texture_scroll.setWidgetResizable(True)
        self.texture_scroll.setFrameShape(QFrame.NoFrame)
        self.texture_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.texture_scroll.setWidget(content)
        self.texture_scroll.enableTransparentBackground()
        layout.addWidget(self.texture_scroll)
        self.tabs.addTab(self.texture_tab, "")

    def retranslate_ui(self, *_args):
        labels = {
            self.title_label: "title", self.open_button: "open", self.save_button: "save",
            self.empty_label: "empty",
            self.source_hint: "source_hint", self.parameter_hint: "parameter_hint",
            self.motion_label: "motion",
            self.replace_texture_button: "replace_texture", self.choose_editor_button: "choose_editor",
            self.edit_texture_button: "edit_texture", self.texture_hint: "texture_hint",
            self.part_visible: "visible", self.local_preview_label: "local_preview",
            self.use_for_mod_button: "use_for_mod", self.mod_hint: "mod_hint",
        }
        for widget, key in labels.items():
            widget.setText(_text("editor.live2d." + key))
        for button, key in ((self.undo_button, "undo"), (self.redo_button, "redo"), (self.help_button, "help"),
                            (self.new_motion_button, "new_motion"), (self.clone_motion_button, "clone_motion"),
                            (self.key_pose_button, "key_pose"), (self.motion_options_button, "motion")):
            button.setToolTip(_text("editor.live2d." + key))
            button.setAccessibleName(button.toolTip())
        self.use_for_mod_button.setToolTip(_text("editor.live2d.mod_hint"))
        self.parameter_name.setText(_text("editor.live2d.selected_parameter"))
        for index, key in enumerate(("parameters", "parts", "textures", "psd", "mod")):
            self.tabs.setTabText(index, _text("editor.live2d." + key))
        self.return_model_button.setText(_text("editor.live2d.return_model"))
        self.open_button.setText(_text("editor.live2d.open_short"))
        self.save_button.setText(_text("editor.live2d.save_short"))
        self.open_button.setToolTip(_text("editor.live2d.project_open"))
        self.save_button.setToolTip(_text("editor.live2d.project_save"))
        self.parameter_search.setPlaceholderText(_text("editor.live2d.search"))
        self.parameter_table.setHorizontalHeaderLabels([_text("editor.live2d." + key) for key in ("parameter", "value", "range")])
        self.editor_label.setText(self._external_editor or _text("editor.live2d.default_editor"))
        self.part_opacity.setToolTip(_text("editor.live2d.opacity"))
        self.timeline.retranslate_ui()
        self.workspace.retranslate_ui()
        self.mod_panel.retranslate_ui()
        self.psd_panel.retranslate_ui()
        self._update_actions()

    def _choose_source(self):
        path, _ = QFileDialog.getOpenFileName(self, _text("editor.live2d.project_open"), "", "Live2D (*.json *.moc3 *.lpk)")
        if path:
            self.open_source(path)

    def open_source(self, path: str) -> bool:
        """Prepare and validate a replacement before releasing the active session."""
        candidate = None
        self.last_open_error = ""
        previous = self.session
        previous_psd = copy.deepcopy(self.psd_panel.current_project)
        if previous_psd:
            previous_psd.data["ui_state"] = self.psd_panel._current_ui_state()
        previous_mod = copy.deepcopy(self.mod_panel.current_project)
        previous_psd_dirty = self.psd_panel._project_dirty
        previous_mod_dirty = self.mod_panel._dirty
        created_preview = self.preview is None
        try:
            if self._projects_busy():
                self.last_open_error = _text("editor.live2d.project_busy")
                self._status(self.last_open_error)
                self.sourceFailed.emit(path)
                return False
            candidate = Live2DEditorSession(path)
            if not self._confirm_session_changes():
                candidate.close()
                return False
            self.timeline.set_playing(False)
            if not self.return_to_current_model():
                raise RuntimeError(_text("editor.live2d.return_failed"))
            self.session = candidate
            if self.preview is None:
                from app.gui.Live2DPreviewWindow import Live2DPreviewWindow
                self.preview = Live2DPreviewWindow(str(candidate.model_path), self.stage, embedded=True)
                self.preview.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
                if self.preview.live2d_container:
                    self.preview.live2d_container.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
                self.preview.set_editor_mode(True)
                self.preview.live2d_canvas.modelLoaded.connect(self._native_model_ready)
                self.preview.live2d_canvas.drawableClicked.connect(self._native_drawable_clicked)
                self.preview.live2d_canvas.modelPointClicked.connect(self._native_model_point_clicked)
                self.stage_layout.addWidget(self.preview, 1)
            else:
                try:
                    self.preview.load_model(str(candidate.model_path))
                except Exception:
                    self.session = previous
                    if previous:
                        self.preview.load_model(str(previous.model_path))
                    raise
            self.empty_label.hide()
            self._manual_pose = bool(candidate.parameter_overrides)
            self._selected_parameter = candidate.parameters[0]["id"] if candidate.parameters else ""
            self._populate_motions()
            self._populate_parameters()
            self._populate_textures()
            self._refresh_mesh()
            self._mod_preview_model_id = ""
            self._bind_subprojects()
            self.source_label.setText(candidate.source_path.name)
            self.source_label.setToolTip(str(candidate.source_path))
            self._native_model_ready()
            self.set_active(self.isVisible())
            self._update_actions()
            self._status("; ".join(candidate.warnings))
            if previous:
                previous.close()
            self.sourceOpened.emit(path)
            return True
        except Exception as exc:
            if self.session is candidate:
                self.session = previous
            if previous and self.preview:
                try:
                    self.preview.load_model(str(previous.model_path))
                    self._binding_projects = True
                    try:
                        self.psd_panel.bind_project(previous_psd)
                        self.mod_panel.bind_project(previous_mod)
                        self.psd_panel._project_dirty = previous_psd_dirty
                        self.mod_panel._dirty = previous_mod_dirty
                    finally:
                        self._binding_projects = False
                    self._native_model_ready()
                    self._populate_motions()
                    self._populate_parameters()
                    self._populate_textures()
                    self._refresh_mesh()
                except Exception as rollback_error:
                    self._status(str(rollback_error))
            if candidate:
                candidate.close()
            if created_preview and self.preview:
                self.preview.close()
                self.preview.deleteLater()
                self.preview = None
                self.empty_label.show()
            self._error(exc)
            self.last_open_error = str(exc)
            self.sourceFailed.emit(path)
            return False

    def _projects_busy(self) -> bool:
        return bool((self.mod_panel.worker and self.mod_panel.worker.isRunning()) or self.psd_panel.is_busy())

    def _bind_subprojects(self):
        self._binding_projects = True
        try:
            self.psd_panel.bind_project(getattr(self.session, "psd_project", None))
            self.mod_panel.bind_project(getattr(self.session, "mod_project", None))
            if self.session:
                self.mod_panel.set_source(str(self.session.model_path))
        finally:
            self._binding_projects = False

    def _psd_project_changed(self, project):
        if self.session and not self._binding_projects:
            self.session.bind_psd_project(project)
            if self.psd_panel._project_dirty:
                self.session.mark_project_changed()
            self._update_actions()

    def _mod_project_changed(self, project):
        if self.session and not self._binding_projects:
            if project is not None:
                self.session.bind_mod_project(project)
            if self.mod_panel._dirty:
                self.session.mark_project_changed()
            self._update_actions()

    def _flush_subprojects(self):
        if not self.session:
            return
        if self.psd_panel.current_project:
            self.psd_panel.save_current_project()
        if self.mod_panel.current_project and not self.mod_panel.flush_pending_changes():
            raise RuntimeError(_text("editor.live2d.project_unsaved"))
        self.session.flush_projects(psd_project=self.psd_panel.current_project,
                                    mod_project=self.mod_panel.current_project)

    def open_psd_workspace(self, model_path=None, project_file=None) -> bool:
        try:
            if self._projects_busy():
                self._status(_text("editor.live2d.project_busy"))
                return False
            if project_file and not self.session:
                from app.core.psd_project import load_project
                if not self.open_source(str(load_project(project_file).base_model_json)):
                    return False
            elif model_path:
                source = Path(model_path).resolve()
                active_paths = {self.session.model_path.resolve(), self.session.source_path.resolve()} if self.session else set()
                if source not in active_paths and not self.open_source(str(source)):
                    return False
            elif not self.session:
                path, _ = QFileDialog.getOpenFileName(self, _text("editor.live2d.project_open"), "", "Live2D (*.json *.moc3 *.lpk)")
                if not path or not self.open_source(path):
                    return False
            same_project = (self.psd_panel.current_project and project_file
                            and Path(project_file).resolve() == self.psd_panel.current_project.project_file.resolve())
            if project_file and self.psd_panel.current_project and not same_project:
                # Keep edits and UI choices in the owned project before changing
                # the active child; importing a recent file never writes its source.
                self.psd_panel.save_current_project()
            if not same_project and (project_file or not self.psd_panel.current_project):
                project = (self.session.attach_psd_project(project_file) if project_file
                           else self.session.ensure_psd_project())
                self._binding_projects = True
                try:
                    self.psd_panel.bind_project(project)
                finally:
                    self._binding_projects = False
            self.tabs.setCurrentWidget(self.psd_tab)
            self.workspace.set_panel_visible("details", True)
            self._update_actions()
            return True
        except Exception as exc:
            self._error(exc)
            return False

    def _new_psd_project(self):
        if not self.session:
            return self.open_psd_workspace()
        if not self.psd_panel.current_project:
            return self.open_psd_workspace()
        if not self._confirm_session_changes():
            return False
        name = self.psd_panel._prompt_non_empty_name("psd.project.new_dialog_title", "psd.project.new_dialog_prompt", self.session.source_path.stem)
        if not name:
            return False
        try:
            from app.core.psd_project import create_project_from_source
            snapshot = self._psd_export_snapshot()
            project = create_project_from_source(snapshot, project_name=name, output_root=self.session.root / "psd-imports")
            return self.open_psd_workspace(project_file=str(project.project_file))
        except Exception as exc:
            self._error(exc)
            return False

    def _psd_export_snapshot(self):
        if not self.session:
            raise RuntimeError(_text("editor.live2d.empty"))
        project = self.session.ensure_psd_project()
        target = project.project_dir / "snapshots" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        result = self.session.export_snapshot(target)
        return str(result["model_path"] if isinstance(result, dict) else result)

    def _capture_psd_pose(self):
        if not self.session or not self.open_psd_workspace():
            return
        name = self.psd_panel._prompt_non_empty_name("psd.project.new_dialog_title", "psd.export.name", "")
        if name:
            self.psd_panel.create_pose_scheme_from_preview({"project_file": str(self.psd_panel.current_project.project_file),
                                                           "name": name, "parameters": self._pose()})

    def _psd_version_textures(self, token: str) -> dict[int, Path]:
        from app.core.psd_project import find_composite_entry, find_repack_entry, resolve_project_path
        project = self.psd_panel.current_project
        if not project or not token.startswith(("repack:", "composite:")):
            raise ValueError(_text("editor.live2d.psd_missing_version"))
        kind, version_id = token.split(":", 1)
        entry = find_repack_entry(project, version_id) if kind == "repack" else find_composite_entry(project, version_id)
        if not entry:
            raise ValueError(_text("editor.live2d.psd_missing_version"))
        values = entry.get("texture_outputs") or {str(index): value for index, value in enumerate(entry.get("output_paths") or [])}
        return {int(index): resolve_project_path(project, path) for index, path in values.items()}

    def _psd_version_workspace(self, token: str):
        from app.core.psd_project import prepare_repack_preview_workspace
        # Validate the selected history entry before preparing its model.
        self._psd_version_textures(token)
        return prepare_repack_preview_workspace(self.psd_panel.current_project, token.split(":", 1)[1])

    def apply_psd_version(self, token: str) -> bool:
        try:
            if not self.session or self._projects_busy():
                return False
            if not self.return_to_current_model():
                raise RuntimeError(_text("editor.live2d.return_failed"))
            self.session.apply_texture_package(self._psd_version_workspace(token))
            self._reload_preview_textures()
            self._status(_text("editor.live2d.psd_applied"))
            self._update_actions()
            return True
        except Exception as exc:
            self._error(exc)
            return False

    def send_psd_version_to_mod(self, token: str) -> bool:
        try:
            if not self.session or self._projects_busy():
                return False
            import shutil
            from app.core.model import resolve_live2d_package
            version_model = self._psd_version_workspace(token)
            info = self.session.validate_texture_package(version_model)
            target = self.session.root / "mod-inputs" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            result = self.session.export_snapshot(target)
            model_path = str(result["model_path"] if isinstance(result, dict) else result)
            package = resolve_live2d_package(model_path)
            for index, texture in enumerate(info.textures):
                if not 0 <= index < len(package.texture_paths):
                    raise ValueError(f"Texture index out of range: {index}")
                shutil.copy2(texture, package.texture_paths[index])
            if not self.mod_panel.current_project:
                self.mod_panel.bind_project(self.session.ensure_mod_project())
            self.mod_panel.queue_source_imports([model_path])
            self.tabs.setCurrentWidget(self.mod_tab)
            return True
        except Exception as exc:
            self._error(exc)
            return False

    def _open_psd_preview(self, path: str, project_file: str):
        self._open_workspace_preview(path, _text("editor.live2d.psd_snapshot", name=Path(path).name))

    def _open_workspace_preview(self, path: str, label: str) -> bool:
        if not self.preview or not self.session:
            return False
        candidate = None
        previous = self._preview_session
        try:
            candidate = Live2DEditorSession(path)
            self.timeline.set_playing(False)
            self._preview_session = candidate
            self.preview.load_model(str(candidate.model_path))
            self._preview_context = label
            self.preview_context_label.setText(label)
            self.preview_context_label.setToolTip(str(path))
            self.preview_toolbar.show()
            self._native_model_ready()
            self.workspace.set_panel_visible("preview", True)
            if previous:
                previous.close()
            return True
        except Exception as exc:
            self._preview_session = previous
            if candidate:
                candidate.close()
            self.preview.load_model(str(previous.model_path if previous else self.session.model_path))
            self._error(exc)
            return False

    def return_to_current_model(self) -> bool:
        previous = self._preview_session
        if not previous:
            return True
        try:
            self._preview_session = None
            self._preview_context = ""
            if self.preview and self.session:
                self.preview.load_model(str(self.session.model_path))
                self._native_model_ready()
                self._refresh_mesh()
            self._mod_preview_model_id = ""
            self.preview_toolbar.hide()
            previous.close()
            return True
        except Exception as exc:
            self._preview_session = previous
            self._error(exc)
            return False

    def _psd_preview_command(self, payload: dict) -> bool:
        if not self.preview or not self._preview_session:
            return False
        if payload.get("type") == "play_motion":
            self.preview.set_motion_frozen(False)
            self.preview.play_motion(str(payload.get("group") or ""), int(payload.get("index") or 0))
            return True
        if payload.get("type") == "set_selected_motion":
            self.preview.set_selected_motion(str(payload.get("group") or ""), int(payload.get("index") or 0))
            return True
        return False

    def _open_mod_preview(self, path: str):
        model_id = self.mod_panel.requested_preview_model_id
        model = self.mod_panel.model_by_id(model_id)
        label = _text("editor.live2d.mod_preview", name=(model or {}).get("skin_name") or model_id)
        if self._open_workspace_preview(path, label):
            self._mod_preview_model_id = model_id
            self.mod_panel.note_preview_opened(model_id)

    def _mod_trigger_selected(self, drawable_id: str):
        if self.session and drawable_id:
            self.artmesh_inspector.select_entry(drawable_id)

    def _native_model_ready(self):
        if not self.preview or not self.session:
            return
        self.preview.set_editor_mode(True)
        self.preview.apply_settings({"motion_frozen": True, "mouse_tracking": False,
                                     "auto_blink": False, "auto_breath": False,
                                     "transparent_bg": False, "antialias": True})
        self._apply_preview_pose()
        self.preview.set_rendering_active(self._active)

    def _current_motion(self) -> str | None:
        return self.motion_combo.currentData()

    def _pose(self) -> dict[str, float]:
        if not self.session:
            return {}
        return self.session.pose_at(self._current_motion(), self.timeline.current_time, overrides=self._manual_pose)

    def _apply_preview_pose(self):
        if not self.session or not self.preview:
            return
        values = self._pose()
        render_session = self._preview_session or self.session
        supported = {parameter["id"] for parameter in render_session.parameters}
        values = {key: value for key, value in values.items() if key in supported}
        self.preview.apply_settings({"advanced_enabled": True, "advanced_params": values})
        defaults = {str(part["id"]): float(part.get("opacity", 1))
                    for part in (render_session.mesh_data or {}).get("parts", [])}
        self.preview.set_part_opacity_overrides({key: value for key, value in self.session.part_overrides.items() if key in defaults}, defaults)
        self._sync_parameter_values(self._pose())

    def _populate_parameters(self):
        self._refreshing = True
        self.parameter_table.setRowCount(0)
        self._parameter_spins.clear()
        for row, parameter in enumerate(self.session.parameters if self.session else []):
            self.parameter_table.insertRow(row)
            parameter_id = parameter["id"]
            name = str(parameter.get("name") or parameter_id)
            item = QTableWidgetItem(name)
            item.setData(Qt.UserRole, parameter_id)
            item.setToolTip(f"{name}\n{parameter_id}\ndefault: {parameter['default']:g}")
            self.parameter_table.setItem(row, 0, item)
            self.parameter_table.setItem(row, 1, QTableWidgetItem(f"{parameter['default']:g}"))
            self._parameter_spins[parameter_id] = _ParameterValue(self, parameter)
            self.parameter_table.setItem(row, 2, QTableWidgetItem(f"{parameter['min']:g} … {parameter['max']:g}"))
            self.parameter_table.setRowHeight(row, 32)
        self._refreshing = False
        if self.parameter_table.rowCount():
            self.parameter_table.setCurrentCell(0, 0)
        self._filter_parameters(self.parameter_search.text())

    def _sync_parameter_values(self, values: dict[str, float]):
        for parameter_id, value in values.items():
            spin = self._parameter_spins.get(parameter_id)
            if spin:
                spin._value = float(value)
        for row in range(self.parameter_table.rowCount()):
            identifier = str(self.parameter_table.item(row, 0).data(Qt.UserRole))
            if identifier in values:
                self.parameter_table.item(row, 1).setText(f"{values[identifier]:.4g}")
        self._sync_parameter_editor()

    def _sync_parameter_editor(self):
        spin = self._parameter_spins.get(self._selected_parameter)
        if not spin:
            self.parameter_editor.setEnabled(False)
            return
        self.parameter_editor.setEnabled(True)
        self.parameter_name.setText(self._selected_parameter)
        self.parameter_name.setToolTip(self._selected_parameter)
        with QSignalBlocker(self.parameter_spin):
            self.parameter_spin.setRange(spin.minimum(), spin.maximum())
            self.parameter_spin.setSingleStep(max(.001, (spin.maximum() - spin.minimum()) / 100))
            self.parameter_spin.setValue(spin.value())
        span = spin.maximum() - spin.minimum()
        with QSignalBlocker(self.parameter_slider):
            self.parameter_slider.setValue(round((spin.value() - spin.minimum()) / span * 1000) if span else 0)

    def _parameter_slider_changed(self, value: int):
        spin = self._parameter_spins.get(self._selected_parameter)
        if spin and not self._refreshing:
            spin.setValue(spin.minimum() + (spin.maximum() - spin.minimum()) * value / 1000)

    def _selected_parameter_value_changed(self, value: float):
        spin = self._parameter_spins.get(self._selected_parameter)
        if spin and not self._refreshing:
            spin.setValue(value)

    def _filter_parameters(self, text: str):
        text = text.casefold()
        for row in range(self.parameter_table.rowCount()):
            item = self.parameter_table.item(row, 0)
            self.parameter_table.setRowHidden(row, text not in (item.text() + " " + str(item.data(Qt.UserRole))).casefold())

    def _parameter_selected(self, row: int, *_args):
        if self._refreshing or row < 0:
            return
        item = self.parameter_table.item(row, 0)
        if item:
            self._selected_parameter = str(item.data(Qt.UserRole))
            self._sync_parameter_editor()
            self._refresh_track()

    def _parameter_value_changed(self, parameter_id: str, value: float):
        if not self.return_to_current_model():
            return
        if self._refreshing or not self.session:
            return
        try:
            self.timeline.set_playing(False)
            self.session.set_parameter(parameter_id, value)
            self._manual_pose = True
            self._selected_parameter = parameter_id
            self._apply_preview_pose()
            self._refresh_track()
            self._schedule_mesh_refresh()
            self._update_actions()
        except Exception as exc:
            self._error(exc)

    def _populate_motions(self, selected: str | None = None):
        selected = selected or self._current_motion()
        with QSignalBlocker(self.motion_combo):
            self.motion_combo.clear()
            for name in self.session.project.motions if self.session else []:
                self.motion_combo.addItem(name, userData=name)
            if not self.motion_combo.count():
                self.motion_combo.addItem(_text("editor.live2d.no_motion"), userData=None)
            self.motion_combo.setCurrentIndex(max(0, self.motion_combo.findData(selected)))
        self._refresh_track()

    def _motion_changed(self, *_args):
        if self._refreshing:
            return
        self.timeline.set_playing(False)
        self._manual_pose = False
        self._refresh_track()
        self._apply_preview_pose()
        self._schedule_mesh_refresh()
        self._update_actions()

    def _refresh_track(self):
        motion = self._current_motion()
        if self.session and motion in self.session.project.motions and self._selected_parameter:
            frames = self.session.keyframes(motion, self._selected_parameter)
            duration = float(self.session.project.motions[motion]["Meta"]["Duration"])
            self.timeline.set_track(frames, duration, ["value"], editable=True)
            self.track_label.setText(self._selected_parameter)
            self.track_label.setToolTip(f"{motion} · {self._selected_parameter}")
        else:
            self.timeline.set_track([], 3, ["value"], editable=False)
            self.track_label.setText(_text("editor.live2d.track_empty"))

    def _frames_edited(self, frames: list):
        if not self.return_to_current_model():
            return
        motion = self._current_motion()
        if self._refreshing or not self.session or not motion or not self._selected_parameter:
            return
        try:
            self.session.set_keyframes(motion, self._selected_parameter, frames)
            self._manual_pose = False
            self._apply_preview_pose()
            self._schedule_mesh_refresh()
            self._update_actions()
        except Exception as exc:
            self._refresh_track()
            self._error(exc)

    def _key_current_pose(self):
        motion = self._current_motion()
        if not self.session or not motion or not self._selected_parameter:
            return
        try:
            frames = self.session.keyframes(motion, self._selected_parameter)
            frame = {"time": self.timeline.current_time, "value": self._pose()[self._selected_parameter], "interpolation": "linear"}
            by_time = {key["time"]: key for key in frames}
            if frame["time"] in by_time:
                by_time[frame["time"]] = dict(by_time[frame["time"]], value=frame["value"])
            else:
                by_time[frame["time"]] = frame
            self.session.set_keyframes(motion, self._selected_parameter, [by_time[t] for t in sorted(by_time)])
            self._refresh_track()
            self._update_actions()
        except Exception as exc:
            self._error(exc)

    def _seek(self, seconds: float):
        self._manual_pose = False
        self._apply_preview_pose()
        self._schedule_mesh_refresh()

    def _play_state_changed(self, playing: bool):
        if playing:
            self._manual_pose = False
        if self.preview:
            self.preview.set_motion_frozen(True)

    def _create_motion(self):
        if not self.session:
            return
        dialog = _MotionDialog(_text("editor.live2d.new_motion"), self.window())
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            name = dialog.name_edit.text().strip()
            self.session.create_motion(name, dialog.duration_spin.value())
            self._populate_motions(name)
            self.timeline.set_time(0)
            self._update_actions()
        except Exception as exc:
            self._error(exc)

    def _clone_motion(self):
        if not self.session or not self._current_motion():
            return
        dialog = _MotionDialog(_text("editor.live2d.clone_motion"), self.window(), duration=False)
        if dialog.exec() == QDialog.Accepted:
            try:
                name = dialog.name_edit.text().strip()
                self.session.clone_motion(self._current_motion(), name)
                self._populate_motions(name)
                self._update_actions()
            except Exception as exc:
                self._error(exc)

    def _schedule_mesh_refresh(self):
        if self.tabs.currentWidget() is self.artmesh_tab:
            self._mesh_timer.start()

    def _refresh_mesh(self):
        if not self.session:
            return
        try:
            path = (self._preview_session or self.session).write_inspector_metadata(self._pose())
            self.artmesh_inspector.load_metadata(path)
        except Exception as exc:
            self._error(exc)

    def _native_drawable_clicked(self, drawable_id: str):
        # Some wrappers return a Part ID from HitPart. Only an actual drawable
        # match can replace the more precise pose-geometry click selection.
        if any(entry.drawable_id == drawable_id for entry in self.artmesh_inspector.entries):
            in_mod = self.tabs.currentWidget() is self.mod_tab
            if not in_mod:
                self.tabs.setCurrentWidget(self.artmesh_tab)
            self.artmesh_inspector.select_entry(drawable_id)
            if in_mod and self._mod_preview_model_id:
                self.mod_panel.select_trigger(drawable_id)

    def _native_model_point_clicked(self, normalized_x: float, normalized_y: float):
        if not self.session:
            return
        in_mod = self.tabs.currentWidget() is self.mod_tab
        if not in_mod:
            self.tabs.setCurrentWidget(self.artmesh_tab)
        self._refresh_mesh()
        canvas = ((self._preview_session or self.session).mesh_data or {}).get("canvas", {})
        x = normalized_x * float(canvas.get("width", 1))
        y = normalized_y * float(canvas.get("height", 1))
        from app.gui.ArtMeshInspector import _point_in_triangle
        candidates = []
        for index, entry in enumerate(self.artmesh_inspector.entries):
            part_id = str(entry.raw.get("parent_part_id") or "")
            if entry.opacity <= .001 or self.session.part_overrides.get(part_id, 1) <= .001:
                continue
            if any(_point_in_triangle((x, y), triangle) for triangle in entry.pose_triangles()):
                candidates.append(((entry.render_order, entry.draw_order, entry.source_index), index))
        if candidates:
            selected = self.artmesh_inspector.entries[max(candidates)[1]]
            self.artmesh_inspector.select_entry(selected.drawable_id)
            if in_mod and self._mod_preview_model_id:
                self.mod_panel.select_trigger(selected.drawable_id)

    def _artmesh_selected(self, entry):
        if not self.session or not entry:
            return
        render_session = self._preview_session or self.session
        image = render_session.local_artmesh_image(entry.raw)
        if image:
            data = image.tobytes("raw", "RGBA")
            qimage = QImage(data, image.width, image.height, image.width * 4, QImage.Format_RGBA8888).copy()
            pixmap = QPixmap.fromImage(qimage)
            self.artmesh_image.setPixmap(pixmap.scaled(290, 88, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.artmesh_image.clear()
        part_id = str(entry.raw.get("parent_part_id") or "")
        available = bool(not self._preview_session and part_id and int(entry.raw.get("parent_part_index", -1)) >= 0)
        self.part_opacity.setEnabled(available)
        self.part_visible.setEnabled(available)
        opacity = self.session.part_overrides.get(part_id, next((float(p.get("opacity", 1)) for p in (self.session.mesh_data or {}).get("parts", []) if p["id"] == part_id), 1))
        with QSignalBlocker(self.part_opacity), QSignalBlocker(self.part_visible):
            self.part_opacity.setValue(opacity)
            self.part_visible.setChecked(opacity > 0)
        count = sum(str(item.get("parent_part_id") or "") == part_id for item in (self.session.mesh_data or {}).get("drawables", []))
        self.part_hint.setText(_text("editor.live2d.part_hint", part=part_id, count=count) if available else _text("editor.live2d.part_unavailable"))

    def _part_visibility_changed(self, visible: bool):
        self._part_opacity_changed(self.part_opacity.value() or 1 if visible else 0)

    def _part_opacity_changed(self, value: float):
        if not self.session:
            return
        entry = self.artmesh_inspector.current_entry()
        part_id = str(entry.raw.get("parent_part_id") or "") if entry else ""
        if not part_id or not self.part_opacity.isEnabled():
            return
        try:
            self.session.set_part_opacity(part_id, value)
            self._apply_preview_pose()
            self._artmesh_selected(entry)
            self._update_actions()
        except Exception as exc:
            self._error(exc)

    def _populate_textures(self):
        with QSignalBlocker(self.texture_combo):
            self.texture_combo.clear()
            for index, (path, size) in enumerate(zip(self.session.texture_paths, self.session.texture_sizes)):
                self.texture_combo.addItem(f"{index}: {path.name} · {size[0]} × {size[1]}", userData=index)
        self._refresh_texture_image()
        self._reset_texture_watcher()

    def _reset_texture_watcher(self):
        watched = self.texture_watcher.files() + self.texture_watcher.directories()
        if watched:
            self.texture_watcher.removePaths(watched)
        if self.session:
            paths = [str(path) for path in self.session.texture_paths]
            paths += list({str(path.parent) for path in self.session.texture_paths})
            if paths:
                self.texture_watcher.addPaths(paths)

    def _refresh_texture_image(self, *_args):
        if not self.session or not self.texture_combo.count():
            self.texture_image.clear()
            return
        index = int(self.texture_combo.currentData() or 0)
        pixmap = QPixmap(str(self.session.texture_paths[index]))
        self.texture_image.setPixmap(pixmap.scaled(max(190, self.texture_image.width() - 8), max(150, self.texture_image.height() - 8), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _choose_texture_replacement(self):
        if not self.session:
            return
        path, _ = QFileDialog.getOpenFileName(self, _text("editor.live2d.replace_texture"), "", "Image (*.png *.jpg *.jpeg *.webp *.bmp)")
        if path:
            self.replace_texture(int(self.texture_combo.currentData() or 0), path)

    def replace_texture(self, index: int, path: str) -> bool:
        if not self.session:
            return False
        try:
            if self.session.replace_texture(index, path):
                self._reload_preview_textures()
                self._update_actions()
            return True
        except Exception as exc:
            self._error(exc)
            return False

    def _choose_external_editor(self):
        path, _ = QFileDialog.getOpenFileName(self, _text("editor.live2d.choose_editor_title"), "", "Application (*.exe);;All files (*)")
        if path:
            self._external_editor = path
            self.editor_label.setText(path)

    def _edit_external_texture(self):
        if not self.session or not self.texture_combo.count():
            return
        path = str(self.session.texture_paths[int(self.texture_combo.currentData() or 0)])
        if self._external_editor:
            success, _pid = QProcess.startDetached(self._external_editor, [path], str(self.session.root))
            if not success:
                self._error("Could not start the selected image editor.")
        elif not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            self._error("Could not open the working texture.")

    def _queue_texture_reload(self, _path: str):
        if self.session:
            self._pending_textures.update(range(len(self.session.texture_paths)))
            self._texture_timer.start()

    def _process_texture_changes(self):
        if not self.session:
            return
        changed = False
        for index in list(self._pending_textures):
            try:
                changed = self.session.accept_texture_change(index) or changed
                self._pending_textures.discard(index)
                self._texture_attempts.pop(index, None)
            except Exception as exc:
                attempts = self._texture_attempts.get(index, 0) + 1
                self._texture_attempts[index] = attempts
                if attempts >= 4:
                    self._pending_textures.discard(index)
                    self.session.restore_texture(index)
                    self._error(exc)
        self._reset_texture_watcher()
        if self._pending_textures:
            self._texture_timer.start()
        if changed:
            self._reload_preview_textures()
            self._update_actions()
            self._status(_text("editor.live2d.texture_reloaded"))

    def _reload_preview_textures(self):
        if self._preview_session:
            if not self.return_to_current_model():
                raise RuntimeError(_text("editor.live2d.return_failed"))
        if self.preview and self.session:
            self.preview.load_model(str(self.session.model_path))
            self._native_model_ready()
        self._refresh_texture_image()
        self._refresh_mesh()
        self._reset_texture_watcher()

    def undo(self):
        try:
            if not self.return_to_current_model():
                return
            if self.session and self.session.undo():
                self.timeline.set_playing(False)
                self._manual_pose = bool(self.session.parameter_overrides)
                self._populate_motions()
                self._reload_preview_textures()
                self._update_actions()
        except Exception as exc:
            self._error(exc)

    def redo(self):
        try:
            if not self.return_to_current_model():
                return
            if self.session and self.session.redo():
                self.timeline.set_playing(False)
                self._manual_pose = bool(self.session.parameter_overrides)
                self._populate_motions()
                self._reload_preview_textures()
                self._update_actions()
        except Exception as exc:
            self._error(exc)

    def _choose_save_copy(self) -> bool:
        if not self.session:
            return False
        parent = QFileDialog.getExistingDirectory(self, _text("editor.live2d.save_parent"), str(self.session.source_root.parent))
        if not parent:
            return False
        name = f"{self.session.source_path.stem}-edited-{datetime.now():%Y%m%d-%H%M%S-%f}"
        return bool(self.save_copy(str(Path(parent) / name)))

    def _use_edited_for_mod(self):
        if not self.session or self._projects_busy():
            return
        try:
            if not self.mod_panel.current_project:
                self.mod_panel.bind_project(self.session.ensure_mod_project())
            target = self.session.root / "mod-inputs" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            self.mod_panel.queue_source_imports([str(self.session.export_snapshot(target))])
        except Exception as exc:
            self._error(exc)

    def save_copy(self, output_dir: str) -> dict | None:
        if not self.session:
            return None
        try:
            if self._projects_busy():
                raise RuntimeError(_text("editor.live2d.project_busy"))
            # An external editor may have saved immediately before this click.
            # Accept that final valid texture revision before making the copy.
            for index in range(len(self.session.texture_paths)):
                self.session.accept_texture_change(index)
            self._flush_subprojects()
            self.last_saved_copy = self.session.save_copy(output_dir)
            self.mod_panel.set_source(str(self.session.model_path))
            self._status(_text("editor.live2d.saved", path=output_dir))
            self._update_actions()
            return self.last_saved_copy
        except Exception as exc:
            self._error(exc)
            return None

    def _confirm_session_changes(self) -> bool:
        if not self.session:
            return True
        for index in range(len(self.session.texture_paths)):
            try:
                self.session.accept_texture_change(index)
            except Exception as exc:
                self._error(exc)
                return False
        if not self.session.dirty and not self.psd_panel._project_dirty and not self.mod_panel._dirty:
            return True
        answer = QMessageBox.warning(self, _text("editor.live2d.unsaved_title"), _text("editor.live2d.project_unsaved"),
                                     QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel)
        if answer == QMessageBox.Save:
            return self._choose_save_copy()
        return answer == QMessageBox.Discard

    def confirm_discard_or_save(self) -> bool:
        if self._projects_busy():
            self._status(_text("editor.live2d.project_busy"))
            return False
        return self._confirm_session_changes()

    def set_active(self, active: bool):
        self._active = bool(active)
        if not active:
            self.timeline.set_playing(False)
            self._mesh_timer.stop()
        if self.preview:
            self.preview.set_rendering_active(active and self.workspace.is_panel_visible("preview"))

    def shutdown(self):
        self.set_active(False)
        self._texture_timer.stop()
        self._mesh_timer.stop()
        self.mod_panel._project_search_timer.stop()
        if not self.psd_panel.shutdown():
            return False
        worker = self.mod_panel.worker
        if worker and worker.isRunning():
            worker.wait(30000)
            if worker.isRunning():
                return False
        watched = self.texture_watcher.files() + self.texture_watcher.directories()
        if watched:
            self.texture_watcher.removePaths(watched)
        if self.preview:
            self.preview.close()
            self.preview.deleteLater()
            self.preview = None
        if self._preview_session:
            self._preview_session.close()
            self._preview_session = None
        if self.session:
            self.session.close()
            self.session = None
        return True

    def _tab_changed(self, *_args):
        if self._binding_projects:
            return
        if self.tabs.currentWidget() is getattr(self, "artmesh_tab", None):
            self._refresh_mesh()
        elif self.tabs.currentWidget() is getattr(self, "texture_tab", None):
            self._refresh_texture_image()
        elif self.tabs.currentWidget() is getattr(self, "psd_tab", None) and self.session and not self.psd_panel.current_project:
            self.open_psd_workspace()
        elif self.tabs.currentWidget() is getattr(self, "mod_tab", None) and self.session and not self.mod_panel.current_project:
            try:
                self.mod_panel.bind_project(self.session.ensure_mod_project())
            except Exception as exc:
                self._error(exc)

    def _update_actions(self):
        session = self.session
        self.save_button.setEnabled(bool(session))
        self.undo_button.setEnabled(bool(session and session.can_undo))
        self.redo_button.setEnabled(bool(session and session.can_redo))
        self.new_motion_button.setEnabled(bool(session))
        self.use_for_mod_button.setEnabled(bool(session))
        self.clone_motion_button.setEnabled(bool(session and self._current_motion()))
        self.key_pose_button.setEnabled(bool(session and self._current_motion() and self._selected_parameter))
        self.parameter_tab.setEnabled(bool(session))
        self.artmesh_tab.setEnabled(bool(session))
        self.texture_tab.setEnabled(bool(session and session.texture_paths))
        self.psd_tab.setEnabled(bool(session))
        self.mod_tab.setEnabled(bool(session))
        self.motion_combo.setEnabled(bool(session and session.project.motions))
        dirty = bool(session and (session.dirty or self.psd_panel._project_dirty or self.mod_panel._dirty))
        self.title_label.setText(_text("editor.live2d.title") + (" *" if dirty else ""))
        self.title_label.setToolTip(_text("editor.live2d.modified" if dirty else "editor.live2d.clean"))
        self.source_label.setVisible(bool(session))

    def _status(self, text: str):
        self.status_label.setText(text)
        self.status_label.setToolTip(text)

    def _error(self, error):
        self._status(_text("editor.live2d.error", error=str(error)))

    def showEvent(self, event):
        super().showEvent(event)
        self.set_active(True)

    def hideEvent(self, event):
        self.set_active(False)
        super().hideEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_texture_image()
