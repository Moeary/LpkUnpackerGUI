"""Live2D runtime editing with native preview and the original MOD workflow."""
from __future__ import annotations

import copy
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QFileSystemWatcher, QPoint, QProcess, QSignalBlocker, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QImage, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFileDialog,
    QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
    QSizePolicy, QTableWidgetItem, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, CheckBox, ComboBox, DoubleSpinBox,
    FluentIcon, LineEdit, PrimaryPushButton,
    PushButton, RoundMenu, ScrollArea, Slider, SubtitleLabel, TableWidget,
    TransparentToolButton,
    TransparentToggleToolButton,
)

from app.core.live2d_editor_session import Live2DEditorSession
from app.gui.ArtMeshInspector import ArtMeshInspector
from app.gui.ImagePreviewPanel import ImageZoomScrollArea
from app.gui.live2d_editor_panels import ProjectBoundModPage
from app.gui.live2d_skin_controls import SkinCatalogControls, SkinDropFrame, SkinNameDialog, TexturePreviewLabel, skin_text
from app.gui.live2d_appearance import (
    AppearanceTaskWorkspace, CompactAppearanceButton, ElidedAppearanceLabel, ReadonlyPreviewPrepareThread,
    SharedTaskFeedback, appearance_text,
)
from app.gui.PsdReconstructionPage import PsdReconstructionPage
from app.gui.editor_timeline import AnimationTimelineEditor
from app.gui.editor_actions import ActionComboBox, ActionDeleteDialog, ActionNameDialog, action_text, close_editor_popup
from app.gui.editor_dialogs import EditorMessageBox as QMessageBox, EditorTextDialog, ThemedEditorDialog as MessageBoxBase
from app.gui.editor_workspace import EditorComboBox, EditorViewportLayout, EditorWorkspace, FluentEditorTabs
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
    "editor.live2d.part_summary": "所属部件 {part} · {count} 个 ArtMesh",
    "editor.live2d.part_opacity": "部件不透明度",
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
    "editor.live2d.project_save": "将当前工作对象保存为独立工程副本",
    "editor.live2d.project_unsaved": "当前模型、PSD 或 MOD 工程有修改。保存完整工程副本后继续？",
    "editor.live2d.psd_missing_version": "先选择一个有效的回写或复合版本。",
    "editor.live2d.return_failed": "当前编辑模型预览恢复失败，操作已停止。",
    "editor.live2d.open_short": "打开",
    "editor.live2d.save_short": "保存工程",
    "editor.live2d.parameter_track_search": "搜索并选择参数轨道",
    "editor.live2d.pick_point": "点选 ArtMesh（可在重叠候选中切换）",
    "editor.live2d.pick_rectangle": "框选当前可见 ArtMesh",
    "editor.live2d.pick_clear": "取消选区",
    "editor.live2d.pick_psd": "选区 PSD",
    "editor.live2d.pick_psd_hint": "将选中的完整 ArtMesh 按当前姿态导出为可回写 PSD",
    "editor.live2d.pick_count": "已选 {count} 个",
    "editor.live2d.local_preview_zoom": "放大查看所选 ArtMesh 贴图区域",
    "editor.live2d.pick_hidden_title": "选中部件已隐藏",
    "editor.live2d.pick_hidden_export": "所属 Part 已隐藏：{parts}。仅在导出副本中恢复这些 Part 可见，并导出所选 ArtMesh；当前预览与编辑记录保持不变。继续？",
    "editor.live2d.pick_restore_export": "恢复可见并导出",
    "editor.live2d.pick_invisible": "以下选中 ArtMesh 在当前参数姿态中没有可见内容，请调整姿态后再导出：{ids}",
    "editor.live2d.drawable_visible": "预览可见",
    "editor.live2d.drawable_opacity": "不透明度",
    "editor.live2d.drawable_mixed": "混合",
    "editor.live2d.drawable_hint": "仅调整当前所选 ArtMesh 的预览可见性和透明度；恢复可见时保留所设透明度。",
    "editor.live2d.parent_part_settings": "所属 Part 设置",
    "editor.live2d.pick_hidden_drawables_export": "以下所选 ArtMesh 在预览中被隐藏或透明度为零：{ids}。仅在导出副本中恢复可见；当前预览和编辑记录保持不变。继续？",
}


def _text(key: str, **values) -> str:
    return tr(key, default=LIVE2D_EDITOR_TEXT[key], **values)


class _MotionDialog(ActionNameDialog):
    """Compatibility name for the shared owned native dialog."""


class _ParameterComboBox(ActionComboBox):
    def retranslate_ui(self):
        text = _text("editor.live2d.parameter_track_search")
        self.setPlaceholderText(text)
        self.setToolTip(text)
        self.setAccessibleName(text)


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
        self._native_return_pending = False
        self._preview_generation = 0
        self._preview_workers = {}
        self._preview_request = None
        self._closing = False
        self._atlas_preview_paths = None
        self._atlas_preview_label = ""
        self._binding_projects = False
        self.last_open_error = ""
        self._refreshing = False
        self._active = False
        self._action_dialog = None
        self._manual_pose = False
        self._selected_parameter = ""
        self._inspector_session = None
        self._inspector_scene_key = None
        self._parameter_spins: dict[str, _ParameterValue] = {}
        self._parameter_rows: dict[str, int] = {}
        self._parameter_dragging = False
        self._parameter_edit_active = False
        self._selection_cache = None
        self._selected_drawable_ids: list[str] = []
        self._skin_ui_state_key = None
        self._selection_region = None
        self._selection_region_key = None
        self._mod_preview_model_id = ""
        self._external_editor = ""
        self.last_saved_copy: dict | None = None
        self._pending_textures: set[int] = set()
        self._texture_attempts: dict[int, int] = {}
        self._texture_indices = {}
        self._texture_directories = {}
        self._texture_stats = {}
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
        self._parameter_commit_timer = QTimer(self)
        self._parameter_commit_timer.setSingleShot(True)
        self._parameter_commit_timer.setInterval(250)
        self._parameter_commit_timer.timeout.connect(self._flush_parameter_edit)
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
        self.editing_context_label = CaptionLabel(self)
        self.editing_context_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        root.addWidget(self.editing_context_label)
        self.source_hint = CaptionLabel(self)
        self.source_hint.hide()
        self.stage = QWidget(self)
        self.stage.setMinimumSize(220, 120)
        self.stage_layout = QVBoxLayout(self.stage)
        self.stage_layout.setContentsMargins(0, 0, 0, 0)
        self.stage_layout.setSpacing(0)
        self.selection_toolbar = QWidget(self.stage)
        self.selection_toolbar.setFixedHeight(32)
        selection_layout = QHBoxLayout(self.selection_toolbar)
        selection_layout.setContentsMargins(6, 0, 6, 0)
        selection_layout.setSpacing(4)
        self.point_select_button = TransparentToggleToolButton(FluentIcon.EDIT, self.selection_toolbar)
        self.rect_select_button = TransparentToggleToolButton(FluentIcon.FULL_SCREEN, self.selection_toolbar)
        self.clear_selection_button = TransparentToolButton(FluentIcon.CANCEL, self.selection_toolbar)
        self.export_selection_button = PushButton(FluentIcon.SAVE, "", self.selection_toolbar)
        self.selection_count = CaptionLabel(self.selection_toolbar)
        self.selection_count.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        for button in (self.point_select_button, self.rect_select_button, self.clear_selection_button):
            button.setFixedSize(28, 28)
            selection_layout.addWidget(button)
        selection_layout.addWidget(self.selection_count, 1)
        selection_layout.addWidget(self.export_selection_button)
        self.point_select_button.clicked.connect(lambda checked=False: self.set_artmesh_selection_mode("point" if checked else "none"))
        self.rect_select_button.clicked.connect(lambda checked=False: self.set_artmesh_selection_mode("rectangle" if checked else "none"))
        self.clear_selection_button.clicked.connect(self.clear_artmesh_selection)
        self.export_selection_button.clicked.connect(self.export_selected_artmeshes)
        self.selection_toolbar.hide()
        self.stage_layout.addWidget(self.selection_toolbar)
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
        self.appearance_tab = QWidget(self.tabs)
        appearance_layout = EditorViewportLayout(self.appearance_tab)
        appearance_layout.setContentsMargins(4, 4, 4, 4)
        self._build_texture_tab()
        self.psd_tab = QWidget(self.appearance_tab)
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
        self.psd_panel.repackSkinReady.connect(self._psd_skin_ready)
        variant_signal = getattr(self.psd_panel, "repackVariantRequested", None)
        if variant_signal is not None:
            variant_signal.connect(self._psd_variant_dialog)
        self.psd_panel.unifiedPreviewRequested.connect(self._open_psd_preview)
        self.psd_panel.previewClosed.connect(self.return_to_current_model)
        self.psd_panel.export_snapshot_provider = self._psd_export_snapshot
        self.psd_panel.export_snapshot_request_provider = self._psd_export_snapshot_request
        self.psd_panel.preview_command_handler = self._psd_preview_command
        provider = getattr(self.psd_panel, "set_version_skin_provider", None)
        if callable(provider):
            provider(self._find_psd_skin)
        texture_signal = getattr(self.psd_panel, "texturePreviewRequested", None)
        if texture_signal is not None:
            texture_signal.connect(self._show_psd_texture_preview)
        psd_layout.addWidget(self.psd_panel)
        self.appearance_workspace = AppearanceTaskWorkspace(
            self.skin_controls, self.texture_tab, self.psd_tab, self.psd_panel, self.appearance_tab,
            settings=self.psd_panel.settings_manager,
        )
        self.appearance_workspace.taskChanged.connect(self._appearance_task_changed)
        appearance_layout.addWidget(self.appearance_workspace)
        self.tabs.addTab(self.appearance_tab, "")
        self.export_tab = QWidget(self.tabs)
        self.mod_tab = self.export_tab  # compatibility route; advanced page is nested below
        export_layout = EditorViewportLayout(self.export_tab)
        export_layout.setContentsMargins(4, 4, 4, 4)
        self.export_tabs = FluentEditorTabs(self.export_tab)
        export_layout.addWidget(self.export_tabs)
        self.model_export_tab = QWidget(self.export_tabs)
        ordinary_layout = EditorViewportLayout(self.model_export_tab)
        ordinary_layout.setContentsMargins(8, 8, 8, 8)
        self.export_skin_combo = ActionComboBox(self.model_export_tab)
        self.export_skin_combo.setMaximumWidth(16777215)
        ordinary_layout.addWidget(self.export_skin_combo)
        self.model_export_hint = CaptionLabel(self.model_export_tab)
        self.model_export_hint.setWordWrap(True)
        ordinary_layout.addWidget(self.model_export_hint)
        self.export_model_button = PrimaryPushButton(FluentIcon.SAVE, "", self.model_export_tab)
        self.export_model_button.clicked.connect(self._choose_skin_export)
        ordinary_layout.addWidget(self.export_model_button)
        ordinary_layout.addStretch(1)
        self.export_tabs.addTab(self.model_export_tab, "")
        self.viewer_tab = QWidget(self.export_tabs)
        mod_layout = EditorViewportLayout(self.viewer_tab)
        mod_layout.setContentsMargins(4, 4, 4, 4)
        self.use_for_mod_button = PrimaryPushButton(self.viewer_tab)
        self.use_for_mod_button.clicked.connect(self._use_edited_for_mod)
        mod_layout.addWidget(self.use_for_mod_button)
        self.mod_hint = CaptionLabel(self.viewer_tab)
        self.mod_hint.hide()
        self.mod_panel = ProjectBoundModPage(lambda: self.session, self.viewer_tab)
        self.mod_panel.previewModelRequested.connect(self._open_mod_preview)
        self.mod_panel.triggerSelectionChanged.connect(self._mod_trigger_selected)
        self.mod_panel.projectChanged.connect(self._mod_project_changed)
        self.mod_panel.set_artmesh_pick_provider(self._mod_pick_scene)
        mod_layout.addWidget(self.mod_panel, 1)
        self.export_tabs.addTab(self.viewer_tab, "")
        self.export_tabs.currentChanged.connect(self._tab_changed)
        self.tabs.addTab(self.export_tab, "")
        timeline_host = QWidget(self)
        timeline_layout = EditorViewportLayout(timeline_host)
        timeline_layout.setContentsMargins(0, 0, 0, 0)
        timeline_layout.setSpacing(4)
        row = QHBoxLayout()
        self.motion_label = CaptionLabel(timeline_host)
        self.motion_label.hide()
        self.motion_combo = ActionComboBox(timeline_host)
        self.motion_combo.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.motion_combo.currentIndexChanged.connect(self._motion_changed)
        self.new_motion_button = TransparentToolButton(FluentIcon.ADD, timeline_host)
        self.clone_motion_button = TransparentToolButton(FluentIcon.COPY, timeline_host)
        self.delete_motion_button = TransparentToolButton(FluentIcon.REMOVE, timeline_host)
        self.key_pose_button = TransparentToolButton(FluentIcon.ACCEPT, timeline_host)
        for button in (self.new_motion_button, self.delete_motion_button, self.clone_motion_button, self.key_pose_button):
            button.setFixedSize(28, 28)
        self.new_motion_button.clicked.connect(self._create_motion)
        self.clone_motion_button.clicked.connect(self._clone_motion)
        self.delete_motion_button.clicked.connect(self._delete_motion)
        self.key_pose_button.clicked.connect(self._key_current_pose)
        row.addWidget(self.motion_combo)
        for button in (self.new_motion_button, self.delete_motion_button, self.clone_motion_button):
            row.addWidget(button)
        self.track_label = CaptionLabel(timeline_host)
        self.track_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.track_label.hide()
        self.parameter_combo = _ParameterComboBox(timeline_host)
        self.parameter_combo.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.parameter_combo.currentIndexChanged.connect(self._timeline_parameter_changed)
        row.addWidget(self.parameter_combo, 1)
        row.addWidget(self.key_pose_button)
        row.addSpacing(28)
        timeline_layout.addLayout(row)
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
        self.workspace.reset_button.clicked.connect(self.appearance_workspace.reset_layout)
        self.workspace.empty_reset_button.clicked.connect(self.appearance_workspace.reset_layout)
        self.workspace.set_task_context("animation", timeline_visible=True)
        root.addWidget(self.workspace, 1)
        self.task_feedback = SharedTaskFeedback(self)
        self.status_label = self.task_feedback.status_label
        self.psd_panel.taskStateChanged.connect(self._task_state_changed)
        if hasattr(self.mod_panel, "taskStateChanged"):
            self.mod_panel.taskStateChanged.connect(self._task_state_changed)
        root.addWidget(self.task_feedback)
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
        self.parameter_slider.sliderPressed.connect(self._parameter_drag_started)
        self.parameter_slider.sliderReleased.connect(self._parameter_drag_finished)
        # Fluent forwards these signals only from its child handle; a click or
        # drag on the groove uses custom mouse events without QSlider signals.
        for widget in (self.parameter_slider, self.parameter_slider.handle):
            widget.installEventFilter(self)
        controls.addWidget(self.parameter_slider, 1)
        self.parameter_spin = DoubleSpinBox(self.parameter_editor)
        self.parameter_spin.setDecimals(4)
        self.parameter_spin.setMinimumWidth(148)
        self.parameter_spin.setMaximumWidth(168)
        self.parameter_spin.valueChanged.connect(self._selected_parameter_value_changed)
        self.parameter_spin.editingFinished.connect(self._flush_parameter_edit)
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
        layout = EditorViewportLayout(self.artmesh_tab)
        layout.setContentsMargins(4, 4, 4, 4)
        self.artmesh_inspector = ArtMeshInspector(parent=self.artmesh_tab)
        inspector = self.artmesh_inspector
        inspector.selectionChanged.connect(self._artmesh_selected)
        inspector.selectionIdsChanged.connect(self._artmesh_selection_changed)
        self.drawable_controls = QWidget(self.artmesh_tab)
        batch = QGridLayout(self.drawable_controls)
        self.drawable_controls_layout = batch
        self._drawable_controls_compact = None
        batch.setContentsMargins(2, 0, 2, 0)
        batch.setSpacing(5)
        self.drawable_selection_label = ElidedAppearanceLabel(self.drawable_controls)
        self.drawable_selection_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.drawable_visible = CheckBox(self.drawable_controls)
        self.drawable_visible.setChecked(True)
        self.drawable_visible.stateChanged.connect(self._drawable_visibility_changed)
        self.drawable_opacity_label = CaptionLabel(self.drawable_controls)
        self.drawable_opacity = DoubleSpinBox(self.drawable_controls)
        self.drawable_opacity.setFixedWidth(148)
        self.drawable_opacity.setRange(-.01, 1)
        self.drawable_opacity.setDecimals(2)
        self.drawable_opacity.setSingleStep(.05)
        self.drawable_opacity.setValue(1)
        self.drawable_opacity.valueChanged.connect(self._drawable_opacity_changed)
        self.drawable_opacity.installEventFilter(self)
        self.drawable_opacity.lineEdit().installEventFilter(self)
        self._resize_drawable_opacity()
        self.drawable_controls.installEventFilter(self)
        self._arrange_drawable_controls()
        layout.addWidget(self.drawable_controls)
        self.artmesh_details_widget = QWidget(inspector)
        details_layout = EditorViewportLayout(self.artmesh_details_widget)
        details_layout.setContentsMargins(0, 4, 0, 0)
        details_layout.setSpacing(6)
        self.local_preview_label = CaptionLabel(self.artmesh_details_widget)
        self.local_preview_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        local_row = QHBoxLayout()
        local_row.addWidget(self.local_preview_label, 1)
        self.local_preview_button = TransparentToolButton(FluentIcon.ZOOM, self.artmesh_details_widget)
        self.local_preview_button.setFixedSize(28, 28)
        self.local_preview_button.clicked.connect(self._show_local_artmesh_image)
        local_row.addWidget(self.local_preview_button)
        details_layout.addLayout(local_row)
        self.artmesh_image = TexturePreviewLabel(self.artmesh_details_widget)
        self.artmesh_image.setObjectName("editorImage")
        self.artmesh_image.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)
        self.artmesh_image.setAlignment(Qt.AlignCenter)
        self.artmesh_image.setMinimumHeight(60)
        self.artmesh_image.setMaximumHeight(160)
        details_layout.addWidget(self.artmesh_image, 1)
        self.parent_part_button = CompactAppearanceButton(self.artmesh_details_widget)
        self.parent_part_button.clicked.connect(lambda: self.parent_part_controls.setVisible(self.parent_part_controls.isHidden()))
        details_layout.addWidget(self.parent_part_button)
        self.parent_part_controls = QWidget(self.artmesh_details_widget)
        part_layout = EditorViewportLayout(self.parent_part_controls)
        part_layout.setContentsMargins(0, 0, 0, 0)
        part_layout.setSpacing(5)
        self.part_visible = CheckBox(self.parent_part_controls)
        self.part_visible.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        part_layout.addWidget(self.part_visible)
        self.part_opacity_label = CaptionLabel(self.parent_part_controls)
        self.part_opacity_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.part_opacity = DoubleSpinBox(self.parent_part_controls)
        self.part_opacity.setMaximumWidth(140)
        self.part_opacity.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.part_opacity.setRange(0, 1)
        self.part_opacity.setSingleStep(.05)
        self.part_opacity.setDecimals(2)
        self.part_opacity.setValue(1)
        self.part_visible.setChecked(True)
        self.part_visible.toggled.connect(self._part_visibility_changed)
        self.part_opacity.valueChanged.connect(self._part_opacity_changed)
        part_layout.addWidget(self.part_opacity_label)
        part_layout.addWidget(self.part_opacity)
        self.part_hint = CaptionLabel(self.parent_part_controls)
        self.part_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        part_layout.addWidget(self.part_hint)
        details_layout.addWidget(self.parent_part_controls)
        self.parent_part_controls.hide()
        self.artmesh_export_button = PushButton(FluentIcon.SAVE, "", self.artmesh_details_widget)
        self.artmesh_export_button.clicked.connect(self.export_selected_artmeshes)
        details_layout.addWidget(self.artmesh_export_button)
        inspector.set_editor_layout(self.artmesh_details_widget)
        inspector.refresh_button.clicked.disconnect()
        inspector.refresh_button.clicked.connect(lambda: self._refresh_mesh(force=True))
        self.artmesh_scroll = inspector.editor_details_scroll
        layout.addWidget(inspector, 1)
        self.tabs.addTab(self.artmesh_tab, "")

    def _build_texture_tab(self):
        self.texture_tab = SkinDropFrame(self.appearance_tab)
        self.texture_tab.filesDropped.connect(self._replace_texture_drop)
        self.texture_tab.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.texture_tab.setFocusPolicy(Qt.StrongFocus)
        layout = EditorViewportLayout(self.texture_tab)
        layout.setContentsMargins(0, 0, 0, 0)
        self.skin_controls = SkinCatalogControls(self.appearance_tab)
        self.skin_controls.filesDropped.connect(self.import_skin_files)
        self.skin_controls.skinSelected.connect(self.apply_skin)
        self.skin_controls.captureRequested.connect(self._capture_skin_dialog)
        self.skin_controls.renameRequested.connect(self._rename_skin_dialog)
        self.skin_controls.deleteRequested.connect(self._delete_skin_dialog)
        self.skin_controls.importModelRequested.connect(self._import_skin_model_dialog)
        self.skin_controls.importTexturesRequested.connect(self._import_skin_textures_dialog)
        self.skin_controls.originRequested.connect(self.open_skin_psd_origin)
        content = QWidget(self.texture_tab)
        content_layout = EditorViewportLayout(content)
        content_layout.setContentsMargins(6, 4, 6, 4)
        content_layout.setSpacing(4)
        self.atlas_context_label = ElidedAppearanceLabel(content)
        self.atlas_context_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        content_layout.addWidget(self.atlas_context_label)
        self.texture_combo = EditorComboBox(content)
        self.texture_combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.texture_combo.currentIndexChanged.connect(self._refresh_texture_image)
        content_layout.addWidget(self.texture_combo)
        self.texture_image = TexturePreviewLabel(self.texture_tab)
        self.texture_image.setObjectName("editorImage")
        self.texture_image.setAlignment(Qt.AlignCenter)
        self.texture_image.setMinimumSize(50, 50)
        self.texture_image.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        content_layout.addWidget(self.texture_image, 1)
        actions = QHBoxLayout()
        actions.setSpacing(3)
        self.replace_texture_button = CompactAppearanceButton(self.texture_tab)
        self.choose_editor_button = TransparentToolButton(FluentIcon.SETTING, self.texture_tab)
        self.choose_editor_button.setFixedSize(26, 28)
        self.edit_texture_button = CompactAppearanceButton(self.texture_tab)
        self.edit_texture_button.clicked.connect(self._edit_external_texture)
        self.replace_texture_button.clicked.connect(self._choose_texture_replacement)
        self.choose_editor_button.clicked.connect(self._choose_external_editor)
        actions.addWidget(self.replace_texture_button, 1)
        actions.addWidget(self.edit_texture_button, 1)
        actions.addWidget(self.choose_editor_button)
        content_layout.addLayout(actions)
        self.editor_label = CaptionLabel(self.texture_tab)
        self.editor_label.hide()
        self.texture_hint = CaptionLabel(self.texture_tab)
        self.texture_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        content_layout.addWidget(self.texture_hint)
        self.texture_scroll = ScrollArea(self.texture_tab)
        self.texture_scroll.setWidgetResizable(True)
        self.texture_scroll.setFrameShape(QFrame.NoFrame)
        self.texture_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.texture_scroll.setWidget(content)
        self.texture_scroll.enableTransparentBackground()
        self.texture_tab.watch_drop_surface(self.texture_scroll.viewport())
        self.texture_tab.watch_drop_surface(self.texture_image)
        layout.addWidget(self.texture_scroll)

    def retranslate_ui(self, *_args):
        labels = {
            self.title_label: "title", self.open_button: "open", self.save_button: "save",
            self.empty_label: "empty",
            self.source_hint: "source_hint", self.parameter_hint: "parameter_hint",
            self.motion_label: "motion",
            self.part_visible: "visible", self.local_preview_label: "local_preview",
            self.use_for_mod_button: "use_for_mod", self.mod_hint: "mod_hint",
        }
        for widget, key in labels.items():
            widget.setText(_text("editor.live2d." + key))
        for button, key in ((self.undo_button, "undo"), (self.redo_button, "redo"), (self.help_button, "help"),
                            (self.key_pose_button, "key_pose")):
            button.setToolTip(_text("editor.live2d." + key))
            button.setAccessibleName(button.toolTip())
        for button, key in ((self.new_motion_button, "new"), (self.clone_motion_button, "copy"), (self.delete_motion_button, "delete")):
            button.setToolTip(action_text("editor.actions." + key))
            button.setAccessibleName(button.toolTip())
        self.motion_combo.retranslate_ui()
        self.parameter_combo.retranslate_ui()
        for button, key in ((self.point_select_button, "pick_point"), (self.rect_select_button, "pick_rectangle"),
                            (self.clear_selection_button, "pick_clear"), (self.export_selection_button, "pick_psd_hint")):
            button.setToolTip(_text("editor.live2d." + key))
            button.setAccessibleName(button.toolTip())
        self.export_selection_button.setText(_text("editor.live2d.pick_psd"))
        self.artmesh_export_button.setText(_text("editor.live2d.pick_psd"))
        self.artmesh_export_button.setToolTip(_text("editor.live2d.pick_psd_hint"))
        self._update_selection_actions()
        self.use_for_mod_button.setToolTip(_text("editor.live2d.mod_hint"))
        self.parameter_name.setText(_text("editor.live2d.selected_parameter"))
        for index, key in enumerate(("animation", "parts")):
            self.tabs.setTabText(index, appearance_text("editor.appearance." + key))
            self.tabs.setTabToolTip(index, appearance_text("editor.appearance." + key + "_hint"))
        self.tabs.setTabText(2, appearance_text("editor.appearance.tab"))
        self.return_model_button.setText(appearance_text("editor.appearance.preview_return"))
        self.open_button.setText(_text("editor.live2d.open_short"))
        self.save_button.setText(_text("editor.live2d.save_short"))
        self.open_button.setToolTip(_text("editor.live2d.project_open"))
        self.save_button.setToolTip(appearance_text("editor.appearance.save_scope"))
        self.parameter_search.setPlaceholderText(_text("editor.live2d.search"))
        self.parameter_table.setHorizontalHeaderLabels([_text("editor.live2d." + key) for key in ("parameter", "value", "range")])
        self.editor_label.setText(self._external_editor or _text("editor.live2d.default_editor"))
        self.part_opacity.setToolTip(_text("editor.live2d.opacity"))
        self.part_opacity_label.setText(_text("editor.live2d.part_opacity"))
        self.part_opacity_label.setToolTip(_text("editor.live2d.opacity"))
        self.parent_part_button.setText(_text("editor.live2d.parent_part_settings"))
        self.parent_part_button.setToolTip(_text("editor.live2d.part_hint", part="Part", count="…"))
        self.drawable_visible.setText(_text("editor.live2d.drawable_visible"))
        self.drawable_opacity_label.setText(_text("editor.live2d.drawable_opacity"))
        self.drawable_opacity.setSpecialValueText(_text("editor.live2d.drawable_mixed"))
        for widget in (self.drawable_controls, self.drawable_visible, self.drawable_opacity):
            widget.setToolTip(_text("editor.live2d.drawable_hint"))
        self.local_preview_label.setToolTip(_text("editor.live2d.local_preview"))
        self.local_preview_button.setToolTip(_text("editor.live2d.local_preview_zoom"))
        self.local_preview_button.setAccessibleName(self.local_preview_button.toolTip())
        self.timeline.retranslate_ui()
        self.skin_controls.retranslate_ui()
        self.replace_texture_button.setText(appearance_text("editor.appearance.atlas_replace"))
        self.replace_texture_button.setToolTip(appearance_text("editor.appearance.atlas_replace_hint"))
        self.edit_texture_button.setText(appearance_text("editor.appearance.atlas_edit"))
        self.edit_texture_button.setToolTip(appearance_text("editor.appearance.atlas_edit_hint"))
        self.choose_editor_button.setToolTip(_text("editor.live2d.choose_editor"))
        self.choose_editor_button.setAccessibleName(self.choose_editor_button.toolTip())
        self.texture_hint.setText(appearance_text("editor.appearance.atlas_drop"))
        self.texture_hint.setToolTip(appearance_text("editor.appearance.atlas_replace_hint"))
        self.tabs.setTabText(3, skin_text("editor.skin.export_tab"))
        self.export_tabs.setTabText(0, skin_text("editor.skin.live2d_export"))
        self.export_tabs.setTabText(1, skin_text("editor.skin.viewer_export"))
        self.model_export_hint.setText(skin_text("editor.skin.live2d_hint"))
        self.export_model_button.setText(skin_text("editor.skin.export_button"))
        self.use_for_mod_button.setText(skin_text("editor.skin.viewer_add"))
        self.use_for_mod_button.setToolTip(skin_text("editor.skin.viewer_hint"))
        self._refresh_skin_controls(force=True)
        self.workspace.retranslate_ui()
        self.appearance_workspace.retranslate_ui()
        self.task_feedback.retranslate_ui()
        self.mod_panel.retranslate_ui()
        self.psd_panel.retranslate_ui()
        self._update_actions()
        if self.artmesh_inspector.current_entry() is not None:
            self._artmesh_selected(self.artmesh_inspector.current_entry())

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
            if not self._cancel_preview_preparation(wait=True):
                self.last_open_error = appearance_text("editor.appearance.preview_stopping")
                self.task_feedback.set_state({"task": "preview", "state": "running", "busy": True,
                                              "message": self.last_open_error})
                self.sourceFailed.emit(path)
                return False
            candidate = Live2DEditorSession(path)
            if not self._confirm_session_changes():
                candidate.close()
                return False
            self.timeline.set_playing(False)
            if not self.return_to_current_model(reload=False, announce=False):
                raise RuntimeError(_text("editor.live2d.return_failed"))
            self.session = candidate
            self.clear_artmesh_selection()
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
                canvas = self.preview.live2d_canvas
                if hasattr(canvas, "drawablesPickedWithMode"):
                    canvas.drawablesPickedWithMode.connect(self._native_drawables_picked)
                    canvas.regionPickedWithMode.connect(self._native_region_picked)
                    canvas.selectionFailed.connect(self._error)
                    canvas.selectionCancelled.connect(self.clear_artmesh_selection)
                elif hasattr(canvas, "drawablesPicked"):
                    canvas.drawablesPicked.connect(self._native_drawables_picked)
                    canvas.regionPicked.connect(self._native_region_picked)
                    canvas.selectionFailed.connect(self._error)
                    canvas.selectionCancelled.connect(self.clear_artmesh_selection)
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
            self._native_return_pending = False
            self._manual_pose = bool(candidate.parameter_overrides)
            self._selected_parameter = candidate.parameters[0]["id"] if candidate.parameters else ""
            self._populate_motions()
            self._populate_parameters()
            self._populate_textures()
            self._refresh_mesh_if_visible()
            self._mod_preview_model_id = ""
            self._bind_subprojects()
            self.source_label.setText(candidate.source_path.name)
            self.source_label.setToolTip(str(candidate.source_path))
            self._native_model_ready()
            self.set_active(self.isVisible())
            self._update_actions()
            self._status("; ".join(candidate.warnings))
            if self.tabs.currentWidget() is self.appearance_tab:
                self._ensure_appearance_project()
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
                    self._native_return_pending = False
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
                    self._refresh_mesh_if_visible()
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
            self.tabs.setCurrentWidget(self.appearance_tab)
            self.appearance_workspace.show_psd_task()
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
        self._flush_parameter_edit()
        project = self.session.ensure_psd_project()
        self.psd_panel.pending_skin_context = self._skin_context()
        target = project.project_dir / "snapshots" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        result = self.session.export_snapshot(target)
        return str(result["model_path"] if isinstance(result, dict) else result)

    def _psd_export_snapshot_request(self):
        if not self.session:
            raise RuntimeError(_text("editor.live2d.empty"))
        # Capture bytes and pose on the GUI thread; the PSD worker writes the
        # leased snapshot and constructs its own CPU model afterward.
        context = self._skin_context()
        self.psd_panel.pending_skin_context = copy.deepcopy(context)
        return self.session.capture_export_snapshot(skin_source=context)

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
        return bool(self.save_psd_version_as_skin(token))

    def _find_psd_skin(self, token: str):
        if not self.session or not self.psd_panel.current_project:
            return None
        project_file = self.psd_panel.current_project.project_file.relative_to(self.session.root).as_posix()
        record = self.session.find_psd_skin(project_file, token)
        if record is not None:
            record["is_current"] = (record["id"] == self.session.active_skin_id and not self.session.skin_modified)
        return record

    def open_skin_psd_origin(self, skin_id=None) -> bool:
        if not self.session:
            return False
        try:
            if not isinstance(skin_id, str):
                skin_id = self.session.active_skin_id
            origin = self.session.get_skin_origin(skin_id)
            if not origin or origin.get("kind") != "psd":
                return False
            path = (self.session.root / origin["project"]).resolve()
            if not path.is_relative_to(self.session.root.resolve()) or not path.is_file():
                raise ValueError(appearance_text("editor.appearance.origin_missing"))
            if not self.open_psd_workspace(project_file=str(path)):
                return False
            version = origin["version"]
            token = version if ":" in version else f"{origin.get('version_kind', 'repack')}:{version}"
            self.appearance_workspace.show_psd_task("history")
            return self.psd_panel.select_version(token)
        except Exception as exc:
            self._error(exc)
            return False

    def _psd_variant_dialog(self, token: str):
        if not self.session or self._projects_busy():
            return
        record = self._find_psd_skin(token)
        initial = (record or {}).get("name", "PSD " + token.split(":", 1)[-1])
        name = self._skin_name_dialog(skin_text("editor.skin.new_title"), initial + " 2")
        if name:
            self.save_psd_version_as_skin(token, name, variant=True)

    def _psd_version_skin_context(self, token: str):
        from app.core.psd_project import find_composite_entry, find_pose_scheme, find_repack_entry, resolve_project_path
        project = self.psd_panel.current_project
        kind, version_id = token.split(":", 1)
        entry = find_repack_entry(project, version_id) if kind == "repack" else find_composite_entry(project, version_id)
        if not entry:
            return None
        if "skin_source" in entry:
            return copy.deepcopy(entry["skin_source"])
        if kind == "composite":
            return {"inputs": [{"version": item.get("version_id"),
                                "skin": self._psd_version_skin_context("repack:" + item["version_id"])}
                               for item in entry.get("inputs", []) if item.get("version_id")]}
        scheme = find_pose_scheme(project, entry["scheme_id"]) if entry.get("scheme_id") else None
        if scheme and scheme.get("skin_source"):
            return copy.deepcopy(scheme["skin_source"])
        source = resolve_project_path(project, entry["source_psd"]) if entry.get("source_psd") else None
        for exported in project.data.get("psd_exports", []):
            if source and exported.get("psd") and resolve_project_path(project, exported["psd"]) == source:
                return copy.deepcopy(exported.get("skin_source"))
        # Legacy history without provenance must not be attributed to the
        # currently selected skin or the most recent unrelated PSD export.
        return None

    def save_psd_version_as_skin(self, token: str, name: str | None = None, *, variant=False):
        was_preview = bool(self._preview_session or self._preview_request or self._atlas_preview_paths)
        try:
            if not self.session or self._projects_busy():
                return None
            # Exact provenance lookup happens before any PSD preview workspace
            # or source-atlas access. Existing immutable results remain usable
            # even when a temporary preview or the edited PSD was removed.
            record = self._find_psd_skin(token)
            if record and not variant and record.get("is_current"):
                if was_preview and not self.return_to_current_model():
                    return None
                self._status(appearance_text("editor.appearance.psd_current", name=record["name"]))
                return record
            if not self.return_to_current_model(reload=False):
                raise RuntimeError(_text("editor.live2d.return_failed"))
            if record:
                if variant:
                    record = self.session.clone_skin(record["id"], name or record["name"] + " 2")
                    if was_preview:
                        self._reload_preview_textures()
                    self._update_actions()
                    self._status(appearance_text("editor.appearance.variant_saved", name=record["name"]))
                else:
                    changed = self.session.apply_skin(record["id"])
                    if changed or was_preview:
                        self._skin_changed()
                    else:
                        self._update_actions()
                    self._status(appearance_text("editor.appearance.psd_existing", name=record["name"]))
                return record
            model = self._psd_version_workspace(token)
            self.session.validate_texture_package(model)
            existing = {item["name"].casefold() for item in self.session.list_skins()}
            base = name or "PSD " + token.split(":", 1)[1]
            chosen = base
            suffix = 2
            while chosen.casefold() in existing:
                chosen = f"{base} ({suffix})"
                suffix += 1
            metadata = {"project_file": self.psd_panel.current_project.project_file.relative_to(self.session.root).as_posix(),
                        "version": token, "export_skin": self._psd_version_skin_context(token)}
            record = self.session.import_psd_skin(
                model, chosen, project_file=metadata["project_file"], version=token,
                metadata={"export_skin": metadata["export_skin"]}, activate=True, reuse_origin=not variant,
            )
            self._skin_changed()
            if record:
                self._status(skin_text("editor.skin.psd_result", name=record["name"]))
            return record
        except Exception as exc:
            self._error(exc)
            return None
        finally:
            self._finish_native_return()

    def _psd_skin_ready(self, token: str):
        self.save_psd_version_as_skin(token)

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
            self.mod_panel.queue_source_imports([model_path], skin_names={model_path: "PSD " + token.split(":", 1)[1]})
            self.show_viewer_export()
            return True
        except Exception as exc:
            self._error(exc)
            return False

    def _open_psd_preview(self, path: str, project_file: str):
        self._open_workspace_preview(path, _text("editor.live2d.psd_snapshot", name=Path(path).name))

    def _open_workspace_preview(self, path: str, label: str, *, context=None) -> bool:
        if not self.preview or not self.session:
            return False
        self._flush_parameter_edit()
        self._cancel_preview_preparation()
        generation = self._preview_generation
        self._preview_request = {"generation": generation, "path": str(path), "label": label,
                                 "context": dict(context or {})}
        self.timeline.set_playing(False)
        self.task_feedback.set_state({"task": "preview", "state": "running", "busy": True, "progress": 0,
                                      "message": appearance_text("editor.appearance.preview_preparing", name=label)})
        self._update_preview_context()
        self._start_latest_preview_request()
        return True

    def _start_latest_preview_request(self):
        request = self._preview_request
        if self._closing or self._preview_workers or not request:
            return
        generation = request["generation"]
        if generation != self._preview_generation:
            return
        worker = ReadonlyPreviewPrepareThread(generation, request["path"], self)
        self._preview_workers[generation] = worker
        worker.prepared.connect(self._preview_prepared)
        worker.failed.connect(self._preview_prepare_failed)
        worker.finished.connect(lambda generation=generation: self._preview_worker_finished(generation))
        worker.start()

    def _cancel_preview_preparation(self, *, wait=False) -> bool:
        self._preview_generation += 1
        self._preview_request = None
        for worker in list(self._preview_workers.values()):
            worker.cancel()
        if wait:
            deadline = time.monotonic() + .1
            for worker in list(self._preview_workers.values()):
                remaining = max(0, int((deadline - time.monotonic()) * 1000))
                if worker.isRunning() and not worker.wait(remaining):
                    return False
                if worker.candidate is not None:
                    worker.candidate.close()
                    worker.candidate = None
        return True

    def _preview_worker_finished(self, generation):
        worker = self._preview_workers.pop(generation, None)
        if worker is None:
            return
        if worker.candidate is not None:
            worker.candidate.close()
            worker.candidate = None
        if not self._preview_workers and not self._preview_request and self.task_feedback._state.get("task") == "preview" and self.task_feedback._state.get("busy"):
            self.task_feedback.set_state({"task": "preview", "state": "cancelled", "busy": False,
                                          "message": appearance_text("editor.appearance.feedback_cancelled")})
        worker.deleteLater()
        self._start_latest_preview_request()

    def _preview_prepared(self, generation: int, candidate):
        worker = self._preview_workers.get(generation)
        if worker is not None:
            worker.candidate = None  # Transfer exclusive ownership to the GUI.
        request = self._preview_request
        if self._closing or generation != self._preview_generation or not request or not self.session:
            candidate.close()
            return
        previous = self._preview_session
        previous_context = self._preview_context
        previous_mod_id = self._mod_preview_model_id
        try:
            self._preview_session = candidate
            self.clear_artmesh_selection()
            self.preview.load_model(str(candidate.model_path))
            self._native_return_pending = False
            readonly_label = appearance_text("editor.appearance.preview_readonly", name=request["label"])
            self._preview_context = readonly_label
            self._preview_request = None
            self._update_preview_context()
            self._native_model_ready()
            self.workspace.set_panel_visible("preview", True)
            context = request["context"]
            if context.get("mod_model_id"):
                self._mod_preview_model_id = context["mod_model_id"]
                self.mod_panel.note_preview_opened(self._mod_preview_model_id)
            else:
                self._mod_preview_model_id = ""
            if previous:
                previous.close()
            self.task_feedback.set_state({"task": "preview", "state": "succeeded", "busy": False,
                                          "message": readonly_label, "details": request["path"]})
        except Exception as exc:
            self._preview_session = previous
            self._preview_context = previous_context
            self._mod_preview_model_id = previous_mod_id
            candidate.close()
            self._preview_request = None
            try:
                self.preview.load_model(str(previous.model_path if previous else self.session.model_path))
                self._native_model_ready()
            except Exception:
                pass
            self._update_preview_context()
            self._error(exc)

    def _preview_prepare_failed(self, generation: int, error: str):
        if generation == self._preview_generation and not self._closing:
            self._preview_request = None
            self._update_preview_context()
            self.task_feedback.set_state({"task": "preview", "state": "failed", "busy": False,
                                          "message": _text("editor.live2d.error", error=error), "details": error})

    def _show_psd_texture_preview(self, paths):
        paths = [Path(path) for path in (paths or [])]
        if not self.session or not paths:
            return
        self._atlas_preview_paths = paths
        self._atlas_preview_label = self.psd_panel.preview_source_token()
        with QSignalBlocker(self.texture_combo):
            self.texture_combo.clear()
            for index, path in enumerate(paths):
                self.texture_combo.addItem(f"{index}: {path.name}", userData=index)
        self.appearance_workspace.show_skin_task()
        self.tabs.setCurrentWidget(self.appearance_tab)
        self._refresh_texture_image()
        self._update_preview_context()

    def _update_preview_context(self):
        context = self._skin_context()
        if context:
            key = "preview_modified" if context.get("modified") else "preview_current"
            text = appearance_text("editor.appearance." + key, name=context["name"])
            self.editing_context_label.setText(text)
            self.editing_context_label.setToolTip(text + "\n" + skin_text("editor.skin.modified_hint"))
        else:
            self.editing_context_label.setText("")
        self.editing_context_label.setVisible(bool(context))
        request = self._preview_request
        preview_label = (appearance_text("editor.appearance.preview_preparing", name=request["label"]) if request
                         else self._preview_context if self._preview_session
                         else appearance_text("editor.appearance.preview_readonly", name=self._atlas_preview_label)
                         if self._atlas_preview_paths else "")
        self.preview_context_label.setText(preview_label)
        self.preview_context_label.setToolTip(preview_label)
        self.preview_toolbar.setVisible(bool(preview_label))
        self.atlas_context_label.setText(preview_label if self._atlas_preview_paths else self.skin_controls.state_label.text())
        self.atlas_context_label.setToolTip(self.atlas_context_label.text())
        editable_atlas = bool(self.session and not self._atlas_preview_paths)
        for button in (self.replace_texture_button, self.choose_editor_button, self.edit_texture_button):
            button.setEnabled(editable_atlas)

    def return_to_current_model(self, _checked=False, *, reload=True, announce=True) -> bool:
        pending = bool(self._preview_request)
        self._cancel_preview_preparation()
        previous = self._preview_session
        previous_context = self._preview_context
        previous_mod_id = self._mod_preview_model_id
        atlas_preview = bool(self._atlas_preview_paths)
        self._atlas_preview_paths = None
        self._atlas_preview_label = ""
        if atlas_preview and self.session:
            self._populate_textures()
        if not previous:
            if reload and self._native_return_pending:
                self._finish_native_return()
            self._update_preview_context()
            if pending or atlas_preview:
                self.task_feedback.set_state({"task": "preview", "state": "cancelled", "busy": False,
                                              "message": appearance_text("editor.appearance.returned", name=(self._skin_context() or {}).get("name", ""))})
            return True
        try:
            self._preview_session = None
            self.clear_artmesh_selection()
            self._preview_context = ""
            if reload and self.preview and self.session:
                self.preview.load_model(str(self.session.model_path))
                self._native_return_pending = False
                self._native_model_ready()
                self._refresh_mesh_if_visible()
            elif not reload:
                self._native_return_pending = True
            self._mod_preview_model_id = ""
            previous.close()
            self._update_preview_context()
            if announce:
                self.task_feedback.set_state({"task": "preview", "state": "succeeded", "busy": False,
                                              "message": appearance_text("editor.appearance.returned", name=(self._skin_context() or {}).get("name", ""))})
            return True
        except Exception as exc:
            self._preview_session = previous
            self._preview_context = previous_context
            self._mod_preview_model_id = previous_mod_id
            self._update_preview_context()
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
        self._open_workspace_preview(path, label, context={"mod_model_id": model_id})

    def _mod_trigger_selected(self, drawable_id: str):
        if self.session and drawable_id:
            self.artmesh_inspector.select_entry(drawable_id)

    def _mod_pick_scene(self, main_model_id: str):
        model = self.mod_panel.model_by_id(main_model_id)
        if not model or not self._preview_session or self._mod_preview_model_id != main_model_id:
            return None
        source = self.mod_panel.resolve_project_path(str(model.get("model_json") or "")).resolve()
        if self._preview_session.source_path.resolve() != source:
            return None
        canvas = self.preview.live2d_canvas if self.preview else None
        if canvas is None:
            return None
        self.timeline.set_playing(False)
        self.preview.set_motion_frozen(True)
        # Only a picker opening reads back GL pixels. Freeze first, then read
        # the pose used by that draw so the background and hit geometry agree.
        frame = canvas.grabFramebuffer()
        if frame.isNull():
            return None
        scene = self._selection_scene()
        polygon = [list(canvas.windowPointToCanvas(x, y, scene["snapshot"]))
                   for x, y in ((0, 0), (canvas.width(), 0),
                                (canvas.width(), canvas.height()), (0, canvas.height()))]
        return dict(scene, model_id=main_model_id, model_path=str(source),
                    pose_frame=frame, pose_frame_canvas_polygon=polygon)

    def _native_model_ready(self):
        if not self.preview or not self.session:
            return
        self.preview.set_editor_mode(True)
        self.preview.apply_settings({"motion_frozen": True, "mouse_tracking": False,
                                     "auto_blink": False, "auto_breath": False,
                                     "transparent_bg": False, "antialias": True})
        self._apply_preview_pose()
        self._selection_cache = None
        canvas = self.preview.live2d_canvas
        if hasattr(canvas, "setSelectionSceneProvider"):
            canvas.setSelectionSceneProvider(self._selection_scene)
        self._update_selection_actions()
        self.preview.set_rendering_active(self._active)

    def _current_motion(self) -> str | None:
        return self.motion_combo.currentData()

    def _pose(self) -> dict[str, float]:
        if not self.session:
            return {}
        return self.session.pose_at(self._current_motion(), self.timeline.current_time, overrides=self._manual_pose)

    def _apply_preview_pose(self, *, light=False):
        if not self.session or not self.preview:
            return
        values = self._pose()
        render_session = self._preview_session or self.session
        supported = {parameter["id"] for parameter in render_session.parameters}
        values = {key: value for key, value in values.items() if key in supported}
        self.preview.apply_settings({"advanced_enabled": True, "advanced_params": values})
        if not light:
            defaults = {str(part["id"]): float(part.get("opacity", 1))
                        for part in (render_session.mesh_data or {}).get("parts", [])}
            self.preview.set_part_opacity_overrides({key: value for key, value in render_session.part_overrides.items() if key in defaults}, defaults)
            drawables = getattr(self.preview, "set_drawable_opacity_overrides", None)
            if callable(drawables):
                drawables(render_session.drawable_opacity_multipliers())
            self._sync_parameter_values(values)
        elif self._selected_parameter in values:
            self._sync_parameter_values({self._selected_parameter: values[self._selected_parameter]})

    def _populate_parameters(self):
        self._refreshing = True
        self.parameter_table.setRowCount(0)
        self._parameter_spins.clear()
        self._parameter_rows.clear()
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
            self._parameter_rows[parameter_id] = row
            self.parameter_table.setItem(row, 2, QTableWidgetItem(f"{parameter['min']:g} … {parameter['max']:g}"))
            self.parameter_table.setRowHeight(row, 32)
        self._refreshing = False
        if self.parameter_table.rowCount():
            self.parameter_table.setCurrentCell(self._parameter_rows.get(self._selected_parameter, 0), 0)
        self._populate_parameter_selector()
        self._filter_parameters(self.parameter_search.text())

    def _sync_parameter_values(self, values: dict[str, float]):
        for parameter_id, value in values.items():
            spin = self._parameter_spins.get(parameter_id)
            if spin:
                spin._value = float(value)
        for identifier, value in values.items():
            row = self._parameter_rows.get(identifier)
            if row is not None:
                item = self.parameter_table.item(row, 1)
                text = f"{value:.4g}"
                if item.text() != text:
                    item.setText(text)
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
            if str(item.data(Qt.UserRole)) != self._selected_parameter:
                self._flush_parameter_edit()
            self._selected_parameter = str(item.data(Qt.UserRole))
            with QSignalBlocker(self.parameter_combo):
                self.parameter_combo.setCurrentIndex(self.parameter_combo.findData(self._selected_parameter))
            self._sync_parameter_editor()
            self._refresh_track()

    def _populate_parameter_selector(self):
        if not hasattr(self, "parameter_combo"):
            return
        parameters = self.session.parameters if self.session else []
        motion = self._current_motion()
        curves = self.session.project.motions.get(motion, {}).get("Curves", []) if self.session else []
        tracks = {curve["Id"] for curve in curves if curve.get("Target") == "Parameter"}
        with QSignalBlocker(self.parameter_combo):
            self.parameter_combo.clear()
            for parameter in sorted(parameters, key=lambda item: item["id"] not in tracks):
                identifier = parameter["id"]
                name = str(parameter.get("name") or identifier)
                label = identifier if name == identifier else f"{identifier} · {name}"
                self.parameter_combo.addItem(label, userData=identifier)
            self.parameter_combo.setCurrentIndex(self.parameter_combo.findData(self._selected_parameter))
        self.parameter_combo.setEnabled(bool(parameters))

    def _timeline_parameter_changed(self, *_args):
        if self._refreshing:
            return
        identifier = self.parameter_combo.currentData()
        row = self._parameter_rows.get(identifier)
        if row is None:
            return
        self._flush_parameter_edit()
        if self.parameter_table.isRowHidden(row):
            self.parameter_search.clear()
        self.parameter_table.setCurrentCell(row, 0)
        # The current cell may already be the chosen parameter after filtering.
        self._parameter_selected(row)

    def _parameter_drag_started(self):
        self._parameter_dragging = True
        self._parameter_commit_timer.stop()

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is getattr(self, "drawable_controls", None) and event.type() == QEvent.Resize:
            self._arrange_drawable_controls()
        opacity = getattr(self, "drawable_opacity", None)
        if opacity is not None and watched in (opacity, opacity.lineEdit()) and event.type() == QEvent.FontChange:
            self._resize_drawable_opacity()
        slider = getattr(self, "parameter_slider", None)
        if slider is not None and watched in (slider, slider.handle):
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._parameter_drag_started()
                slider.setFocus(Qt.MouseFocusReason)
            elif event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                self._parameter_drag_finished()
        return super().eventFilter(watched, event)

    def _resize_drawable_opacity(self):
        # Fluent's two arrow buttons and editor margins need their own space.
        digits = self.drawable_opacity.lineEdit().fontMetrics().horizontalAdvance("1.00")
        self.drawable_opacity.setFixedWidth(max(148, digits + 104))

    def _arrange_drawable_controls(self):
        compact = self.drawable_controls.width() < 460
        if compact == self._drawable_controls_compact:
            return
        self._drawable_controls_compact = compact
        layout = self.drawable_controls_layout
        widgets = (self.drawable_selection_label, self.drawable_visible,
                   self.drawable_opacity_label, self.drawable_opacity)
        for widget in widgets:
            layout.removeWidget(widget)
        layout.setColumnStretch(0, 1)
        for column in (1, 2, 3):
            layout.setColumnStretch(column, 0)
        for index, widget in enumerate(widgets):
            row, column = divmod(index, 2) if compact else (0, index)
            layout.addWidget(widget, row, column, Qt.AlignRight if index == 1 else Qt.Alignment())

    def _parameter_drag_finished(self):
        self._parameter_dragging = False
        self._flush_parameter_edit()

    def _flush_parameter_edit(self):
        self._parameter_commit_timer.stop()
        if self.session:
            changed = self.session.commit_parameter_preview()
            if changed or self._parameter_edit_active:
                self._parameter_edit_active = False
                self._schedule_mesh_refresh()
                self._update_actions()

    def _parameter_value_changed(self, parameter_id: str, value: float):
        if not self.return_to_current_model():
            return
        if self._refreshing or not self.session:
            return
        try:
            self.timeline.set_playing(False)
            self.session.preview_parameter(parameter_id, value)
            self._parameter_edit_active = True
            self._manual_pose = True
            self._selected_parameter = parameter_id
            self._apply_preview_pose(light=True)
            self.undo_button.setEnabled(self.session.can_undo)
            self.redo_button.setEnabled(self.session.can_redo)
            dirty = self.session.dirty or self.psd_panel._project_dirty or self.mod_panel._dirty
            self.title_label.setText(_text("editor.live2d.title") + (" *" if dirty else ""))
            self.title_label.setToolTip(_text("editor.live2d.modified" if dirty else "editor.live2d.clean"))
            if not self._parameter_dragging:
                self._parameter_commit_timer.start()
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
        self._populate_parameter_selector()
        self._refresh_track()

    def _motion_changed(self, *_args):
        if self._refreshing:
            return
        self._flush_parameter_edit()
        self.timeline.set_playing(False)
        self._manual_pose = False
        if self.session:
            curves = self.session.project.motions.get(self._current_motion(), {}).get("Curves", [])
            identifiers = [curve["Id"] for curve in curves if curve.get("Target") == "Parameter" and curve["Id"] in self._parameter_spins]
            if identifiers and self._selected_parameter not in identifiers:
                self._selected_parameter = identifiers[0]
                with QSignalBlocker(self.parameter_table):
                    self.parameter_table.setCurrentCell(self._parameter_rows[self._selected_parameter], 0)
            self._populate_parameter_selector()
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
        if not self.return_to_current_model():
            return
        self._flush_parameter_edit()
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
        self._flush_parameter_edit()
        self._manual_pose = False
        self._apply_preview_pose()
        self._schedule_mesh_refresh()

    def _play_state_changed(self, playing: bool):
        if playing:
            self._flush_parameter_edit()
            self._manual_pose = False
        if self.preview:
            self.preview.set_motion_frozen(True)

    def _create_motion(self):
        if not self.session or self._action_dialog is not None:
            return
        if not self.return_to_current_model():
            return
        names = set(self.session.project.motions) | set(self.session.project.document["FileReferences"].get("Motions", {}))
        dialog = _MotionDialog(action_text("editor.actions.new"), self.window(), existing_names=names)
        if self._exec_action_dialog(dialog) != QDialog.Accepted:
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
        if not self.session or not self._current_motion() or self._action_dialog is not None:
            return
        if not self.return_to_current_model():
            return
        source = self._current_motion()
        names = set(self.session.project.motions) | set(self.session.project.document["FileReferences"].get("Motions", {}))
        dialog = _MotionDialog(action_text("editor.actions.copy"), self.window(), duration=False, existing_names=names)
        if self._exec_action_dialog(dialog) == QDialog.Accepted:
            try:
                name = dialog.name_edit.text().strip()
                self.session.clone_motion(source, name)
                self._populate_motions(name)
                self._update_actions()
            except Exception as exc:
                self._error(exc)

    def _exec_action_dialog(self, dialog):
        close_editor_popup()
        self.timeline.set_playing(False)
        self._action_dialog = dialog
        try:
            return dialog.exec()
        finally:
            self._action_dialog = None
            dialog.deleteLater()

    def _delete_motion(self):
        if not self.session or not self._current_motion() or self._action_dialog is not None:
            return
        if not self.return_to_current_model():
            return
        name = self._current_motion()
        if self._exec_action_dialog(ActionDeleteDialog(name, self.window())) != QDialog.Accepted:
            return
        try:
            self.session.delete_motion(name)
            self._populate_motions()
            self._apply_preview_pose()
            self._update_actions()
        except Exception as exc:
            self._error(exc)

    def _schedule_mesh_refresh(self):
        if self.tabs.currentWidget() is self.artmesh_tab:
            self._mesh_timer.start()

    def _refresh_mesh_if_visible(self):
        self._inspector_session = None
        if self.tabs.currentWidget() is self.artmesh_tab:
            self._refresh_mesh()

    def _refresh_mesh(self, *, force=False):
        if not self.session:
            return
        try:
            scene = self._selection_scene()
            texture_stamps = []
            for path in scene["texture_paths"]:
                try:
                    stamp = Path(path).stat()
                    texture_stamps.append((path, stamp.st_mtime_ns, stamp.st_size))
                except OSError:
                    texture_stamps.append((path, None, None))
            key = (id(scene["snapshot"]), tuple(texture_stamps))
            if not force and self._inspector_session is (self._preview_session or self.session) and self._inspector_scene_key == key:
                return
            self.artmesh_inspector.load_snapshot(scene["snapshot"], scene["texture_paths"],
                                                 selected_ids=self._selected_drawable_ids)
            self._inspector_session = self._preview_session or self.session
            self._inspector_scene_key = key
        except Exception as exc:
            self._error(exc)

    def _selection_scene(self):
        render_session = self._preview_session or self.session
        if not render_session:
            return {"snapshot": {}, "texture_paths": [], "pose": {"parameters": {}, "parts": {}, "drawables": {}}}
        canvas = self.preview.live2d_canvas if self.preview else None
        pose = canvas.getSelectionPose() if canvas and hasattr(canvas, "getSelectionPose") else {}
        parameters = dict(pose.get("parameters") or {})
        if not parameters:
            parameters = {parameter["id"]: float(parameter["default"]) for parameter in render_session.parameters}
            parameters.update({key: value for key, value in self._pose().items() if key in parameters})
        defaults = {str(part["id"]): float(part.get("opacity", 1))
                    for part in (render_session.mesh_data or {}).get("parts", [])}
        parts = dict(pose.get("parts") or defaults)
        if not pose.get("parts"):
            parts.update({key: value for key, value in render_session.part_overrides.items() if key in defaults})
        drawables = dict(pose.get("drawables", render_session.drawable_opacity_multipliers()))
        key = (id(render_session), tuple(sorted(parameters.items())), tuple(sorted(parts.items())), tuple(sorted(drawables.items())))
        if self._selection_cache is None or self._selection_cache[0] != key:
            snapshot = render_session.exact_pose_mesh(parameters, parts, drawable_opacities=drawables)
            scene = {"snapshot": snapshot, "texture_paths": [str(path) for path in render_session.texture_paths],
                     "pose": {"parameters": parameters, "parts": parts, "drawables": drawables,
                              "parts_complete": bool(pose.get("parts_complete", False))}}
            self._selection_cache = key, scene
        return self._selection_cache[1]

    def set_artmesh_selection_mode(self, mode: str):
        if mode not in ("none", "point", "rectangle"):
            raise ValueError("Selection mode must be none, point or rectangle")
        self._flush_parameter_edit()
        self.timeline.set_playing(False)
        canvas = self.preview.live2d_canvas if self.preview else None
        if canvas and hasattr(canvas, "setSelectionMode"):
            canvas.setSelectionMode(mode)
        with QSignalBlocker(self.point_select_button), QSignalBlocker(self.rect_select_button):
            self.point_select_button.setChecked(mode == "point")
            self.rect_select_button.setChecked(mode == "rectangle")
        if mode == "none":
            self.clear_artmesh_selection()

    def clear_artmesh_selection(self):
        self._selection_region = None
        self._selection_region_key = None
        self._selected_drawable_ids = []
        self._selection_cache = None
        with QSignalBlocker(self.point_select_button), QSignalBlocker(self.rect_select_button):
            self.point_select_button.setChecked(False)
            self.rect_select_button.setChecked(False)
        canvas = self.preview.live2d_canvas if self.preview else None
        if canvas and hasattr(canvas, "setSelectionMode"):
            canvas.setSelectionMode("none")
        self.artmesh_inspector.select_entries([])
        self.artmesh_image.clear()
        self.local_preview_button.setEnabled(False)
        self.part_opacity.setEnabled(False)
        self.part_visible.setEnabled(False)
        self._update_selection_actions()

    def _update_selection_actions(self):
        available = bool(self.session and self.preview)
        self.selection_toolbar.setVisible(available)
        for button in (self.point_select_button, self.rect_select_button):
            button.setEnabled(available)
        self.clear_selection_button.setEnabled(bool(self._selected_drawable_ids or self._selection_region))
        self.export_selection_button.setEnabled(bool(available and not self._preview_session
                                                     and self._selected_drawable_ids and not self._projects_busy()))
        self.artmesh_export_button.setEnabled(self.export_selection_button.isEnabled())
        self.artmesh_export_button.setToolTip(_text("editor.live2d.pick_psd_hint") + "\n" +
                                             _text("editor.live2d.pick_count", count=len(self._selected_drawable_ids)))
        self.selection_count.setText(_text("editor.live2d.pick_count", count=len(self._selected_drawable_ids)))
        self._update_drawable_controls()

    def _artmesh_selection_changed(self, identifiers: list):
        self._selected_drawable_ids = list(dict.fromkeys(str(identifier) for identifier in identifiers))
        # A UV/list/candidate gesture no longer describes the earlier rectangle.
        # Rectangle handlers restore their own region only after final IDs sync.
        self._selection_region = None
        self._selection_region_key = None
        self._update_selection_actions()

    def _native_drawables_picked(self, identifiers: list, mode: str = "replace"):
        self._selection_region = None
        in_mod = self._in_viewer_workspace()
        if not in_mod:
            self.tabs.setCurrentWidget(self.artmesh_tab)
        self._refresh_mesh()
        self.artmesh_inspector.set_pick_candidates(identifiers, selection_mode=mode)
        if in_mod and identifiers and self._mod_preview_model_id:
            self.mod_panel.select_trigger(identifiers[0])

    def _native_region_picked(self, identifiers: list, region: dict, mode: str = "replace"):
        self.tabs.setCurrentWidget(self.artmesh_tab)
        self._refresh_mesh()
        self.artmesh_inspector.apply_selection(identifiers, mode=mode)
        # An additive selection can span several regions and atlas pages.
        # Do not attach only its final rectangle to the complete selection.
        self._selection_region = dict(region) if mode == "replace" else None
        self._selection_region_key = self._selection_cache[0] if self._selection_region and self._selection_cache else None
        self._update_selection_actions()

    def _selection_export_scene(self, identifiers: list):
        scene = self._selection_scene()
        by_id = {item["id"]: item for item in scene["snapshot"].get("drawables", [])}
        parts = dict(scene["pose"]["parts"])
        drawables = dict(scene["pose"].get("drawables") or {})
        hidden_drawables = [identifier for identifier in identifiers if float(drawables.get(identifier, 1)) <= .001]
        hidden = sorted({str(by_id[identifier].get("parent_part_id") or "") for identifier in identifiers
                         if identifier in by_id and parts.get(str(by_id[identifier].get("parent_part_id") or ""), 1) <= .001})
        if hidden or hidden_drawables:
            message = _text("editor.live2d.pick_hidden_export", parts=", ".join(hidden)) if hidden else ""
            if hidden_drawables:
                message += ("\n" if message else "") + _text("editor.live2d.pick_hidden_drawables_export", ids=", ".join(hidden_drawables))
            answer = QMessageBox.question(self.window(), _text("editor.live2d.pick_hidden_title"),
                                          message,
                                          QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
                                          button_texts={QMessageBox.Yes: _text("editor.live2d.pick_restore_export")})
            if answer != QMessageBox.Yes:
                return None
            parts.update({part: 1.0 for part in hidden})
            drawables.update({identifier: 1.0 for identifier in hidden_drawables})
            snapshot = self.session.exact_pose_mesh(scene["pose"]["parameters"], parts, drawable_opacities=drawables)
            pose = dict(scene["pose"], parts=parts, drawables=drawables, export_visibility_restored_parts=hidden,
                        export_visibility_restored_drawables=hidden_drawables)
            scene = dict(scene, snapshot=snapshot, pose=pose)
            by_id = {item["id"]: item for item in snapshot.get("drawables", [])}
        invisible = [identifier for identifier in identifiers if identifier not in by_id or
                     not by_id[identifier].get("visible", True) or float(by_id[identifier].get("opacity", 1)) <= .001]
        if invisible:
            raise RuntimeError(_text("editor.live2d.pick_invisible", ids=", ".join(invisible)))
        return scene

    def export_selected_artmeshes(self) -> bool:
        if not self.session or self._preview_session or not self._selected_drawable_ids or self._projects_busy():
            return False
        try:
            self._flush_parameter_edit()
            self.timeline.set_playing(False)
            identifiers = list(self._selected_drawable_ids)
            scene = self._selection_export_scene(identifiers)
            if scene is None:
                return False
            region = (dict(self._selection_region) if self._selection_region and self._selection_cache
                      and self._selection_region_key == self._selection_cache[0] else None)
            if not self.open_psd_workspace():
                return False
            return self.psd_panel.export_selected_artmeshes(identifiers, scene["pose"], region,
                                                          mesh_data=scene["snapshot"])
        except Exception as exc:
            self._error(exc)
            return False

    def _native_drawable_clicked(self, drawable_id: str):
        # Some wrappers return a Part ID from HitPart. Only an actual drawable
        # match can replace the more precise pose-geometry click selection.
        if self._inspector_session is not (self._preview_session or self.session):
            self._refresh_mesh()
        if any(entry.drawable_id == drawable_id for entry in self.artmesh_inspector.entries):
            in_mod = self._in_viewer_workspace()
            if not in_mod:
                self.tabs.setCurrentWidget(self.artmesh_tab)
            self.artmesh_inspector.select_entry(drawable_id)
            if in_mod and self._mod_preview_model_id:
                self.mod_panel.select_trigger(drawable_id)

    def _native_model_point_clicked(self, normalized_x: float, normalized_y: float):
        if not self.session:
            return
        in_mod = self._in_viewer_workspace()
        if not in_mod:
            self.tabs.setCurrentWidget(self.artmesh_tab)
        # MOD keeps its own tab visible while picking a trigger in the stage.
        # It still needs the current pose geometry for an accurate hit test.
        self._refresh_mesh()
        canvas = ((self._preview_session or self.session).mesh_data or {}).get("canvas", {})
        x = normalized_x * float(canvas.get("width", 1))
        y = normalized_y * float(canvas.get("height", 1))
        from app.gui.ArtMeshInspector import _point_in_triangle
        candidates = []
        render_session = self._preview_session or self.session
        for index, entry in enumerate(self.artmesh_inspector.entries):
            part_id = str(entry.raw.get("parent_part_id") or "")
            if entry.opacity <= .001 or render_session.part_overrides.get(part_id, 1) <= .001:
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
            self.artmesh_image.clear()
            self.part_opacity.setEnabled(False)
            self.part_visible.setEnabled(False)
            self.local_preview_button.setEnabled(False)
            return
        render_session = self._preview_session or self.session
        image = render_session.local_artmesh_image(entry.raw)
        if image:
            data = image.tobytes("raw", "RGBA")
            qimage = QImage(data, image.width, image.height, image.width * 4, QImage.Format_RGBA8888).copy()
            pixmap = QPixmap.fromImage(qimage)
            self.artmesh_image.setPixmap(self._checker_pixmap(pixmap))
        else:
            self.artmesh_image.clear()
        self.local_preview_button.setEnabled(bool(image))
        part_id = str(entry.raw.get("parent_part_id") or "")
        available = bool(not self._preview_session and part_id and int(entry.raw.get("parent_part_index", -1)) >= 0)
        self.part_opacity.setEnabled(available)
        self.part_visible.setEnabled(available)
        opacity = self.session.part_overrides.get(part_id, next((float(p.get("opacity", 1)) for p in (self.session.mesh_data or {}).get("parts", []) if p["id"] == part_id), 1))
        with QSignalBlocker(self.part_opacity), QSignalBlocker(self.part_visible):
            self.part_opacity.setValue(opacity)
            self.part_visible.setChecked(opacity > 0)
        count = sum(str(item.get("parent_part_id") or "") == part_id for item in (self.session.mesh_data or {}).get("drawables", []))
        hint = _text("editor.live2d.part_hint", part=part_id, count=count) if available else _text("editor.live2d.part_unavailable")
        self.part_hint.setText(_text("editor.live2d.part_summary", part=part_id, count=count) if available else "")
        self.part_hint.setToolTip(hint)
        self.part_visible.setToolTip(hint)
        self.part_opacity.setToolTip(_text("editor.live2d.opacity") + "\n" + hint)
        self.artmesh_image.setToolTip(hint)

    @staticmethod
    def _checker_pixmap(source: QPixmap) -> QPixmap:
        tile = QPixmap(16, 16)
        tile.fill(QColor("#dddddd"))
        painter = QPainter(tile)
        painter.fillRect(0, 0, 8, 8, QColor("#aaaaaa"))
        painter.fillRect(8, 8, 8, 8, QColor("#aaaaaa"))
        painter.end()
        result = QPixmap(source.size())
        painter = QPainter(result)
        painter.fillRect(result.rect(), QBrush(tile))
        painter.drawPixmap(0, 0, source)
        painter.end()
        return result

    def _show_local_artmesh_image(self):
        entry = self.artmesh_inspector.current_entry()
        render_session = self._preview_session or self.session
        if not entry or not render_session:
            return
        image = render_session.local_artmesh_image(entry.raw, max_size=None)
        if image is None:
            return
        raw = image.tobytes("raw", "RGBA")
        qimage = QImage(raw, image.width, image.height, image.width * 4, QImage.Format_RGBA8888).copy()
        source = self._checker_pixmap(QPixmap.fromImage(qimage))
        dialog = MessageBoxBase(self.window())
        dialog.setWindowTitle(entry.drawable_id)
        dialog.setMaximumWidth(900)
        dialog.resize(720, 560)
        area = ImageZoomScrollArea(dialog)
        area.setFrameShape(QFrame.NoFrame)
        area.setMinimumSize(260, 220)
        area.setAlignment(Qt.AlignCenter)
        label = QLabel(area)
        label.setAlignment(Qt.AlignCenter)
        area.setWidget(label)
        dialog.viewLayout.addWidget(area, 1)
        toolbar = QHBoxLayout()
        zoom_label = CaptionLabel(dialog)
        zoom = [1.0]
        def set_zoom(value):
            zoom[0] = max(.01, min(8.0, value))
            pixmap = source.scaled(max(1, round(source.width() * zoom[0])), max(1, round(source.height() * zoom[0])),
                                   Qt.KeepAspectRatio, Qt.SmoothTransformation)
            label.setPixmap(pixmap)
            label.setFixedSize(pixmap.size())
            zoom_label.setText(f"{round(zoom[0] * 100)}% · {image.width} × {image.height}")
        def fit():
            set_zoom(min(area.viewport().width() / source.width(), area.viewport().height() / source.height()))
        for icon, tooltip, callback in (
                (FluentIcon.FIT_PAGE, tr("preview.image_fit"), fit),
                (FluentIcon.ZOOM, tr("preview.image_actual"), lambda: set_zoom(1)),
                (FluentIcon.ZOOM_OUT, tr("preview.image_zoom_out_tooltip"), lambda: set_zoom(zoom[0] / 1.25)),
                (FluentIcon.ZOOM_IN, tr("preview.image_zoom_in_tooltip"), lambda: set_zoom(zoom[0] * 1.25))):
            button = TransparentToolButton(icon, dialog)
            button.setFixedSize(28, 28)
            button.setToolTip(tooltip)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
        toolbar.addWidget(zoom_label, 1)
        dialog.viewLayout.addLayout(toolbar)
        area.zoomRequested.connect(lambda factor, _point: set_zoom(zoom[0] * factor))
        dialog.hideCancelButton()
        dialog.yesButton.setText(tr("common.close", default="关闭"))
        QTimer.singleShot(0, fit)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def _update_drawable_controls(self):
        identifiers = list(self._selected_drawable_ids)
        self.drawable_selection_label.setText(_text("editor.live2d.pick_count", count=len(identifiers)))
        self.drawable_selection_label.setToolTip(", ".join(identifiers))
        canvas = self.preview.live2d_canvas if self.preview else None
        supported = getattr(canvas, "supportsDrawableOpacityOverrides", True)
        supported = supported() if callable(supported) else bool(supported)
        enabled = bool(self.session and identifiers and not self._preview_session and supported and not self._projects_busy())
        self.drawable_visible.setEnabled(enabled)
        self.drawable_opacity.setEnabled(enabled)
        if not self.session:
            return
        visibility = {self.session.drawable_visibility_overrides.get(identifier, True) for identifier in identifiers}
        opacities = {self.session.drawable_opacity_overrides.get(identifier, 1.0) for identifier in identifiers}
        with QSignalBlocker(self.drawable_visible), QSignalBlocker(self.drawable_opacity):
            self.drawable_visible.setTristate(len(visibility) > 1)
            self.drawable_visible.setCheckState(Qt.PartiallyChecked if len(visibility) > 1
                                               else Qt.Checked if True in visibility else Qt.Unchecked)
            self.drawable_opacity.setValue(next(iter(opacities)) if len(opacities) == 1 else -.01)

    def _drawable_visibility_changed(self, state):
        if not self.session or self._preview_session or not self._selected_drawable_ids or self._projects_busy():
            return
        try:
            self._flush_parameter_edit()
            self.session.set_drawable_visibility(self._selected_drawable_ids, state != Qt.Unchecked.value)
            self._apply_preview_pose()
            self._schedule_mesh_refresh()
            self._update_drawable_controls()
            self._update_actions()
        except Exception as exc:
            self._error(exc)

    def _drawable_opacity_changed(self, value):
        if not self.session or self._preview_session or not self._selected_drawable_ids or value < 0 or self._projects_busy():
            return
        try:
            self._flush_parameter_edit()
            self.session.set_drawable_opacity(self._selected_drawable_ids, value)
            self._apply_preview_pose()
            self._schedule_mesh_refresh()
            self._update_drawable_controls()
            self._update_actions()
        except Exception as exc:
            self._error(exc)

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
            self._schedule_mesh_refresh()
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

    def _skin_context(self):
        if not self.session:
            return None
        snapshot_provider = getattr(self.session, "get_skin_state_snapshot", None)
        snapshot = snapshot_provider() if callable(snapshot_provider) else None
        records = snapshot.get("entries", []) if isinstance(snapshot, dict) else (
            self.session.list_skins() if hasattr(self.session, "list_skins") else []
        )
        active_id = (snapshot.get("active_id") if isinstance(snapshot, dict)
                     else getattr(self.session, "active_skin_id", None))
        skin = next((item for item in records if item.get("id") == active_id), {})
        name = skin_text("editor.skin.original") if skin.get("is_original") else skin.get("name", "")
        context = copy.deepcopy(skin)
        context.update({"id": active_id, "name": name,
                        "modified": snapshot.get("modified", False) if isinstance(snapshot, dict)
                        else getattr(self.session, "skin_modified", False)})
        if isinstance(snapshot, dict):
            context["revision"] = snapshot.get("revision")
        return context

    def _refresh_skin_controls(self, *, force=False):
        snapshot_provider = getattr(self.session, "get_skin_state_snapshot", None) if self.session else None
        snapshot = snapshot_provider() if callable(snapshot_provider) else None
        records = snapshot.get("entries", []) if isinstance(snapshot, dict) else (
            self.session.list_skins() if self.session and hasattr(self.session, "list_skins") else []
        )
        active = (snapshot.get("active_id") if isinstance(snapshot, dict)
                  else getattr(self.session, "active_skin_id", None)) if records else None
        modified = (snapshot.get("modified", False) if isinstance(snapshot, dict)
                    else getattr(self.session, "skin_modified", False)) if records else False
        busy = self._projects_busy()
        state_key = (id(self.session), (snapshot.get("revision") if isinstance(snapshot, dict) else None), active, modified,
                     bool(records), busy)
        if not force and state_key == self._skin_ui_state_key:
            self.skin_controls.setEnabled(bool(records and not busy))
            self.export_model_button.setEnabled(bool(records and not busy))
            self._update_preview_context()
            return
        self._skin_ui_state_key = state_key
        self.skin_controls.refresh(records, active, modified, enabled=bool(records and not self._projects_busy()))
        with QSignalBlocker(self.export_skin_combo):
            previous = self.export_skin_combo.currentData()
            self.export_skin_combo.clear()
            self.export_skin_combo.addItem(skin_text("editor.skin.current_working"), None)
            for item in records:
                name = skin_text("editor.skin.original") if item.get("is_original") else item["name"]
                self.export_skin_combo.addItem(name, item["id"])
            self.export_skin_combo.setCurrentIndex(max(0, self.export_skin_combo.findData(previous)))
        self.export_model_button.setEnabled(bool(records and not self._projects_busy()))
        self.psd_panel.set_skin_context(self._skin_context())
        self._update_preview_context()

    def _accept_working_textures(self):
        for index in range(len(self.session.texture_paths)):
            self.session.accept_texture_change(index)

    def _skin_changed(self):
        self._reload_preview_textures()
        self._update_actions()
        context = self._skin_context()
        if context:
            self._status(skin_text("editor.skin.applied", name=context["name"]))

    def apply_skin(self, skin_id: str) -> bool:
        if not self.session or self._projects_busy():
            return False
        try:
            was_preview = bool(self._preview_session)
            if not self.return_to_current_model(reload=False):
                return False
            self.timeline.set_playing(False)
            self._accept_working_textures()
            if self.session.apply_skin(skin_id):
                self._skin_changed()
            else:
                if was_preview:
                    self._reload_preview_textures()
                self._refresh_skin_controls()
            return True
        except Exception as exc:
            self._refresh_skin_controls()
            self._error(exc)
            return False
        finally:
            self._finish_native_return()

    def capture_skin(self, name: str):
        if not self.session or self._projects_busy() or not self.return_to_current_model():
            return None
        try:
            self._accept_working_textures()
            record = self.session.capture_skin(name)
            self._update_actions()
            return record
        except Exception as exc:
            self._error(exc)
            return None

    def import_skin(self, source, name: str | None = None, *, mapping=None, source_kind="model", source_metadata=None):
        if not self.session or self._projects_busy() or not self.return_to_current_model(reload=False):
            return None
        try:
            self._accept_working_textures()
            record = self.session.import_skin(source, name, mapping=mapping, source_kind=source_kind,
                                              source_metadata=source_metadata, activate=True)
            self._skin_changed()
            return record
        except Exception as exc:
            self._error(exc)
            return None
        finally:
            self._finish_native_return()

    def _skin_name_dialog(self, title, initial="", allow_name=None):
        close_editor_popup()
        existing = {item["name"] for item in self.session.list_skins()}
        dialog = SkinNameDialog(title, self.window(), existing, initial, allow_name=allow_name)
        result = dialog.exec()
        name = dialog.name_edit.text().strip() if result == QDialog.Accepted else ""
        dialog.deleteLater()
        return name

    def _capture_skin_dialog(self):
        if self.session:
            name = self._skin_name_dialog(skin_text("editor.skin.new_title"))
            if name:
                self.capture_skin(name)

    def _rename_skin_dialog(self):
        if not self.session:
            return
        skin = next(item for item in self.session.list_skins() if item["id"] == self.session.active_skin_id)
        if skin.get("is_original"):
            return
        name = self._skin_name_dialog(skin_text("editor.skin.rename_title"), skin["name"], allow_name=skin["name"])
        if name:
            try:
                self.session.rename_skin(skin["id"], name)
                self._update_actions()
            except Exception as exc:
                self._error(exc)

    def _delete_skin_dialog(self):
        if not self.session:
            return
        skin = next(item for item in self.session.list_skins() if item["id"] == self.session.active_skin_id)
        if skin.get("is_original"):
            return
        answer = QMessageBox.question(self, skin_text("editor.skin.delete_title"),
                                      skin_text("editor.skin.delete_confirm", name=skin["name"]),
                                      QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            try:
                self._accept_working_textures()
                self.session.remove_skin(skin["id"])
                self._skin_changed()
            except Exception as exc:
                self._error(exc)

    def _import_skin_model_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, skin_text("editor.skin.model_picker"), "", "Live2D (*.json *.moc3 *.lpk)")
        if path:
            self.import_skin_files([path])

    def _import_skin_textures_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(self, skin_text("editor.skin.texture_picker"), "", "Images (*.png *.jpg *.jpeg *.webp *.bmp)")
        if paths:
            self.import_skin_files(paths)

    def import_skin_files(self, paths):
        if not self.session or not paths or self._projects_busy():
            return None
        from app.gui.Live2DModPage import TextureMappingDialog
        images = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
        paths = [Path(path) for path in paths]
        name = self._skin_name_dialog(skin_text("editor.skin.import_title"), paths[0].stem)
        if not name:
            return None
        try:
            if len(paths) == 1 and paths[0].suffix.lower() not in images:
                info = self.session.inspect_skin_source(paths[0])
                sources = [Path(texture["path"]) for texture in info["textures"]]
                # A foreign MOC never replaces this model. Its atlas can still
                # be explicitly mapped through the same mature mapping form.
                if info["automatic_mapping"]:
                    return self.import_skin(paths[0], name)
            else:
                sources = paths
            dialog = TextureMappingDialog(name, self.session.texture_paths, sources, [], self.window(),
                                          hint_text=skin_text("editor.skin.mapping_hint"))
            try:
                if dialog.exec() != QDialog.Accepted:
                    return None
                mapping = dict(dialog.mappings())
            finally:
                dialog.deleteLater()
            return self.import_skin(None, name, mapping=mapping, source_kind="textures",
                                    source_metadata={"source_files": [str(path) for path in sources]})
        except Exception as exc:
            self._error(exc)
            return None

    def export_current_skin(self, output_dir: str, skin_id=None):
        if not self.session or self._projects_busy():
            return None
        try:
            self._accept_working_textures()
            result = self.session.export_skin(skin_id, output_dir)
            self._status(skin_text("editor.skin.export_done", path=output_dir))
            return result
        except Exception as exc:
            self._error(exc)
            return None

    def _choose_skin_export(self):
        if not self.session:
            return
        parent = QFileDialog.getExistingDirectory(self, skin_text("editor.skin.export_parent"))
        if parent:
            target = Path(parent) / f"Live2D-{datetime.now():%Y%m%d-%H%M%S-%f}"
            self.export_current_skin(str(target), self.export_skin_combo.currentData())

    def _reset_texture_watcher(self):
        watched = self.texture_watcher.files() + self.texture_watcher.directories()
        if watched:
            self.texture_watcher.removePaths(watched)
        self._texture_indices.clear()
        self._texture_directories.clear()
        self._texture_stats.clear()
        if self.session:
            for index, path in enumerate(self.session.texture_paths):
                self._texture_indices[self._watch_path(path)] = index
                self._texture_directories.setdefault(self._watch_path(path.parent), set()).add(index)
                self._texture_stats[index] = self._texture_stat(path)
            paths = [str(path) for path in self.session.texture_paths if path.is_file()]
            paths += list({str(path.parent) for path in self.session.texture_paths if path.parent.is_dir()})
            if paths:
                self.texture_watcher.addPaths(paths)

    @staticmethod
    def _watch_path(path):
        return str(Path(path).resolve()).casefold()

    @staticmethod
    def _texture_stat(path):
        try:
            stat = Path(path).stat()
            return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino
        except OSError:
            return None

    def _rearm_texture_watcher(self):
        if not self.session:
            return
        watched = {self._watch_path(path) for path in self.texture_watcher.files()}
        missing = [str(path) for path in self.session.texture_paths
                   if self._watch_path(path) not in watched and path.is_file()]
        if missing:
            self.texture_watcher.addPaths(missing)

    def _refresh_texture_image(self, *_args):
        if not self.session or not self.texture_combo.count():
            self.texture_image.clear()
            return
        index = int(self.texture_combo.currentData() or 0)
        paths = self._atlas_preview_paths or self.session.texture_paths
        if not 0 <= index < len(paths):
            return
        pixmap = QPixmap(str(paths[index]))
        self.texture_image.setPixmap(pixmap)
        self.texture_combo.setToolTip(self.texture_combo.currentText())

    def _replace_texture_drop(self, paths):
        if not self.session or self._projects_busy():
            return False
        paths = list(paths)
        if (len(paths) != 1 or not Path(paths[0]).is_file()
                or Path(paths[0]).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}):
            self._error(appearance_text("editor.appearance.atlas_drop_invalid"))
            return False
        return self.replace_texture(int(self.texture_combo.currentData() or 0), str(paths[0]))

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
            if not self.return_to_current_model(reload=False):
                return False
            if self.session.replace_texture(index, path):
                self._reload_preview_textures()
                self._update_actions()
                self._status(appearance_text("editor.appearance.atlas_replaced", index=index))
            else:
                self._status(appearance_text("editor.appearance.atlas_unchanged"))
            return True
        except Exception as exc:
            self._error(exc)
            return False
        finally:
            self._finish_native_return()

    def _choose_external_editor(self):
        path, _ = QFileDialog.getOpenFileName(self, _text("editor.live2d.choose_editor_title"), "", "Application (*.exe);;All files (*)")
        if path:
            self._external_editor = path
            self.editor_label.setText(path)

    def _edit_external_texture(self):
        if not self.session or not self.texture_combo.count():
            return
        if not self.return_to_current_model():
            return
        path = str(self.session.texture_paths[int(self.texture_combo.currentData() or 0)])
        if self._external_editor:
            success, _pid = QProcess.startDetached(self._external_editor, [path], str(self.session.root))
            if not success:
                self._error("Could not start the selected image editor.")
        elif not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            self._error("Could not open the working texture.")

    def _queue_texture_reload(self, path: str):
        if not self.session:
            return
        key = self._watch_path(path)
        index = self._texture_indices.get(key)
        candidates = {index} if index is not None else self._texture_directories.get(key, set())
        changed = {index for index in candidates
                   if self._texture_stat(self.session.texture_paths[index]) != self._texture_stats.get(index)}
        self._rearm_texture_watcher()
        if changed:
            self._pending_textures.update(changed)
            self._texture_timer.start()

    def _process_texture_changes(self):
        if not self.session:
            return
        changed = False
        for index in list(self._pending_textures):
            try:
                changed = self.session.accept_texture_change(index) or changed
                self._texture_stats[index] = self._texture_stat(self.session.texture_paths[index])
                self._pending_textures.discard(index)
                self._texture_attempts.pop(index, None)
            except Exception as exc:
                attempts = self._texture_attempts.get(index, 0) + 1
                self._texture_attempts[index] = attempts
                if attempts >= 4:
                    self._pending_textures.discard(index)
                    self.session.restore_texture(index)
                    self._texture_stats[index] = self._texture_stat(self.session.texture_paths[index])
                    self._error(exc)
        self._rearm_texture_watcher()
        if self._pending_textures:
            self._texture_timer.start()
        if changed:
            self._reload_preview_textures()
            self._update_actions()
            self._status(_text("editor.live2d.texture_reloaded"))

    def _reload_preview_textures(self):
        if self._preview_session or self._preview_request or self._atlas_preview_paths:
            if not self.return_to_current_model(reload=False):
                raise RuntimeError(_text("editor.live2d.return_failed"))
        if self.preview and self.session:
            self.preview.load_model(str(self.session.model_path))
            self._native_return_pending = False
            self._native_model_ready()
        self._refresh_texture_image()
        self._refresh_mesh_if_visible()
        self._reset_texture_watcher()

    def _finish_native_return(self):
        if self._native_return_pending and self.preview and self.session:
            try:
                self._reload_preview_textures()
            except Exception as exc:
                self._error(exc)

    def _capture_editor_view(self):
        entry = self.artmesh_inspector.current_entry()
        return {"motion": self._current_motion(), "parameter": self._selected_parameter,
                "time": self.timeline.current_time, "texture": self.texture_combo.currentIndex(),
                "mesh_texture": self.artmesh_inspector.texture_combo.currentIndex(),
                "mesh": entry.drawable_id if entry else None,
                "meshes": list(self._selected_drawable_ids),
                "scrolls": [(bar, bar.value()) for bar in (
                    self.parameter_table.verticalScrollBar(), self.parameter_table.horizontalScrollBar(),
                    self.artmesh_inspector.entry_list.verticalScrollBar(), self.texture_scroll.verticalScrollBar())]}

    def _restore_editor_view(self, state):
        self._selected_parameter = state["parameter"]
        self._populate_motions(state["motion"])
        with QSignalBlocker(self.parameter_table), QSignalBlocker(self.texture_combo):
            for row in range(self.parameter_table.rowCount()):
                if self.parameter_table.item(row, 0).data(Qt.UserRole) == state["parameter"]:
                    self.parameter_table.setCurrentCell(row, 0)
                    break
            if 0 <= state["texture"] < self.texture_combo.count():
                self.texture_combo.setCurrentIndex(state["texture"])
        self.timeline.set_time(state["time"])
        if state.get("meshes"):
            self.artmesh_inspector.select_entries(state["meshes"], primary_id=state["mesh"])
        elif state["mesh"]:
            self.artmesh_inspector.select_entry(state["mesh"])
        if 0 <= state["mesh_texture"] < self.artmesh_inspector.texture_combo.count():
            self.artmesh_inspector.texture_combo.setCurrentIndex(state["mesh_texture"])
        self._apply_preview_pose()
        self._sync_parameter_editor()
        self._refresh_texture_image()
        for bar, value in state["scrolls"]:
            bar.setValue(value)

    def _history_step(self, redo=False):
        try:
            self._flush_parameter_edit()
            was_preview = bool(self._preview_session)
            if not self.return_to_current_model(reload=False):
                return
            state = self._capture_editor_view()
            textures = tuple(self.session._texture_data) if self.session else ()
            if self.session and (self.session.redo() if redo else self.session.undo()):
                self.timeline.set_playing(False)
                self._manual_pose = bool(self.session.parameter_overrides)
                self._populate_motions(state["motion"])
                self._selection_cache = None
                if was_preview or textures != tuple(self.session._texture_data):
                    self._reload_preview_textures()
                else:
                    self._apply_preview_pose()
                    self._refresh_mesh_if_visible()
                self._restore_editor_view(state)
                self._schedule_mesh_refresh()
                self._update_actions()
        except Exception as exc:
            self._error(exc)
        finally:
            self._finish_native_return()

    def undo(self):
        self._history_step()

    def redo(self):
        self._history_step(redo=True)

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
            self._accept_working_textures()
            if not self.mod_panel.current_project:
                self.mod_panel.bind_project(self.session.ensure_mod_project())
            target = self.session.root / "mod-inputs" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            model_path = str(self.session.export_snapshot(target))
            context = self._skin_context()
            self.mod_panel.queue_source_imports([model_path], skin_names={model_path: context["name"]})
        except Exception as exc:
            self._error(exc)

    def save_copy(self, output_dir: str) -> dict | None:
        if not self.session:
            return None
        try:
            self._flush_parameter_edit()
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
        self._flush_parameter_edit()
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
        if self._preview_workers and not self._cancel_preview_preparation(wait=True):
            self.task_feedback.set_state({"task": "preview", "state": "running", "busy": True,
                                          "message": appearance_text("editor.appearance.preview_stopping")})
            return False
        if self._projects_busy():
            self._status(_text("editor.live2d.project_busy"))
            return False
        return self._confirm_session_changes()

    def set_active(self, active: bool):
        self._active = bool(active)
        if not active:
            self._flush_parameter_edit()
            self._parameter_dragging = False
            self.timeline.set_playing(False)
            self._mesh_timer.stop()
        if self.preview:
            self.preview.set_rendering_active(active and self.workspace.is_panel_visible("preview"))

    def shutdown(self):
        self._closing = True
        self.set_active(False)
        self._texture_timer.stop()
        self._mesh_timer.stop()
        self._parameter_commit_timer.stop()
        self.mod_panel._project_search_timer.stop()
        if not self._cancel_preview_preparation(wait=True):
            self._closing = False
            self.task_feedback.set_state({"task": "preview", "state": "running", "busy": True,
                                          "message": appearance_text("editor.appearance.preview_stopping")})
            return False
        if not self.psd_panel.shutdown():
            self._closing = False
            return False
        worker = self.mod_panel.worker
        if worker and worker.isRunning():
            worker.wait(30000)
            if worker.isRunning():
                self._closing = False
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

    def _appearance_task_changed(self, task: str):
        if task == "skin":
            self._refresh_texture_image()
        self._ensure_appearance_project()

    def _ensure_appearance_project(self):
        """Show defaults immediately while an immutable PSD base is prepared."""
        if not self.session or self._binding_projects or self._projects_busy():
            return False
        if self.psd_panel.current_project:
            return True
        project = self.session.psd_project
        if project:
            self._binding_projects = True
            try:
                self.psd_panel.bind_project(project)
            finally:
                self._binding_projects = False
            return True
        begin = getattr(self.psd_panel, "begin_editor_project_preparation", None)
        if not callable(begin):
            return False
        request = self._psd_export_snapshot_request()
        try:
            if begin(request, self.session.root / "psd", project_name="psd"):
                return True
        except Exception as exc:
            self._error(exc)
        request.close()
        return False

    def _tab_changed(self, *_args):
        if self._binding_projects:
            return
        if hasattr(self, "workspace"):
            index = self.tabs.currentIndex()
            self.workspace.set_task_context(("animation", "selection", "appearance", "export")[index],
                                            timeline_visible=index == 0)
        if self.tabs.currentWidget() is getattr(self, "artmesh_tab", None):
            self._refresh_mesh()
        elif self.tabs.currentWidget() is getattr(self, "appearance_tab", None):
            self._refresh_texture_image()
            self._ensure_appearance_project()
        elif self._in_viewer_workspace() and self.session and not self.mod_panel.current_project:
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
        self.delete_motion_button.setEnabled(bool(session and self._current_motion()))
        self.key_pose_button.setEnabled(bool(session and self._current_motion() and self._selected_parameter))
        self.parameter_tab.setEnabled(bool(session))
        self.artmesh_tab.setEnabled(bool(session))
        self.appearance_tab.setEnabled(bool(session))
        self.mod_tab.setEnabled(bool(session))
        self.motion_combo.setEnabled(bool(session and session.project.motions))
        self._update_selection_actions()
        dirty = bool(session and (session.dirty or self.psd_panel._project_dirty or self.mod_panel._dirty))
        self.title_label.setText(_text("editor.live2d.title") + (" *" if dirty else ""))
        self.title_label.setToolTip(_text("editor.live2d.modified" if dirty else "editor.live2d.clean"))
        self.source_label.setVisible(bool(session))
        self._refresh_skin_controls()

    def _in_viewer_workspace(self):
        return (hasattr(self, "export_tabs") and self.tabs.currentWidget() is self.export_tab
                and self.export_tabs.currentWidget() is self.viewer_tab)

    def show_viewer_export(self):
        self.tabs.setCurrentWidget(self.export_tab)
        self.export_tabs.setCurrentWidget(self.viewer_tab)
        self.workspace.set_panel_visible("details", True)

    def _status(self, text: str):
        self.task_feedback.set_message(text)

    def _task_state_changed(self, state):
        self.task_feedback.set_state(state)
        self._update_actions()

    def _error(self, error):
        self.task_feedback.set_message(_text("editor.live2d.error", error=str(error)), failed=True)

    def showEvent(self, event):
        super().showEvent(event)
        self.set_active(True)

    def hideEvent(self, event):
        self.set_active(False)
        super().hideEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_texture_image()
