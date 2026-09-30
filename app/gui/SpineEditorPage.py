"""Native Spine preview, setup-pose and animation editing workspace."""

from __future__ import annotations

import copy
import io
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QInputDialog, QListWidgetItem,
    QMessageBox, QFrame, QSplitter, QStackedWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QSizePolicy,
)
from qfluentwidgets import (
    BodyLabel as QLabel, CaptionLabel, CheckBox as QCheckBox,
    DoubleSpinBox as QDoubleSpinBox, FluentIcon, ListWidget as QListWidget,
    PrimaryPushButton, PushButton as QPushButton, ScrollArea as QScrollArea,
    SearchLineEdit, SubtitleLabel, TransparentToolButton, TreeWidget as QTreeWidget,
)

from app.core.animation_editing import AnimationEditingError
from app.core.editor_session import BONE_FIELDS, SpineEditorSession
from app.gui.SpinePreviewWidget import SpinePreviewWidget
from app.gui.editor_timeline import AnimationTimelineEditor
from app.gui.editor_workspace import EditorComboBox as QComboBox, EditorTabs, EditorViewportLayout, EditorWorkspace
from app.i18n import get_i18n, tr


SPINE_EDITOR_TEXT = {
    "spine_editor.title": "Spine 预览与动画编辑",
    "spine_editor.open": "打开模型",
    "spine_editor.save": "导出模型副本",
    "spine_editor.undo": "撤销",
    "spine_editor.redo": "重做",
    "spine_editor.empty": "打开或拖入 Spine JSON / SKEL / model0.json，开始预览与编辑",
    "spine_editor.loading": "正在复制模型与依赖资源…",
    "spine_editor.ready": "模型已载入，编辑仅作用于独立副本",
    "spine_editor.modified": "有未导出的修改",
    "spine_editor.saved": "已导出：{path}",
    "spine_editor.failed": "无法打开模型：{error}",
    "spine_editor.error": "编辑失败：{error}",
    "spine_editor.preview_error": "原生预览不可用：{error}",
    "spine_editor.skeleton": "骨骼树",
    "spine_editor.layers": "图层顺序",
    "spine_editor.atlas": "图集部件",
    "spine_editor.animation": "动画",
    "spine_editor.setup": "设置姿态",
    "spine_editor.skin": "皮肤",
    "spine_editor.track": "轨道",
    "spine_editor.duration": "时长 (秒)",
    "spine_editor.new_animation": "新建动作",
    "spine_editor.clone_animation": "复制动作",
    "spine_editor.delete_animation": "删除动作",
    "spine_editor.animation_name": "动作名称",
    "spine_editor.choose_item": "选择骨骼、slot 或图集部件查看属性",
    "spine_editor.bone_setup": "骨骼设置姿态",
    "spine_editor.apply_transform": "应用设置姿态",
    "spine_editor.transform_hint": "平移与旋转关键帧是设置姿态的偏移；缩放关键帧是倍率",
    "spine_editor.slot": "Slot 与 attachment",
    "spine_editor.attachment": "Attachment",
    "spine_editor.visible": "显示 slot",
    "spine_editor.hidden": "（隐藏）",
    "spine_editor.key_slot": "在当前时间写入关键帧",
    "spine_editor.apply_slot": "应用 attachment / 显示状态",
    "spine_editor.delete_slot_key": "删除当前 attachment 关键帧",
    "spine_editor.layer_up": "上移图层",
    "spine_editor.layer_down": "下移图层",
    "spine_editor.order_hint": "上方图层覆盖下方图层；已有 draw-order 动画会保留原顺序语义",
    "spine_editor.region_info": "{name}\n原始尺寸：{width} × {height}",
    "spine_editor.replace_region": "替换部件 PNG",
    "spine_editor.export_region": "导出部件 PNG",
    "spine_editor.region_hint": "替换须保持原始尺寸与裁切范围；旋转及预乘 Alpha 会自动处理",
    "spine_editor.discard_title": "保留当前编辑",
    "spine_editor.discard_message": "当前模型有未导出的修改。是否先导出独立副本？",
    "spine_editor.save_dialog": "导出到新的模型文件夹（输入文件夹名称）",
    "spine_editor.open_dialog": "选择 Spine 骨骼或模型配置",
    "spine_editor.cancelled": "已取消载入模型",
    "spine_editor.rotate": "旋转",
    "spine_editor.translate": "平移",
    "spine_editor.scale": "缩放",
    "spine_editor.shear": "倾斜",
    "spine_editor.advanced": "更多属性",
    "spine_editor.search": "搜索骨骼、插槽或部件",
}


def _text(key: str, **values) -> str:
    return tr(key, SPINE_EDITOR_TEXT[key], **values)


class _TrackComboBox(QComboBox):
    def findData(self, data, role=Qt.ItemDataRole.UserRole, flags=Qt.MatchFlag.MatchExactly | Qt.MatchFlag.MatchCaseSensitive):  # noqa: N802
        # Qt's QVariant comparison does not compare Python tuples, even when
        # itemData returns the same tuple. Tracks are (bone, channel) pairs.
        if isinstance(data, (tuple, list)):
            return next((index for index in range(self.count()) if self.itemData(index) == data), -1)
        return super().findData(data)


class _OpenWorker(QThread):
    completed = Signal(object, object)
    failed = Signal(str)

    def __init__(self, source: str, request: int, parent=None):
        super().__init__(parent)
        self.source, self.request = source, request
        self.session = None

    def run(self):
        try:
            self.session = SpineEditorSession.open(self.source)
            if self.isInterruptionRequested():
                self.session.close()
                self.session = None
                self.failed.emit(_text("spine_editor.cancelled"))
                return
            plan = self.session.prepare_preview()
            self.completed.emit(self.session, plan)
        except Exception as exc:
            if self.session is not None:
                self.session.close()
                self.session = None
            self.failed.emit(str(exc))


class _PreviewWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, session: SpineEditorSession, request: int, parent=None):
        super().__init__(parent)
        self.request = request
        # Document snapshots are immutable to this worker while GUI edits
        # continue. The one preview worker serializes writes to preview_root.
        self.snapshot = copy.copy(session)
        self.snapshot.project = copy.copy(session.project)
        self.snapshot.project.document = copy.deepcopy(session.document)
        self.snapshot._textures = dict(session._textures)

    def run(self):
        try:
            self.completed.emit(self.snapshot.prepare_preview())
        except Exception as exc:
            self.failed.emit(str(exc))


class _BoneInspector(QWidget):
    transformEdited = Signal(str, dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.name = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 5, 4, 8)
        layout.setSpacing(9)
        self.title = CaptionLabel(self)
        layout.addWidget(self.title)
        self.name_label = QLabel(self)
        self.name_label.setWordWrap(True)
        layout.addWidget(self.name_label)
        self.spins = {}
        for field in ("x", "y", "rotation", "scaleX", "scaleY", "shearX", "shearY", "length"):
            spin = QDoubleSpinBox(self)
            spin.setDecimals(4)
            spin.setRange(-1e7, 1e7)
            spin.setSingleStep(0.05 if field.startswith("scale") else 1.0)
            spin.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            spin.setMinimumWidth(110)
            self.spins[field] = spin
        self.group_labels = {}
        for channel, fields in (("translate", ("x", "y")), ("rotate", ("rotation",)), ("scale", ("scaleX", "scaleY"))):
            label = CaptionLabel(self)
            self.group_labels[channel] = label
            layout.addWidget(label)
            row = QHBoxLayout()
            row.setSpacing(8)
            for field in fields:
                self.spins[field].setPrefix(("X " if field in {"x", "scaleX"} else "Y " if field in {"y", "scaleY"} else ""))
                row.addWidget(self.spins[field])
            layout.addLayout(row)
        self.advanced_check = QCheckBox(self)
        layout.addWidget(self.advanced_check)
        self.advanced_panel = QWidget(self)
        advanced_layout = QFormLayout(self.advanced_panel)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        for field in ("shearX", "shearY", "length"):
            advanced_layout.addRow(field, self.spins[field])
        self.advanced_panel.hide()
        self.advanced_check.toggled.connect(self.advanced_panel.setVisible)
        layout.addWidget(self.advanced_panel)
        self.apply_button = QPushButton(FluentIcon.ACCEPT, "", self)
        self.hint = CaptionLabel(self)
        self.hint.hide()
        layout.addWidget(self.apply_button)
        layout.addWidget(self.hint)
        layout.addStretch(1)
        self.apply_button.clicked.connect(lambda: self.transformEdited.emit(self.name, {field: spin.value() for field, spin in self.spins.items()}))
        self.retranslate_ui()

    def set_bone(self, bone: dict):
        self.name = bone["name"]
        self.name_label.setText(self.name)
        for field, spin in self.spins.items():
            spin.setValue(float(bone.get(field, 1 if field.startswith("scale") else 0)))

    def retranslate_ui(self):
        self.title.setText(_text("spine_editor.bone_setup"))
        self.apply_button.setText(_text("spine_editor.apply_transform"))
        self.hint.setText(_text("spine_editor.transform_hint"))
        self.apply_button.setToolTip(_text("spine_editor.transform_hint"))
        self.advanced_check.setText(_text("spine_editor.advanced"))
        for channel, label in self.group_labels.items():
            label.setText(_text(f"spine_editor.{channel}"))


class _SlotInspector(QWidget):
    applyRequested = Signal()
    deleteKeyRequested = Signal()
    moveRequested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.name = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 5, 4, 8)
        layout.setSpacing(9)
        self.title, self.name_label = CaptionLabel(self), QLabel(self)
        self.name_label.setWordWrap(True)
        layout.addWidget(self.title)
        layout.addWidget(self.name_label)
        self.attachment_label = QLabel(self)
        self.attachment_combo = QComboBox(self)
        layout.addWidget(self.attachment_label)
        layout.addWidget(self.attachment_combo)
        self.visible_check = QCheckBox(self)
        self.key_check = QCheckBox(self)
        self.key_check.setChecked(True)
        layout.addWidget(self.visible_check)
        layout.addWidget(self.key_check)
        self.apply_button = QPushButton(FluentIcon.ACCEPT, "", self)
        self.delete_button = TransparentToolButton(FluentIcon.DELETE, self)
        self.delete_button.setFixedSize(30, 30)
        apply_row = QHBoxLayout()
        apply_row.addWidget(self.apply_button, 1)
        apply_row.addWidget(self.delete_button)
        layout.addLayout(apply_row)
        row = QHBoxLayout()
        self.up_button = TransparentToolButton(FluentIcon.UP, self)
        self.down_button = TransparentToolButton(FluentIcon.DOWN, self)
        self.up_button.setFixedSize(32, 32)
        self.down_button.setFixedSize(32, 32)
        self.order_label = CaptionLabel(self)
        row.addWidget(self.order_label)
        row.addWidget(self.up_button)
        row.addWidget(self.down_button)
        layout.addLayout(row)
        row.addStretch(1)
        self.hint = CaptionLabel(self)
        self.hint.hide()
        layout.addWidget(self.hint)
        layout.addStretch(1)
        self.apply_button.clicked.connect(self.applyRequested)
        self.delete_button.clicked.connect(self.deleteKeyRequested)
        self.up_button.clicked.connect(lambda: self.moveRequested.emit(1))
        self.down_button.clicked.connect(lambda: self.moveRequested.emit(-1))
        self.retranslate_ui()

    def set_slot(self, name: str, names: list[str], attachment: str | None, animation: str | None):
        self.name = name
        self.name_label.setText(name)
        self.attachment_combo.clear()
        self.attachment_combo.addItem(_text("spine_editor.hidden"), None)
        for item in names:
            self.attachment_combo.addItem(item, item)
        self.attachment_combo.setCurrentIndex(max(0, self.attachment_combo.findData(attachment)))
        self.visible_check.setChecked(attachment is not None)
        self.key_check.setEnabled(bool(animation))
        self.delete_button.setEnabled(bool(animation))

    def retranslate_ui(self):
        self.title.setText(_text("spine_editor.slot"))
        for widget, key in ((self.attachment_label, "attachment"), (self.visible_check, "visible"), (self.key_check, "key_slot"),
                            (self.apply_button, "apply_slot"), (self.hint, "order_hint"), (self.order_label, "layers")):
            widget.setText(_text(f"spine_editor.{key}"))
        for widget, key in ((self.delete_button, "delete_slot_key"), (self.up_button, "layer_up"), (self.down_button, "layer_down")):
            widget.setToolTip(_text(f"spine_editor.{key}"))
        self.order_label.setToolTip(_text("spine_editor.order_hint"))


class _AtlasInspector(QWidget):
    replaceRequested = Signal()
    exportRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.region = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 5, 4, 8)
        layout.setSpacing(9)
        self.info, self.image_label = QLabel(self), QLabel(self)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumSize(120, 160)
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        layout.addWidget(self.image_label)
        self.replace_button, self.export_button = QPushButton(self), QPushButton(self)
        layout.addWidget(self.replace_button)
        layout.addWidget(self.export_button)
        self.hint = QLabel(self)
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        layout.addStretch(1)
        self.replace_button.clicked.connect(self.replaceRequested)
        self.export_button.clicked.connect(self.exportRequested)
        self.retranslate_ui()

    def set_region(self, item: dict, image):
        self.region = item
        self.info.setText(_text("spine_editor.region_info", name=item["name"], width=item["size"][0], height=item["size"][1]))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        pixmap = QPixmap()
        pixmap.loadFromData(buffer.getvalue())
        self.image_label.setPixmap(pixmap.scaled(240, 220, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def retranslate_ui(self):
        self.replace_button.setText(_text("spine_editor.replace_region"))
        self.export_button.setText(_text("spine_editor.export_region"))
        self.hint.setText(_text("spine_editor.region_hint"))


class SpineEditorPage(QWidget):
    sourceOpened = Signal(str)
    sourceFailed = Signal(str)

    def __init__(self, parent=None, *, layout_settings=None):
        super().__init__(parent)
        self.setObjectName("spineEditorPage")
        self.setAcceptDrops(True)
        self.session: SpineEditorSession | None = None
        self._request = 0
        self._workers: list[QThread] = []
        self._open_worker = None
        self._preview_worker = None
        self._pending_preview = False
        self._updating = False
        self._closing = False
        self._selection = None
        self._track = None
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(240)
        self._preview_timer.timeout.connect(self._start_preview_refresh)
        layout = EditorViewportLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)
        header = QHBoxLayout()
        header.setSpacing(8)
        self.title = SubtitleLabel(self)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title.setMinimumWidth(120)
        header.addWidget(self.title, 1)
        self.source_label = CaptionLabel(self)
        self.source_label.setMaximumWidth(180)
        self.source_label.setMinimumWidth(100)
        self.source_label.hide()
        self.source_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        header.addWidget(self.source_label)
        self.open_button = QPushButton(FluentIcon.FOLDER, "", self)
        self.save_button = PrimaryPushButton(FluentIcon.SAVE, "", self)
        self.undo_button = TransparentToolButton(FluentIcon.RETURN, self)
        self.redo_button = TransparentToolButton(FluentIcon.RIGHT_ARROW, self)
        self.undo_button.setFixedSize(32, 32)
        self.redo_button.setFixedSize(32, 32)
        for button in (self.open_button, self.save_button, self.undo_button, self.redo_button):
            header.addWidget(button)
        layout.addLayout(header)
        self.animation_label, self.skin_label, self.duration_label = CaptionLabel(self), CaptionLabel(self), CaptionLabel(self)
        self.animation_combo, self.skin_combo = QComboBox(self), QComboBox(self)
        self.animation_combo.setMinimumWidth(130)
        self.animation_combo.setMaximumWidth(200)
        self.skin_combo.setFixedWidth(160)
        self.duration_spin = QDoubleSpinBox(self)
        self.duration_spin.setRange(0.001, 100000)
        self.duration_spin.setDecimals(3)
        self.duration_spin.setValue(2)
        self.duration_spin.setFixedWidth(132)
        self.new_button = TransparentToolButton(FluentIcon.ADD, self)
        self.clone_button = TransparentToolButton(FluentIcon.COPY, self)
        self.delete_animation_button = TransparentToolButton(FluentIcon.DELETE, self)
        for button in (self.new_button, self.clone_button, self.delete_animation_button):
            button.setFixedSize(28, 28)
        self.tabs = EditorTabs(self)
        self.tree = QTreeWidget(self)
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(18)
        self.layers = QListWidget(self)
        self.atlas_list = QListWidget(self)
        self.tabs.addTab(self.tree, "")
        self.tabs.addTab(self.layers, "")
        self.tabs.addTab(self.atlas_list, "")
        stage = QWidget(self)
        stage_layout = EditorViewportLayout(stage)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        stage_layout.setSpacing(8)
        view_controls = QHBoxLayout()
        view_controls.addWidget(self.skin_label)
        view_controls.addWidget(self.skin_combo)
        view_controls.addStretch(1)
        stage_layout.addLayout(view_controls)
        self.preview_stack = QStackedWidget(self)
        self.preview_stack.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.preview = SpinePreviewWidget(self)
        self.preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.empty_label = QLabel(self)
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_stack.addWidget(self.empty_label)
        self.preview_stack.addWidget(self.preview)
        stage_layout.addWidget(self.preview_stack, 1)
        self.inspector_stack = QStackedWidget(self)
        self.inspector_stack.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.inspector_empty = QLabel(self)
        self.inspector_empty.setWordWrap(True)
        self.inspector_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bone_inspector, self.slot_inspector, self.atlas_inspector = _BoneInspector(self), _SlotInspector(self), _AtlasInspector(self)
        for widget in (self.inspector_empty, self.bone_inspector, self.slot_inspector, self.atlas_inspector):
            self.inspector_stack.addWidget(widget)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.inspector_stack)
        scroll.setMinimumWidth(220)
        self.details_widget = QWidget(self)
        details_layout = EditorViewportLayout(self.details_widget)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(8)
        self.search_edit = SearchLineEdit(self)
        details_layout.addWidget(self.search_edit)
        self.details_splitter = QSplitter(Qt.Orientation.Vertical, self)
        self.details_splitter.setHandleWidth(7)
        self.details_splitter.setChildrenCollapsible(False)
        self.details_splitter.addWidget(self.tabs)
        self.details_splitter.addWidget(scroll)
        self.details_splitter.setSizes([210, 260])
        self.details_splitter.setStretchFactor(0, 1)
        self.details_splitter.setStretchFactor(1, 1)
        details_layout.addWidget(self.details_splitter, 1)
        timeline_panel = QWidget(self)
        timeline_layout = EditorViewportLayout(timeline_panel)
        timeline_layout.setContentsMargins(0, 0, 0, 0)
        timeline_layout.setSpacing(6)
        track_bar = QHBoxLayout()
        track_bar.setSpacing(6)
        self.track_label = CaptionLabel(self)
        self.track_label.hide()
        self.duration_label.hide()
        self.track_combo, self.channel_combo = _TrackComboBox(self), QComboBox(self)
        self.track_combo.setMinimumWidth(130)
        self.channel_combo.setMinimumWidth(90)
        for widget in (self.animation_label, self.animation_combo, self.new_button, self.clone_button, self.delete_animation_button):
            track_bar.addWidget(widget)
        track_bar.addWidget(self.track_combo, 1)
        track_bar.addWidget(self.channel_combo)
        track_bar.addWidget(self.duration_spin)
        timeline_layout.addLayout(track_bar)
        self.timeline = AnimationTimelineEditor(self)
        self.timeline.set_allowed_interpolations(["linear", "stepped", "bezier"])
        timeline_layout.addWidget(self.timeline, 1)
        self.workspace = EditorWorkspace(stage, self.details_widget, timeline_panel, self,
                                         settings_key="spine", settings=layout_settings)
        self.workspace.timeline_panel.setMinimumHeight(245)
        self.model_splitter = self.workspace.horizontal_splitter
        self.vertical_splitter = self.workspace.vertical_splitter
        self.workspace.panelVisibilityChanged.connect(self._panel_visibility_changed)
        layout.addWidget(self.workspace, 1)
        self.status_label = CaptionLabel(self)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.status_label.setFixedHeight(18)
        layout.addWidget(self.status_label)
        self.open_button.clicked.connect(self._choose_source)
        self.save_button.clicked.connect(self.export_copy)
        self.undo_button.clicked.connect(self.undo)
        self.redo_button.clicked.connect(self.redo)
        self.new_button.clicked.connect(self._new_animation)
        self.clone_button.clicked.connect(lambda: self._new_animation(clone=True))
        self.delete_animation_button.clicked.connect(self._delete_animation)
        self.animation_combo.currentIndexChanged.connect(self._animation_changed)
        self.skin_combo.currentTextChanged.connect(self._skin_changed)
        self.duration_spin.editingFinished.connect(self._duration_changed)
        self.tree.currentItemChanged.connect(self._tree_selected)
        self.layers.currentItemChanged.connect(self._layer_selected)
        self.atlas_list.currentItemChanged.connect(self._atlas_selected)
        self.tabs.currentChanged.connect(self._details_tab_changed)
        self.search_edit.textChanged.connect(self._filter_structure)
        self.track_combo.currentIndexChanged.connect(self._track_changed)
        self.channel_combo.currentIndexChanged.connect(self._channel_changed)
        self.timeline.timeChanged.connect(self._seek)
        self.timeline.framesEdited.connect(self._frames_edited)
        self.bone_inspector.transformEdited.connect(self._transform_edited)
        self.slot_inspector.applyRequested.connect(self._apply_slot)
        self.slot_inspector.deleteKeyRequested.connect(self._delete_slot_key)
        self.slot_inspector.moveRequested.connect(self._move_slot)
        self.atlas_inspector.replaceRequested.connect(self._replace_region)
        self.atlas_inspector.exportRequested.connect(self._export_region)
        self.preview.previewFailed.connect(self._preview_failed)
        self._shortcuts = []
        for key, callback in ((QKeySequence.StandardKey.Undo, self.undo), (QKeySequence.StandardKey.Redo, self.redo), (QKeySequence.StandardKey.Save, self.export_copy)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)
        get_i18n().languageChanged.connect(self.retranslate_ui)
        self.retranslate_ui()
        self._update_actions()

    @property
    def animation_name(self) -> str | None:
        return self.animation_combo.currentData() or None

    @property
    def loading(self) -> bool:
        return bool(self._open_worker and self._open_worker.isRunning())

    def _choose_source(self):
        source, _filter = QFileDialog.getOpenFileName(self, _text("spine_editor.open_dialog"), "", "Spine (*.json *.skel *.bytes)")
        if source:
            self.open_source(source)

    def open_source(self, path: str) -> bool:
        if self._closing:
            self.sourceFailed.emit(path)
            return False
        source = str(Path(path).expanduser().resolve())
        if self.session and source == str(self.session.original_source) and not self.loading:
            self.sourceOpened.emit(source)
            return True
        if not self.confirm_discard_or_save():
            self.sourceFailed.emit(source)
            return False
        self.pause_playback()
        self._request += 1
        worker = _OpenWorker(source, self._request, self)
        self._open_worker = worker
        self._workers.append(worker)
        worker.completed.connect(lambda session, plan, w=worker: self._accept_source(w, session, plan))
        worker.failed.connect(lambda error, w=worker: self._source_failed(w, error))
        worker.finished.connect(lambda w=worker: self._worker_finished(w))
        self.status_label.setText(_text("spine_editor.loading"))
        self._update_actions()
        worker.start()
        return True

    def _accept_source(self, worker, session, plan):
        if self._closing or worker.request != self._request or worker.isInterruptionRequested():
            session.close()
            self.sourceFailed.emit(worker.source)
            return
        # A refresh of the previous session must finish before its resources
        # are removed. It only writes a private snapshot, never a live model.
        if self._preview_worker and self._preview_worker.isRunning():
            self._preview_worker.requestInterruption()
            self._preview_worker.wait()
        self._preview_worker = None
        self._pending_preview = False
        self._preview_timer.stop()
        self.preview.clear_preview(keep_visible=True)
        if self.session:
            self.session.close()
        self.session = session
        worker.session = None  # ownership transfers to the page
        self._open_worker = None
        self._selection, self._track = None, None
        self.timeline.set_time(0)
        self.source_label.setText(session.original_source.name)
        self.source_label.setToolTip(str(session.original_source))
        self.source_label.show()
        self._populate()
        self._load_preview(plan)
        self.status_label.setText(_text("spine_editor.ready"))
        self._update_actions()
        self.sourceOpened.emit(worker.source)

    def _source_failed(self, worker, error: str):
        if worker is self._open_worker:
            self._open_worker = None
            self.status_label.setText(_text("spine_editor.failed", error=error))
            self._update_actions()
        self.sourceFailed.emit(worker.source)

    def _worker_finished(self, worker):
        if worker in self._workers:
            self._workers.remove(worker)
        if worker is self._preview_worker:
            self._preview_worker = None
            if self._pending_preview and not self._closing:
                self._preview_timer.start()
        worker.deleteLater()

    def _populate(self):
        if not self.session:
            return
        animation, skin, selection, track = self.animation_name, self.skin_combo.currentText(), self._selection, self._track
        self._updating = True
        self.animation_combo.clear()
        self.animation_combo.addItem(_text("spine_editor.setup"), "")
        for name in self.session.animation_names:
            self.animation_combo.addItem(name, name)
        index = self.animation_combo.findData(animation)
        self.animation_combo.setCurrentIndex(index if index >= 0 else (1 if self.session.animation_names else 0))
        self.skin_combo.clear()
        self.skin_combo.addItems(self.session.skin_names)
        self.skin_combo.setCurrentIndex(max(0, self.skin_combo.findText(skin)))
        self.tree.clear()
        nodes = {}
        for bone in self.session.document["bones"]:
            item = QTreeWidgetItem([bone["name"]])
            item.setData(0, Qt.ItemDataRole.UserRole, ("bone", bone["name"]))
            nodes[bone["name"]] = item
            parent = nodes.get(bone.get("parent"))
            parent.addChild(item) if parent else self.tree.addTopLevelItem(item)
        for slot in self.session.document.get("slots", []):
            item = QTreeWidgetItem([f"◇ {slot['name']}"])
            item.setData(0, Qt.ItemDataRole.UserRole, ("slot", slot["name"]))
            nodes[slot["bone"]].addChild(item)
        self.tree.expandToDepth(1)
        self.channel_combo.clear()
        channels = ["rotate", "translate", "scale", "shear"]
        if self.session.project.version.startswith("4.0."):
            channels += ["translatex", "translatey", "scalex", "scaley", "shearx", "sheary"]
        for channel in channels:
            self.channel_combo.addItem(_text(f"spine_editor.{channel}") if channel in {"rotate", "translate", "scale", "shear"} else channel, channel)
        self.channel_combo.setCurrentIndex(max(0, self.channel_combo.findData(track[1] if track else "rotate")))
        self.atlas_list.clear()
        for region in self.session.atlas_regions():
            item = QListWidgetItem(region["name"])
            item.setData(Qt.ItemDataRole.UserRole, (region["atlas"], region["id"]))
            self.atlas_list.addItem(item)
        self._updating = False
        self._selection = selection or ("bone", self.session.document["bones"][0]["name"])
        self._refresh_track_list()
        self._refresh_layers()
        self._show_selection()
        self._refresh_track()
        self._update_actions()

    def _refresh_track_list(self):
        self._updating = True
        self.track_combo.clear()
        self.track_combo.addItem(_text("spine_editor.track"), None)
        if self.session and self.animation_name:
            animation = self.session.project._animation(self.animation_name)
            for bone, channels in animation.get("bones", {}).items():
                for channel in channels:
                    self.track_combo.addItem(f"{bone} / {channel}", (bone, channel))
        if self._selection and self._selection[0] == "bone":
            desired = (self._selection[1], self.channel_combo.currentData())
            index = self.track_combo.findData(desired)
            if index < 0:
                self.track_combo.addItem(f"{desired[0]} / {desired[1]}", desired)
                index = self.track_combo.count() - 1
            self.track_combo.setCurrentIndex(index)
        self._updating = False

    def _refresh_track(self):
        if not self.session:
            return
        self._track = self.track_combo.currentData()
        if self.animation_name and self._track:
            data = self.session.get_bone_track(self.animation_name, *self._track)
            frames = data["keyframes"]
            if self.session.project.version.startswith("3.8."):
                for frame in frames:
                    frame["bezier_shared"] = True
            self.timeline.set_track(frames, self.session.duration(self.animation_name), data["fields"], data["editable"])
        else:
            self.timeline.set_track([], self.session.duration(self.animation_name) if self.animation_name else 2, [], False)
        self._updating = True
        self.duration_spin.setValue(self.timeline.duration)
        self._updating = False

    def _animation_changed(self):
        if self._updating or not self.session:
            return
        self.pause_playback()
        self.timeline.set_time(0)
        self._refresh_track_list()
        self._refresh_track()
        self._refresh_layers()
        self._show_selection()
        if self.animation_name:
            self.preview.set_animation(self.animation_name, False)
        else:
            self.preview.reset_pose()
        self.preview.set_paused(True)
        self.preview.set_time(0)
        self._update_actions()

    def _skin_changed(self, name: str):
        if not self._updating and self.session:
            self.preview.set_skin(name)
            self.preview.set_paused(True)
            self.preview.set_time(self.timeline.current_time)
            self._show_selection()

    def _tree_selected(self, item, previous):
        if item is not None and not self._updating:
            self._selection = item.data(0, Qt.ItemDataRole.UserRole)
            self._show_selection()
            if self._selection[0] == "bone":
                self._refresh_track_list()
                self._refresh_track()

    def _layer_selected(self, item, previous):
        if item is not None and not self._updating:
            self._selection = "slot", item.data(Qt.ItemDataRole.UserRole)
            self._show_selection()

    def _atlas_selected(self, item, previous):
        if item is not None and not self._updating:
            self._selection = "atlas", item.data(Qt.ItemDataRole.UserRole)
            self._show_selection()

    def _show_selection(self):
        if not self.session or not self._selection:
            self.inspector_stack.setCurrentWidget(self.inspector_empty)
            return
        kind, name = self._selection
        try:
            if kind == "bone":
                self.bone_inspector.set_bone(self.session.bone(name))
                self.inspector_stack.setCurrentWidget(self.bone_inspector)
            elif kind == "slot":
                attachment = self.session.attachment_at(name, self.animation_name, self.timeline.current_time)
                self.slot_inspector.set_slot(name, self.session.attachments(name, self.skin_combo.currentText() or None), attachment, self.animation_name)
                self.inspector_stack.setCurrentWidget(self.slot_inspector)
            elif kind == "atlas":
                item = self.session._region(*name)
                self.atlas_inspector.set_region(item, self.session.region_image(*name))
                self.inspector_stack.setCurrentWidget(self.atlas_inspector)
        except Exception as exc:
            self._report_error(exc)

    def _details_tab_changed(self, index: int):
        if self._updating:
            return
        if index == 0:
            item = self.tree.currentItem() or self.tree.topLevelItem(0)
            if item:
                self.tree.setCurrentItem(item)
                self._tree_selected(item, None)
        elif index == 1:
            item = self.layers.currentItem() or self.layers.item(0)
            if item:
                self.layers.setCurrentItem(item)
                self._layer_selected(item, None)
        elif index == 2:
            item = self.atlas_list.currentItem() or self.atlas_list.item(0)
            if item:
                self.atlas_list.setCurrentItem(item)
                self._atlas_selected(item, None)

    def _filter_structure(self, text: str):
        query = text.casefold().strip()
        def filter_item(item):
            children = [filter_item(item.child(index)) for index in range(item.childCount())]
            match = query in item.text(0).casefold() or any(children)
            item.setHidden(not match)
            if query and any(children):
                item.setExpanded(True)
            return match
        for index in range(self.tree.topLevelItemCount()):
            filter_item(self.tree.topLevelItem(index))
        for view in (self.layers, self.atlas_list):
            for index in range(view.count()):
                item = view.item(index)
                item.setHidden(bool(query and query not in item.text().casefold()))

    def _panel_visibility_changed(self, panel: str, visible: bool):
        if not visible and panel in {"preview", "timeline"}:
            self.pause_playback()
        if panel == "preview":
            if not visible:
                self.preview._status_timer.stop()
            elif self.preview.model is not None and self.isVisible():
                self.preview._last_tick = time.monotonic()
                self.preview._status_timer.start()
                self.preview._request_canvas_update()

    def _track_changed(self):
        if self._updating or not self.session:
            return
        track = self.track_combo.currentData()
        if track:
            self._selection = "bone", track[0]
            self._updating = True
            self.channel_combo.setCurrentIndex(self.channel_combo.findData(track[1]))
            self._updating = False
            self._show_selection()
        self._refresh_track()

    def _channel_changed(self):
        if not self._updating and self.session:
            self._refresh_track_list()
            self._refresh_track()

    def _seek(self, seconds: float):
        self.preview.set_paused(True)
        self.preview.set_loop(False)
        self.preview.set_time(seconds)
        if self._selection and self._selection[0] == "slot":
            self._show_selection()
        self._refresh_layers()

    def _refresh_layers(self):
        if not self.session:
            return
        order = self.session.draw_order_at(self.animation_name, self.timeline.current_time)
        old = [self.layers.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.layers.count())]
        if old == list(reversed(order)):
            return
        selected = self._selection[1] if self._selection and self._selection[0] == "slot" else None
        self._updating = True
        self.layers.clear()
        for slot in reversed(order):
            item = QListWidgetItem(slot)
            item.setData(Qt.ItemDataRole.UserRole, slot)
            self.layers.addItem(item)
            if slot == selected:
                self.layers.setCurrentItem(item)
        self._updating = False

    def _edit(self, operation, refresh_tree: bool = False):
        if not self.session:
            return False
        try:
            operation()
        except Exception as exc:
            self._report_error(exc)
            self._refresh_track()
            return False
        if refresh_tree:
            self._populate()
        else:
            self._refresh_track_list()
            self._refresh_track()
            self._refresh_layers()
            self._show_selection()
        self._pending_preview = True
        self._preview_timer.start()
        self._update_actions()
        self.status_label.setText(_text("spine_editor.modified") if self.session.dirty else _text("spine_editor.ready"))
        return True

    def _frames_edited(self, frames: list[dict]):
        if self.session and self.animation_name and self._track:
            self._edit(lambda: self.session.set_bone_track(self.animation_name, *self._track, frames))

    def _transform_edited(self, bone: str, values: dict):
        self._edit(lambda: self.session.set_bone_transform(bone, values))

    def _apply_slot(self):
        if not self.session or not self.slot_inspector.name:
            return
        slot = self.slot_inspector.name
        attachment = self.slot_inspector.attachment_combo.currentData() if self.slot_inspector.visible_check.isChecked() else None
        if self.slot_inspector.visible_check.isChecked() and attachment is None:
            attachment = next(iter(self.session.attachments(slot, self.skin_combo.currentText() or None)), None)
        animation = self.animation_name if self.slot_inspector.key_check.isChecked() else None
        self._edit(lambda: self.session.set_slot_attachment(slot, attachment, animation=animation, seconds=self.timeline.current_time))

    def _delete_slot_key(self):
        if self.session and self.animation_name:
            self._edit(lambda: self.session.delete_slot_key(self.slot_inspector.name, self.animation_name, self.timeline.current_time))

    def _move_slot(self, direction: int):
        if not self.session or not self.slot_inspector.name:
            return
        animation = self.animation_name if self.slot_inspector.key_check.isChecked() else None
        order = self.session.draw_order_at(animation, self.timeline.current_time)
        index = order.index(self.slot_inspector.name)
        target = index + direction
        if not 0 <= target < len(order):
            return
        order[index], order[target] = order[target], order[index]
        self._edit(lambda: self.session.set_draw_order(order, animation=animation, seconds=self.timeline.current_time), refresh_tree=not animation)

    def _new_animation(self, checked=False, clone: bool = False):
        if not self.session or (clone and not self.animation_name):
            return
        name, accepted = QInputDialog.getText(self, _text("spine_editor.clone_animation" if clone else "spine_editor.new_animation"), _text("spine_editor.animation_name"))
        if not accepted:
            return
        previous = self.animation_name
        if self._edit(lambda: self.session.clone_animation(previous, name) if clone else self.session.create_animation(name, max(2, self.timeline.duration)), refresh_tree=True):
            self.animation_combo.setCurrentIndex(self.animation_combo.findData(name))

    def _delete_animation(self):
        if self.session and self.animation_name:
            self._edit(lambda: self.session.delete_animation(self.animation_name), refresh_tree=True)

    def _duration_changed(self):
        if not self._updating and self.session and self.animation_name:
            self._edit(lambda: self.session.set_duration(self.animation_name, self.duration_spin.value()))

    def _replace_region(self):
        if not self.session or not self.atlas_inspector.region:
            return
        source, _filter = QFileDialog.getOpenFileName(self, _text("spine_editor.replace_region"), "", "PNG (*.png)")
        item = self.atlas_inspector.region
        if source:
            self._edit(lambda: self.session.replace_atlas_region(item["atlas"], item["id"], source))

    def _export_region(self):
        if not self.session or not self.atlas_inspector.region:
            return
        item = self.atlas_inspector.region
        output, _filter = QFileDialog.getSaveFileName(self, _text("spine_editor.export_region"), item["id"] + ".png", "PNG (*.png)")
        if output:
            try:
                self.session.region_image(item["atlas"], item["id"]).save(output, format="PNG")
            except Exception as exc:
                self._report_error(exc)

    def undo(self):
        if self.session:
            self.pause_playback()
            self._edit(self.session.undo, refresh_tree=True)

    def redo(self):
        if self.session:
            self.pause_playback()
            self._edit(self.session.redo, refresh_tree=True)

    def _start_preview_refresh(self):
        if not self.session or self._closing:
            return
        if self._preview_worker and self._preview_worker.isRunning():
            self._pending_preview = True
            return
        self._pending_preview = False
        worker = _PreviewWorker(self.session, self._request, self)
        self._preview_worker = worker
        self._workers.append(worker)
        worker.completed.connect(lambda plan, w=worker: self._preview_prepared(w, plan))
        worker.failed.connect(lambda error, w=worker: self._preview_failed(error) if w.request == self._request else None)
        worker.finished.connect(lambda w=worker: self._worker_finished(w))
        worker.start()

    def _preview_prepared(self, worker, plan):
        if not self._closing and worker.request == self._request and not self._pending_preview:
            self._load_preview(plan)

    def _load_preview(self, plan):
        if not plan.dynamic or not plan.asset.atlas_paths:
            self.preview.clear_preview(keep_visible=True)
            self.empty_label.setText(_text("spine_editor.preview_error", error=plan.reason or "Skeleton and atlas are required"))
            self.preview_stack.setCurrentWidget(self.empty_label)
            return
        self.preview_stack.setCurrentWidget(self.preview)
        self.preview.open_plan(plan)
        self.preview.set_paused(True)
        self.preview.set_loop(False)
        if self.skin_combo.currentText():
            self.preview.set_skin(self.skin_combo.currentText())
        if self.animation_name:
            self.preview.set_animation(self.animation_name, False)
        else:
            self.preview.reset_pose()
        self.preview.set_time(self.timeline.current_time)
        if not self.workspace.is_panel_visible("preview"):
            self.preview._status_timer.stop()

    def _preview_failed(self, error: str):
        self.status_label.setText(_text("spine_editor.preview_error", error=error))

    def _report_error(self, error):
        self.status_label.setText(_text("spine_editor.error", error=str(error)))

    def _update_actions(self):
        opened = self.session is not None
        busy = self.loading
        self.save_button.setEnabled(opened and not busy)
        self.undo_button.setEnabled(opened and self.session.can_undo and not busy)
        self.redo_button.setEnabled(opened and self.session.can_redo and not busy)
        self.new_button.setEnabled(opened and not busy)
        self.clone_button.setEnabled(opened and bool(self.animation_name) and not busy)
        self.delete_animation_button.setEnabled(opened and bool(self.animation_name) and not busy)
        self.duration_spin.setEnabled(opened and bool(self.animation_name) and not busy)
        self.animation_combo.setEnabled(opened and not busy)
        self.skin_combo.setEnabled(opened and not busy)
        self.model_splitter.setEnabled(not busy)
        self.timeline.setEnabled(opened and not busy)

    def export_copy(self) -> bool:
        if not self.session or self.loading:
            return False
        source = self.session.original_source
        suggested = source.parent.parent / (source.stem + "-edited")
        output, _filter = QFileDialog.getSaveFileName(self, _text("spine_editor.save_dialog"), str(suggested), "")
        if not output:
            return False
        try:
            result = self.session.save_copy(output)
        except Exception as exc:
            self._report_error(exc)
            return False
        self.status_label.setText(_text("spine_editor.saved", path=result["model_path"]))
        self._update_actions()
        return True

    def confirm_discard_or_save(self) -> bool:
        if self.session and self.session.dirty:
            answer = QMessageBox.question(self, _text("spine_editor.discard_title"), _text("spine_editor.discard_message"),
                                          QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                                          QMessageBox.StandardButton.Save)
            if answer == QMessageBox.StandardButton.Cancel:
                return False
            if answer == QMessageBox.StandardButton.Save and not self.export_copy():
                return False
        if self._open_worker:
            self._open_worker.requestInterruption()
        return True

    def pause_playback(self):
        self.timeline.set_playing(False)
        self.preview.set_paused(True)

    def shutdown(self):
        if self._closing:
            return
        self._closing = True
        self.workspace.save_layout()
        self._request += 1
        self.pause_playback()
        self._preview_timer.stop()
        for worker in list(self._workers):
            worker.requestInterruption()
        for worker in list(self._workers):
            worker.wait()
            if isinstance(worker, _OpenWorker) and worker.session is not None:
                worker.session.close()
                worker.session = None
        self.preview.shutdown()
        if self.session:
            self.session.close()
            self.session = None

    def retranslate_ui(self, *args):
        self.title.setText(_text("spine_editor.title"))
        for widget, key in ((self.open_button, "open"), (self.save_button, "save"),
                            (self.animation_label, "animation"), (self.skin_label, "skin"), (self.duration_label, "duration"),
                            (self.track_label, "track"), (self.inspector_empty, "choose_item")):
            widget.setText(_text(f"spine_editor.{key}"))
        for widget, key in ((self.undo_button, "undo"), (self.redo_button, "redo"), (self.new_button, "new_animation"),
                            (self.clone_button, "clone_animation"), (self.delete_animation_button, "delete_animation"),
                            (self.duration_spin, "duration"), (self.track_combo, "track"), (self.channel_combo, "track")):
            widget.setToolTip(_text(f"spine_editor.{key}"))
        self.search_edit.setPlaceholderText(_text("spine_editor.search"))
        self.workspace.retranslate_ui()
        for index, key in enumerate(("skeleton", "layers", "atlas")):
            self.tabs.setTabText(index, _text(f"spine_editor.{key}"))
        self.bone_inspector.retranslate_ui()
        self.slot_inspector.retranslate_ui()
        self.atlas_inspector.retranslate_ui()
        self.timeline.retranslate_ui()
        if self.session is None:
            self.empty_label.setText(_text("spine_editor.empty"))
        else:
            if self.animation_combo.count():
                self.animation_combo.setItemText(0, _text("spine_editor.setup"))
            for index in range(self.channel_combo.count()):
                channel = self.channel_combo.itemData(index)
                if channel in {"rotate", "translate", "scale", "shear"}:
                    self.channel_combo.setItemText(index, _text(f"spine_editor.{channel}"))

    def dragEnterEvent(self, event: QDragEnterEvent):  # noqa: N802
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):  # noqa: N802
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.open_source(paths[0])
            event.acceptProposedAction()

    def hideEvent(self, event):  # noqa: N802
        self.pause_playback()
        super().hideEvent(event)


__all__ = ["SpineEditorPage", "SPINE_EDITOR_TEXT"]
