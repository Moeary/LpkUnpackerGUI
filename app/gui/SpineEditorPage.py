"""Native Spine preview, setup-pose and animation editing workspace."""

from __future__ import annotations

import copy
import io
import time
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QDragEnterEvent, QDropEvent, QPixmap
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QListWidgetItem,
    QFrame, QStackedWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QSizePolicy,
)
from qfluentwidgets import (
    BodyLabel as QLabel, CaptionLabel, CheckBox as QCheckBox,
    DoubleSpinBox as QDoubleSpinBox, FluentIcon, IndeterminateProgressBar, ListWidget as QListWidget,
    Action, PrimaryPushButton, PushButton as QPushButton, RoundMenu, ScrollArea as QScrollArea,
    SearchLineEdit, SegmentedWidget, StrongBodyLabel, SubtitleLabel, TogglePushButton, TransparentPushButton,
    TransparentToolButton, TreeWidget as QTreeWidget, VerticalSeparator,
)

from app.core.animation_editing import AnimationEditingError
from app.core.editor_session import BONE_FIELDS, SpineEditorSession, clone_json
from app.gui.SpinePreviewWidget import SpinePreviewWidget
from app.gui.editor_shortcuts import EditorShortcutRouter
from app.gui.editor_recovery import EditorRecoveryController
from app.gui.editor_timeline import AnimationTimelineEditor
from app.gui.editor_actions import ActionComboBox, ActionDeleteDialog, ActionNameDialog, close_editor_popup
from app.gui.editor_dialogs import EditorMessageBox as QMessageBox
from app.gui.editor_workspace import (
    EditorComboBox as QComboBox, EditorSplitter, EditorTabs, EditorViewportLayout, EditorWorkspace,
    ViewZoomControls,
)
from app.i18n import get_i18n, tr
from app.gui.theme import transparent_scroll_area


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
    "spine_editor.track_placeholder": "选择骨骼轨道",
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
    "spine_editor.psd": "PSD",
    "spine_editor.psd_hint": "把全部图集部件导出为一个 PSD 编辑，再回写到当前模型",
    "spine_editor.psd_export": "导出整体 PSD…",
    "spine_editor.psd_import": "从 PSD 回写…",
    "spine_editor.psd_export_dialog": "选择用于保存 PSD 的空文件夹",
    "spine_editor.psd_import_dialog": "选择之前导出并已编辑的 spine_atlas.psd",
    "spine_editor.psd_exported": "已导出 PSD：{path}（保留同目录的 spine_atlas.json 与 psd_baseline 以便回写）",
    "spine_editor.psd_imported": "已从 PSD 回写 {count} 个部件：{names}",
    "spine_editor.psd_unchanged": "PSD 中没有改动的部件，模型保持不变",
    "spine_editor.psd_exporting": "正在后台导出 PSD，可继续编辑…",
    "spine_editor.psd_importing": "正在后台读取 PSD 回写…",
    "spine_editor.pick_hint": "在预览中单击可选中该处的部件；Ctrl+滚轮或 Ctrl+= / Ctrl+- 缩放，Ctrl+0 复位，按住中键拖动平移",
    "spine_editor.picked": "已选中部件：{name}",
    "spine_editor.pick_none": "单击处没有可见的部件",
    "spine_editor.bone_mode": "骨骼模式",
    "spine_editor.bone_mode_hint": "显示骨骼：白色为 FK 骨骼，橙色为 IK 链，红色十字为 IK 目标，绿色受变换/路径约束。单击优先选中骨骼；拖动旋转，Shift+拖动或切到平移工具则移动，拖动 IK 目标会带动整条 IK 链。设置姿态下修改骨骼设置姿态，动作中在当前时间写入关键帧。",
    "spine_editor.bone_tool": "拖动骨骼时的工具",
    "spine_editor.bone_mode_unavailable": "已安装的 Spine 原生桥不支持骨骼模式，请重新构建 Spine 原生运行时（pixi run native-spine）",
    "spine_editor.bone_picked": "已选中骨骼：{name}",
    "spine_editor.bone_keyed": "已在 {time} 秒为 {bone} 写入{channel}关键帧",
    "spine_editor.bone_setup_posed": "已修改 {bone} 的设置姿态",
    "spine_editor.bone_filter": "骨骼模式显示与可选的骨骼",
    "spine_editor.bone_filter_all": "全部",
    "spine_editor.bone_filter_fk": "仅 FK",
    "spine_editor.bone_filter_ik": "仅 IK",
    "spine_editor.bone_filter_constrained": "仅约束",
    "spine_editor.bone_names": "名称",
    "spine_editor.bone_names_hint": "在每根显示的骨骼旁标注名称；选中的骨骼始终标注",
    "spine_editor.hide_selected": "隐藏 / 显示选中 (H)",
    "spine_editor.hide_selected_hint": "在预览中临时隐藏选中的 slot，全部已隐藏时重新显示（H）。选中骨骼时作用于其下全部 slot，选中图集部件时作用于用到它的 slot；只影响预览，不改变模型与导出",
    "spine_editor.solo_selected": "仅看选中 (Shift+H)",
    "spine_editor.solo_selected_hint": "只显示选中的 slot，其余临时隐藏，再按一次恢复（Shift+H）；只影响预览",
    "spine_editor.show_all": "显示全部 ({count})",
    "spine_editor.show_all_hint": "恢复所有临时隐藏的 slot（Alt+H）",
    "spine_editor.hidden_selected": "已在预览中隐藏 {count} 个 slot",
    "spine_editor.shown_selected": "已重新显示 {count} 个 slot",
    "spine_editor.solo_applied": "仅显示 {count} 个 slot，其余已临时隐藏",
    "spine_editor.shown_all": "已显示全部 slot",
    "spine_editor.hidden_count": "预览中隐藏了 {count} 个 slot",
    "spine_editor.no_slots_selected": "当前选中项下没有 slot",
    "spine_editor.slot_visibility_unavailable": "已安装的 Spine 原生桥不支持临时隐藏部件，请重新构建 Spine 原生运行时（pixi run native-spine）",
    "spine_editor.hide_in_preview": "在预览中临时隐藏 / 显示此 slot（H）",
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


def _session_snapshot(session: SpineEditorSession) -> SpineEditorSession:
    """A copy whose document and textures stay fixed while GUI edits go on."""
    snapshot = copy.copy(session)
    snapshot.project = copy.copy(session.project)
    snapshot.project.document = clone_json(session.document)
    snapshot._textures = dict(session._textures)
    return snapshot


class _PreviewWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, session: SpineEditorSession, request: int, parent=None):
        super().__init__(parent)
        self.request = request
        # The one preview worker serializes writes to preview_root.
        self.snapshot = _session_snapshot(session)

    def run(self):
        try:
            self.completed.emit(self.snapshot.prepare_preview())
        except Exception as exc:
            self.failed.emit(str(exc))


class _PsdWorker(QThread):
    """Export or read back a whole-atlas PSD off the GUI thread.

    PSD encoding/decoding of a large atlas takes seconds; on the GUI thread it
    froze the editor.  The worker only reads a session snapshot; an import's
    page images are applied by the page, on the GUI thread, as one undo step.
    """

    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, session: SpineEditorSession, operation: str, path: str, parent=None):
        super().__init__(parent)
        self.session, self.operation, self.path = session, operation, path
        self.snapshot = _session_snapshot(session)

    def run(self):
        try:
            if self.operation == "export":
                self.completed.emit(self.snapshot.export_atlas_psd(self.path))
            else:
                self.completed.emit(self.snapshot.read_atlas_psd(self.path))
        except Exception as exc:
            self.failed.emit(str(exc))


class _PsdPanel(QWidget):
    """Right-panel PSD tab: whole-atlas export and write-back."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 8, 6, 8)
        layout.setSpacing(8)
        self.hint = CaptionLabel(self)
        self.hint.setWordWrap(True)
        self.export_button = PrimaryPushButton(FluentIcon.SAVE_AS, "", self)
        self.import_button = QPushButton(FluentIcon.SYNC, "", self)
        self.progress = IndeterminateProgressBar(self, start=False)
        self.progress.hide()
        self.result = CaptionLabel(self)
        self.result.setWordWrap(True)
        self.result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        for widget in (self.hint, self.export_button, self.import_button, self.progress, self.result):
            layout.addWidget(widget)
        layout.addStretch(1)

    def set_busy(self, busy: bool) -> None:
        self.progress.setVisible(busy)
        if busy:
            self.progress.start()
        else:
            self.progress.stop()

    def retranslate_ui(self):
        self.hint.setText(_text("spine_editor.psd_hint"))
        self.export_button.setText(_text("spine_editor.psd_export"))
        self.import_button.setText(_text("spine_editor.psd_import"))


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
        self.name_label = StrongBodyLabel(self)
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
    previewHideRequested = Signal()
    applyRequested = Signal()
    deleteKeyRequested = Signal()
    moveRequested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.name = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 5, 4, 8)
        layout.setSpacing(9)
        self.title, self.name_label = CaptionLabel(self), StrongBodyLabel(self)
        self.name_label.setWordWrap(True)
        layout.addWidget(self.title)
        # The slot name with its temporary preview visibility beside it.
        name_row = QHBoxLayout()
        name_row.setSpacing(4)
        name_row.addWidget(self.name_label, 1)
        self.preview_hide_button = TransparentToolButton(FluentIcon.VIEW, self)
        self.preview_hide_button.setFixedSize(30, 30)
        name_row.addWidget(self.preview_hide_button)
        layout.addLayout(name_row)
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
        self.preview_hide_button.clicked.connect(self.previewHideRequested)
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

    def set_preview_hidden(self, hidden: bool):
        self.preview_hide_button.setIcon(FluentIcon.HIDE if hidden else FluentIcon.VIEW)

    def retranslate_ui(self):
        self.title.setText(_text("spine_editor.slot"))
        for widget, key in ((self.attachment_label, "attachment"), (self.visible_check, "visible"), (self.key_check, "key_slot"),
                            (self.apply_button, "apply_slot"), (self.hint, "order_hint"), (self.order_label, "layers")):
            widget.setText(_text(f"spine_editor.{key}"))
        for widget, key in ((self.delete_button, "delete_slot_key"), (self.up_button, "layer_up"), (self.down_button, "layer_down"),
                            (self.preview_hide_button, "hide_in_preview")):
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
        self.last_open_error = ""
        self._request = 0
        self._workers: list[QThread] = []
        self._open_worker = None
        self._preview_worker = None
        self._pending_preview = False
        self._updating = False
        self._closing = False
        self._action_dialog = None
        self._selection = None
        self._track = None
        # Slots hidden in the preview only: view state, never part of the
        # document, its undo history or an export.
        self._hidden_slots: set[str] = set()
        # Order of document edits and hiding steps, so Ctrl+Z undoes whichever
        # came last, as in the Live2D editor.  A document token holds the
        # session's own undo/redo entry and only replays while still on top.
        self._undo_tokens: list[tuple] = []
        self._redo_tokens: list[tuple] = []
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
        self.animation_combo, self.skin_combo = ActionComboBox(self), QComboBox(self)
        self.animation_combo.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.animation_label.hide()
        self.skin_combo.setFixedWidth(160)
        self.duration_spin = QDoubleSpinBox(self)
        self.duration_spin.setRange(0.001, 100000)
        self.duration_spin.setDecimals(3)
        self.duration_spin.setValue(2)
        self.duration_spin.setFixedWidth(132)
        self.duration_spin.setSuffix(" s")
        self.new_button = TransparentToolButton(FluentIcon.ADD, self)
        self.clone_button = TransparentToolButton(FluentIcon.COPY, self)
        self.delete_animation_button = TransparentToolButton(FluentIcon.REMOVE, self)
        for button in (self.new_button, self.clone_button, self.delete_animation_button):
            button.setFixedSize(28, 28)
        self.tabs = EditorTabs(self)
        self.tree = QTreeWidget(self)
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(18)
        self.layers = QListWidget(self)
        self.atlas_list = QListWidget(self)
        for view in (self.tree, self.layers, self.atlas_list):
            view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            view.customContextMenuRequested.connect(lambda pos, v=view: self._structure_menu(v, pos))
        self.tabs.addTab(self.tree, "")
        self.tabs.addTab(self.layers, "")
        self.tabs.addTab(self.atlas_list, "")
        # Whole-atlas PSD round trip, a right-panel tab like the Live2D
        # editor's PSD workbench: export every part, edit, write back.
        self.psd_panel = _PsdPanel(self)
        self.tabs.addTab(self.psd_panel, "")
        self.psd_panel.export_button.clicked.connect(lambda _checked=False: self.export_atlas_psd())
        self.psd_panel.import_button.clicked.connect(lambda _checked=False: self.import_atlas_psd())
        self._psd_worker = None
        stage = QWidget(self)
        stage_layout = EditorViewportLayout(stage)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        stage_layout.setSpacing(8)
        view_controls = QHBoxLayout()
        view_controls.addWidget(self.skin_label)
        view_controls.addWidget(self.skin_combo)
        # Bone mode: draw FK/IK bones over the preview, pick and pose them.
        self.bone_mode_button = TogglePushButton(self)
        self.bone_mode_button.setIcon(FluentIcon.ROBOT)
        self.bone_tool_combo = QComboBox(self)
        self.bone_tool_combo.setFixedWidth(96)
        for tool in ("rotate", "translate"):
            self.bone_tool_combo.addItem(_text(f"spine_editor.{tool}"), tool)
        self.bone_tool_combo.hide()
        # Which bones bone mode draws and picks: all, only FK, only IK, only constrained.
        self.bone_filter_bar = SegmentedWidget(self)
        for kind in SpinePreviewWidget.BONE_FILTERS:
            self.bone_filter_bar.addItem(kind, kind, onClick=lambda _checked=False, k=kind: self.preview.set_bone_filter(k))
        self.bone_filter_bar.setCurrentItem("all")
        self.bone_filter_bar.hide()
        self.bone_names_button = TogglePushButton(self)
        self.bone_names_button.setIcon(FluentIcon.FONT)
        self.bone_names_button.hide()
        self.bone_mode_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        view_controls.addWidget(self.bone_mode_button)
        # Temporary part visibility, like the Live2D editor's ArtMesh hiding.
        self.visibility_separator = VerticalSeparator(self)
        view_controls.addWidget(self.visibility_separator)
        self.hide_button = TransparentToolButton(FluentIcon.HIDE, self)
        self.solo_button = TransparentToolButton(FluentIcon.FILTER, self)
        for button in (self.hide_button, self.solo_button):
            button.setFixedSize(32, 32)
            view_controls.addWidget(button)
        self.show_all_button = TransparentPushButton(FluentIcon.VIEW, "", self)
        self.show_all_button.hide()
        view_controls.addWidget(self.show_all_button)
        view_controls.addStretch(1)
        self.zoom_controls = ViewZoomControls(self)
        view_controls.addWidget(self.zoom_controls)
        view_controls.addSpacing(28)
        stage_layout.addLayout(view_controls)
        # Bone tools get their own row so the stage toolbar never squeezes.
        self.bone_bar = QWidget(self)
        bone_row = QHBoxLayout(self.bone_bar)
        bone_row.setContentsMargins(0, 0, 0, 0)
        bone_row.setSpacing(8)
        for widget in (self.bone_tool_combo, self.bone_filter_bar, self.bone_names_button):
            widget.show()
            bone_row.addWidget(widget)
        bone_row.addStretch(1)
        self.bone_bar.hide()
        stage_layout.addWidget(self.bone_bar)
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
        transparent_scroll_area(scroll)
        scroll.setMinimumWidth(220)
        self.details_widget = QWidget(self)
        details_layout = EditorViewportLayout(self.details_widget)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(8)
        self.search_edit = SearchLineEdit(self)
        search_row = QHBoxLayout()
        search_row.addWidget(self.search_edit, 1)
        search_row.addSpacing(28)
        details_layout.addLayout(search_row)
        self.details_splitter = EditorSplitter(Qt.Orientation.Vertical, self)
        self.details_splitter.setHandleWidth(10)
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
        self.track_combo.setMinimumWidth(80)
        self.track_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.channel_combo.setMinimumWidth(90)
        track_bar.addWidget(self.animation_combo)
        for button in (self.new_button, self.delete_animation_button, self.clone_button):
            track_bar.addWidget(button)
        track_bar.addWidget(self.track_combo, 1)
        self.animation_options_button = TransparentToolButton(FluentIcon.MORE, self)
        self.animation_options_button.setFixedSize(26, 28)
        track_bar.addWidget(self.animation_options_button)
        track_bar.addSpacing(28)
        self.animation_options_menu = RoundMenu(parent=self)
        options = QWidget(self.animation_options_menu)
        options.setFixedSize(280, 42)
        options_layout = QVBoxLayout(options)
        options_layout.setContentsMargins(7, 3, 7, 3)
        channel_row = QHBoxLayout()
        channel_row.addWidget(self.channel_combo, 1)
        channel_row.addWidget(self.duration_spin)
        options_layout.addLayout(channel_row)
        self.animation_options_menu.addWidget(options, selectable=False)
        self.animation_options_button.clicked.connect(lambda: self.animation_options_menu.exec(self.animation_options_button.mapToGlobal(QPoint(0, self.animation_options_button.height()))))
        timeline_layout.addLayout(track_bar)
        self.timeline = AnimationTimelineEditor(self)
        self.timeline.set_allowed_interpolations(["linear", "stepped", "bezier"])
        timeline_layout.addWidget(self.timeline, 1)
        self.workspace = EditorWorkspace(stage, self.details_widget, timeline_panel, self,
                                         settings_key="spine", settings=layout_settings)
        self.workspace.move_toolbar_to(header)
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
        self.slot_inspector.previewHideRequested.connect(self.toggle_selected_visibility)
        self.hide_button.clicked.connect(lambda _checked=False: self.toggle_selected_visibility())
        self.solo_button.clicked.connect(lambda _checked=False: self.solo_selected())
        self.show_all_button.clicked.connect(lambda _checked=False: self.show_all_slots())
        self.slot_inspector.deleteKeyRequested.connect(self._delete_slot_key)
        self.slot_inspector.moveRequested.connect(self._move_slot)
        self.atlas_inspector.replaceRequested.connect(self._replace_region)
        self.atlas_inspector.exportRequested.connect(self._export_region)
        self.preview.previewFailed.connect(self._preview_failed)
        self.preview.set_view_interaction(True)
        self.preview.set_pick_enabled(True)
        self.preview.regionPicked.connect(self._preview_picked)
        self.preview.bonePicked.connect(self._preview_bone_picked)
        self.preview.bonePosed.connect(self._preview_bone_posed)
        self.bone_mode_button.toggled.connect(self._bone_mode_toggled)
        self.bone_names_button.toggled.connect(self.preview.set_bone_names_visible)
        self.bone_tool_combo.currentIndexChanged.connect(
            lambda _index: self.preview.set_bone_tool(self.bone_tool_combo.currentData() or "rotate"))
        self.preview.viewScaleChanged.connect(self.zoom_controls.set_scale)
        self.zoom_controls.zoomInRequested.connect(lambda: self.preview.zoom_by(self.preview.ZOOM_STEP))
        self.zoom_controls.zoomOutRequested.connect(lambda: self.preview.zoom_by(1 / self.preview.ZOOM_STEP))
        self.zoom_controls.resetRequested.connect(self.preview.reset_view)
        self.recovery = EditorRecoveryController(self, "spine")
        self.layout().insertWidget(1, self.recovery.bar)
        self.shortcuts = EditorShortcutRouter(self, "spine")
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
        self.last_open_error = ""
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
        self._hidden_slots.clear()
        self.preview.set_hidden_slots(())
        self._undo_tokens.clear()
        self._redo_tokens.clear()
        self._populate(preserve_view=False)
        self._load_preview(plan)
        self.status_label.setText(_text("spine_editor.ready"))
        self._update_actions()
        self.sourceOpened.emit(worker.source)

    def _source_failed(self, worker, error: str):
        self.last_open_error = str(error) if worker is self._open_worker else ""
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

    def _structure_items(self):
        pending = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while pending:
            item = pending.pop()
            yield item
            pending.extend(item.child(i) for i in range(item.childCount()))

    def _structure_view_state(self):
        """The history owns model data; the user's browsing state stays local."""
        current = self.tree.currentItem()
        return {
            "current": current.data(0, Qt.ItemDataRole.UserRole) if current else None,
            "selected": {item.data(0, Qt.ItemDataRole.UserRole) for item in self.tree.selectedItems()},
            "expanded": {item.data(0, Qt.ItemDataRole.UserRole) for item in self._structure_items() if item.isExpanded()},
            "scroll": (self.tree.horizontalScrollBar().value(), self.tree.verticalScrollBar().value()),
            "filter": self.search_edit.text(),
        }

    def _populate(self, preserve_view=True):
        if not self.session:
            return
        animation, skin, selection, track = self.animation_name, self.skin_combo.currentText(), self._selection, self._track
        tree_state = self._structure_view_state() if preserve_view and self.tree.topLevelItemCount() else None
        atlas_item = self.atlas_list.currentItem() if preserve_view else None
        atlas_key = atlas_item.data(Qt.ItemDataRole.UserRole) if atlas_item else None
        atlas_scroll = (self.atlas_list.horizontalScrollBar().value(), self.atlas_list.verticalScrollBar().value())
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
        bone_icon = FluentIcon.ROBOT.icon()
        for bone in self.session.document["bones"]:
            item = QTreeWidgetItem([bone["name"]])
            item.setIcon(0, bone_icon)
            item.setData(0, Qt.ItemDataRole.UserRole, ("bone", bone["name"]))
            nodes[bone["name"]] = item
            parent = nodes.get(bone.get("parent"))
            parent.addChild(item) if parent else self.tree.addTopLevelItem(item)
        for slot in self.session.document.get("slots", []):
            item = QTreeWidgetItem([slot["name"]])
            item.setData(0, Qt.ItemDataRole.UserRole, ("slot", slot["name"]))
            nodes[slot["bone"]].addChild(item)
        keyed_items = {item.data(0, Qt.ItemDataRole.UserRole): item for item in self._structure_items()}
        if tree_state is None:
            self.tree.expandToDepth(1)
        # Reapply the existing search to the rebuilt nodes; filtering is a view
        # preference and must not be undone together with skeleton edits.
        self._filter_structure(self.search_edit.text())
        if tree_state:
            for key, item in keyed_items.items():
                item.setExpanded(key in tree_state["expanded"])
            current = keyed_items.get(tree_state["current"])
        else:
            current = None
        if selection and selection[0] in {"bone", "slot"} and selection not in keyed_items:
            selection = None
        selection = selection or ("bone", self.session.document["bones"][0]["name"])
        current = current or keyed_items.get(selection) or self.tree.topLevelItem(0)
        if current:
            self.tree.setCurrentItem(current)
        if tree_state:
            selected_keys = tree_state["selected"].intersection(keyed_items)
            if tree_state["current"] not in keyed_items and current:
                selected_keys.add(current.data(0, Qt.ItemDataRole.UserRole))
            for key, item in keyed_items.items():
                item.setSelected(key in selected_keys)
            self.tree.doItemsLayout()
            self.tree.horizontalScrollBar().setValue(tree_state["scroll"][0])
            self.tree.verticalScrollBar().setValue(tree_state["scroll"][1])
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
            if item.data(Qt.ItemDataRole.UserRole) == atlas_key:
                self.atlas_list.setCurrentItem(item)
        if preserve_view:
            self.atlas_list.horizontalScrollBar().setValue(atlas_scroll[0])
            self.atlas_list.verticalScrollBar().setValue(atlas_scroll[1])
        self._updating = False
        self._selection = selection
        # Slots removed by an edit (or undo) are no longer hidden.
        if self._hidden_slots - self._slot_names():
            self._hidden_slots &= self._slot_names()
            self.preview.set_hidden_slots(self._hidden_slots)
        self._refresh_track_list()
        self._refresh_layers()
        self._refresh_visibility_markers()
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
        # A newly created animation belongs to the next snapshot. The current
        # native model still contains the previous document until it is loaded.
        if not self._pending_preview and self._preview_worker is None:
            if self.animation_name:
                self.preview.set_animation(self.animation_name, False)
            else:
                self.preview.show_setup_pose()
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
        self.preview.set_selected_bone(self._selected_bone_name())
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
                self.slot_inspector.set_preview_hidden(name in self._hidden_slots)
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
        self._refresh_visibility_markers()

    def _edit(self, operation, refresh_tree: bool = False, immediate: bool = False, record: bool = True):
        if not self.session:
            return False
        top = self.session._undo[-1] if self.session._undo else None
        try:
            operation()
        except Exception as exc:
            self._report_error(exc)
            self._refresh_track()
            return False
        if record and self.session._undo and self.session._undo[-1] is not top:
            self._push_undo_token(("document", self.session._undo[-1]))
        self._pending_preview = True
        if refresh_tree:
            self._populate()
        else:
            self._refresh_track_list()
            self._refresh_track()
            self._refresh_layers()
            self._show_selection()
        # Typing and dragging edits are coalesced; undo/redo should show the
        # restored pose straight away instead of waiting for the debounce.
        self._preview_timer.start(0 if immediate else 240)
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
        if not self.session or (clone and not self.animation_name) or self._action_dialog is not None:
            return
        dialog = ActionNameDialog(_text("spine_editor.clone_animation" if clone else "spine_editor.new_animation"),
                                  self.window(), duration=not clone, existing_names=self.session.animation_names)
        dialog.duration_spin.setValue(max(2, self.timeline.duration))
        if self._exec_action_dialog(dialog) != QDialog.Accepted:
            return
        name = dialog.name_edit.text().strip()
        previous = self.animation_name
        if self._edit(lambda: self.session.clone_animation(previous, name) if clone else self.session.create_animation(name, dialog.duration_spin.value()), refresh_tree=True):
            self.animation_combo.setCurrentIndex(self.animation_combo.findData(name))

    def _delete_animation(self):
        if self.session and self.animation_name and self._action_dialog is None:
            name = self.animation_name
            if self._exec_action_dialog(ActionDeleteDialog(name, self.window())) == QDialog.Accepted:
                self._edit(lambda: self.session.delete_animation(name), refresh_tree=True)

    def _exec_action_dialog(self, dialog):
        close_editor_popup()
        self.pause_playback()
        self._action_dialog = dialog
        try:
            return dialog.exec()
        finally:
            self._action_dialog = None
            dialog.deleteLater()

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

    # Whole-atlas PSD round trip ------------------------------------------
    @property
    def psd_busy(self) -> bool:
        # Cleared on ``finished``, after the result has been delivered.
        return self._psd_worker is not None

    def export_atlas_psd(self, output: str | None = None) -> bool:
        """Start a background PSD export; True when the export started."""
        if not self.session or self.loading or self.psd_busy:
            return False
        if output is None:
            output = QFileDialog.getExistingDirectory(self, _text("spine_editor.psd_export_dialog"),
                                                      str(self.session.original_source.parent))
        if not output:
            return False
        return self._start_psd_worker("export", output)

    def import_atlas_psd(self, source: str | None = None) -> bool:
        """Start reading an edited PSD back; True when the import started."""
        if not self.session or self.loading or self.psd_busy:
            return False
        if source is None:
            source, _filter = QFileDialog.getOpenFileName(self, _text("spine_editor.psd_import_dialog"),
                                                          str(self.session.original_source.parent), "PSD (*.psd)")
        if not source:
            return False
        return self._start_psd_worker("import", source)

    def _start_psd_worker(self, operation: str, path: str) -> bool:
        worker = _PsdWorker(self.session, operation, path, self)
        worker.completed.connect(lambda result, worker=worker: self._psd_finished(worker, result))
        worker.failed.connect(lambda error, worker=worker: self._psd_failed(worker, error))
        worker.finished.connect(lambda worker=worker: self._psd_worker_done(worker))
        self._psd_worker = worker
        self._workers.append(worker)
        self.psd_panel.set_busy(True)
        self.psd_panel.result.clear()
        self.status_label.setText(_text("spine_editor.psd_exporting" if operation == "export" else "spine_editor.psd_importing"))
        self._update_actions()
        worker.start()
        return True

    def _psd_finished(self, worker: _PsdWorker, result):
        if self._closing or worker.session is not self.session:
            return  # the model was closed or replaced meanwhile
        if worker.operation == "export":
            message = _text("spine_editor.psd_exported", path="; ".join(str(path) for path in result))
        else:
            updates, names = result
            if not names:
                message = _text("spine_editor.psd_unchanged")
            elif self._edit(lambda: self.session.apply_texture_edits(updates)):
                message = _text("spine_editor.psd_imported", count=len(names),
                                names=", ".join(names[:8]) + ("…" if len(names) > 8 else ""))
            else:
                return  # _edit() reported the error
        self.status_label.setText(message)
        self.psd_panel.result.setText(message)

    def _psd_failed(self, worker: _PsdWorker, error: str):
        if not self._closing and worker.session is self.session:
            self._report_error(error)
            self.psd_panel.result.setText(self.status_label.text())

    def _psd_worker_done(self, worker: _PsdWorker):
        if worker in self._workers:
            self._workers.remove(worker)
        if self._psd_worker is worker:
            self._psd_worker = None
        worker.deleteLater()
        if not self._closing:
            self.psd_panel.set_busy(False)
            self._update_actions()

    # Click-to-pick in the preview ----------------------------------------
    def _preview_picked(self, result):
        if not self.session:
            return
        if result is None:
            self.status_label.setText(_text("spine_editor.pick_none"))
            return
        if self.bone_mode_button.isChecked():
            slot = self._slot_for_region(result.name)
            bone = next((item.get("bone") for item in self.session.document.get("slots", [])
                         if item.get("name") == slot), None) if slot else None
            if bone:
                self._preview_bone_picked(bone)
                return
        tab = self.tabs.currentIndex()
        if tab == 2:
            item = next((self.atlas_list.item(index) for index in range(self.atlas_list.count())
                         if self.atlas_list.item(index).data(Qt.ItemDataRole.UserRole)[1] == result.region_id), None)
            if item is not None:
                self.atlas_list.setCurrentItem(item)
                self.atlas_list.scrollToItem(item)
        else:
            slot = self._slot_for_region(result.name)
            if slot is None:
                self.status_label.setText(_text("spine_editor.pick_none"))
                return
            if tab == 1:
                item = next((self.layers.item(index) for index in range(self.layers.count())
                             if self.layers.item(index).data(Qt.ItemDataRole.UserRole) == slot), None)
                if item is not None:
                    self.layers.setCurrentItem(item)
                    self.layers.scrollToItem(item)
            else:
                item = next((node for node in self._structure_items()
                             if node.data(0, Qt.ItemDataRole.UserRole) == ("slot", slot)), None)
                if item is not None:
                    self.tree.setCurrentItem(item)
                    self.tree.scrollToItem(item)
        self.status_label.setText(_text("spine_editor.picked", name=result.name))

    # Temporary slot visibility -------------------------------------------
    @property
    def hidden_slots(self) -> frozenset[str]:
        return frozenset(self._hidden_slots)

    def _slot_names(self) -> set[str]:
        return {slot["name"] for slot in self.session.document.get("slots", [])} if self.session else set()

    def _visibility_targets(self) -> list[str]:
        """Slots the selection stands for: the slot itself, every slot under a
        selected bone, or every slot that can show a selected atlas part."""
        if not self.session or not self._selection:
            return []
        kind, name = self._selection
        slots = self.session.document.get("slots", [])
        if kind == "slot":
            return [name]
        if kind == "bone":
            children = {}
            for bone in self.session.document["bones"]:
                children.setdefault(bone.get("parent"), []).append(bone["name"])
            bones, pending = set(), [name]
            while pending:
                bone = pending.pop()
                bones.add(bone)
                pending.extend(children.get(bone, ()))
            return [slot["name"] for slot in slots if slot.get("bone") in bones]
        if kind == "atlas":
            try:
                region = self.session._region(*name)["name"]
            except Exception:
                return []
            users = {slot for slot, _attachment in self._region_attachments(region)}
            return [slot["name"] for slot in slots if slot["name"] in users]
        return []

    def _region_attachments(self, region: str) -> list[tuple[str, str]]:
        """(slot, attachment) pairs of the current skin that can draw ``region``."""
        raw = self.session.document.get("skins", {})
        skins = [(item.get("name"), item.get("attachments", {})) for item in raw] if isinstance(raw, list) else list(raw.items())
        skin = self.skin_combo.currentText()
        result = []
        for name, attachments in skins:
            if skin and name not in {skin, "default"}:
                continue
            for slot, entries in (attachments or {}).items():
                for attachment, data in (entries or {}).items():
                    data = data if isinstance(data, dict) else {}
                    if (data.get("path") or data.get("name") or attachment) == region:
                        result.append((slot, attachment))
        return result

    def _set_hidden_slots(self, names, message: str) -> None:
        """A user hiding step: one undo step, ordered with document edits."""
        before, after = frozenset(self._hidden_slots), frozenset(set(names) & self._slot_names())
        if after != before:
            self._push_undo_token(("visibility", before, after))
        self._apply_hidden_slots(after)
        if self.preview.model is not None and not self.preview.slot_visibility_supported:
            message = _text("spine_editor.slot_visibility_unavailable")
        self.status_label.setText(message)
        self._update_actions()

    def _apply_hidden_slots(self, names) -> None:
        self._hidden_slots = set(names) & self._slot_names()
        self.preview.set_hidden_slots(self._hidden_slots)
        self._refresh_visibility_markers()
        if self._selection and self._selection[0] == "slot":
            self.slot_inspector.set_preview_hidden(self._selection[1] in self._hidden_slots)
        self.status_label.setText(_text("spine_editor.hidden_count", count=len(self._hidden_slots)))

    def toggle_selected_visibility(self) -> bool:
        """H: hide the selected slots in the preview, or show them when all are hidden.

        Only the preview changes; the document and exports keep every slot.
        Like the Live2D editor's ArtMesh hiding, each step is undone and
        redone with Ctrl+Z / Ctrl+Shift+Z in order with document edits, yet
        never marks the model modified.  Hidden slots are skipped by picking.
        """
        if not self.session or self.loading:
            return False
        targets = self._visibility_targets()
        if not targets:
            self.status_label.setText(_text("spine_editor.no_slots_selected"))
            return False
        hide = any(slot not in self._hidden_slots for slot in targets)
        hidden = self._hidden_slots | set(targets) if hide else self._hidden_slots - set(targets)
        self._set_hidden_slots(hidden, _text("spine_editor.hidden_selected" if hide else "spine_editor.shown_selected",
                                             count=len(targets)))
        return True

    def solo_selected(self) -> bool:
        """Shift+H: show only the selected slots; again restores every slot."""
        if not self.session or self.loading:
            return False
        targets = set(self._visibility_targets())
        if not targets:
            self.status_label.setText(_text("spine_editor.no_slots_selected"))
            return False
        others = self._slot_names() - targets
        if self._hidden_slots and self._hidden_slots == others:
            return self.show_all_slots()
        self._set_hidden_slots(others, _text("spine_editor.solo_applied", count=len(targets)))
        return True

    def show_all_slots(self) -> bool:
        """Alt+H: draw every slot again."""
        if not self.session or not self._hidden_slots:
            return False
        self._set_hidden_slots((), _text("spine_editor.shown_all"))
        return True

    def _refresh_visibility_markers(self) -> None:
        """Mark hidden slots in the tree and layer list with a closed eye."""
        hidden = self._hidden_slots
        dim = QBrush(QColor(128, 128, 128))
        slot_icon, hidden_icon = FluentIcon.PHOTO.icon(), FluentIcon.HIDE.icon()
        for item in self._structure_items():
            kind, name = item.data(0, Qt.ItemDataRole.UserRole)
            if kind == "slot":
                item.setIcon(0, hidden_icon if name in hidden else slot_icon)
                item.setData(0, Qt.ItemDataRole.ForegroundRole, dim if name in hidden else None)
        for index in range(self.layers.count()):
            item = self.layers.item(index)
            name = item.data(Qt.ItemDataRole.UserRole)
            item.setIcon(hidden_icon if name in hidden else slot_icon)
            item.setData(Qt.ItemDataRole.ForegroundRole, dim if name in hidden else None)
        self.show_all_button.setText(_text("spine_editor.show_all", count=len(hidden)))
        self.show_all_button.setVisible(bool(hidden))

    def _structure_menu(self, view, position) -> None:
        """Right-click menu of the tree and lists: hide, solo, show all."""
        if not self.session:
            return
        item = view.itemAt(position)
        if item is not None:
            view.setCurrentItem(item)
        menu = RoundMenu(parent=self)
        targets = self._visibility_targets()
        hide = not targets or any(slot not in self._hidden_slots for slot in targets)
        toggle = Action(FluentIcon.HIDE if hide else FluentIcon.VIEW, _text("spine_editor.hide_selected"), menu)
        toggle.setEnabled(bool(targets))
        toggle.triggered.connect(lambda: self.toggle_selected_visibility())
        solo = Action(FluentIcon.FILTER, _text("spine_editor.solo_selected"), menu)
        solo.setEnabled(bool(targets))
        solo.triggered.connect(lambda: self.solo_selected())
        show_all = Action(FluentIcon.VIEW, _text("spine_editor.show_all", count=len(self._hidden_slots)), menu)
        show_all.setEnabled(bool(self._hidden_slots))
        show_all.triggered.connect(lambda: self.show_all_slots())
        menu.addActions([toggle, solo])
        menu.addSeparator()
        menu.addAction(show_all)
        menu.exec(view.viewport().mapToGlobal(position))

    # Bone mode ------------------------------------------------------------
    def _selected_bone_name(self) -> str:
        if not self.session or not self._selection:
            return ""
        kind, name = self._selection
        if kind == "bone":
            return name
        if kind == "slot":
            return next((item.get("bone", "") for item in self.session.document.get("slots", [])
                         if item.get("name") == name), "")
        return ""

    def _bone_mode_toggled(self, checked: bool):
        self.bone_bar.setVisible(checked)
        self.preview.set_bone_mode(checked)
        if checked:
            self.preview.set_selected_bone(self._selected_bone_name())
            if self.preview.model is not None and not self.preview.bones_supported:
                self.status_label.setText(_text("spine_editor.bone_mode_unavailable"))

    def _preview_bone_picked(self, name: str):
        """Jump the skeleton tree to a bone clicked (or posed) in the preview."""
        if not self.session:
            return
        item = next((node for node in self._structure_items()
                     if node.data(0, Qt.ItemDataRole.UserRole) == ("bone", name)), None)
        if item is None:
            return
        if self.tabs.currentIndex() != 0:
            self.tabs.setCurrentIndex(0)
        if item.isHidden():
            self.search_edit.clear()
        parent = item.parent()
        while parent is not None:
            parent.setExpanded(True)
            parent = parent.parent()
        self.tree.setCurrentItem(item)
        self.tree.scrollToItem(item)
        self.status_label.setText(_text("spine_editor.bone_picked", name=name))

    def _preview_bone_posed(self, name: str, values: dict):
        """Commit a preview drag: setup pose, or a key at the current time."""
        if not self.session:
            return
        channel = "rotate" if "rotation" in values else "translate"
        animation, seconds = self.animation_name, self.timeline.current_time
        self._selection = "bone", name
        index = self.channel_combo.findData(channel)
        if index >= 0:
            self._updating = True
            self.channel_combo.setCurrentIndex(index)
            self._updating = False
        if animation:
            ok = self._edit(lambda: self.session.key_bone_pose(animation, name, seconds, values))
            message = _text("spine_editor.bone_keyed", time=f"{seconds:.3f}", bone=name,
                            channel=_text(f"spine_editor.{channel}"))
        else:
            ok = self._edit(lambda: self.session.set_bone_transform(name, values))
            message = _text("spine_editor.bone_setup_posed", bone=name)
        if ok:
            self.status_label.setText(message)
        else:
            # The native pose was only a preview of the rejected edit.
            self.preview.clear_bone_overrides()

    def _slot_for_region(self, region: str) -> str | None:
        """The slot drawing ``region`` now; the topmost one when several can."""
        candidates = self._region_attachments(region)
        if not candidates:
            return None
        animation, seconds = self.animation_name, self.timeline.current_time
        shown = [slot for slot, attachment in candidates
                 if self.session.attachment_at(slot, animation, seconds) == attachment]
        order = self.session.draw_order_at(animation, seconds)
        pool = shown or [slot for slot, _attachment in candidates]
        return max(pool, key=lambda slot: order.index(slot) if slot in order else -1)

    def _push_undo_token(self, token) -> None:
        """A new step: remember its order and drop the redo branch."""
        self._undo_tokens.append(token)
        del self._undo_tokens[:-200]
        self._redo_tokens.clear()

    def undo(self):
        """Undo the latest step: a document edit or hiding/showing slots."""
        self._history(redo=False)

    def redo(self):
        self._history(redo=True)

    def _history(self, *, redo: bool) -> bool:
        if not self.session or self.loading:
            return False
        self.pause_playback()
        tokens, opposite = (self._redo_tokens, self._undo_tokens) if redo else (self._undo_tokens, self._redo_tokens)
        token = tokens.pop() if tokens else None
        if token is not None and token[0] == "visibility":
            _kind, before, after = token
            self._apply_hidden_slots(after if redo else before)
            opposite.append(token)
            self._update_actions()
            return True
        stack = self.session._redo if redo else self.session._undo
        current = token is not None and bool(stack) and stack[-1] is token[1]
        # Undo falls back to the session's own history (e.g. after recovery);
        # redo does not, since a newer step dropped that redo branch.
        if not stack or (redo and not current):
            self._update_actions()
            return False
        if not self._edit(self.session.redo if redo else self.session.undo,
                          refresh_tree=True, immediate=True, record=False):
            return False
        after = self.session._undo if redo else self.session._redo
        if after:
            opposite.append(("document", after[-1]))
        self._update_actions()
        return True

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
            self.preview.show_setup_pose()
        self.preview.set_time(self.timeline.current_time)
        if not self.workspace.is_panel_visible("preview"):
            self.preview._status_timer.stop()
        if self.bone_mode_button.isChecked() and not self.preview.bones_supported:
            self.status_label.setText(_text("spine_editor.bone_mode_unavailable"))

    def _preview_failed(self, error: str):
        self.status_label.setText(_text("spine_editor.preview_error", error=error))

    def _report_error(self, error):
        self.status_label.setText(_text("spine_editor.error", error=str(error)))

    def _update_actions(self):
        if hasattr(self, "recovery"):
            self.recovery.refresh()
        opened = self.session is not None
        busy = self.loading
        self.save_button.setEnabled(opened and not busy)
        self.undo_button.setEnabled(opened and not busy and (self.session.can_undo or bool(self._undo_tokens)))
        self.redo_button.setEnabled(opened and not busy and bool(self._redo_tokens))
        self.new_button.setEnabled(opened and not busy)
        self.clone_button.setEnabled(opened and bool(self.animation_name) and not busy)
        self.delete_animation_button.setEnabled(opened and bool(self.animation_name) and not busy)
        self.duration_spin.setEnabled(opened and bool(self.animation_name) and not busy)
        self.animation_combo.setEnabled(opened and not busy)
        self.skin_combo.setEnabled(opened and not busy)
        for button in (self.hide_button, self.solo_button):
            button.setEnabled(opened and not busy)
        psd_ready = opened and not busy and not self.psd_busy
        self.psd_panel.export_button.setEnabled(psd_ready)
        self.psd_panel.import_button.setEnabled(psd_ready)
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
        self.recovery.note_saved(result["model_path"])
        self.status_label.setText(_text("spine_editor.saved", path=result["model_path"]))
        self._update_actions()
        return True

    def confirm_discard_or_save(self) -> bool:
        if self.session and self.session.dirty:
            self.pause_playback()
            answer = QMessageBox.question(self, _text("spine_editor.discard_title"), _text("spine_editor.discard_message"),
                                          QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                                          QMessageBox.StandardButton.Save,
                                          button_texts={QMessageBox.StandardButton.Save: _text("spine_editor.save")})
            if answer == QMessageBox.StandardButton.Cancel:
                return False
            if answer == QMessageBox.StandardButton.Discard:
                self.recovery.note_discard()
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
        self.recovery.shutdown()
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
        self.psd_panel.retranslate_ui()
        self.zoom_controls.retranslate_ui()
        self.preview.setToolTip(_text("spine_editor.pick_hint"))
        self.bone_mode_button.setText(_text("spine_editor.bone_mode"))
        self.bone_mode_button.setToolTip(_text("spine_editor.bone_mode_hint"))
        self.bone_tool_combo.setToolTip(_text("spine_editor.bone_tool"))
        for index in range(self.bone_tool_combo.count()):
            self.bone_tool_combo.setItemText(index, _text(f"spine_editor.{self.bone_tool_combo.itemData(index)}"))
        self.bone_filter_bar.setToolTip(_text("spine_editor.bone_filter"))
        for kind in SpinePreviewWidget.BONE_FILTERS:
            self.bone_filter_bar.items[kind].setText(_text(f"spine_editor.bone_filter_{kind}"))
        self.bone_names_button.setText(_text("spine_editor.bone_names"))
        self.bone_names_button.setToolTip(_text("spine_editor.bone_names_hint"))
        self.hide_button.setToolTip(_text("spine_editor.hide_selected_hint"))
        self.solo_button.setToolTip(_text("spine_editor.solo_selected_hint"))
        self.show_all_button.setToolTip(_text("spine_editor.show_all_hint"))
        self.show_all_button.setText(_text("spine_editor.show_all", count=len(self._hidden_slots)))
        # Shown while no model/track list is loaded, like the Live2D track picker.
        self.track_combo.setPlaceholderText(_text("spine_editor.track_placeholder"))
        self.animation_combo.retranslate_ui()
        self.animation_options_button.setToolTip(_text("spine_editor.animation") + " · " + _text("spine_editor.duration"))
        self.workspace.retranslate_ui()
        for index, key in enumerate(("skeleton", "layers", "atlas", "psd")):
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
