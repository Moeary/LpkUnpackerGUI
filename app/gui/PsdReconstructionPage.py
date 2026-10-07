import copy
import json
import math
import os
import re
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QProcess, QThread, QTimer, QUrl, Signal, QStringListModel, QSize
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QAbstractButton,
    QAbstractSpinBox,
    QComboBox,
    QLineEdit,
    QPushButton,
    QListWidget,
    QListWidgetItem,
    QCompleter,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    CaptionLabel,
    CheckBox,
    ComboBox,
    EditableComboBox,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    SpinBox,
    ScrollArea,
    SingleDirectionScrollArea,
    SubtitleLabel,
    TextEdit,
    FluentIcon,
)

from app.core.psd_reconstructor import (
    ReconstructionResult,
    reconstruct_live2d_psd,
    repack_atlas_png_from_psd,
    repack_multiple_psds,
)
from app.core.psd_project import (
    Live2DPSDProject,
    PsdPoseExportStage,
    PsdSnapshotRequest,
    PROJECT_FILE_NAME,
    commit_pose_export_stage,
    create_project_from_source,
    create_parameter_preset,
    create_repack_dir,
    create_multi_repack_dir,
    find_composite_entry,
    find_pose_scheme,
    find_parameter_preset,
    find_repack_entry,
    finalize_pose_export_stage,
    load_project,
    prepare_repack_preview_workspace,
    record_psd_export,
    record_pose_scheme_export,
    record_repack,
    record_multi_repack,
    resolve_project_path,
    discard_pose_export_stage,
    save_project,
    select_pose_scheme,
    stage_pose_export,
    select_parameter_preset,
    select_repack,
    set_preview_state,
    sanitize_project_name,
)
from app.core.model import prepare_model_json_for_preview, resolve_live2d_package
from app.core.model.motions import load_live2d_motions
from app.core.settings_manager import SettingsManager
from app.core.psd_worker import (
    PsdWorkerError, decode_result, decode_stage, publish_output, run_psd_job, snapshot_payload,
)
from app.gui.Live2DPreviewWindow import Live2DPreviewWindow
from app.gui.ArtMeshInspector import ArtMeshInspectorDialog
from app.gui.PreviewPage import ImagePreviewPanel
from app.gui.editor_workspace import EditorComboBox, EditorViewportLayout, EditorSurface
from app.gui.live2d_skin_controls import skin_text
from app.gui.editor_dialogs import ThemedEditorDialog, get_editor_text
from app.i18n import get_i18n, tr


MAX_WIDGET_SIZE = 16777215

PSD_WORKSPACE_TEXT = {
    "psd.workspace.export": "导出与姿态",
    "psd.workspace.repack": "PSD 回写",
    "psd.workspace.versions": "版本与预览",
    "psd.workspace.project": "工程与工具",
    "psd.workspace.bound": "PSD 子工程随当前 Live2D 工程一起保存。导出是独立快照，查看版本不会替换当前动作。",
    "psd.workspace.capture_pose": "保存当前预览参数为预设",
    "psd.workspace.capture_pose_short": "保存预设",
    "psd.workspace.paths": "工程与输出路径",
    "psd.workspace.help": "导出说明",
    "psd.workspace.import_project": "导入 PSD 工程副本",
    "psd.workspace.external_psd": "选择外部 PSD",
    "psd.workspace.single_repack": "回写选中 PSD",
    "psd.workspace.multi_repack": "多 PSD 按顺序回写",
    "psd.workspace.apply_version": "应用版本贴图到当前模型",
    "psd.workspace.apply_skin_short": "存为皮肤",
    "psd.workspace.send_mod": "将版本贴图送入 MOD",
    "psd.workspace.version_hint": "原始项是 PSD 工程的导出源快照。回写版本仅提供贴图；应用后可撤销，当前动作保持不变。",
    "psd.workspace.empty": "先打开 Live2D 工程，再创建或导入 PSD 子工程。",
    "psd.workspace.variant": "另存为皮肤变体",
    "psd.workspace.applied": "已应用",
    "psd.workspace.apply_existing": "应用已有皮肤",
    "psd.task.preparing": "正在准备 PSD 快照",
    "psd.task.prepared": "PSD 快照准备完成",
    "psd.task.cancelled": "PSD 任务已取消",
    "psd.task.close_pending": "PSD 任务正在结束，请稍候再关闭。",
    "psd.task.refresh_failed": "PSD 文件已生成，界面刷新失败：{error}",
    "psd.task.worker_failed": "PSD 后台进程异常退出（{code}）；已有工程已保留。{details}",
    "psd.task.ready": "PSD 已就绪",
    "psd.selection.export": "导出选中部件 PSD",
    "psd.selection.default_name": "选中部件",
    "psd.selection.hint": "按当前姿态导出框选命中的完整部件，保持相对位置。请在原画布与原图层位置绘制；回写会生成新皮肤。",
    "psd.selection.empty": "先在模型预览中框选可见部件。",
    "psd.selection.busy": "PSD 工作正在进行，请完成后再导出选区。",
    "psd.selection.no_project": "先打开当前模型的 PSD 子工程。",
    "psd.selection.shared_hint": "选区与未选部件共用贴图像素；默认拒绝影响这些部件的修改：",
    "psd.selection.shared_enable": "同步修改共享部件",
    "psd.selection.shared_effect": "这些贴图像素由 {count} 个未选部件共用。勾选后，它们也会随新皮肤一起改变；不修改原模型。",
    "psd.selection.shared_actual": "本次修改同时影响 {count} 个共享部件：",
}


def _workspace_text(key: str) -> str:
    return tr(key, default=PSD_WORKSPACE_TEXT[key])


class _WorkspaceEditableCombo(EditableComboBox):
    def minimumSizeHint(self):  # noqa: N802
        return QSize(max(80, self.minimumWidth()), super().minimumSizeHint().height())


class PsdReconstructionThread(QThread):
    progressUpdated = Signal(int, str)
    reconstructionFinished = Signal(object)
    reconstructionError = Signal(str)
    reconstructionCancelled = Signal()

    def __init__(
        self,
        source_path: str,
        output_dir: str,
        mode: str,
        metadata_path: str | None = None,
        parameter_values: dict[str, float] | None = None,
        pose_name: str | None = None,
        output_name: str | None = None,
        resource_limits: dict[str, int] | None = None,
        multi_psd_paths: list[str] | None = None,
        selected_drawable_ids: list[str] | None = None,
        selection_region=None,
        mesh_data: dict | None = None,
        allow_shared_uv: bool = False,
        pose_stage_request: dict | None = None,
        atlas_layout: str = "packed",
    ):
        super().__init__()
        self.source_path = source_path
        self.output_dir = output_dir
        self.mode = mode
        self.metadata_path = metadata_path
        self.parameter_values = copy.deepcopy(parameter_values)
        self.pose_name = pose_name
        self.output_name = output_name
        self.resource_limits = dict(resource_limits or {})
        self.multi_psd_paths = list(multi_psd_paths or [])
        self.selected_drawable_ids = list(selected_drawable_ids) if selected_drawable_ids is not None else None
        self.selection_region = copy.deepcopy(selection_region)
        self.mesh_data = copy.deepcopy(mesh_data)
        self.allow_shared_uv = bool(allow_shared_uv)
        self.atlas_layout = atlas_layout
        # This is a plain-data request captured by the GUI before the thread
        # starts.  The worker never calls a provider, QWidget, or editor
        # session.  On success ``pose_stage`` is committed by the page after
        # the reconstruction result arrives.
        self.pose_stage_request = None
        if pose_stage_request:
            plain_request = dict(pose_stage_request)
            snapshot = plain_request.pop("snapshot")
            self.pose_stage_request = copy.deepcopy(plain_request)
            self.pose_stage_request["snapshot"] = snapshot
        self.pose_stage: PsdPoseExportStage | None = None
        self._job_dir: Path | None = None

    def cleanup_job(self):
        if self._job_dir is not None:
            shutil.rmtree(self._job_dir, ignore_errors=True)
            self._job_dir = None

    def cleanup_stage(self):
        discard_pose_export_stage(self.pose_stage)
        self.pose_stage = None
        self.cleanup_job()

    def _check_cancelled(self):
        if self.isInterruptionRequested():
            raise InterruptedError("PSD task cancelled")

    def _progress(self, value: int, message: str):
        self._check_cancelled()
        if self.pose_stage_request:
            value = 10 + int(max(0, min(100, value)) * .9)
        self.progressUpdated.emit(value, message)

    def run(self):
        try:
            self._check_cancelled()
            job = {name: getattr(self, name) for name in (
                "source_path", "output_dir", "mode", "metadata_path", "parameter_values", "pose_name",
                "output_name", "resource_limits", "multi_psd_paths", "selected_drawable_ids",
                "selection_region", "mesh_data", "allow_shared_uv", "atlas_layout")}
            if self.pose_stage_request:
                parent = Path(self.pose_stage_request["project_dir"]) / ".psd-staging"
                parent.mkdir(parents=True, exist_ok=True)
                self._job_dir = Path(tempfile.mkdtemp(prefix="task-", dir=parent))
                request = dict(self.pose_stage_request)
                self.progressUpdated.emit(1, _workspace_text("psd.task.preparing"))
                request["snapshot"] = snapshot_payload(request["snapshot"], self._job_dir / "legacy-snapshot")
                job["pose_stage_request"] = request
            else:
                parent = Path(self.output_dir).resolve().parent
                parent.mkdir(parents=True, exist_ok=True)
                self._job_dir = Path(tempfile.mkdtemp(prefix=".psd-task-", dir=parent))
            payload = run_psd_job(job, self._job_dir, progress=self._progress,
                                  cancelled=self.isInterruptionRequested)
            self._check_cancelled()
            result = decode_result(payload["result"])
            if payload.get("stage"):
                self.pose_stage = decode_stage(payload["stage"])
            else:
                # Publishing complete output is the cancellation boundary.
                # A later close request must not report cancelled after files
                # have replaced previous standalone output successfully.
                publish_output(Path(payload["output_stage_dir"]), Path(self.output_dir).resolve(),
                               self._job_dir / "previous-output")
            self.reconstructionFinished.emit(result)
        except InterruptedError:
            self.cleanup_stage()
            self.reconstructionCancelled.emit()
        except Exception as exc:
            self.cleanup_stage()
            message = str(exc)
            if isinstance(exc, PsdWorkerError) and exc.exit_code not in (None, 1):
                message = tr("psd.task.worker_failed", default=PSD_WORKSPACE_TEXT["psd.task.worker_failed"],
                             code=f"0x{exc.exit_code & 0xffffffff:08X}", details="\n" + message)
            self.reconstructionError.emit(message)


class PsdMultiRepackDialog(ThemedEditorDialog):
    """Choose project PSDs and order only the stack being written back."""

    def __init__(
        self,
        parent: QWidget,
        project_psds: list[tuple[str, str]],
        initial_psd: str = "",
    ):
        super().__init__(parent)
        self.setMinimumWidth(560)
        self.setWindowTitle(tr("psd.multi.title"))
        self.main_layout.setContentsMargins(18, 18, 18, 18)
        self.title_label.hide()
        self.yesButton.hide()
        self.cancelButton.hide()
        layout = self.content_layout
        hint = CaptionLabel(tr("psd.multi.hint"), self)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        source_row = QHBoxLayout()
        self.project_psd_combo = ComboBox(self)
        for label, path in project_psds:
            self.project_psd_combo.addItem(label, userData=path)
        self.add_button = PushButton(tr("psd.multi.add"), self)
        self.add_button.clicked.connect(self.add_selected_project_psd)
        source_row.addWidget(self.project_psd_combo, 1)
        source_row.addWidget(self.add_button)
        layout.addLayout(source_row)
        self.psd_list = QListWidget(self)
        self.psd_list.setMinimumHeight(150)
        layout.addWidget(self.psd_list)

        controls = QHBoxLayout()
        self.remove_button = PushButton(tr("psd.multi.remove"), self)
        self.up_button = PushButton(tr("psd.multi.move_up"), self)
        self.down_button = PushButton(tr("psd.multi.move_down"), self)
        self.remove_button.clicked.connect(self.remove_current)
        self.up_button.clicked.connect(lambda: self.move_current(-1))
        self.down_button.clicked.connect(lambda: self.move_current(1))
        for button in (self.remove_button, self.up_button, self.down_button):
            controls.addWidget(button)
        controls.addStretch(1)
        layout.addLayout(controls)

        name_layout = QHBoxLayout()
        name_layout.addWidget(BodyLabel(tr("psd.multi.texture_name"), self))
        self.texture_name_edit = LineEdit(self)
        self.texture_name_edit.setPlaceholderText(tr("psd.repack.texture_name_placeholder"))
        name_layout.addWidget(self.texture_name_edit, 1)
        layout.addLayout(name_layout)

        actions = QHBoxLayout()
        self.cancel_button = PushButton(tr("common.cancel"), self)
        self.confirm_button = PrimaryPushButton(tr("psd.multi.confirm"), self)
        self.cancel_button.clicked.connect(self.reject)
        self.confirm_button.clicked.connect(self.accept_if_valid)
        actions.addStretch(1)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.confirm_button)
        layout.addLayout(actions)
        if initial_psd and Path(initial_psd).is_file():
            self.add_path(initial_psd)

    def add_selected_project_psd(self):
        path = str(self.project_psd_combo.currentData() or "")
        if path:
            self.add_path(path)

    def add_path(self, path: str):
        resolved = str(Path(path).resolve())
        if any(self.psd_list.item(i).data(Qt.ItemDataRole.UserRole) == resolved for i in range(self.psd_list.count())):
            return
        item = QListWidgetItem(Path(resolved).name)
        item.setToolTip(resolved)
        item.setData(Qt.ItemDataRole.UserRole, resolved)
        self.psd_list.addItem(item)
        self.psd_list.setCurrentItem(item)

    def remove_current(self):
        row = self.psd_list.currentRow()
        if row >= 0:
            self.psd_list.takeItem(row)

    def move_current(self, offset: int):
        row = self.psd_list.currentRow()
        target = row + offset
        if row < 0 or not 0 <= target < self.psd_list.count():
            return
        item = self.psd_list.takeItem(row)
        self.psd_list.insertItem(target, item)
        self.psd_list.setCurrentRow(target)

    def ordered_paths(self) -> list[str]:
        return [str(self.psd_list.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.psd_list.count())]

    def accept_if_valid(self):
        count = len(self.ordered_paths())
        if count < 1 or (count > 1 and not self.texture_name_edit.text().strip()):
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("psd.multi.validation"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3500,
            )
            return
        self.accept()


class PsdProjectImportThread(QThread):
    progressUpdated = Signal(str)
    projectFinished = Signal(object)
    projectError = Signal(str)

    def __init__(self, source_path: str, project_name: str):
        super().__init__()
        self.source_path = source_path
        self.project_name = project_name

    def run(self):
        try:
            project = create_project_from_source(
                self.source_path,
                project_name=self.project_name or None,
                log=lambda message: self.progressUpdated.emit(str(message)),
            )
            self.projectFinished.emit(project)
        except Exception as exc:
            self.projectError.emit(str(exc))


class PsdEditorProjectPreparationThread(QThread):
    progressUpdated = Signal(str)
    projectFinished = Signal(object)
    projectError = Signal(str)
    projectCancelled = Signal()

    def __init__(self, snapshot_request, project_dir, project_name):
        super().__init__()
        self.snapshot_request = snapshot_request
        self.project_dir = Path(project_dir).resolve()
        self.project_name = str(project_name)
        self._job_dir = None
        self._owner_token = ""

    def _discard_owned_project(self):
        marker = self.project_dir / ".psd-init-owner"
        if self._owner_token and marker.is_file() and marker.read_text(encoding="utf-8") == self._owner_token:
            shutil.rmtree(self.project_dir, ignore_errors=True)

    def cleanup_job(self):
        if self._job_dir is not None:
            shutil.rmtree(self._job_dir, ignore_errors=True)
            self._job_dir = None

    def run(self):
        try:
            self.project_dir.parent.mkdir(parents=True, exist_ok=True)
            self._job_dir = Path(tempfile.mkdtemp(prefix=".psd-init-task-", dir=self.project_dir.parent))
            self._owner_token = self._job_dir.name
            snapshot = PsdSnapshotRequest(export_request=self.snapshot_request, source_origin="appearance")
            job = {"operation": "initialize", "project_dir": str(self.project_dir),
                   "project_name": self.project_name,
                   "snapshot": snapshot_payload(snapshot, self._job_dir / "legacy-snapshot")}
            result = run_psd_job(job, self._job_dir, cancelled=self.isInterruptionRequested,
                                 progress=lambda _value, message: self.progressUpdated.emit(message))
            if self.isInterruptionRequested():
                raise InterruptedError("PSD task cancelled")
            project = Live2DPSDProject(Path(result["project_dir"]), Path(result["project_file"]), result["data"])
            self.projectFinished.emit(project)
        except InterruptedError:
            self._discard_owned_project()
            self.projectCancelled.emit()
        except Exception as exc:
            self._discard_owned_project()
            self.projectError.emit(str(exc))
        finally:
            self.cleanup_job()


class PsdPreviewPrepareThread(QThread):
    progressUpdated = Signal(str)
    previewReady = Signal(str, str)
    previewError = Signal(str)

    def __init__(self, project: Live2DPSDProject, version_id: str):
        super().__init__()
        self.project = project
        self.version_id = version_id

    def run(self):
        try:
            model_json = prepare_repack_preview_workspace(
                self.project,
                self.version_id,
                log=lambda message: self.progressUpdated.emit(str(message)),
            )
            self.previewReady.emit(str(model_json), self.version_id)
        except Exception as exc:
            self.previewError.emit(str(exc))


class PsdReconstructionPage(QFrame):
    unifiedPreviewRequested = Signal(str, str)
    projectChanged = Signal(object)
    projectRequested = Signal(str)
    newProjectRequested = Signal()
    sourceRequested = Signal(str)
    capturePoseRequested = Signal()
    repackApplyRequested = Signal(str)
    repackModRequested = Signal(str)
    repackSkinReady = Signal(str)
    previewClosed = Signal()
    texturePreviewRequested = Signal(object)
    contextChanged = Signal(object)
    repackVariantRequested = Signal(str)
    editWorkingTextureRequested = Signal()
    # Stable compact-workbench contract.  The dict contains only plain data:
    # ``task`` (e.g. ``selection-export``), ``state`` (starting/preparing/
    # running/succeeded/failed), ``busy``, ``progress`` and ``message``.
    taskStateChanged = Signal(object)
    taskStarted = Signal(str)
    taskProgressChanged = Signal(int, str)
    taskFinished = Signal(object)
    taskFailed = Signal(str)

    def __init__(self, parent=None, *, compact: bool = False, settings=None):
        super().__init__(parent)
        self._compact = bool(compact)
        self._task_embedded = False
        self._version_skin_provider = None
        self.comparison_mesh_provider = None
        self._last_comparison_request = None
        self._task_result_committed = False
        self._task_progress = 0
        self._task_state = "idle"
        self._pending_skin_ready_token = ""
        self._pending_preview_ready = None
        self.export_snapshot_provider = None
        self.export_snapshot_request_provider = None
        self.skin_context = None
        self.pending_skin_context = None
        self.preview_command_handler = None
        self._export_snapshot = ""
        self._shared_uv_context = None
        self._shared_uv_available = False
        self._shared_uv_multimode = False
        self._bound_project_files: list[str] = []
        self.setObjectName("psdReconstructionPage")
        self.setAcceptDrops(True)

        self.i18n = get_i18n()
        self.settings_manager = settings or SettingsManager()
        self.selected_source = ""
        self.selected_metadata = ""
        self.manual_repack_psd = ""
        self.workflow = "export"
        self.output_manually_selected = False
        self.last_output_dir = self.default_output_dir()
        self.last_psd_path = ""
        self.worker: PsdReconstructionThread | None = None
        self.project_worker: PsdProjectImportThread | None = None
        self.preview_prepare_worker: PsdPreviewPrepareThread | None = None
        self.current_project: Live2DPSDProject | None = None
        self.managed_project_root_provider = None
        self._project_combo_refreshing = False
        self._pose_scheme_refreshing = False
        self._parameter_preset_refreshing = False
        self._ui_refreshing = False
        self._project_dirty = False
        self._is_busy = False
        self._task_id = ""
        self._task_message = ""
        self._motion_items: list[dict] = []
        self.preview_dialog: QDialog | None = None
        self.live2d_preview_window: Live2DPreviewWindow | None = None
        self.artmesh_inspector_dialog: ArtMeshInspectorDialog | None = None
        self.preview_mode = ""
        self.preview_repack_id = ""
        self._last_preview_dock_rect: dict | None = None
        self.pending_repack_id = ""
        self.pending_repack_dir = ""
        self.pending_repack_source = ""
        self.pending_repack_metadata = ""
        self.pending_repack_name = ""
        self.pending_multi_psd_paths: list[str] = []
        self.pending_pose_scheme_id = ""
        self._log_expanded = False
        self._expanded_log_height = 220
        self._preview_dock_timer = QTimer(self)
        self._preview_dock_timer.setInterval(700)
        self._preview_dock_timer.timeout.connect(self._send_preview_dock_geometry)
        self._arrange_timer = QTimer(self)
        self._arrange_timer.setSingleShot(True)
        self._arrange_timer.timeout.connect(self._arrange_button_rows)
        self._last_project_timer = QTimer(self)
        self._last_project_timer.setSingleShot(True)
        self._last_project_timer.timeout.connect(lambda: self.load_last_project(silent=True))
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.close_project_preview)

        self.setupUI()
        self.retranslate_ui()
        self.i18n.languageChanged.connect(self.retranslate_ui)
        if not self._compact:
            self._last_project_timer.start(0)

    def setupUI(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0) if self._compact else self.main_layout.setContentsMargins(20, 20, 20, 20)
        self.main_layout.setSpacing(8 if self._compact else 12)
        if self._compact:
            self._setup_compact_task_view()
        else:
            self._setup_standalone_workspace()
        self._configure_responsive_controls()
        self._set_log_expanded(False)
        self.log_toggle_button.setMinimumWidth(52)
        self.log_toggle_button.setMaximumWidth(64)
        self.log_toggle_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._apply_static_styles()

    def _setup_standalone_workspace(self):
        self._build_page_header()
        self.workspace_splitter = QSplitter(Qt.Vertical, self)
        self.workspace_splitter.setChildrenCollapsible(False)
        self.main_layout.addWidget(self.workspace_splitter, 1)

        self.content_splitter = QSplitter(Qt.Horizontal, self.workspace_splitter)
        self.content_splitter.setChildrenCollapsible(False)
        self.content_splitter.setMinimumHeight(240)
        self.workspace_splitter.addWidget(self.content_splitter)

        self.left_scroll = SingleDirectionScrollArea(
            orient=Qt.Vertical,
            parent=self.content_splitter,
        )
        self.left_scroll.setWidgetResizable(True)
        self.left_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.left_scroll.enableTransparentBackground()
        self.left_scroll.setMinimumWidth(340)
        self.left_panel = QWidget()
        self.left_panel_layout = QVBoxLayout(self.left_panel)
        self.left_panel_layout.setContentsMargins(0, 0, 10, 0)
        self.left_panel_layout.setSpacing(10)
        self.left_scroll.setWidget(self.left_panel)
        self.content_splitter.addWidget(self.left_scroll)

        self.right_column = QWidget(self.content_splitter)
        self.right_column_layout = QVBoxLayout(self.right_column)
        self.right_column_layout.setContentsMargins(0, 0, 0, 0)
        self.right_column_layout.setSpacing(10)
        self.right_scroll = SingleDirectionScrollArea(
            orient=Qt.Vertical,
            parent=self.right_column,
        )
        self.right_scroll.setWidgetResizable(True)
        self.right_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.right_scroll.enableTransparentBackground()
        self.right_scroll.setMinimumWidth(340)
        self.right_panel = QWidget()
        self.right_panel_layout = QVBoxLayout(self.right_panel)
        self.right_panel_layout.setContentsMargins(10, 0, 0, 0)
        self.right_panel_layout.setSpacing(10)
        self.right_scroll.setWidget(self.right_panel)
        self.right_column_layout.addWidget(self.right_scroll, 1)
        self.right_footer = QWidget(self.right_column)
        self.right_footer_layout = QVBoxLayout(self.right_footer)
        self.right_footer_layout.setContentsMargins(10, 0, 0, 0)
        self.right_footer_layout.setSpacing(8)
        self.right_column_layout.addWidget(self.right_footer)
        self.content_splitter.addWidget(self.right_column)
        self.content_splitter.setStretchFactor(0, 1)
        self.content_splitter.setStretchFactor(1, 1)
        self.content_splitter.setSizes([500, 500])
        self.content_splitter.splitterMoved.connect(
            lambda *_args: self._arrange_button_rows()
        )

        self._build_project_card(self.left_panel)
        self.left_panel_layout.addWidget(self.project_frame)
        self._build_workflow_controls(self.left_panel)
        self.left_panel_layout.addLayout(self.workflow_layout)
        self._build_export_card(self.right_panel)
        self.right_panel_layout.addWidget(self.export_card)
        self._build_repack_card(self.right_panel)
        self.right_panel_layout.addWidget(self.repack_card)
        self._build_action_controls(self.right_footer, self.right_footer_layout)
        self._build_history_card(self.left_panel)
        self.left_panel_layout.addWidget(self.preview_control_frame)
        self._build_motion_card(self.left_panel)
        self.left_panel_layout.addWidget(self.motion_frame)
        self.left_panel_layout.addStretch(1)
        self._build_preview_dialog()
        self._build_log_card(self.workspace_splitter)
        self.workspace_splitter.addWidget(self.log_frame)
        self.workspace_splitter.setStretchFactor(0, 1)
        self.workspace_splitter.setStretchFactor(1, 0)

    def _build_page_header(self):
        self.header_layout = QHBoxLayout()
        self.title_label = SubtitleLabel("", self)
        self.current_project_title_label = BodyLabel("", self)
        self.current_project_title_label.setStyleSheet(
            "color: palette(text); font-weight: 600;"
        )
        self.unsaved_label = SubtitleLabel("*", self)
        self.unsaved_label.setStyleSheet("color: #D83B01; font-weight: 700;")
        self.unsaved_label.setVisible(False)
        self.header_layout.addWidget(self.title_label)
        self.header_layout.addWidget(self.current_project_title_label)
        self.header_layout.addStretch(1)
        self.header_layout.addWidget(self.unsaved_label)
        self.main_layout.addLayout(self.header_layout)

        self.desc_label = CaptionLabel("", self)
        self.desc_label.setWordWrap(True)
        self.desc_label.setVisible(False)

        self.drop_frame = QFrame(self)
        self.drop_frame.setObjectName("psdDropFrame")
        self.drop_frame.setMinimumHeight(42)
        self.drop_frame.setMaximumHeight(52)
        self.drop_frame.setStyleSheet(
            """
            QFrame#psdDropFrame {
                border: 1px dashed palette(mid);
                border-radius: 6px;
                background: palette(alternate-base);
            }
            """
        )
        drop_layout = QVBoxLayout(self.drop_frame)
        drop_layout.setContentsMargins(14, 6, 14, 6)
        drop_layout.setSpacing(0)
        self.drop_main_label = BodyLabel("", self.drop_frame)
        self.drop_sub_label = CaptionLabel("", self.drop_frame)
        self.drop_sub_label.setWordWrap(True)
        drop_layout.addWidget(self.drop_main_label)
        drop_layout.addWidget(self.drop_sub_label)
        self.drop_sub_label.setVisible(False)
        self.main_layout.addWidget(self.drop_frame)


    def _build_project_card(self, parent):
        self.project_frame = (EditorSurface if self._compact else CardWidget)(parent)
        self.project_frame.setObjectName("psdProjectFrame")
        self.project_layout = QVBoxLayout(self.project_frame)
        self.project_layout.setContentsMargins(12, 12, 12, 12)
        self.project_layout.setSpacing(8)
        self.project_title_label = SubtitleLabel("", self.project_frame)
        self.project_layout.addWidget(self.project_title_label)

        self.project_name_edit = LineEdit(self.project_frame)
        self.project_name_edit.setPlaceholderText("")
        self.project_name_edit.setVisible(False)

        self.project_combo = (_WorkspaceEditableCombo if self._compact else EditableComboBox)(self.project_frame)
        self.project_combo.setPlaceholderText("")
        self.project_combo.setClearButtonEnabled(True)
        self._project_completer_model = QStringListModel(self.project_combo)
        self._project_completer = QCompleter(
            self._project_completer_model,
            self.project_combo,
        )
        self._project_completer.setCaseSensitivity(
            Qt.CaseSensitivity.CaseInsensitive
        )
        self._project_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._project_completer.setCompletionMode(
            QCompleter.CompletionMode.PopupCompletion
        )
        self._project_completer.setMaxVisibleItems(12)
        self.project_combo.setCompleter(self._project_completer)
        self.project_combo.currentIndexChanged.connect(self.on_project_combo_changed)
        self.project_layout.addWidget(self.project_combo)

        self.project_button_layout = QGridLayout()
        self.project_button_layout.setSpacing(8)
        self.new_project_button = PushButton("", self.project_frame)
        self.new_project_button.clicked.connect(self.create_project_from_current_source)
        self.save_project_button = PushButton("", self.project_frame)
        self.save_project_button.clicked.connect(self.save_current_project)
        self.open_project_folder_button = PushButton("", self.project_frame)
        self.open_project_folder_button.setEnabled(False)
        self.open_project_folder_button.clicked.connect(self.open_current_project_folder)
        self.project_button_layout.addWidget(self.new_project_button, 0, 0)
        self.project_button_layout.addWidget(self.save_project_button, 0, 1)
        self.project_button_layout.addWidget(self.open_project_folder_button, 0, 2)
        self.project_layout.addLayout(self.project_button_layout)

        self.project_status_label = CaptionLabel("", self.project_frame)
        self.project_status_label.setWordWrap(True)
        self.project_layout.addWidget(self.project_status_label)



    def _build_workflow_controls(self, parent):
        self.workflow_layout = QVBoxLayout()
        self.workflow_layout.setSpacing(10)
        self.workflow_label = SubtitleLabel("", parent)
        self.workflow_segment = QFrame(parent)
        self.workflow_segment.setObjectName("psdWorkflowSegment")
        self.workflow_segment_layout = QGridLayout(self.workflow_segment)
        self.workflow_segment_layout.setContentsMargins(3, 3, 3, 3)
        self.workflow_segment_layout.setSpacing(3)
        self.export_flow_button = PushButton("", self.workflow_segment)
        self.export_flow_button.setObjectName("psdWorkflowButton")
        self.export_flow_button.clicked.connect(lambda: self.set_workflow("export"))
        self.repack_flow_button = PushButton("", self.workflow_segment)
        self.repack_flow_button.setObjectName("psdWorkflowButton")
        self.repack_flow_button.clicked.connect(lambda: self.set_workflow("repack"))
        self.workflow_segment_layout.addWidget(self.export_flow_button, 0, 0)
        self.workflow_segment_layout.addWidget(self.repack_flow_button, 0, 1)
        self.workflow_layout.addWidget(self.workflow_label)
        self.workflow_layout.addWidget(self.workflow_segment)


    def _build_export_card(self, parent):
        self.export_card = (EditorSurface if self._compact else CardWidget)(parent)
        self.export_card.setObjectName("psdExportCard")
        self.export_card_layout = QVBoxLayout(self.export_card)
        self.export_card_layout.setContentsMargins(14, 14, 14, 14)
        self.export_card_layout.setSpacing(10)
        self.export_card_title = SubtitleLabel("", self.export_card)
        self.export_card_layout.addWidget(self.export_card_title)

        self.source_layout = QVBoxLayout()
        self.source_label = BodyLabel("", self.export_card)
        self.source_edit = LineEdit(self.export_card)
        self.source_edit.setReadOnly(True)
        self.source_file_button = PushButton("", self.export_card)
        self.source_file_button.clicked.connect(self.browse_source_file)
        self.source_folder_button = PushButton("", self.export_card)
        self.source_folder_button.clicked.connect(self.browse_source_folder)
        self.source_layout.addWidget(self.source_label)
        self.source_layout.addWidget(self.source_edit, 1)
        self.source_button_layout = QHBoxLayout()
        self.source_button_layout.setSpacing(8)
        self.source_button_layout.addWidget(self.source_file_button)
        self.source_button_layout.addWidget(self.source_folder_button)
        self.source_layout.addLayout(self.source_button_layout)
        self.export_card_layout.addLayout(self.source_layout)

        self.export_name_layout = QVBoxLayout()
        self.export_name_label = BodyLabel("", self.export_card)
        self.export_name_edit = (_WorkspaceEditableCombo if self._compact else EditableComboBox)(self.export_card)
        self.export_name_edit.setClearButtonEnabled(True)
        self._parameter_preset_completer_model = QStringListModel(
            self.export_name_edit
        )
        self._parameter_preset_completer = QCompleter(
            self._parameter_preset_completer_model,
            self.export_name_edit,
        )
        self._parameter_preset_completer.setCaseSensitivity(
            Qt.CaseSensitivity.CaseInsensitive
        )
        self._parameter_preset_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._parameter_preset_completer.setCompletionMode(
            QCompleter.CompletionMode.PopupCompletion
        )
        self._parameter_preset_completer.setMaxVisibleItems(12)
        self.export_name_edit.setCompleter(self._parameter_preset_completer)
        self.export_name_edit.currentIndexChanged.connect(
            self.on_parameter_preset_changed
        )
        self.export_name_edit.currentTextChanged.connect(
            self._update_action_availability
        )
        self.export_name_layout.addWidget(self.export_name_label)
        self.export_name_layout.addWidget(self.export_name_edit)
        self.export_card_layout.addLayout(self.export_name_layout)
        self.export_preset_hint = CaptionLabel("", self.export_card)
        self.export_preset_hint.setWordWrap(True)
        self.export_name_edit.currentTextChanged.connect(
            self._update_export_preset_hint
        )
        self.export_card_layout.addWidget(self.export_preset_hint)

        self.mode_frame = QFrame(self.export_card)
        self.mode_container_layout = QVBoxLayout(self.mode_frame)
        self.mode_container_layout.setContentsMargins(0, 0, 0, 0)
        self.mode_container_layout.setSpacing(6)
        self.mode_layout = QVBoxLayout()
        self.mode_label = SubtitleLabel("", self.mode_frame)
        self.mode_combo = (EditorComboBox if self._compact else ComboBox)(self.mode_frame)
        self.mode_combo.addItem("", userData="mesh")
        self.mode_combo.addItem("", userData="atlas-components")
        self.mode_combo.currentIndexChanged.connect(self.on_mode_changed)
        self.mode_combo.setMinimumWidth(0)
        self.mode_combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.mode_layout.addWidget(self.mode_label)
        self.mode_layout.addWidget(self.mode_combo)
        self.mode_hint_label = CaptionLabel("", self.mode_frame)
        self.mode_hint_label.setWordWrap(True)
        self.mode_container_layout.addLayout(self.mode_layout)
        self.mode_container_layout.addWidget(self.mode_hint_label)

        self.mesh_canvas_frame = QFrame(self.mode_frame)
        self.mesh_canvas_layout = QVBoxLayout(self.mesh_canvas_frame)
        self.mesh_canvas_layout.setContentsMargins(0, 0, 0, 0)
        self.mesh_canvas_label = BodyLabel("", self.mesh_canvas_frame)
        self.mesh_canvas_preset_combo = (EditorComboBox if self._compact else ComboBox)(self.mesh_canvas_frame)
        self.mesh_canvas_preset_combo.addItem("2048 px", userData=2048)
        self.mesh_canvas_preset_combo.addItem("4096 px", userData=4096)
        self.mesh_canvas_preset_combo.addItem("", userData="custom")
        self.mesh_canvas_preset_combo.currentIndexChanged.connect(
            self.on_mesh_canvas_preset_changed
        )
        self.mesh_canvas_spin = SpinBox(self.mesh_canvas_frame)
        self.mesh_canvas_spin.setRange(0, 16384)
        self.mesh_canvas_spin.setSingleStep(256)
        self.mesh_canvas_spin.setSuffix(" px")
        self.mesh_canvas_spin.setValue(
            int(self.settings_manager.get("psd.resource_limits.mesh_max_dimension", 2048))
        )
        self.mesh_canvas_spin.editingFinished.connect(self.on_mesh_canvas_limit_changed)
        self._sync_mesh_canvas_preset()
        self.mesh_canvas_hint_label = CaptionLabel("", self.mesh_canvas_frame)
        self.mesh_canvas_hint_label.setWordWrap(True)
        self.mesh_canvas_layout.addWidget(self.mesh_canvas_label)
        self.mesh_canvas_input_layout = QHBoxLayout()
        self.mesh_canvas_input_layout.addWidget(self.mesh_canvas_preset_combo, 1)
        self.mesh_canvas_input_layout.addWidget(self.mesh_canvas_spin, 1)
        self.mesh_canvas_layout.addLayout(self.mesh_canvas_input_layout)
        self.mode_container_layout.addWidget(self.mesh_canvas_frame)
        self.mode_container_layout.addWidget(self.mesh_canvas_hint_label)
        self.export_card_layout.addWidget(self.mode_frame)

        self.output_layout = QVBoxLayout()
        self.output_label = BodyLabel("", self.export_card)
        self.output_edit = LineEdit(self.export_card)
        self.output_edit.setText(self.last_output_dir)
        self.output_edit.textChanged.connect(self.mark_project_dirty)
        self.output_button = PushButton("", self.export_card)
        self.output_button.clicked.connect(self.browse_output)
        self.output_layout.addWidget(self.output_label)
        self.output_input_layout = QHBoxLayout()
        self.output_input_layout.addWidget(self.output_edit, 1)
        self.output_input_layout.addWidget(self.output_button)
        self.output_layout.addLayout(self.output_input_layout)
        self.export_card_layout.addLayout(self.output_layout)


    def _build_repack_card(self, parent):
        self.repack_card = (EditorSurface if self._compact else CardWidget)(parent)
        self.repack_card.setObjectName("psdRepackCard")
        self.repack_card_layout = QVBoxLayout(self.repack_card)
        self.repack_card_layout.setContentsMargins(14, 14, 14, 14)
        self.repack_card_layout.setSpacing(10)
        self.repack_card_title = SubtitleLabel("", self.repack_card)
        self.repack_card_title.setWordWrap(True)
        self.repack_card_layout.addWidget(self.repack_card_title)

        self.repack_psd_layout = QVBoxLayout()
        self.pose_scheme_label = BodyLabel("", self.repack_card)
        self.pose_scheme_combo = (_WorkspaceEditableCombo if self._compact else EditableComboBox)(self.repack_card)
        self.pose_scheme_combo.setClearButtonEnabled(True)
        self._psd_completer_model = QStringListModel(self.pose_scheme_combo)
        self._psd_completer = QCompleter(
            self._psd_completer_model,
            self.pose_scheme_combo,
        )
        self._psd_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._psd_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._psd_completer.setCompletionMode(
            QCompleter.CompletionMode.PopupCompletion
        )
        self._psd_completer.setMaxVisibleItems(12)
        self.pose_scheme_combo.setCompleter(self._psd_completer)
        self.pose_scheme_combo.currentIndexChanged.connect(
            self.on_pose_scheme_changed
        )
        self.repack_psd_button = PushButton("", self.repack_card)
        self.repack_psd_button.clicked.connect(self.open_project_psd_folder)
        self.repack_photoshop_button = PushButton("", self.repack_card)
        self.repack_photoshop_button.clicked.connect(self.open_current_psd_in_photoshop)
        self.repack_psd_layout.addWidget(self.pose_scheme_label)
        self.repack_psd_layout.addWidget(self.pose_scheme_combo)
        self.repack_psd_button_layout = QHBoxLayout()
        self.repack_psd_button_layout.addWidget(self.repack_photoshop_button)
        self.repack_psd_button_layout.addWidget(self.repack_psd_button)
        self.repack_psd_layout.addLayout(self.repack_psd_button_layout)
        self.repack_card_layout.addLayout(self.repack_psd_layout)

        self.metadata_frame = QFrame(self.repack_card)
        self.metadata_layout = QVBoxLayout(self.metadata_frame)
        self.metadata_layout.setContentsMargins(0, 0, 0, 0)
        self.metadata_label = BodyLabel("", self.metadata_frame)
        self.metadata_edit = LineEdit(self.metadata_frame)
        self.metadata_edit.setReadOnly(True)
        self.metadata_button = PushButton("", self.metadata_frame)
        self.metadata_button.clicked.connect(self.browse_metadata_file)
        self.metadata_default_button = PushButton("", self.metadata_frame)
        self.metadata_default_button.clicked.connect(self.clear_metadata_file)
        self.metadata_layout.addWidget(self.metadata_label)
        self.metadata_layout.addWidget(self.metadata_edit)
        self.metadata_button_layout = QHBoxLayout()
        self.metadata_button_layout.addWidget(self.metadata_button)
        self.metadata_button_layout.addWidget(self.metadata_default_button)
        self.metadata_layout.addLayout(self.metadata_button_layout)
        self.repack_card_layout.addWidget(self.metadata_frame)
        self.metadata_frame.setVisible(False)
        self.shared_uv_checkbox = CheckBox(_workspace_text("psd.selection.shared_enable"), self.repack_card)
        self.shared_uv_hint = CaptionLabel(self.repack_card)
        self.shared_uv_hint.setWordWrap(True)
        self.shared_uv_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.repack_card_layout.addWidget(self.shared_uv_checkbox)
        self.repack_card_layout.addWidget(self.shared_uv_hint)
        self.shared_uv_checkbox.hide()
        self.shared_uv_hint.hide()
        self.metadata_edit.textChanged.connect(self._refresh_shared_uv_option)

        self.texture_name_layout = QVBoxLayout()
        self.texture_name_label = BodyLabel("", self.repack_card)
        self.texture_name_edit = LineEdit(self.repack_card)
        self.texture_name_edit.textChanged.connect(self.on_texture_name_changed)
        self.texture_name_layout.addWidget(self.texture_name_label)
        self.texture_name_layout.addWidget(self.texture_name_edit)
        self.repack_card_layout.addLayout(self.texture_name_layout)
        self.texture_name_label.setVisible(False)
        self.texture_name_edit.setVisible(False)

        self.repack_output_layout = QVBoxLayout()
        self.repack_output_label = BodyLabel("", self.repack_card)
        self.repack_output_edit = LineEdit(self.repack_card)
        self.repack_output_edit.setReadOnly(True)
        self.repack_output_layout.addWidget(self.repack_output_label)
        self.repack_output_layout.addWidget(self.repack_output_edit)
        self.repack_card_layout.addLayout(self.repack_output_layout)
        self.compare_repack_button = PushButton(tr("psd.compare.open"), self.repack_card)
        self.compare_repack_button.setEnabled(False)
        self.compare_repack_button.clicked.connect(lambda: self.open_comparison())
        self.repack_card_layout.addWidget(self.compare_repack_button)
        self.edit_working_png_button = PushButton(tr("psd.edit.working_png"), self.repack_card)
        self.edit_working_png_button.setToolTip(tr("psd.edit.working_png_hint"))
        self.edit_working_png_button.clicked.connect(self.editWorkingTextureRequested)
        self.edit_working_png_button.hide()
        self.repack_card_layout.addWidget(self.edit_working_png_button)


    def _build_action_controls(self, parent, layout):
        self.action_layout = QGridLayout()
        self.reconstruct_button = PrimaryPushButton("", parent)
        self.reconstruct_button.clicked.connect(self.start_reconstruction)
        self.open_output_button = PushButton("", parent)
        self.open_output_button.setEnabled(False)
        self.open_output_button.clicked.connect(self.open_output_folder)
        self.open_photoshop_button = PushButton("", parent)
        self.open_photoshop_button.setEnabled(False)
        self.open_photoshop_button.clicked.connect(self.open_current_psd_in_photoshop)
        self.artmesh_inspector_button = PushButton("", parent)
        self.artmesh_inspector_button.clicked.connect(self.open_artmesh_inspector)
        self.artmesh_inspector_button.setEnabled(False)
        self.multi_repack_button = PushButton("", parent)
        self.multi_repack_button.clicked.connect(self.start_multi_repack)
        self.multi_repack_button.setVisible(False)
        self.preview_toggle_button = PushButton("", parent)
        self.preview_toggle_button.clicked.connect(self.toggle_preview_panel)
        self.action_layout.addWidget(self.reconstruct_button, 0, 0)
        self.action_layout.addWidget(self.artmesh_inspector_button, 0, 1)
        self.action_layout.addWidget(self.open_output_button, 1, 0)
        self.action_layout.addWidget(self.preview_toggle_button, 1, 1)
        self.action_layout.addWidget(self.open_photoshop_button, 2, 0, 1, 2)
        layout.addLayout(self.action_layout)

        self.progress_layout = QHBoxLayout()
        self.progress_bar = ProgressBar(parent)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.stage_label = CaptionLabel("", parent)
        self.stage_label.setMinimumWidth(160)
        self.stage_label.setWordWrap(True)
        self.progress_layout.addWidget(self.progress_bar, 1)
        self.progress_layout.addWidget(self.stage_label)
        layout.addLayout(self.progress_layout)


    def _build_history_card(self, parent):
        self.preview_control_frame = (EditorSurface if self._compact else CardWidget)(parent)
        self.preview_control_frame.setObjectName("psdPreviewControlFrame")
        self.preview_control_layout = QVBoxLayout(self.preview_control_frame)
        self.preview_control_layout.setContentsMargins(12, 12, 12, 12)
        self.preview_control_layout.setSpacing(8)
        self.preview_control_title_label = SubtitleLabel("", self.preview_control_frame)
        self.preview_control_layout.addWidget(self.preview_control_title_label)

        self.preview_source_layout = QVBoxLayout()
        self.preview_source_label = BodyLabel("", self.preview_control_frame)
        self.preview_source_combo = (EditorComboBox if self._compact else ComboBox)(self.preview_control_frame)
        self.preview_source_combo.currentIndexChanged.connect(self.on_preview_source_changed)
        self.preview_source_layout.addWidget(self.preview_source_label)
        self.preview_source_layout.addWidget(self.preview_source_combo)
        self.preview_control_layout.addLayout(self.preview_source_layout)

        self.preview_texture_layout = QVBoxLayout()
        self.preview_texture_label = BodyLabel("", self.preview_control_frame)
        self.preview_texture_combo = (EditorComboBox if self._compact else ComboBox)(self.preview_control_frame)
        self.preview_texture_layout.addWidget(self.preview_texture_label)
        self.preview_texture_layout.addWidget(self.preview_texture_combo)
        self.preview_control_layout.addLayout(self.preview_texture_layout)

        self.preview_action_layout = QGridLayout()
        self.preview_action_layout.setSpacing(8)
        self.preview_image_button = PrimaryPushButton("", self.preview_control_frame)
        self.preview_image_button.clicked.connect(self.load_selected_preview_images)
        self.preview_live2d_button = PushButton("", self.preview_control_frame)
        self.preview_live2d_button.clicked.connect(self.load_selected_preview_live2d)
        self.preview_close_button = PushButton("", self.preview_control_frame)
        self.preview_close_button.clicked.connect(self.close_preview_panel)
        self.preview_action_layout.addWidget(self.preview_image_button, 0, 0)
        self.preview_action_layout.addWidget(self.preview_live2d_button, 0, 1)
        self.preview_action_layout.addWidget(self.preview_close_button, 0, 2)
        self.preview_control_layout.addLayout(self.preview_action_layout)

        self.preview_hint_label = CaptionLabel("", self.preview_control_frame)
        self.preview_hint_label.setWordWrap(True)
        self.preview_control_layout.addWidget(self.preview_hint_label)
        self.compare_history_button = PushButton(tr("psd.compare.open"), self.preview_control_frame)
        self.compare_history_button.setEnabled(False)
        self.compare_history_button.clicked.connect(lambda: self.open_comparison(history=True))
        self.preview_control_layout.addWidget(self.compare_history_button)


    def _build_motion_card(self, parent):
        self.motion_frame = (EditorSurface if self._compact else CardWidget)(parent)
        self.motion_frame.setObjectName("psdMotionFrame")
        self.motion_layout = QVBoxLayout(self.motion_frame)
        self.motion_layout.setContentsMargins(12, 12, 12, 12)
        self.motion_layout.setSpacing(8)
        self.motion_title_label = SubtitleLabel("", self.motion_frame)
        self.motion_layout.addWidget(self.motion_title_label)
        self.motion_row_layout = QHBoxLayout()
        self.motion_label = BodyLabel("", self.motion_frame)
        self.motion_combo = (EditorComboBox if self._compact else ComboBox)(self.motion_frame)
        self.motion_combo.currentIndexChanged.connect(self.on_motion_selection_changed)
        self.motion_row_layout.addWidget(self.motion_label)
        self.motion_row_layout.addWidget(self.motion_combo, 1)
        self.motion_layout.addLayout(self.motion_row_layout)
        self.motion_play_button = PushButton("", self.motion_frame)
        self.motion_play_button.clicked.connect(self.play_selected_motion)
        self.motion_layout.addWidget(self.motion_play_button)
        self.motion_hint_label = CaptionLabel("", self.motion_frame)
        self.motion_hint_label.setWordWrap(True)
        self.motion_layout.addWidget(self.motion_hint_label)
        self.motion_frame.hide()



    def _build_preview_dialog(self):
        self.preview_dialog = QDialog(self)
        self.preview_dialog.setModal(False)
        self.preview_dialog.setMinimumSize(540, 360)
        self.preview_dialog.resize(960, 700)
        self.preview_dialog.finished.connect(self._on_preview_dialog_closed)
        self.preview_dialog_layout = QVBoxLayout(self.preview_dialog)
        self.preview_dialog_layout.setContentsMargins(12, 12, 12, 12)
        self.preview_frame = CardWidget(self.preview_dialog)
        self.preview_frame.setObjectName("psdPreviewFrame")
        self.preview_layout = QVBoxLayout(self.preview_frame)
        self.preview_layout.setContentsMargins(16, 16, 16, 16)
        self.preview_layout.setSpacing(10)
        self.preview_title_label = SubtitleLabel("", self.preview_frame)
        self.preview_layout.addWidget(self.preview_title_label)
        self.preview_placeholder_label = BodyLabel("", self.preview_frame)
        self.preview_placeholder_label.setAlignment(Qt.AlignCenter)
        self.preview_placeholder_label.setWordWrap(True)
        self.preview_placeholder_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview_layout.addWidget(self.preview_placeholder_label, 1)
        self.preview_image_panel = ImagePreviewPanel(self.preview_frame)
        self.preview_image_panel.setVisible(False)
        self.preview_layout.addWidget(self.preview_image_panel, 1)
        self.live2d_preview_host = QFrame(self.preview_frame)
        self.live2d_preview_host.setObjectName("psdLive2DPreviewHost")
        self.live2d_preview_host_layout = QVBoxLayout(self.live2d_preview_host)
        self.live2d_preview_host_layout.setContentsMargins(0, 0, 0, 0)
        self.live2d_preview_host_layout.setSpacing(0)
        self.live2d_preview_host.setVisible(False)
        self.preview_layout.addWidget(self.live2d_preview_host, 1)
        self.preview_dialog_layout.addWidget(self.preview_frame, 1)


    def _build_log_card(self, parent):
        self.log_frame = (EditorSurface if self._compact else CardWidget)(parent)
        self.log_frame.setObjectName("psdLogFrame")
        self.log_layout = QVBoxLayout(self.log_frame)
        self.log_layout.setContentsMargins(16, 10, 16, 10)
        self.log_layout.setSpacing(8)
        self.log_header_layout = QHBoxLayout()
        self.log_header_layout.setContentsMargins(0, 0, 0, 0)
        self.log_label = SubtitleLabel("", self.log_frame)
        self.log_header_layout.addWidget(self.log_label)
        self.log_header_layout.addStretch(1)
        # A plain Qt button avoids QFluent's per-widget palette override,
        # which otherwise makes a tiny +/- glyph disappear in dark mode.
        self.log_toggle_button = QPushButton("", self.log_frame)
        self.log_toggle_button.setObjectName("psdLogToggleButton")
        self.log_toggle_button.clicked.connect(self.toggle_log_panel)
        self.log_header_layout.addWidget(self.log_toggle_button)
        self.log_layout.addLayout(self.log_header_layout)

        self.log_text = TextEdit(self.log_frame)
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(0)
        self.log_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.log_layout.addWidget(self.log_text, 1)

    def _setup_compact_task_view(self):
        """Build shared task cards directly in their editor-owned task pages."""
        # Header labels are only controller state here; no standalone shell,
        # splitters, two-column scroll areas or nested tab controls are built.
        self.title_label = SubtitleLabel(self)
        self.current_project_title_label = BodyLabel(self)
        self.unsaved_label = SubtitleLabel(self)
        self.desc_label = CaptionLabel(self)
        self.drop_frame = QFrame(self)
        self.drop_main_label = BodyLabel(self.drop_frame)
        self.drop_sub_label = CaptionLabel(self.drop_frame)
        for widget in (self.title_label, self.current_project_title_label,
                       self.unsaved_label, self.desc_label, self.drop_frame):
            widget.hide()
        self.bound_hint = CaptionLabel(_workspace_text("psd.workspace.bound"), self)
        self.bound_hint.setWordWrap(True)
        self.bound_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.main_layout.addWidget(self.bound_hint)
        self.skin_context_label = CaptionLabel(self)
        self.skin_context_label.hide()
        self.main_layout.addWidget(self.skin_context_label)
        self.task_selector = EditorComboBox(self)
        for key in ("export", "repack", "versions", "project"):
            self.task_selector.addItem(_workspace_text("psd.workspace." + key))
        self.main_layout.addWidget(self.task_selector)
        self.workflow_tabs = QStackedWidget(self)
        self.workflow_tabs.setMinimumWidth(0)
        self.workflow_tabs.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self._compact_scrolls = []
        self._compact_pages = []
        for _key in ("export", "repack", "history", "advanced"):
            scroll = ScrollArea(self.workflow_tabs)
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            scroll.enableTransparentBackground()
            content = QWidget(scroll)
            layout = EditorViewportLayout(content)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(8)
            scroll.setWidget(content)
            self.workflow_tabs.addWidget(scroll)
            self._compact_scrolls.append(scroll)
            self._compact_pages.append((content, layout))
        self.main_layout.addWidget(self.workflow_tabs, 1)
        self._build_export_card(self._compact_pages[0][0])
        self._compact_pages[0][1].addWidget(self.export_card)
        self._build_repack_card(self._compact_pages[1][0])
        self._compact_pages[1][1].addWidget(self.repack_card)
        self._build_history_card(self._compact_pages[2][0])
        self._compact_pages[2][1].addWidget(self.preview_control_frame)
        self._build_motion_card(self._compact_pages[3][0])
        self._compact_pages[3][1].addWidget(self.motion_frame)
        self._build_project_card(self._compact_pages[3][0])
        self._compact_pages[3][1].addWidget(self.project_frame)
        self._build_workflow_controls(self)
        self.workflow_label.hide()
        self.workflow_segment.hide()
        self.right_footer = QWidget(self)
        self.right_footer_layout = QVBoxLayout(self.right_footer)
        self.right_footer_layout.setContentsMargins(0, 0, 0, 0)
        self.right_footer_layout.setSpacing(8)
        self.main_layout.addWidget(self.right_footer)
        self._build_action_controls(self.right_footer, self.right_footer_layout)
        self._build_log_card(self._compact_pages[3][0])
        self._compact_pages[3][1].addWidget(self.log_frame)
        self.capture_pose_button = PushButton(_workspace_text("psd.workspace.capture_pose"), self.export_card)
        self.capture_pose_button.clicked.connect(self.capturePoseRequested)
        self.export_card_layout.insertWidget(1, self.capture_pose_button)
        self.import_project_button = PushButton(_workspace_text("psd.workspace.import_project"), self.project_frame)
        self.import_project_button.clicked.connect(self.browse_project_file)
        self.project_layout.insertWidget(3, self.import_project_button)
        self.external_psd_button = PushButton(_workspace_text("psd.workspace.external_psd"), self.repack_card)
        self.external_psd_button.clicked.connect(self.browse_repack_psd_file)
        self.repack_card_layout.insertWidget(1, self.external_psd_button)
        self.single_repack_button = PrimaryPushButton(_workspace_text("psd.workspace.single_repack"), self.repack_card)
        self.single_repack_button.clicked.connect(lambda: self.start_reconstruction(self.selected_repack_psd_path()))
        self.repack_card_layout.addWidget(self.single_repack_button)
        self.repack_card_layout.addWidget(self.multi_repack_button)
        self.multi_repack_button.show()
        self.metadata_frame.show()
        self.apply_version_button = PrimaryPushButton(_workspace_text("psd.workspace.apply_skin_short"), self.preview_control_frame)
        self.apply_version_button.clicked.connect(lambda: self.repackApplyRequested.emit(self.preview_source_token()))
        self.variant_version_button = PushButton(_workspace_text("psd.workspace.variant"), self.preview_control_frame)
        self.variant_version_button.clicked.connect(lambda: self.repackVariantRequested.emit(self.preview_source_token()))
        self.send_mod_button = PushButton(_workspace_text("psd.workspace.send_mod"), self.preview_control_frame)
        self.send_mod_button.clicked.connect(lambda: self.repackModRequested.emit(self.preview_source_token()))
        self.version_hint = CaptionLabel(_workspace_text("psd.workspace.version_hint"), self.preview_control_frame)
        self.version_hint.setWordWrap(True)
        self.preview_control_layout.addWidget(self.version_hint)
        self.preview_control_layout.addWidget(self.apply_version_button)
        self.preview_control_layout.addWidget(self.variant_version_button)
        self.preview_control_layout.addWidget(self.send_mod_button)
        self.tools_card = (EditorSurface if self._compact else CardWidget)(self._compact_pages[3][0])
        tools = QVBoxLayout(self.tools_card)
        tools.setContentsMargins(12, 12, 12, 12)
        for button in (self.artmesh_inspector_button, self.open_output_button, self.preview_toggle_button):
            self.action_layout.removeWidget(button)
            tools.addWidget(button)
        self._compact_pages[3][1].addWidget(self.tools_card)
        self.action_layout.removeWidget(self.open_photoshop_button)
        self.export_card_layout.addWidget(self.open_photoshop_button)
        for _content, layout in self._compact_pages:
            layout.addStretch(1)
        self.stage_label.setMinimumWidth(0)
        self.stage_label.setMaximumWidth(130)
        self.project_title_label.hide()
        self.project_status_label.hide()
        self.preview_texture_label.hide()
        self.preview_texture_combo.hide()
        self.preview_hint_label.hide()
        self.version_hint.hide()
        self.output_edit.setReadOnly(True)
        self.output_button.hide()
        self._compact_button_grids = []
        for owner_layout, old_layout, buttons in (
            (self.source_layout, self.source_button_layout, (self.source_file_button, self.source_folder_button)),
            (self.repack_psd_layout, self.repack_psd_button_layout, (self.repack_photoshop_button, self.repack_psd_button)),
            (self.metadata_layout, self.metadata_button_layout, (self.metadata_button, self.metadata_default_button)),
        ):
            owner_layout.removeItem(old_layout)
            for button in buttons:
                old_layout.removeWidget(button)
            grid = QGridLayout()
            grid.setSpacing(8)
            owner_layout.addLayout(grid)
            self._compact_button_grids.append((grid, buttons))
        self.task_selector.currentIndexChanged.connect(self.workflow_tabs.setCurrentIndex)
        self.workflow_tabs.currentChanged.connect(self._compact_workflow_changed)
        self._compact_common_forms()

    def _compact_common_forms(self):
        """Keep the common controls inside the embedded task's first viewport."""
        self.export_card_layout.setContentsMargins(10, 8, 10, 8)
        self.export_card_layout.setSpacing(6)
        self.export_card_layout.removeWidget(self.export_card_title)
        self.export_card_layout.removeWidget(self.capture_pose_button)
        header = QHBoxLayout()
        header.addWidget(self.export_card_title)
        header.addStretch(1)
        header.addWidget(self.capture_pose_button)
        self.export_help_button = PushButton("?", self.export_card)
        self.export_help_button.setFixedWidth(32)
        header.addWidget(self.export_help_button)
        self.export_card_layout.insertLayout(0, header)
        self.export_card_layout.removeItem(self.export_name_layout)
        self.export_name_layout.removeWidget(self.export_name_label)
        self.export_name_layout.removeWidget(self.export_name_edit)
        self.mode_layout.removeWidget(self.mode_label)
        self.mode_layout.removeWidget(self.mode_combo)
        self.mode_container_layout.removeWidget(self.mesh_canvas_frame)
        self.mesh_canvas_layout.removeWidget(self.mesh_canvas_label)
        form = QGridLayout()
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(6)
        form.setColumnStretch(1, 1)
        for row, label, control in (
            (0, self.export_name_label, self.export_name_edit),
            (1, self.mode_label, self.mode_combo),
            (2, self.mesh_canvas_label, self.mesh_canvas_frame),
        ):
            label.setMaximumWidth(110)
            form.addWidget(label, row, 0)
            form.addWidget(control, row, 1)
        self.export_card_layout.insertLayout(1, form)
        self.export_card_layout.removeWidget(self.mode_frame)
        self.export_card_layout.removeWidget(self.export_preset_hint)
        self.mode_container_layout.removeWidget(self.mode_hint_label)
        self.mode_container_layout.removeWidget(self.mesh_canvas_hint_label)
        self.export_help_frame = QFrame(self.export_card)
        help_layout = QVBoxLayout(self.export_help_frame)
        help_layout.setContentsMargins(0, 0, 0, 0)
        help_layout.setSpacing(5)
        for label in (self.export_preset_hint, self.mesh_canvas_hint_label):
            help_layout.addWidget(label)
        self.export_card_layout.insertWidget(2, self.mode_hint_label)
        self.export_card_layout.addWidget(self.export_help_frame)
        self.export_help_frame.hide()
        self.mode_frame.hide()
        self.export_help_button.clicked.connect(
            lambda: self.export_help_frame.setVisible(not self.export_help_frame.isVisible()))

        self.repack_card_layout.setContentsMargins(10, 8, 10, 8)
        self.repack_card_layout.setSpacing(6)
        self.repack_card_layout.removeWidget(self.repack_card_title)
        self.repack_card_layout.removeWidget(self.external_psd_button)
        header = QHBoxLayout()
        header.addWidget(self.repack_card_title)
        header.addStretch(1)
        header.addWidget(self.external_psd_button)
        self.repack_card_layout.insertLayout(0, header)
        self.repack_card_layout.removeItem(self.repack_psd_layout)
        for widget in (self.pose_scheme_label, self.pose_scheme_combo):
            self.repack_psd_layout.removeWidget(widget)
        psd_buttons, metadata_buttons = self._compact_button_grids[1:]
        for button in psd_buttons[1]:
            psd_buttons[0].removeWidget(button)
        for button in metadata_buttons[1]:
            metadata_buttons[0].removeWidget(button)
        self.metadata_layout.removeItem(metadata_buttons[0])
        self.metadata_layout.removeWidget(self.metadata_label)
        self.metadata_layout.removeWidget(self.metadata_edit)
        metadata_inputs = QHBoxLayout()
        metadata_inputs.setSpacing(6)
        metadata_inputs.addWidget(self.metadata_edit, 1)
        metadata_inputs.addWidget(self.metadata_button)
        metadata_inputs.addWidget(self.metadata_default_button)
        self.metadata_layout.addLayout(metadata_inputs)
        self.repack_card_layout.removeWidget(self.metadata_frame)
        self.repack_card_layout.removeItem(self.texture_name_layout)
        self.texture_name_layout.removeWidget(self.texture_name_label)
        self.texture_name_layout.removeWidget(self.texture_name_edit)
        form = QGridLayout()
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(6)
        form.setColumnStretch(1, 1)
        form.addWidget(self.pose_scheme_label, 0, 0)
        form.addWidget(self.pose_scheme_combo, 0, 1)
        form.addWidget(self.repack_photoshop_button, 0, 2)
        form.addWidget(self.metadata_label, 1, 0)
        form.addWidget(self.metadata_frame, 1, 1, 1, 2)
        form.addWidget(self.texture_name_label, 2, 0)
        form.addWidget(self.texture_name_edit, 2, 1, 1, 2)
        self.repack_card_layout.insertLayout(1, form)
        self.texture_name_label.show()
        self.texture_name_edit.show()
        for label in (self.pose_scheme_label, self.metadata_label, self.texture_name_label):
            label.setMaximumWidth(100)
        self.repack_card_layout.removeWidget(self.single_repack_button)
        self.repack_card_layout.removeWidget(self.multi_repack_button)
        actions = QGridLayout()
        actions.setSpacing(6)
        self.repack_card_layout.addLayout(actions)
        self.tools_card.layout().addWidget(self.repack_psd_button)
        self._compact_button_grids = self._compact_button_grids[:1] + [
            (actions, (self.single_repack_button, self.multi_repack_button))]

    def _compact_control_text(self):
        self.capture_pose_button.setText(_workspace_text("psd.workspace.capture_pose_short"))
        self.capture_pose_button.setToolTip(_workspace_text("psd.workspace.capture_pose"))
        for button in (self.capture_pose_button, self.external_psd_button,
                       self.single_repack_button, self.multi_repack_button):
            button.setMinimumWidth(max(74, button.fontMetrics().horizontalAdvance(button.text()) + 28))
        self.export_help_button.setFixedWidth(32)
        self.export_help_button.setToolTip(_workspace_text("psd.workspace.help"))
        self.export_help_button.setAccessibleName(_workspace_text("psd.workspace.help"))
        self.export_name_edit.setToolTip(self.export_preset_hint.text())
        self.mode_combo.setToolTip(self.mode_hint_label.text())
        self.mesh_canvas_frame.setToolTip(self.mesh_canvas_hint_label.text())
        self.mesh_canvas_preset_combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.mesh_canvas_spin.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.mesh_canvas_input_layout.setStretch(0, 1)
        self.mesh_canvas_input_layout.setStretch(1, 2)
        for button, icon, key in (
            (self.repack_photoshop_button, FluentIcon.EDIT, "psd.pose.open_photoshop"),
            (self.metadata_button, FluentIcon.FOLDER, "psd.browse_metadata"),
            (self.metadata_default_button, FluentIcon.SYNC, "psd.use_default_metadata"),
        ):
            button.setIcon(icon)
            button.setText("")
            button.setFixedWidth(32)
            button.setToolTip(tr(key))
            button.setAccessibleName(tr(key))
        if hasattr(self, "context_paths_toggle"):
            self.context_paths_toggle.setText(_workspace_text("psd.workspace.paths"))

    def configure_task_embedding(self):
        """Let the Appearance page own navigation, feedback and texture preview."""
        if not self._compact or self._task_embedded:
            return
        self._task_embedded = True
        # Keep the primary action next to its settings instead of across a
        # large empty viewport. Other actions live in their workflow cards.
        self.action_layout.removeWidget(self.reconstruct_button)
        self.export_card_layout.addWidget(self.reconstruct_button, 0, Qt.AlignRight)
        self.reconstruct_button.setMinimumWidth(160)
        self.reconstruct_button.setMaximumWidth(320)
        self.right_footer.hide()
        self.task_selector.hide()
        self.bound_hint.hide()
        self.skin_context_label.hide()
        self.log_frame.hide()
        self.progress_bar.hide()
        self.stage_label.hide()
        self.preview_toggle_button.hide()
        self.export_card_title.hide()
        self.repack_card_title.hide()
        self.source_label.hide()
        self.source_edit.hide()
        self.source_file_button.hide()
        self.source_folder_button.hide()
        self.output_label.hide()
        self.output_edit.hide()
        self.export_card_layout.removeItem(self.source_layout)
        self.export_card_layout.removeItem(self.output_layout)
        self.repack_card_layout.removeItem(self.repack_output_layout)
        parent, layout = self._compact_pages[3]
        self.context_paths_toggle = PushButton(_workspace_text("psd.workspace.paths"), parent)
        self.context_paths_frame = (EditorSurface if self._compact else CardWidget)(parent)
        paths = QVBoxLayout(self.context_paths_frame)
        paths.setContentsMargins(10, 8, 10, 8)
        paths.addLayout(self.source_layout)
        paths.addLayout(self.output_layout)
        paths.addLayout(self.repack_output_layout)
        for widget in (self.source_label, self.source_edit, self.output_label, self.output_edit):
            widget.show()
        self.output_edit.setReadOnly(True)
        self.context_paths_frame.hide()
        self.context_paths_toggle.clicked.connect(
            lambda: self.context_paths_frame.setVisible(not self.context_paths_frame.isVisible()))
        layout.insertWidget(0, self.context_paths_toggle)
        layout.insertWidget(1, self.context_paths_frame)
        self._compact_control_text()
        self._update_version_skin_action()

    def show_task(self, task: str):
        if not self._compact:
            self.set_workflow("repack" if task == "repack" else "export")
            return
        aliases = {"versions": "history", "project": "advanced", "edit": "repack"}
        key = aliases.get(str(task), str(task))
        index = ("export", "repack", "history", "advanced").index(key)
        self.workflow_tabs.setCurrentIndex(index)
        if key in {"export", "repack"} and self.workflow != key:
            self.set_workflow(key)

    def set_version_skin_provider(self, provider):
        self._version_skin_provider = provider
        self._update_version_skin_action()

    def select_version(self, token: str) -> bool:
        index = self._combo_index_by_data(self.preview_source_combo, str(token))
        if index < 0:
            return False
        self.preview_source_combo.setCurrentIndex(index)
        self._update_version_skin_action()
        self._emit_context_changed()
        return True

    def _update_version_skin_action(self):
        if not self._compact or not hasattr(self, "apply_version_button"):
            return
        token = self.preview_source_token()
        record = self._version_skin_provider(token) if self._version_skin_provider and token else None
        current = bool(record and record.get("is_current"))
        key = "psd.workspace.applied" if current else ("psd.workspace.apply_existing" if record else "psd.workspace.apply_skin_short")
        self.apply_version_button.setText(_workspace_text(key))
        self.apply_version_button.setEnabled(not self._is_busy and bool(self.preview_texture_paths_for_current_source()) and not current)
        self.variant_version_button.setEnabled(not self._is_busy and token.startswith(("repack:", "composite:")))
        self.variant_version_button.setVisible(bool(record))

    def _emit_context_changed(self):
        self._update_version_skin_action()
        self.contextChanged.emit({"token": self.preview_source_token(),
                                  "skin_source": copy.deepcopy(self.skin_context),
                                  "psd_path": self.selected_repack_psd_path(),
                                  "project_file": str(self.current_project.project_file) if self.current_project else ""})

    def set_skin_context(self, context):
        self.skin_context = dict(context) if context else None
        if not self._compact:
            return
        self.skin_context_label.setVisible(bool(context) and not self._task_embedded)
        if context:
            label = skin_text("editor.skin.psd_current", name=context.get("name", ""))
            self.skin_context_label.setText(label)
            self.skin_context_label.setToolTip(label + "\n" + skin_text("editor.skin.psd_hint"))
        self.apply_version_button.setText(_workspace_text("psd.workspace.apply_skin_short"))
        self.apply_version_button.setToolTip(skin_text("editor.skin.psd_save"))
        self.apply_version_button.setMinimumWidth(max(74, self.apply_version_button.fontMetrics().horizontalAdvance(self.apply_version_button.text()) + 28))
        self.send_mod_button.setText(skin_text("editor.skin.psd_viewer"))
        self._update_version_skin_action()

    def _refresh_skin_labels(self):
        if self._compact:
            self.set_skin_context(self.skin_context)

    def _compact_workflow_changed(self, index: int):
        self.task_selector.blockSignals(True)
        self.task_selector.setCurrentIndex(index)
        self.task_selector.blockSignals(False)
        workflow = "repack" if index else "export"
        if index < 2 and self.workflow != workflow:
            self.set_workflow(workflow)
        self._update_workflow_ui()
        self._arrange_timer.start(0)

    def hasHeightForWidth(self):  # noqa: N802
        return False if self._compact else super().hasHeightForWidth()

    def heightForWidth(self, width):  # noqa: N802
        return -1 if self._compact else super().heightForWidth(width)

    def retranslate_ui(self):
        self.title_label.setText(tr("psd.title"))
        self.desc_label.setText(tr("psd.description"))
        self.drop_main_label.setText(tr("psd.drop_main"))
        self.drop_sub_label.setText(tr("psd.drop_sub"))
        self.project_title_label.setText(tr("psd.project.title"))
        self.project_name_edit.setPlaceholderText(tr("psd.project.name_placeholder"))
        self.project_combo.setPlaceholderText(tr("psd.project.search_placeholder"))
        self.new_project_button.setText(tr("psd.project.new"))
        self.save_project_button.setText(tr("psd.project.save"))
        self.open_project_folder_button.setText(tr("psd.project.open_folder"))
        self.project_status_label.setText(tr("psd.project.no_project"))
        self.pose_scheme_label.setText(tr("psd.pose.scheme"))
        self.metadata_label.setText(tr("psd.metadata_file"))
        self.metadata_edit.setPlaceholderText(tr("psd.placeholder_metadata"))
        self.metadata_button.setText(tr("psd.browse_metadata"))
        self.metadata_default_button.setText(tr("psd.use_default_metadata"))
        self.export_card_title.setText(tr("psd.export.card_title"))
        self.repack_card_title.setText(tr("psd.repack.card_title"))
        self.export_name_label.setText(tr("psd.export.name"))
        self.export_name_edit.setPlaceholderText(tr("psd.export.name_placeholder"))
        self.export_preset_hint.setText(tr("psd.export.preset_hint"))
        self.repack_psd_button.setText(tr("psd.repack.open_psd_directory"))
        self.repack_photoshop_button.setText(tr("psd.pose.open_photoshop"))
        self.texture_name_label.setText(tr("psd.repack.texture_name"))
        self.texture_name_edit.setPlaceholderText(
            tr("psd.repack.texture_name_placeholder")
        )
        self.repack_output_label.setText(tr("psd.repack.output"))
        self.source_label.setText(tr("psd.source"))
        self.source_edit.setPlaceholderText(tr("psd.placeholder_source"))
        self.source_file_button.setText(tr("psd.browse_file"))
        self.source_folder_button.setText(tr("psd.browse_folder"))
        self.mode_label.setText(tr("psd.mode"))
        self.mode_combo.setItemText(0, tr("psd.mode.mesh_pose"))
        self.mode_combo.setItemText(1, tr("psd.mode.editable_atlas"))
        self.mesh_canvas_label.setText(tr("psd.mesh_canvas.max_dimension"))
        self.mesh_canvas_preset_combo.setItemText(2, tr("psd.mesh_canvas.custom"))
        self.mesh_canvas_spin.setSpecialValueText("0 px" if self._compact else tr("psd.mesh_canvas.original"))
        self.mesh_canvas_spin.setToolTip(tr("psd.mesh_canvas.original"))
        self.mesh_canvas_hint_label.setText(tr("psd.mesh_canvas.hint"))
        self.output_label.setText(tr("psd.output_directory"))
        self.output_edit.setPlaceholderText(tr("psd.placeholder_output"))
        self.output_button.setText(tr("common.browse"))
        self.reconstruct_button.setText(tr("psd.reconstruct_button"))
        self.open_output_button.setText(tr("psd.open_output_folder"))
        self.open_photoshop_button.setText(tr("psd.pose.open_photoshop"))
        for button in (self.compare_repack_button, self.compare_history_button):
            button.setText(tr("psd.compare.open"))
        self.edit_working_png_button.setText(tr("psd.edit.working_png"))
        self.edit_working_png_button.setToolTip(tr("psd.edit.working_png_hint"))
        self.artmesh_inspector_button.setText(tr("psd.inspector.title"))
        self.preview_toggle_button.setText(
            tr("psd.preview.hide") if (self.preview_dialog is not None and self.preview_dialog.isVisible()) else tr("psd.preview.show")
        )
        self.preview_control_title_label.setText(tr("psd.preview.controls"))
        self.preview_source_label.setText(tr("psd.preview.source"))
        self.preview_texture_label.setText(tr("psd.preview.texture"))
        self.preview_image_button.setText(tr("psd.preview.load_images"))
        self.preview_live2d_button.setText(tr("psd.preview.load_live2d"))
        self.preview_close_button.setText(tr("psd.preview.close"))
        self.preview_hint_label.setText(tr("psd.preview.hint_live2d_enabled"))
        self.motion_title_label.setText(tr("psd.preview.motion_title"))
        self.motion_label.setText(tr("psd.preview.motion"))
        self.motion_play_button.setText(tr("psd.preview.play_motion"))
        self.motion_hint_label.setText(tr("psd.preview.motion_hint"))
        if self.preview_dialog is not None:
            self.preview_title_label.setText(tr("psd.preview.title"))
            self.preview_dialog.setWindowTitle(tr("psd.preview.title"))
            self.preview_placeholder_label.setText(tr("psd.preview.placeholder"))
            self.preview_image_panel.retranslate_ui()
        self.log_label.setText(tr("psd.log"))
        self._update_log_toggle_button()
        self.workflow_label.setText(tr("psd.workflow"))
        self.export_flow_button.setText(tr("psd.workflow.export"))
        self.repack_flow_button.setText(tr("psd.workflow.repack"))
        self.stage_label.setText(tr("psd.stage.idle"))
        self._update_workflow_ui()
        self.refresh_project_ui()
        self.refresh_project_combo()
        self.refresh_preview_source_combo()
        self._update_project_header()
        self._configure_responsive_controls()
        self._arrange_button_rows()
        if self._compact and hasattr(self, "workflow_tabs"):
            for index, key in enumerate(("export", "repack", "versions", "project")):
                self.task_selector.setItemText(index, _workspace_text("psd.workspace." + key))
            for widget, key in ((self.bound_hint, "bound"), (self.capture_pose_button, "capture_pose"),
                                (self.import_project_button, "import_project"), (self.external_psd_button, "external_psd"),
                                (self.single_repack_button, "single_repack"), (self.multi_repack_button, "multi_repack"),
                                (self.apply_version_button, "apply_version"), (self.send_mod_button, "send_mod"),
                                (self.variant_version_button, "variant"), (self.version_hint, "version_hint")):
                widget.setText(_workspace_text("psd.workspace." + key))
            self._update_project_header()
            self._refresh_skin_labels()
            self._compact_control_text()
            self._arrange_button_rows()
        self._refresh_shared_uv_option()

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if self._is_supported_path(url.toLocalFile()):
                    event.acceptProposedAction()
                    return

    def dropEvent(self, event: QDropEvent):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if self._is_supported_path(path):
                self.set_source(path)
                event.acceptProposedAction()
                return

    def browse_source_file(self):
        title = tr("dialog.select_live2d_psd_source")
        file_filter = tr("dialog.filter_live2d_psd_sources")
        path, _ = QFileDialog.getOpenFileName(
            self,
            title,
            "",
            file_filter,
        )
        if path:
            self.set_source(path)

    def browse_source_folder(self):
        path = QFileDialog.getExistingDirectory(
            self,
            tr("dialog.select_live2d_model_folder"),
        )
        if path:
            self.set_source(path)

    def browse_metadata_file(self):
        source = self.selected_repack_psd_path()
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("dialog.select_psd_metadata_file"),
            str(Path(source).parent) if source else "",
            tr("dialog.filter_lpkpsd_metadata_files"),
        )
        if path:
            self.selected_metadata = os.path.abspath(path)
            self.metadata_edit.setText(self.selected_metadata)
            self._update_artmesh_inspector_button()
            self.mark_project_dirty()
            self.append_log(tr("psd.selected_metadata", path=self.selected_metadata))

    def clear_metadata_file(self):
        self.selected_metadata = ""
        self.metadata_edit.clear()
        self._update_artmesh_inspector_button()
        self.mark_project_dirty()
        self.append_log(tr("psd.metadata_default_log"))

    def browse_output(self):
        path = QFileDialog.getExistingDirectory(
            self,
            tr("dialog.select_output_directory"),
            self.output_edit.text(),
        )
        if path:
            output_path = os.path.abspath(path)
            self.output_manually_selected = True
            self.output_edit.setText(output_path)
            self.last_output_dir = output_path

    def set_source(self, path: str):
        selected = os.path.abspath(path)
        if Path(selected).suffix.lower() == ".psd":
            self.set_workflow("repack")
            self._set_manual_repack_psd(selected)
            self._sync_default_metadata()
            self._sync_repack_output_dir()
            self.append_log(tr("psd.selected_source", path=selected))
            return

        if self._compact:
            self.sourceRequested.emit(selected)
            return

        self.selected_source = selected
        self.source_edit.setText(self.selected_source)
        self.set_workflow("export")
        if not self.output_manually_selected:
            self.last_output_dir = self.default_output_dir(self.selected_source)
            self.output_edit.setText(self.last_output_dir)
        self.mark_project_dirty()
        self.append_log(tr("psd.selected_source", path=self.selected_source))

    def browse_repack_psd_file(self):
        start_dir = ""
        current = self.selected_repack_psd_path()
        if current:
            start_dir = str(Path(current).parent)
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("dialog.select_psd_file"),
            start_dir,
            tr("dialog.filter_psd_files"),
        )
        if path:
            self._set_manual_repack_psd(path)
            self._sync_default_metadata()
            self._sync_repack_output_dir()
            self.mark_project_dirty()
            self.append_log(tr("psd.selected_source", path=os.path.abspath(path)))

    def project_psd_choices(self) -> list[tuple[str, str]]:
        if not self.current_project:
            return []
        choices: list[tuple[str, str]] = []
        for scheme in self.current_project.data.get("pose_schemes", []):
            if not isinstance(scheme, dict) or not scheme.get("psd"):
                continue
            path = resolve_project_path(self.current_project, scheme["psd"])
            if not path.is_file():
                continue
            name = str(scheme.get("name") or scheme.get("id") or path.stem)
            choices.append((f"{name} — {path.name}", str(path)))
        return choices

    def open_project_psd_folder(self):
        if not self.current_project:
            return
        folder = self.current_project.project_dir / "psd"
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder.resolve())))

    def _artmesh_metadata_path(self) -> Path | None:
        candidates: list[str] = [
            str(self.selected_metadata or ""),
            str(self.metadata_edit.text().strip() if hasattr(self, "metadata_edit") else ""),
        ]
        source = self.selected_repack_psd_path()
        if source:
            candidates.append(str(Path(source).with_suffix(".lpkpsd.json")))
        latest = self.latest_project_export()
        if latest:
            candidates.append(str(latest.get("metadata") or ""))
        for value in candidates:
            if not value:
                continue
            path = Path(value)
            if self.current_project and not path.is_absolute():
                path = resolve_project_path(self.current_project, path)
            if path.is_file():
                return path.resolve()
        return None

    def _update_artmesh_inspector_button(self) -> None:
        if not hasattr(self, "artmesh_inspector_button"):
            return
        self.artmesh_inspector_button.setEnabled(
            not self._is_busy and self._artmesh_metadata_path() is not None
        )

    def open_artmesh_inspector(self) -> None:
        metadata_path = self._artmesh_metadata_path()
        if metadata_path is None:
            self.append_log(tr("psd.inspector.choose_first"))
            return
        if self.artmesh_inspector_dialog is not None:
            self.artmesh_inspector_dialog.inspector.load_metadata(metadata_path)
            self.artmesh_inspector_dialog.show()
            self.artmesh_inspector_dialog.raise_()
            self.artmesh_inspector_dialog.activateWindow()
            return
        dialog = ArtMeshInspectorDialog(metadata_path, self)
        self.artmesh_inspector_dialog = dialog
        dialog.finished.connect(lambda _result: setattr(self, "artmesh_inspector_dialog", None))
        dialog.show()

    def _set_manual_repack_psd(self, path: str):
        self.manual_repack_psd = os.path.abspath(path)
        self.refresh_pose_scheme_controls(selected_token=f"file:{self.manual_repack_psd}")

    def selected_pose_scheme(self) -> dict | None:
        if not self.current_project:
            return None
        token = str(self.pose_scheme_combo.currentData() or "")
        if not token or token.startswith("file:"):
            return None
        return find_pose_scheme(self.current_project, token)

    def selected_repack_psd_path(self) -> str:
        token = str(self.pose_scheme_combo.currentData() or "")
        if token.startswith("file:"):
            path = token[5:]
            return path if Path(path).is_file() else ""
        scheme = self.selected_pose_scheme()
        if scheme and scheme.get("psd"):
            path = resolve_project_path(self.current_project, scheme["psd"])
            return str(path) if path.is_file() else ""
        return self.manual_repack_psd if Path(self.manual_repack_psd).is_file() else ""

    def on_texture_name_changed(self, *_args):
        self._sync_repack_output_dir()
        self.mark_project_dirty()

    def _prompt_non_empty_name(self, title_key: str, prompt_key: str, default: str = "") -> str:
        value, accepted = get_editor_text(
            self,
            tr(title_key),
            tr(prompt_key),
            text=default,
        )
        return value.strip() if accepted else ""

    def refresh_parameter_preset_controls(
        self,
        selected_id: str | None = None,
    ):
        if not hasattr(self, "export_name_edit"):
            return
        selected = selected_id
        if selected is None:
            selected = str(self.export_name_edit.currentData() or "")
        if not selected and self.current_project:
            selected = str(
                self.current_project.data.get("selected_parameter_preset")
                or "default"
            )
        selected = selected or "default"

        if self.current_project:
            presets = [
                dict(item)
                for item in self.current_project.data.get("parameter_presets", [])
                if isinstance(item, dict)
            ]
        else:
            presets = [
                {
                    "id": "default",
                    "name": "Default",
                    "parameters": {},
                    "source": "initial",
                    "is_default": True,
                }
            ]

        self._parameter_preset_refreshing = True
        self.export_name_edit.blockSignals(True)
        self.export_name_edit.clear()
        labels: list[str] = []
        for preset in presets:
            preset_id = str(preset.get("id") or "")
            if not preset_id:
                continue
            label = (
                tr("psd.export.default_preset")
                if bool(preset.get("is_default")) or preset_id == "default"
                else str(preset.get("name") or preset_id)
            )
            self.export_name_edit.addItem(label, userData=preset_id)
            labels.append(label)
        self._parameter_preset_completer_model.setStringList(labels)
        index = self._combo_index_by_data(self.export_name_edit, selected)
        self.export_name_edit.setCurrentIndex(
            index if index >= 0 else (0 if self.export_name_edit.count() else -1)
        )
        self.export_name_edit.blockSignals(False)
        self._parameter_preset_refreshing = False
        self._update_export_preset_hint()
        self._update_action_availability()

    def selected_parameter_preset(self) -> dict | None:
        preset_id = str(self.export_name_edit.currentData() or "")
        if not preset_id:
            return None
        if self.current_project:
            return find_parameter_preset(self.current_project, preset_id)
        if preset_id == "default":
            return {
                "id": "default",
                "name": "default",
                "parameters": {},
                "source": "initial",
                "is_default": True,
            }
        return None

    def on_parameter_preset_changed(self, *_args):
        if self._parameter_preset_refreshing:
            return
        preset = self.selected_parameter_preset()
        self._update_export_preset_hint()
        self._update_action_availability()
        if not preset or not self.current_project:
            return
        try:
            self.current_project = select_parameter_preset(
                self.current_project,
                str(preset.get("id") or ""),
            )
            self.projectChanged.emit(self.current_project)
        except Exception as exc:
            self.append_log(tr("psd.warning_prefix", message=str(exc)))

    def _update_export_preset_hint(self, *_args):
        if not hasattr(self, "export_preset_hint"):
            return
        preset = self.selected_parameter_preset()
        if not preset:
            self.export_preset_hint.setText(
                tr("psd.export.invalid_preset_hint")
            )
            return
        if bool(preset.get("is_default")) or preset.get("id") == "default":
            self.export_preset_hint.setText(tr("psd.export.preset_hint"))
            return
        self.export_preset_hint.setText(
            tr(
                "psd.export.selected_preset_hint",
                name=str(preset.get("name") or preset.get("id") or ""),
                count=len(dict(preset.get("parameters") or {})),
            )
        )

    def _capture_psd_snapshot(self) -> PsdSnapshotRequest:
        """Capture detached values on the GUI; all filesystem work is deferred."""
        context = copy.deepcopy(self.skin_context)
        if self.export_snapshot_request_provider:
            request = self.export_snapshot_request_provider()
            if isinstance(request, PsdSnapshotRequest):
                return request
            if not callable(getattr(request, "write", None)):
                raise TypeError("PSD export requires a detached snapshot request.")
            return PsdSnapshotRequest(source_origin="appearance", skin_source=context,
                                      export_request=request)
        return PsdSnapshotRequest(str(self.current_project.base_model_json),
                                  source_origin="original", skin_source=context)

    def _make_pose_stage_request(self, name, scheme_id, parameters, *,
                                 selection=None, pose_source="preview", preset_id=""):
        project = self.current_project
        return {"project_dir": str(project.project_dir),
                "project_file": str(project.project_file),
                "project_data": copy.deepcopy(project.data),
                "snapshot": self._capture_psd_snapshot(),
                "name": str(name), "scheme_id": str(scheme_id), "priority": 0,
                "parameters": dict(parameters), "selection": copy.deepcopy(selection),
                "skin_source": copy.deepcopy(self.skin_context),
                "pose_source": pose_source, "parameter_preset_id": preset_id}

    def _emit_task_state(self):
        phase = {
            "idle": tr("psd.stage.idle"), "preparing": _workspace_text("psd.task.preparing"),
            "running": self.stage_label.text(), "succeeded": tr("psd.stage.done"),
            "failed": tr("psd.stage.failed"), "cancelled": _workspace_text("psd.task.cancelled"),
        }.get(self._task_state, "")
        payload = {"task": self._task_id, "state": self._task_state,
                   "phase": phase, "busy": self._is_busy,
                   "progress": self._task_progress, "message": self._task_message,
                   "details": self.log_text.toPlainText()}
        self.taskStateChanged.emit(payload)
        return payload

    def _begin_task(self, task: str, message: str):
        self._task_id = task
        self._task_progress = 0
        self._task_state = "preparing"
        self._task_message = message
        self._task_result_committed = False
        self._pending_skin_ready_token = ""
        self._pending_preview_ready = None
        self.log_text.clear()
        self.set_busy(True)
        self.progress_bar.setValue(0)
        self.stage_label.setText(tr("psd.stage.starting"))
        self.taskStarted.emit(task)
        self._emit_task_state()

    def _start_worker(self, worker: PsdReconstructionThread):
        self.worker = worker
        worker.progressUpdated.connect(self.on_progress_updated)
        worker.reconstructionFinished.connect(self.on_reconstruction_finished)
        worker.reconstructionError.connect(self.on_reconstruction_error)
        worker.reconstructionCancelled.connect(self.on_reconstruction_cancelled)
        worker.finished.connect(self._on_worker_stopped)
        worker.start()

    def _on_worker_stopped(self):
        worker = self.sender()
        if isinstance(worker, PsdReconstructionThread):
            # Release the static asset lease only after run() has returned.
            worker.pose_stage_request = None
            worker.cleanup_job()
        elif isinstance(worker, PsdEditorProjectPreparationThread):
            worker.snapshot_request.close()
            worker.snapshot_request = None
        self.set_busy(False)
        payload = self._emit_task_state()
        if self._task_state == "succeeded":
            self.taskFinished.emit(payload)
        elif self._task_state == "failed":
            self.taskFailed.emit(self._task_message)
        token = self._pending_skin_ready_token
        self._pending_skin_ready_token = ""
        if token:
            self.repackSkinReady.emit(token)
        ready = self._pending_preview_ready
        self._pending_preview_ready = None
        if ready:
            self.unifiedPreviewRequested.emit(*ready)

    def export_selected_artmeshes(self, drawable_ids, pose, region=None, *, mesh_data=None,
                                 mode=None, atlas_layout="packed", output_name=None) -> bool:
        """Export a frozen selection and pose through one private worker stage."""
        if self.is_busy():
            self.append_log(_workspace_text("psd.selection.busy"))
            return False
        ids = list(dict.fromkeys(str(value) for value in drawable_ids or []))
        if not ids:
            self.append_log(_workspace_text("psd.selection.empty"))
            return False
        if not self.current_project:
            self.append_log(_workspace_text("psd.selection.no_project"))
            return False
        try:
            values = dict(pose or {})
            structured_pose = any(key in values for key in ("parameters", "parts", "drawables"))
            parameter_values = values.get("parameters", {}) if structured_pose else values
            parameters = {str(key): float(value) for key, value in dict(parameter_values).items()}
            parts = {str(key): float(value) for key, value in dict(values.get("parts") or {}).items()}
            drawables = {str(key): float(value) for key, value in dict(values.get("drawables") or {}).items()}
            if not all(math.isfinite(value) for value in [*parameters.values(), *parts.values(), *drawables.values()]):
                raise ValueError("Pose parameters and opacities must be finite.")
            if (parts or drawables) and mesh_data is None:
                raise ValueError("Current opacity overrides require the editor's exact drawable pose snapshot.")
            mode = mode or self.mode_combo.currentData() or "mesh"
            name = output_name or self._prompt_non_empty_name(
                "psd.project.new_dialog_title", "psd.export.name", _workspace_text("psd.selection.default_name"))
            if not name:
                return False
            self._begin_task("selection-export", _workspace_text("psd.task.preparing"))
            exact_mesh = copy.deepcopy(mesh_data)
            exact_pose = copy.deepcopy(values)
            exact_pose.update(parameters=parameters, parts=parts, drawables=drawables)
            if exact_mesh is not None:
                exact_mesh["selection_pose"] = exact_pose
            scheme_id = "selection-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            stage_request = self._make_pose_stage_request(
                name, scheme_id, parameters,
                selection={"drawable_ids": ids, "region": copy.deepcopy(region), "parts": parts,
                           "drawables": drawables, "pose": exact_pose})
            self.pending_repack_id = ""
            self.pending_multi_psd_paths = []
            self.pending_pose_scheme_id = scheme_id
            self.pending_skin_context = copy.deepcopy(self.skin_context)
            self.set_workflow("export")
            self.last_output_dir = str(self.current_project.project_dir / "psd" / scheme_id)
            self.output_edit.setText(self.last_output_dir)
            self.append_log(tr("psd.selection.atlas_hint") if mode == "atlas-components"
                            else _workspace_text("psd.selection.hint"))
            self._start_worker(PsdReconstructionThread(
                str(self.current_project.base_model_json), self.last_output_dir, mode,
                parameter_values=parameters, pose_name=name, output_name=scheme_id,
                resource_limits=self.settings_manager.get("psd.resource_limits", {}),
                selected_drawable_ids=ids, selection_region=region, mesh_data=exact_mesh,
                pose_stage_request=stage_request, atlas_layout=atlas_layout))
            return True
        except Exception as exc:
            self.on_reconstruction_error(str(exc))
            return False

    def start_reconstruction(self, direct_psd_path: str = ""):
        if self.is_busy() or (self._compact and not self.current_project):
            return
        self.pending_repack_id = ""
        self.pending_repack_dir = ""
        self.pending_repack_source = ""
        self.pending_repack_metadata = ""
        self.pending_repack_name = ""
        self.pending_multi_psd_paths = []
        self.pending_pose_scheme_id = ""

        metadata_path = None
        parameter_values = None
        pose_name = None
        output_name = None
        allow_shared_uv = False
        pose_stage_request = None

        if self.workflow == "repack" and not direct_psd_path:
            self.start_multi_repack()
            return

        if self.workflow == "repack":
            metadata_path = self.metadata_edit.text().strip() or None
            source = direct_psd_path or self.selected_repack_psd_path()
            self._shared_uv_multimode = False
            self._refresh_shared_uv_option(source=source, metadata_path=metadata_path)
            allow_shared_uv = self._shared_uv_available and self.shared_uv_checkbox.isChecked()
            if not source:
                InfoBar.warning(
                    title=tr("common.warning"),
                    content=tr("psd.warning_no_psd_source"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=3000,
                )
                return

            texture_name = Path(source).stem

            scheme = self.pose_scheme_for_psd(source) or self.selected_pose_scheme()
            scheme_id = str((scheme or {}).get("id") or "")
            if self.current_project:
                version_id, version_dir = create_repack_dir(
                    self.current_project,
                    scheme_id,
                    texture_name,
                )
                output_dir = str(version_dir)
                self.pending_repack_id = version_id
                self.pending_repack_dir = output_dir
                self.pending_pose_scheme_id = scheme_id
            else:
                output_dir = str(Path(source).resolve().parent / sanitize_project_name(texture_name))
            mode = "repack-atlas"
            mode_text = tr("psd.workflow.repack")
            self.pending_repack_source = source
            self.pending_repack_metadata = metadata_path or ""
            self.pending_repack_name = texture_name
            self.repack_output_edit.setText(os.path.abspath(output_dir))
        else:
            source = (
                str(self.current_project.base_model_json)
                if self.current_project
                else self.source_edit.text().strip()
            )
            if not source:
                InfoBar.warning(
                    title=tr("common.warning"),
                    content=tr("psd.warning_no_source"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=3000,
                )
                return
            if Path(source).suffix.lower() == ".psd":
                InfoBar.warning(
                    title=tr("common.warning"),
                    content=tr("psd.warning_export_needs_model"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=3000,
                )
                return

            preset = self.selected_parameter_preset()
            if not preset:
                InfoBar.warning(
                    title=tr("common.warning"),
                    content=tr("psd.warning_no_parameter_preset"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=3500,
                )
                return
            pose_name = str(preset.get("name") or preset.get("id") or "").strip()
            parameter_values = {
                str(key): float(value)
                for key, value in dict(preset.get("parameters") or {}).items()
            }
            preset_id = str(preset.get("id") or "")
            pose_source = str(preset.get("source") or "preview")

            mode = self.mode_combo.currentData() or "mesh"
            mode_text = self.mode_combo.currentText()
            if self.current_project:
                self._begin_task("export", _workspace_text("psd.task.preparing"))
                try:
                    scheme_id = preset_id
                    if find_pose_scheme(self.current_project, scheme_id) or (self.current_project.project_dir / "psd" / scheme_id).exists():
                        scheme_id = f"{preset_id}-{datetime.now():%Y%m%d-%H%M%S-%f}"
                    pose_stage_request = self._make_pose_stage_request(
                        pose_name, scheme_id, parameter_values,
                        pose_source=pose_source, preset_id=preset_id)
                except Exception as exc:
                    self.on_reconstruction_error(str(exc))
                    return
                self.pending_pose_scheme_id = scheme_id
                self.pending_skin_context = copy.deepcopy(self.skin_context)
                output_name = scheme_id
                output_dir = str(self.current_project.project_dir / "psd" / scheme_id)
            else:
                output_dir = (
                    self.output_edit.text().strip()
                    or self.default_output_dir(source)
                )
                output_name = sanitize_project_name(pose_name)
            self.output_edit.setText(os.path.abspath(output_dir))

        self.last_output_dir = os.path.abspath(output_dir)
        if not self._is_busy:
            self._begin_task("repack" if mode == "repack-atlas" else "export", tr("psd.stage.starting"))
        self.append_log(tr("psd.started", mode=mode_text))
        if mode == "repack-atlas":
            self.append_log(tr("psd.metadata_default_log"))

        worker = PsdReconstructionThread(
            source,
            self.last_output_dir,
            mode,
            metadata_path,
            parameter_values=parameter_values,
            pose_name=pose_name,
            output_name=output_name,
            resource_limits=self.settings_manager.get("psd.resource_limits", {}),
            allow_shared_uv=allow_shared_uv,
            pose_stage_request=pose_stage_request,
        )
        self._start_worker(worker)

    def start_multi_repack(self):
        if self.is_busy():
            return
        self.shared_uv_checkbox.setChecked(False)
        self._shared_uv_multimode = True
        self.shared_uv_checkbox.setEnabled(False)
        initial_psd = self.selected_repack_psd_path()
        project_psds = self.project_psd_choices()
        if not project_psds:
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("psd.multi.no_project_psd"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3500,
            )
            return
        dialog = PsdMultiRepackDialog(self, project_psds, initial_psd)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        psd_paths = dialog.ordered_paths()
        if len(psd_paths) == 1:
            self.start_reconstruction(psd_paths[0])
            return
        texture_name = dialog.texture_name_edit.text().strip()
        if not self.current_project:
            output_dir = Path(psd_paths[0]).resolve().parent / "multi" / sanitize_project_name(texture_name)
            output_dir.mkdir(parents=True, exist_ok=True)
            version_id = ""
        else:
            version_id, output_dir = create_multi_repack_dir(self.current_project, texture_name)

        self.pending_repack_id = version_id
        self.pending_repack_dir = str(output_dir)
        self.pending_repack_name = texture_name
        self.pending_multi_psd_paths = psd_paths
        self.last_output_dir = str(output_dir.resolve())
        self.repack_output_edit.setText(self.last_output_dir)
        self._begin_task("multi-repack", tr("psd.stage.starting"))
        self.append_log(tr("psd.started", mode=tr("psd.multi.button")))
        worker = PsdReconstructionThread(
            psd_paths[0],
            self.last_output_dir,
            "multi-repack",
            resource_limits=self.settings_manager.get("psd.resource_limits", {}),
            multi_psd_paths=psd_paths,
        )
        self._start_worker(worker)

    def on_progress_updated(self, value: int, message: str):
        self._task_progress = int(value)
        self._task_state = "preparing" if value < 10 else "running"
        self._task_message = str(message)
        self.progress_bar.setValue(value)
        stage_text = self.stage_text_from_message(message)
        if stage_text:
            self.stage_label.setText(stage_text)
            self._task_message = stage_text
        if message:
            self.append_log(message)
        self.taskProgressChanged.emit(value, message)
        self._emit_task_state()

    def on_reconstruction_finished(self, result: ReconstructionResult):
        stage = self.worker.pose_stage if self.worker else None
        if stage is not None:
            try:
                self.current_project = commit_pose_export_stage(self.current_project, stage)
                result.psd_path = stage.rebase_path(result.psd_path)
                if result.metadata_path:
                    result.metadata_path = stage.rebase_path(result.metadata_path)
                result.output_paths = [stage.rebase_path(path) for path in result.output_paths]
                result.texture_outputs = {index: stage.rebase_path(path) for index, path in result.texture_outputs.items()}
                if result.report_path:
                    result.report_path = stage.rebase_path(result.report_path)
                    result.report = json.loads(result.report_path.read_text(encoding="utf-8"))
                self._export_snapshot = str(stage.rebase_path(stage.source_model))
                self.last_output_dir = str(stage.final_scheme_dir)
                self.worker.pose_stage = None
                self._task_result_committed = True
            except Exception as exc:
                self.worker.cleanup_stage()
                self.on_reconstruction_error(str(exc))
                return
        self.progress_bar.setValue(100)
        self.stage_label.setText(tr("psd.stage.done"))
        self.open_output_button.setEnabled(True)
        if result.mode in {"mesh", "atlas-components", "atlas-artmesh"} and Path(result.psd_path).is_file():
            self.last_psd_path = str(Path(result.psd_path).resolve())
        self.open_photoshop_button.setEnabled(
            result.mode in {"mesh", "atlas-components", "atlas-artmesh"} and Path(result.psd_path).is_file()
        )
        self.repack_photoshop_button.setEnabled(bool(self.selected_repack_psd_path()))

        for warning in result.warnings:
            self.append_log(tr("psd.warning_prefix", message=warning))

        if result.metadata_path:
            self.append_log(tr("psd.metadata_log", path=str(result.metadata_path)))
            if Path(result.metadata_path).is_file():
                self.selected_metadata = str(Path(result.metadata_path).resolve())
                self.metadata_edit.setText(self.selected_metadata)
        if result.output_paths:
            self.append_log(tr("psd.output_count_log", count=len(result.output_paths)))
        if result.shared_regions:
            self.append_log(tr("psd.report.shared_regions", count=len(result.shared_regions)))
            if result.report.get("selection"):
                ids = sorted({item["unselected_id"] for item in result.shared_regions if item.get("unselected_id")})
                self.append_log(_workspace_text("psd.selection.shared_hint") + ", ".join(ids))
        affected = result.report.get("affected_unselected_ids") or []
        if affected:
            self.append_log(tr("psd.selection.shared_actual", default=PSD_WORKSPACE_TEXT["psd.selection.shared_actual"],
                               count=len(affected)) + ", ".join(affected))
        if result.change_regions:
            self.append_log(tr("psd.report.change_regions", count=len(result.change_regions)))
        if result.conflicts:
            self.append_log(tr("psd.report.conflicts", count=len(result.conflicts)))
        if result.report_path:
            self.append_log(tr("psd.report.path", path=result.report_path))
        self._update_artmesh_inspector_button()

        try:
            self.update_project_after_result(result)
        except Exception as exc:
            message = str(exc)
            if self._task_result_committed:
                message = tr("psd.task.refresh_failed", default=PSD_WORKSPACE_TEXT["psd.task.refresh_failed"], error=message)
            self.on_reconstruction_error(message)
            return
        if result.mode in {"mesh-repack", "atlas-repack", "multi-repack"} and result.metadata_path and result.texture_outputs:
            self._last_comparison_request = {
                "metadata_path": str(result.metadata_path),
                "texture_outputs": {int(index): str(path) for index, path in result.texture_outputs.items()},
            }
            self.compare_repack_button.setEnabled(True)
        if (self._compact and result.mode in {"multi-repack", "mesh-repack", "atlas-repack"}
                and self.pending_repack_id and self.current_project
                and find_repack_entry(self.current_project, self.pending_repack_id)):
            self._pending_skin_ready_token = "repack:" + self.pending_repack_id

        self.append_log(
            tr(
                "psd.finished_log",
                path=str(result.primary_path),
                count=result.layer_count,
                mode=result.mode,
            )
        )
        self._task_progress = 100
        self._task_state = "succeeded"
        self._task_message = tr("psd.success_content", path=str(result.primary_path))
        self._emit_context_changed()
        self._emit_task_state()
        InfoBar.success(
            title=tr("common.success"),
            content=tr("psd.success_content", path=str(result.primary_path)),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000,
        )

    def on_reconstruction_error(self, error: str):
        self._task_state = "failed"
        self._task_progress = 0
        self._task_message = str(error)
        # A queued error can arrive after run() returns but before Qt delivers
        # finished.  Let the finished handler release controls and emit the
        # terminal event exactly once for all worker-originated failures.
        running = isinstance(self.sender(), QThread)
        if not running:
            self.set_busy(False)
        self.progress_bar.setValue(0)
        self.stage_label.setText(tr("psd.stage.failed"))
        self.append_log(tr("psd.error_log", error=error), expand=True)
        self._emit_task_state()
        if not running:
            self.taskFailed.emit(error)
        InfoBar.error(
            title=tr("common.error"),
            content=error,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=6000,
        )

    def on_reconstruction_cancelled(self):
        self._task_state = "cancelled"
        self._task_progress = 0
        self._task_message = _workspace_text("psd.task.cancelled")
        self.append_log(self._task_message)
        self._emit_task_state()

    def set_busy(self, busy: bool):
        self._is_busy = bool(busy)
        self.reconstruct_button.setEnabled(not busy)
        self.source_file_button.setEnabled(not busy)
        self.source_folder_button.setEnabled(not busy)
        self.export_name_edit.setEnabled(not busy)
        self.repack_psd_button.setEnabled(not busy)
        self.artmesh_inspector_button.setEnabled(
            not busy and self._artmesh_metadata_path() is not None
        )
        self.repack_photoshop_button.setEnabled(not busy and bool(self.selected_repack_psd_path()))
        self.open_photoshop_button.setEnabled(not busy and bool(self.last_psd_path)
                                             and Path(self.last_psd_path).is_file())
        self.texture_name_edit.setEnabled(not busy)
        self.new_project_button.setEnabled(not busy)
        self.save_project_button.setEnabled(not busy)
        self.open_project_folder_button.setEnabled(not busy and self.current_project is not None)
        self.project_combo.setEnabled(not busy)
        self.pose_scheme_combo.setEnabled(not busy)
        self.output_button.setEnabled(not busy and self.workflow != "repack")
        self.output_edit.setReadOnly(self._compact or self.workflow == "repack")
        self.mode_combo.setEnabled(not busy)
        self.compare_repack_button.setEnabled(not busy and self._last_comparison_request is not None)
        self.edit_working_png_button.setEnabled(not busy and self.current_project is not None)
        self.mesh_canvas_spin.setEnabled(not busy)
        self.mesh_canvas_preset_combo.setEnabled(not busy)
        self.export_flow_button.setEnabled(not busy)
        self.repack_flow_button.setEnabled(not busy)
        self.preview_image_button.setEnabled(not busy and self.current_project is not None)
        self.motion_play_button.setEnabled(not busy and bool(self._motion_items))
        self.update_preview_controls()
        self._update_action_availability()
        if self._compact and hasattr(self, "single_repack_button"):
            self.single_repack_button.setEnabled(not busy and bool(self.selected_repack_psd_path()))
            self.multi_repack_button.setEnabled(not busy and bool(self.project_psd_choices()))
            self.import_project_button.setEnabled(not busy)
            self.capture_pose_button.setEnabled(not busy and self.current_project is not None)
        self.shared_uv_checkbox.setEnabled(not busy and self._shared_uv_available and not self._shared_uv_multimode)
        self._update_version_skin_action()
        self._emit_task_state()

    def set_workflow(self, workflow: str):
        self.workflow = "repack" if workflow == "repack" else "export"
        if self.workflow == "repack":
            self._sync_repack_output_dir()
        self._update_workflow_ui()
        self.mark_project_dirty()

    def on_mode_changed(self, *_args):
        self.mode_combo.setToolTip(self.mode_combo.currentText())
        self.update_mode_hint()
        self.mark_project_dirty()

    def on_mesh_canvas_limit_changed(self):
        self.settings_manager.set(
            "psd.resource_limits.mesh_max_dimension",
            int(self.mesh_canvas_spin.value()),
        )
        self.update_mode_hint()
        self._sync_mesh_canvas_preset()
        self.mark_project_dirty()

    def on_mesh_canvas_preset_changed(self):
        value = self.mesh_canvas_preset_combo.currentData()
        if value == "custom":
            return
        self.mesh_canvas_spin.setValue(int(value))
        self.on_mesh_canvas_limit_changed()

    def _sync_mesh_canvas_preset(self):
        value = int(self.mesh_canvas_spin.value())
        index = next(
            (
                index
                for index in range(self.mesh_canvas_preset_combo.count())
                if self.mesh_canvas_preset_combo.itemData(index) == value
            ),
            self.mesh_canvas_preset_combo.count() - 1,
        )
        self.mesh_canvas_preset_combo.blockSignals(True)
        self.mesh_canvas_preset_combo.setCurrentIndex(index)
        self.mesh_canvas_preset_combo.blockSignals(False)

    def _update_workflow_ui(self):
        is_repack = self.workflow == "repack"
        self.export_card.setVisible(self._compact or not is_repack)
        self.repack_card.setVisible(self._compact or is_repack)
        self.reconstruct_button.setText(
            tr("psd.repack.single_button") if is_repack else tr("psd.export_button")
        )
        self.multi_repack_button.setVisible(self._compact)
        if self._compact and hasattr(self, "workflow_tabs"):
            self.reconstruct_button.setVisible(self.workflow_tabs.currentIndex() == 0)
        self._style_workflow_button(self.export_flow_button, not is_repack)
        self._style_workflow_button(self.repack_flow_button, is_repack)
        self.update_mode_hint()
        self._update_action_availability()

    def _update_action_availability(self, *_args):
        if not hasattr(self, "reconstruct_button"):
            return
        if self._compact and hasattr(self, "single_repack_button"):
            ready = not self._is_busy and self.current_project is not None
            self.single_repack_button.setEnabled(ready and bool(self.selected_repack_psd_path()))
            self.multi_repack_button.setEnabled(ready and bool(self.project_psd_choices()))
        if self._is_busy or (self._compact and not self.current_project):
            self.reconstruct_button.setEnabled(False)
            return
        if self.workflow == "export":
            self.reconstruct_button.setEnabled(
                self.selected_parameter_preset() is not None
            )
        else:
            self.reconstruct_button.setEnabled(True)

    def _style_workflow_button(self, button: PushButton, selected: bool):
        if selected:
            button.setStyleSheet(
                """
                PushButton#psdWorkflowButton {
                    border: 0;
                    border-radius: 6px;
                    background: #00A6B3;
                    color: white;
                    font-weight: 600;
                    padding: 0 14px;
                }
                PushButton#psdWorkflowButton:disabled {
                    background: palette(alternate-base);
                    color: palette(placeholder-text);
                }
                """
            )
        else:
            button.setStyleSheet(
                """
                PushButton#psdWorkflowButton {
                    border: 0;
                    border-radius: 6px;
                    background: transparent;
                    color: palette(text);
                    padding: 0 14px;
                }
                PushButton#psdWorkflowButton:hover {
                    background: palette(alternate-base);
                }
                PushButton#psdWorkflowButton:disabled {
                    background: transparent;
                    color: palette(placeholder-text);
                }
                """
            )

    def _apply_static_styles(self):
        self.workflow_segment.setStyleSheet(
            """
            QFrame#psdWorkflowSegment {
                border: 1px solid palette(mid);
                border-radius: 9px;
                background: palette(alternate-base);
            }
            """
        )
        self.drop_frame.setStyleSheet(
            """
            QFrame#psdDropFrame {
                border: 1px dashed palette(mid);
                border-radius: 8px;
                background: palette(alternate-base);
            }
            """
        )
        self.project_frame.setStyleSheet(
            """
            QFrame#psdProjectFrame {
                border: 1px solid palette(mid);
                border-radius: 8px;
                background: palette(base);
            }
            """
        )
        self.preview_control_frame.setStyleSheet(
            """
            QFrame#psdPreviewControlFrame {
                border: 1px solid palette(mid);
                border-radius: 8px;
                background: palette(base);
            }
            """
        )
        self.motion_frame.setStyleSheet(
            """
            QFrame#psdMotionFrame {
                border: 1px solid palette(mid);
                border-radius: 8px;
                background: palette(base);
            }
            """
        )
        if hasattr(self, "preview_frame"):
            self.preview_frame.setStyleSheet(
                """
                QFrame#psdPreviewFrame {
                    border: 1px solid palette(mid);
                    border-radius: 8px;
                    background: palette(alternate-base);
                }
                """
            )
        self.log_frame.setStyleSheet(
            """
            QFrame#psdLogFrame {
                border: 1px solid palette(mid);
                border-radius: 8px;
                background: palette(alternate-base);
            }
            QPushButton#psdLogToggleButton {
                color: #00a6b3;
                background: transparent;
                border: 1px solid palette(mid);
                border-radius: 5px;
                padding: 0 8px;
            }
            QPushButton#psdLogToggleButton:hover {
                background: palette(alternate-base);
            }
            """
        )
        # QFluentWidgets installs a per-button palette whose base and text
        # roles are both white in dark mode.  Keep the compact +/- affordance
        # readable after a theme switch by styling this control directly.
        self.log_toggle_button.setStyleSheet(
            """
            QPushButton#psdLogToggleButton {
                color: #00a6b3;
                background: transparent;
                border: 1px solid #00a6b3;
                border-radius: 5px;
                padding: 0 8px;
            }
            QPushButton#psdLogToggleButton:hover {
                background: rgba(0, 166, 179, 30);
            }
            """
        )
        if hasattr(self, "live2d_preview_host"):
            self.live2d_preview_host.setStyleSheet(
                """
                QFrame#psdLive2DPreviewHost {
                    border: 1px solid palette(mid);
                    border-radius: 8px;
                    background: palette(base);
                }
                """
            )

    def update_mode_hint(self):
        mode = self.mode_combo.currentData() or "mesh"
        is_mesh_export = self.workflow == "export" and mode == "mesh"
        if hasattr(self, "mesh_canvas_frame"):
            self.mesh_canvas_frame.setVisible(is_mesh_export)
            self.mesh_canvas_hint_label.setVisible(is_mesh_export)
            if self._compact:
                self.mesh_canvas_label.setVisible(is_mesh_export)
        if self.workflow == "repack":
            self.mode_hint_label.setText("")
        elif mode == "atlas-components":
            self.mode_hint_label.setText(tr("psd.mode_hint.atlas_components"))
        else:
            self.mode_hint_label.setText(tr("psd.mode_hint.mesh_pose"))
        if self._compact and hasattr(self, "export_help_button"):
            self._compact_control_text()

    def _sync_default_metadata(self):
        source = self.selected_repack_psd_path()
        if not source:
            self.selected_metadata = ""
            self.metadata_edit.clear()
            self._update_artmesh_inspector_button()
            self._refresh_shared_uv_option()
            return
        scheme = self.selected_pose_scheme()
        scheme_metadata = str((scheme or {}).get("metadata") or "")
        if scheme_metadata and self.current_project:
            metadata_path = resolve_project_path(self.current_project, scheme_metadata)
        else:
            metadata_path = Path(source).with_suffix(".lpkpsd.json")
        if metadata_path.is_file():
            self.selected_metadata = str(metadata_path.resolve())
            self.metadata_edit.setText(self.selected_metadata)
        else:
            self.selected_metadata = ""
            self.metadata_edit.clear()
        self._update_artmesh_inspector_button()
        self._refresh_shared_uv_option()

    def _refresh_shared_uv_option(self, *_args, source=None, metadata_path=None):
        if not hasattr(self, "shared_uv_checkbox"):
            return
        source = source or self.selected_repack_psd_path()
        value = metadata_path or self.metadata_edit.text().strip()
        path = Path(value) if value else (Path(source).with_suffix(".lpkpsd.json") if source else None)
        try:
            stamp = (path.stat().st_mtime_ns, path.stat().st_size) if path and path.is_file() else None
        except OSError:
            stamp = None
        context = (str(Path(source).resolve()) if source else "", str(path.resolve()) if path else "", stamp)
        if context != self._shared_uv_context:
            self.shared_uv_checkbox.setChecked(False)
            self._shared_uv_context = context
            self._shared_uv_multimode = False
            ids = []
            if stamp:
                try:
                    metadata = json.loads(path.read_text(encoding="utf-8"))
                    if metadata.get("mode") in {"mesh", "atlas-components"} and isinstance(metadata.get("selection"), dict):
                        ids = sorted({str(item["unselected_id"]) for item in metadata["selection"].get("shared_regions", [])
                                      if isinstance(item, dict) and item.get("unselected_id")})
                except (OSError, ValueError, TypeError):
                    pass
            self._shared_uv_ids = ids
            self._shared_uv_available = bool(ids)
        ids = getattr(self, "_shared_uv_ids", [])
        self.shared_uv_checkbox.setText(_workspace_text("psd.selection.shared_enable"))
        text = tr("psd.selection.shared_effect", default=PSD_WORKSPACE_TEXT["psd.selection.shared_effect"], count=len(ids))
        detail = text + "\n" + ", ".join(ids)
        self.shared_uv_checkbox.setToolTip(detail)
        self.shared_uv_hint.setText(text)
        self.shared_uv_hint.setToolTip(detail)
        self.shared_uv_checkbox.setVisible(bool(ids))
        self.shared_uv_hint.setVisible(bool(ids) and not self._compact)
        self.shared_uv_checkbox.setEnabled(bool(ids) and not self._is_busy and not self._shared_uv_multimode)

    def _sync_repack_output_dir(self):
        source = self.selected_repack_psd_path()
        if not source:
            self.repack_output_edit.clear()
            return
        texture_name = sanitize_project_name(
            self.texture_name_edit.text().strip() or tr("psd.repack.unnamed_texture")
        )
        scheme = self.selected_pose_scheme()
        if self.current_project:
            scheme_id = sanitize_project_name(str((scheme or {}).get("id") or "unassigned"))
            output_path = self.current_project.project_dir / "tex" / scheme_id / texture_name
        else:
            output_path = Path(source).resolve().parent / texture_name
        self.repack_output_edit.setText(str(output_path.resolve()))

    def default_output_dir(self, source_path: str = "") -> str:
        if source_path and Path(source_path).suffix.lower() == ".psd":
            target = Path(source_path).resolve().parent
            target.mkdir(parents=True, exist_ok=True)
            return str(target)
        base_dir = Path(self.settings_manager.get_output_root()).resolve() / "psd"
        name = self.source_output_name(source_path)
        target = base_dir / name if name else base_dir
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    @staticmethod
    def source_output_name(source_path: str) -> str:
        if not source_path:
            return ""
        path = Path(source_path)
        if not path.exists() and str(path.parent) in {"", "."}:
            return "psd"
        try:
            if path.is_dir():
                candidate = path.name
            elif path.suffix.lower() in {".json", ".moc3"}:
                candidate = path.parent.name or path.stem
            elif path.suffix.lower() == ".psd":
                candidate = path.stem
            else:
                candidate = path.parent.name or path.stem
        except Exception:
            candidate = ""
        candidate = "".join("_" if ord(ch) < 32 else ch for ch in str(candidate or "").strip())
        candidate = re.sub(r'[<>:"/\\|?*]+', "_", candidate)
        candidate = candidate.strip(" ._")
        return candidate or "psd"

    def stage_text_from_message(self, message: str) -> str:
        lower = (message or "").lower()
        if not lower:
            return ""
        if "reading psd" in lower or "loaded psd" in lower:
            return tr("psd.stage.read_psd")
        if "writing atlas png" in lower or "atlas png written" in lower:
            return tr("psd.stage.write_png")
        if "packed " in lower:
            return tr("psd.stage.repack_layer")
        if "writing metadata" in lower:
            return tr("psd.stage.write_metadata")
        if (
            "writing psd" in lower
            or "preparing psd" in lower
            or "prepared psd layer" in lower
            or "saving psd" in lower
            or "psd written" in lower
        ):
            return tr("psd.stage.write_psd")
        if "cubism core" in lower or "drawable" in lower:
            return tr("psd.stage.export_sidecar")
        if "loaded" in lower and "texture" in lower:
            return tr("psd.stage.load_textures")
        if "rendered" in lower:
            return tr("psd.stage.render_mesh")
        if "split texture" in lower or "editable layer" in lower:
            return tr("psd.stage.atlas_components")
        if "extracted" in lower or "artmesh" in lower:
            return "Atlas ArtMesh"
        if "source resolved" in lower or "resolv" in lower:
            return tr("psd.stage.resolve_model")
        return message

    def open_output_folder(self):
        if os.path.isdir(self.last_output_dir):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.last_output_dir))

    def _history_comparison_request(self):
        token = self.preview_source_token()
        if not self.current_project or not token.startswith("repack:"):
            return None
        entry = find_repack_entry(self.current_project, token.split(":", 1)[1])
        if not entry or not entry.get("metadata") or not entry.get("texture_outputs"):
            return None
        return {"metadata_path": str(resolve_project_path(self.current_project, entry["metadata"])),
                "texture_outputs": {int(index): str(resolve_project_path(self.current_project, path))
                                    for index, path in entry["texture_outputs"].items()}}

    def open_comparison(self, *, history=False):
        if self.is_busy():
            return
        request = self._history_comparison_request() if history else self._last_comparison_request
        if not request:
            return
        try:
            from app.gui.psd_comparison_dialog import PsdComparisonDialog
            from app.gui.editor_dialogs import exec_editor_dialog
            request = copy.deepcopy(request)
            if callable(self.comparison_mesh_provider):
                request["mesh_data"] = self.comparison_mesh_provider()
            dialog = PsdComparisonDialog(request, self)
            exec_editor_dialog(dialog)
            dialog.deleteLater()
        except Exception as exc:
            InfoBar.error(title=tr("common.error"), content=str(exc), parent=self,
                          position=InfoBarPosition.TOP, duration=5000)

    def open_current_psd_in_photoshop(self):
        psd_path = Path(self.last_psd_path) if self.last_psd_path else None
        if not psd_path or not psd_path.is_file():
            scheme = find_pose_scheme(self.current_project) if self.current_project else None
            value = str((scheme or {}).get("psd") or "")
            psd_path = resolve_project_path(self.current_project, value) if value and self.current_project else None
        photoshop = Path(self.settings_manager.get_photoshop_executable())
        if not psd_path or not psd_path.is_file():
            return
        if photoshop.is_file():
            opened, _pid = QProcess.startDetached(str(photoshop), [str(psd_path)])
        else:
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(psd_path)))
        if not opened:
            InfoBar.warning(title=tr("common.error"), content=tr("psd.edit.open_failed"),
                            parent=self, position=InfoBarPosition.TOP, duration=5000)

    def pose_scheme_for_psd(self, psd_path: str | Path) -> dict | None:
        if not self.current_project:
            return None
        target = Path(psd_path).resolve()
        for scheme in self.current_project.data.get("pose_schemes", []):
            value = str(scheme.get("psd") or "")
            if value and resolve_project_path(self.current_project, value) == target:
                return dict(scheme)
        return None

    def refresh_pose_scheme_controls(self, selected_token: str | None = None):
        if not hasattr(self, "pose_scheme_combo"):
            return
        selected = selected_token
        if selected is None:
            selected = str(self.pose_scheme_combo.currentData() or "")
        if not selected and self.current_project:
            selected = str(self.current_project.data.get("selected_pose_scheme") or "")
        self._pose_scheme_refreshing = True
        self.pose_scheme_combo.blockSignals(True)
        self.pose_scheme_combo.clear()
        schemes = self.current_project.data.get("pose_schemes", []) if self.current_project else []
        completion_labels: list[str] = []
        for scheme in schemes:
            psd_path = (
                resolve_project_path(self.current_project, scheme.get("psd", ""))
                if scheme.get("psd")
                else None
            )
            label = str(scheme.get("name") or scheme.get("id") or "")
            if psd_path:
                label = f"{label} — {psd_path.name}"
            self.pose_scheme_combo.addItem(
                label,
                userData=str(scheme.get("id") or ""),
            )
            completion_labels.append(label)
        if self.manual_repack_psd and Path(self.manual_repack_psd).is_file():
            label = tr(
                "psd.repack.external_psd",
                name=Path(self.manual_repack_psd).name,
            )
            self.pose_scheme_combo.addItem(
                label,
                userData=f"file:{self.manual_repack_psd}",
            )
            completion_labels.append(label)
        self._psd_completer_model.setStringList(completion_labels)
        index = self._combo_index_by_data(self.pose_scheme_combo, selected)
        self.pose_scheme_combo.setCurrentIndex(
            index if index >= 0 else (0 if self.pose_scheme_combo.count() else -1)
        )
        self.pose_scheme_combo.blockSignals(False)
        self.open_photoshop_button.setEnabled(
            bool(self.selected_repack_psd_path())
        )
        self.repack_photoshop_button.setEnabled(bool(self.selected_repack_psd_path()))
        self._pose_scheme_refreshing = False
        self._sync_default_metadata()
        self._sync_repack_output_dir()
        self._update_action_availability()

    def on_pose_scheme_changed(self, *_args):
        if self._pose_scheme_refreshing:
            return
        scheme = self.selected_pose_scheme()
        if not scheme:
            path = self.selected_repack_psd_path()
            self.last_psd_path = path
            self.open_photoshop_button.setEnabled(bool(path))
            self.repack_photoshop_button.setEnabled(bool(path))
            self._sync_default_metadata()
            self._sync_repack_output_dir()
            return
        try:
            self.current_project = select_pose_scheme(
                self.current_project,
                str(scheme.get("id") or ""),
            )
            self.projectChanged.emit(self.current_project)
        except Exception as exc:
            self.append_log(tr("psd.warning_prefix", message=str(exc)))
            return
        self.last_psd_path = str(resolve_project_path(self.current_project, scheme.get("psd", "")))
        self.open_photoshop_button.setEnabled(Path(self.last_psd_path).is_file())
        self.repack_photoshop_button.setEnabled(Path(self.last_psd_path).is_file())
        self._sync_default_metadata()
        self._sync_repack_output_dir()

    def create_project_from_current_source(self):
        if self.is_busy():
            return
        if self._compact:
            self.newProjectRequested.emit()
            return
        source = self.source_edit.text().strip()
        if not source:
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("psd.warning_no_source"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return
        if Path(source).suffix.lower() == ".psd":
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("psd.warning_export_needs_model"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return
        default_name = self.source_output_name(source)
        project_name = self._prompt_non_empty_name(
            "psd.project.new_dialog_title",
            "psd.project.new_dialog_prompt",
            default_name,
        )
        if not project_name:
            return
        self._begin_task("import", tr("psd.project.creating"))
        self.project_status_label.setText(tr("psd.project.creating"))
        self.append_log(tr("psd.project.new_save_hint", name=project_name))
        self.project_worker = PsdProjectImportThread(source, project_name)
        self.project_worker.progressUpdated.connect(self.append_log)
        self.project_worker.projectFinished.connect(self.on_project_created)
        self.project_worker.projectError.connect(self.on_project_error)
        self.project_worker.finished.connect(self._on_worker_stopped)
        self.project_worker.start()

    def browse_project_file(self):
        last_project = self.settings_manager.get("psd.last_project_file", "")
        start_dir = str(Path(last_project).parent) if last_project and Path(last_project).exists() else self.settings_manager.get_output_dir("psd_projects")
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("psd.project.open_dialog"),
            start_dir,
            tr("psd.project.filter"),
        )
        if path:
            if self._compact:
                self.projectRequested.emit(path)
                return
            try:
                self.set_current_project(load_project(path))
                self.append_log(tr("psd.project.opened", path=path))
            except Exception as exc:
                InfoBar.error(
                    title=tr("common.error"),
                    content=str(exc),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=5000,
                )

    def load_last_project(self, silent: bool = False):
        path = str(self.settings_manager.get("psd.last_project_file", "") or "").strip()
        if not path or not Path(path).is_file():
            if not silent:
                InfoBar.warning(
                    title=tr("common.warning"),
                    content=tr("psd.project.no_last"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=3000,
                )
            return
        try:
            self.set_current_project(load_project(path))
            self.append_log(tr("psd.project.opened", path=path))
        except Exception as exc:
            if not silent:
                InfoBar.error(
                    title=tr("common.error"),
                    content=str(exc),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=5000,
                )

    def remember_project_file(self, project: Live2DPSDProject):
        if self._compact:
            self._bound_project_files = [str(project.project_file)]
            return
        path = str(project.project_file)
        recent = self._recent_project_files()
        recent = [item for item in recent if str(Path(item).resolve()) != path]
        recent.insert(0, path)
        self.settings_manager.set("psd.last_project_file", path)
        self.settings_manager.set("psd.project_files", recent[:30])

    def _recent_project_files(self) -> list[str]:
        raw = self.settings_manager.get("psd.project_files", [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        result = []
        seen = set()
        for item in raw:
            try:
                path = str(Path(str(item)).resolve())
            except Exception:
                continue
            if path in seen or not Path(path).is_file():
                continue
            seen.add(path)
            result.append(path)
        return result

    def discover_project_files(self) -> list[str]:
        paths: list[str] = []
        seen = set()

        def add(path: str | Path):
            try:
                resolved = Path(path).resolve()
            except Exception:
                return
            if not resolved.is_file() or resolved.name != PROJECT_FILE_NAME:
                return
            key = str(resolved)
            if key in seen:
                return
            seen.add(key)
            paths.append(key)

        if self._compact:
            for path in self._bound_project_files:
                add(path)
            root = self.managed_project_root_provider() if self.managed_project_root_provider else None
            if root and Path(root).is_dir():
                for path in sorted(Path(root).rglob(PROJECT_FILE_NAME)):
                    add(path)

        last_project = str(self.settings_manager.get("psd.last_project_file", "") or "").strip()
        if last_project:
            add(last_project)
        for item in self._recent_project_files():
            add(item)
        try:
            root = Path(self.settings_manager.get_output_dir("psd_projects"))
            if root.is_dir():
                discovered = sorted(
                    root.rglob(PROJECT_FILE_NAME),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
                for path in discovered:
                    add(path)
        except Exception:
            pass
        return paths

    def refresh_project_combo(self, *_args, select_project_file: str | None = None):
        if not hasattr(self, "project_combo"):
            return
        selected = select_project_file
        if selected is None and self.current_project:
            selected = str(self.current_project.project_file)
        selected = str(Path(selected).resolve()) if selected else ""
        self._project_combo_refreshing = True
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        paths = self.discover_project_files()
        completion_labels: list[str] = []
        entries = []
        for path in paths:
            try:
                label = load_project(path).project_name
            except Exception:
                label = Path(path).parent.name
            entries.append((path, label))
        for path, label in entries:
            if self._compact and sum(name == label for _, name in entries) > 1:
                label = f"{label} · {Path(path).parent.name}"
            self.project_combo.addItem(label, userData=path)
            completion_labels.append(label)
        self._project_completer_model.setStringList(completion_labels)
        if not paths:
            self.project_combo.addItem(tr("psd.project.no_projects"), userData="")
        index = self._combo_index_by_data(self.project_combo, selected)
        self.project_combo.setCurrentIndex(index if index >= 0 else 0)
        self.project_combo.blockSignals(False)
        self._project_combo_refreshing = False

    def on_project_combo_text_changed(self, *_args):
        return

    def on_project_combo_changed(self, *_args):
        if self._project_combo_refreshing:
            return
        path = str(self.project_combo.currentData() or "")
        if not path:
            return
        if self.current_project and str(self.current_project.project_file) == path:
            return
        if self._compact:
            self.projectRequested.emit(path)
            return
        try:
            self.set_current_project(load_project(path))
            self.append_log(tr("psd.project.opened", path=path))
        except Exception as exc:
            InfoBar.error(
                title=tr("common.error"),
                content=str(exc),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )

    def mark_project_dirty(self, *_args):
        if self._ui_refreshing or not self.current_project:
            return
        self._project_dirty = True
        self._update_project_header()
        self.projectChanged.emit(self.current_project)

    def _update_project_header(self):
        if not hasattr(self, "current_project_title_label"):
            return
        if self.current_project:
            self.current_project_title_label.setText(
                tr("psd.project.header_current", name=self.current_project.project_name)
            )
        else:
            self.current_project_title_label.setText(tr("psd.project.header_none"))
        self.unsaved_label.setVisible(bool(self.current_project and self._project_dirty))
        self.unsaved_label.setToolTip(tr("psd.project.unsaved_tooltip"))
        if self._compact and hasattr(self, "bound_hint"):
            self.bound_hint.setText(self.current_project_title_label.text())
            path = str(self.current_project.project_file) if self.current_project else ""
            self.bound_hint.setToolTip(_workspace_text("psd.workspace.bound") + ("\n" + path if path else ""))

    def _current_ui_state(self) -> dict:
        return {
            "workflow": self.workflow,
            "selected_parameter_preset": str(
                self.export_name_edit.currentData() or ""
            ),
            "export_mode": str(self.mode_combo.currentData() or "mesh"),
            "export_output": self.output_edit.text().strip(),
            "texture_name": self.texture_name_edit.text().strip(),
            "selected_pose_scheme": str(self.pose_scheme_combo.currentData() or ""),
            "metadata": self.metadata_edit.text().strip(),
        }

    def save_current_project(self):
        if not self.current_project:
            return
        data = dict(self.current_project.data)
        data["project_name"] = self.current_project.project_name
        data["ui_state"] = self._current_ui_state()
        project_file = save_project(self.current_project.project_dir, data)
        self.current_project = Live2DPSDProject(self.current_project.project_dir, project_file, data)
        self._project_dirty = False
        self.remember_project_file(self.current_project)
        self.refresh_project_ui()
        self.refresh_project_combo(select_project_file=str(project_file))
        self._update_project_header()
        self.append_log(tr("psd.project.saved", path=str(project_file)))
        self.projectChanged.emit(self.current_project)

    def open_current_project_folder(self):
        if not self.current_project:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.current_project.project_dir.resolve())))

    def on_project_created(self, project: Live2DPSDProject):
        previous = self.current_project
        try:
            self.set_current_project(project)
            if isinstance(self.project_worker, PsdEditorProjectPreparationThread):
                (project.project_dir / ".psd-init-owner").unlink(missing_ok=True)
        except Exception as exc:
            if isinstance(self.project_worker, PsdEditorProjectPreparationThread):
                self.project_worker._discard_owned_project()
                self.bind_project(previous)
            self.on_project_error(str(exc))
            return
        self._task_state = "succeeded"
        self._task_progress = 100
        automatic = isinstance(self.project_worker, PsdEditorProjectPreparationThread)
        self._task_message = (_workspace_text("psd.task.ready") if automatic
                              else tr("psd.project.created", path=str(project.project_file)))
        self.append_log(tr("psd.project.created", path=str(project.project_file)))
        if automatic:
            return
        InfoBar.success(
            title=tr("common.success"),
            content=tr("psd.project.created", path=str(project.project_file)),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=3500,
        )

    def on_project_error(self, error: str):
        self.project_status_label.setText(tr("psd.project.no_project"))
        self.on_reconstruction_error(error)

    def set_current_project(self, project: Live2DPSDProject):
        self._last_comparison_request = None
        self.compare_repack_button.setEnabled(False)
        self.close_project_preview()
        self._ui_refreshing = True
        self.current_project = project
        self.remember_project_file(project)
        self.project_name_edit.setText(project.project_name)
        self.selected_source = str(project.base_model_json)
        self.source_edit.setText(self.selected_source)
        ui_state = dict(project.data.get("ui_state") or {})
        self.workflow = (
            "repack" if ui_state.get("workflow") == "repack" else "export"
        )
        self.output_manually_selected = False
        self.last_output_dir = str(
            ui_state.get("export_output")
            or (project.project_dir / "psd").resolve()
        )
        self.output_edit.setText(self.last_output_dir)
        self.texture_name_edit.setText(str(ui_state.get("texture_name") or ""))
        export_mode = str(ui_state.get("export_mode") or "mesh")
        if export_mode == "atlas-artmesh":
            export_mode = "atlas-components"
        mode_index = self._combo_index_by_data(self.mode_combo, export_mode)
        if mode_index >= 0:
            self.mode_combo.setCurrentIndex(mode_index)
        self.manual_repack_psd = ""
        self._project_dirty = False
        self._update_workflow_ui()
        selected_preset_id = str(
            ui_state.get("selected_parameter_preset")
            or project.data.get("selected_parameter_preset")
            or "default"
        )
        self.refresh_parameter_preset_controls(selected_preset_id)
        self.refresh_project_ui(
            selected_pose_token=str(ui_state.get("selected_pose_scheme") or "")
        )
        self.refresh_project_combo(select_project_file=str(project.project_file))
        self.refresh_preview_source_combo()
        self.refresh_motion_controls(project.base_model_json)
        self._ui_refreshing = False
        self._update_project_header()
        self.projectChanged.emit(self.current_project)

    def bind_project(self, project: Live2DPSDProject | None):
        """Bind the owner editor's isolated child project without opening a model."""
        self._last_comparison_request = None
        self.compare_repack_button.setEnabled(False)
        if project is not None:
            self.set_current_project(project)
            return
        self.close_project_preview()
        self.current_project = None
        self._project_dirty = False
        self._bound_project_files = []
        self.manual_repack_psd = ""
        self.selected_metadata = ""
        self.last_psd_path = ""
        self.source_edit.clear()
        self.metadata_edit.clear()
        self.output_edit.clear()
        self.repack_output_edit.clear()
        self.refresh_project_ui()
        self.refresh_project_combo()
        self._update_action_availability()

    def begin_editor_project_preparation(self, request, project_dir, project_name="psd") -> bool:
        """Show defaults immediately; create the immutable base in a CPU child."""
        if self.is_busy():
            request.close()
            return False
        try:
            target = Path(project_dir).resolve()
            if target.exists():
                raise ValueError(f"PSD project already exists: {target}")
            self._begin_task("project-preparation", _workspace_text("psd.task.preparing"))
            self.project_worker = PsdEditorProjectPreparationThread(request, target, project_name)
            self.project_worker.progressUpdated.connect(self.append_log)
            self.project_worker.projectFinished.connect(self.on_project_created)
            self.project_worker.projectError.connect(self.on_project_error)
            self.project_worker.projectCancelled.connect(self.on_reconstruction_cancelled)
            self.project_worker.finished.connect(self._on_worker_stopped)
            self.project_worker.start()
            return True
        except Exception as exc:
            request.close()
            self.on_reconstruction_error(str(exc))
            return False

    def is_busy(self) -> bool:
        return self._is_busy or any(worker is not None and worker.isRunning()
                   for worker in (self.worker, self.project_worker, self.preview_prepare_worker))

    def shutdown(self) -> bool:
        self._arrange_timer.stop()
        self._last_project_timer.stop()
        self._preview_dock_timer.stop()
        workers = (self.worker, self.project_worker, self.preview_prepare_worker)
        for worker in workers:
            if worker is not None and worker.isRunning():
                worker.requestInterruption()
        deadline = time.monotonic() + .1
        for worker in workers:
            if worker is not None and worker.isRunning():
                worker.wait(max(0, int((deadline - time.monotonic()) * 1000)))
                if worker.isRunning():
                    return False
        self.close_project_preview()
        if self.preview_dialog is not None:
            self.preview_dialog.close()
        if self.artmesh_inspector_dialog:
            self.artmesh_inspector_dialog.close()
        return True

    def closeEvent(self, event):  # noqa: N802
        if self.is_busy():
            for worker in (self.worker, self.project_worker, self.preview_prepare_worker):
                if worker is not None and worker.isRunning():
                    worker.requestInterruption()
            self.append_log(_workspace_text("psd.task.close_pending"))
            event.ignore()
            return
        self.close_project_preview()
        super().closeEvent(event)

    def refresh_project_ui(
        self,
        selected_id: str | None = None,
        selected_pose_token: str | None = None,
    ):
        if not self.current_project:
            if hasattr(self, "open_project_folder_button"):
                self.open_project_folder_button.setEnabled(False)
            self.project_status_label.setText(tr("psd.project.no_project"))
            self.refresh_parameter_preset_controls("default")
            self.refresh_pose_scheme_controls(selected_pose_token)
            self.refresh_preview_source_combo()
            self.refresh_motion_controls(None)
            self._update_project_header()
            return
        if hasattr(self, "open_project_folder_button"):
            self.open_project_folder_button.setEnabled(True)
        self.project_status_label.setText(
            tr("psd.project.current", path=str(self.current_project.project_file))
        )
        self.refresh_parameter_preset_controls()
        self.refresh_pose_scheme_controls(selected_pose_token)
        self.refresh_preview_source_combo(selected_id)
        self._update_project_header()

    def selected_repack_id(self) -> str:
        source_id = self.preview_source_repack_id() if hasattr(self, "preview_source_combo") else ""
        if source_id:
            return source_id
        return str(self.current_project.data.get("selected_repack") or "") if self.current_project else ""

    def on_version_selection_changed(self):
        if not self.current_project:
            return
        version_id = self.selected_repack_id()
        if not version_id:
            return
        try:
            self.current_project = select_repack(self.current_project, version_id)
            self.set_preview_source_to_repack(version_id)
            if self.preview_mode == "current" and (self.preview_dialog is not None and self.preview_dialog.isVisible()):
                self.start_project_preview("current")
        except Exception as exc:
            self.append_log(tr("psd.warning_prefix", message=str(exc)))

    def refresh_preview_source_combo(self, selected_id: str | None = None):
        if not hasattr(self, "preview_source_combo"):
            return
        previous = str(self.preview_source_combo.currentData() or "")
        if selected_id:
            previous = f"repack:{selected_id}"
        self.preview_source_combo.blockSignals(True)
        self.preview_source_combo.clear()
        if not self.current_project:
            self.preview_source_combo.addItem(tr("psd.preview.no_project_source"), userData="")
        else:
            self.preview_source_combo.addItem(tr("psd.preview.original"), userData="original")
            for item in self.current_project.data.get("repack_history", []):
                version_id = str(item.get("id") or "")
                if version_id:
                    self.preview_source_combo.addItem(
                        tr(
                            "psd.preview.repack_source",
                            version=str(item.get("name") or version_id),
                        ),
                        userData=f"repack:{version_id}",
                    )
            for item in self.current_project.data.get("composite_history", []):
                version_id = str(item.get("id") or "")
                if version_id:
                    self.preview_source_combo.addItem(
                        tr("psd.preview.composite_source", version=version_id),
                        userData=f"composite:{version_id}",
                    )
        index = self._combo_index_by_data(self.preview_source_combo, previous)
        if index < 0:
            index = 0
        self.preview_source_combo.setCurrentIndex(index)
        self.preview_source_combo.blockSignals(False)
        self.refresh_preview_texture_combo()
        self.update_preview_controls()

    def set_preview_source_to_repack(self, version_id: str):
        if not hasattr(self, "preview_source_combo") or not version_id:
            return
        index = self._combo_index_by_data(self.preview_source_combo, f"repack:{version_id}")
        if index >= 0:
            self.preview_source_combo.setCurrentIndex(index)

    def on_preview_source_changed(self, *_args):
        token = self.preview_source_token()
        if token.startswith("repack:") and self.current_project and not self._compact:
            try:
                self.current_project = select_repack(self.current_project, token.split(":", 1)[1])
                self.projectChanged.emit(self.current_project)
            except Exception as exc:
                self.append_log(tr("psd.warning_prefix", message=str(exc)))
        self.refresh_preview_texture_combo()
        self.update_preview_controls()
        self._emit_context_changed()

    def refresh_preview_texture_combo(self):
        if not hasattr(self, "preview_texture_combo"):
            return
        previous = str(self.preview_texture_combo.currentData() or "")
        paths = self.preview_texture_paths_for_current_source()
        self.preview_texture_combo.blockSignals(True)
        self.preview_texture_combo.clear()
        if paths:
            self.preview_texture_combo.addItem(tr("psd.preview.all_textures"), userData="")
            for path in paths:
                self.preview_texture_combo.addItem(path.name, userData=str(path))
            index = self._combo_index_by_data(self.preview_texture_combo, previous)
            self.preview_texture_combo.setCurrentIndex(index if index >= 0 else 0)
        else:
            self.preview_texture_combo.addItem(tr("psd.preview.no_textures"), userData="")
        self.preview_texture_combo.blockSignals(False)

    def preview_source_token(self) -> str:
        return str(self.preview_source_combo.currentData() or "")

    def preview_source_repack_id(self) -> str:
        token = self.preview_source_token()
        return token.split(":", 1)[1] if token.startswith("repack:") else ""

    def preview_texture_paths_for_current_source(self) -> list[Path]:
        if not self.current_project:
            return []
        token = self.preview_source_token()
        try:
            if token.startswith("repack:"):
                entry = find_repack_entry(self.current_project, token.split(":", 1)[1])
                if not entry:
                    return []
                return [
                    resolve_project_path(self.current_project, value)
                    for value in (entry.get("output_paths") or [])
                    if str(value or "").strip() and resolve_project_path(self.current_project, value).is_file()
                ]
            if token.startswith("composite:"):
                entry = find_composite_entry(self.current_project, token.split(":", 1)[1])
                if not entry:
                    return []
                return [
                    resolve_project_path(self.current_project, value)
                    for value in (entry.get("output_paths") or [])
                    if str(value or "").strip() and resolve_project_path(self.current_project, value).is_file()
                ]
            package = resolve_live2d_package(self.current_project.live2d_dir)
            return [path for path in package.texture_paths if path.is_file()]
        except Exception as exc:
            self.append_log(tr("psd.warning_prefix", message=str(exc)))
            return []

    def selected_preview_image_paths(self) -> list[str]:
        selected = str(self.preview_texture_combo.currentData() or "")
        if selected and Path(selected).is_file():
            return [selected]
        return [str(path) for path in self.preview_texture_paths_for_current_source()]

    @staticmethod
    def _load_motions_from_model_json(model_json_path: str | Path | None) -> list[dict]:
        return load_live2d_motions(model_json_path)

    def refresh_motion_controls(self, model_json_path: str | Path | None):
        if not hasattr(self, "motion_combo"):
            return
        previous = ""
        current = self.selected_motion()
        if current:
            previous = f"{current.get('group')}::{current.get('index')}"
        self._motion_items = self._load_motions_from_model_json(model_json_path)
        self.motion_combo.blockSignals(True)
        self.motion_combo.clear()
        if not self._motion_items:
            self.motion_combo.addItem(tr("psd.preview.no_motions"), userData="")
            self.motion_combo.setEnabled(False)
            self.motion_play_button.setEnabled(False)
        else:
            self.motion_combo.setEnabled(True)
            for item in self._motion_items:
                key = f"{item.get('group')}::{item.get('index')}"
                self.motion_combo.addItem(str(item.get("display") or key), userData=key)
            index = self._combo_index_by_data(self.motion_combo, previous)
            self.motion_combo.setCurrentIndex(index if index >= 0 else 0)
            self.motion_play_button.setEnabled(True)
        self.motion_combo.blockSignals(False)
        self.send_selected_motion_to_preview()

    def selected_motion(self) -> dict | None:
        if not hasattr(self, "motion_combo") or not self._motion_items:
            return None
        key = str(self.motion_combo.currentData() or "")
        for item in self._motion_items:
            if key == f"{item.get('group')}::{item.get('index')}":
                return item
        return self._motion_items[0] if self._motion_items else None

    def on_motion_selection_changed(self, *_args):
        self.send_selected_motion_to_preview()

    def send_selected_motion_to_preview(self):
        motion = self.selected_motion()
        if not motion:
            return
        self._send_preview_command(
            {
                "type": "set_selected_motion",
                "group": str(motion.get("group") or ""),
                "index": int(motion.get("index") or 0),
            }
        )

    def play_selected_motion(self):
        motion = self.selected_motion()
        if not motion:
            return
        self._send_preview_command(
            {
                "type": "play_motion",
                "group": str(motion.get("group") or ""),
                "index": int(motion.get("index") or 0),
            }
        )

    def update_preview_controls(self):
        self.compare_history_button.setEnabled(not self._is_busy and self._history_comparison_request() is not None)
        if not hasattr(self, "preview_live2d_button"):
            return
        has_project = self.current_project is not None
        self.preview_image_button.setEnabled(has_project)
        self.preview_live2d_button.setEnabled(has_project)
        self.motion_play_button.setEnabled(has_project and bool(self._motion_items))
        self.preview_hint_label.setText(tr("psd.preview.hint_live2d_enabled"))
        if self._compact and hasattr(self, "apply_version_button"):
            has_version = has_project and self.preview_source_token().startswith(("repack:", "composite:"))
            self.apply_version_button.setEnabled(has_version and not self.is_busy())
            self.send_mod_button.setEnabled(has_version and not self.is_busy())
            self.preview_live2d_button.setToolTip(self.preview_hint_label.text())
            self.apply_version_button.setToolTip(self.version_hint.text())
            self.send_mod_button.setToolTip(self.version_hint.text())

    @staticmethod
    def _combo_index_by_data(combo: ComboBox, value: str) -> int:
        for index in range(combo.count()):
            if str(combo.itemData(index) or "") == value:
                return index
        return -1

    def latest_project_export(self) -> dict | None:
        if not self.current_project:
            return None
        scheme = find_pose_scheme(self.current_project)
        if scheme and scheme.get("psd"):
            return scheme
        exports = self.current_project.data.get("psd_exports") or []
        return exports[-1] if exports else None

    def update_project_after_result(self, result: ReconstructionResult):
        if not self.current_project:
            return
        try:
            if self._task_result_committed:
                pass
            elif result.mode in {"mesh", "atlas-components", "atlas-artmesh"} and self.pending_pose_scheme_id:
                self.current_project = record_pose_scheme_export(
                    self.current_project,
                    self.pending_pose_scheme_id,
                    result.psd_path,
                    result.metadata_path,
                    result.mode,
                    result.layer_count,
                )
            elif result.mode in {"mesh", "atlas-components", "atlas-artmesh"}:
                self.current_project = record_psd_export(
                    self.current_project,
                    result.psd_path,
                    result.metadata_path,
                    result.mode,
                    result.layer_count,
                )
            elif result.mode == "multi-repack" and self.pending_repack_id:
                self.current_project = record_multi_repack(
                    self.current_project,
                    self.pending_repack_id,
                    self.pending_multi_psd_paths,
                    self.pending_repack_dir,
                    result.output_paths,
                    self.pending_repack_name,
                    texture_outputs=result.texture_outputs,
                )
            elif result.mode in {"mesh-repack", "atlas-repack"} and self.pending_repack_id:
                self.current_project = record_repack(
                    self.current_project,
                    self.pending_repack_id,
                    self.pending_repack_source,
                    self.pending_repack_metadata or result.metadata_path,
                    self.pending_repack_dir,
                    result.output_paths,
                    scheme_id=self.pending_pose_scheme_id,
                    display_name=self.pending_repack_name,
                    texture_outputs=result.texture_outputs,
                    allow_shared_uv=bool(result.report.get("allow_shared_uv")),
                    affected_unselected_ids=result.report.get("affected_unselected_ids"),
                )
            if (not self._task_result_committed and result.mode in {"mesh", "atlas-components", "atlas-artmesh"}
                    and self.pending_skin_context):
                exports = self.current_project.data.get("psd_exports") or []
                if exports and not self.pending_pose_scheme_id:
                    exports[-1]["skin_source"] = dict(self.pending_skin_context)
                for scheme in self.current_project.data.get("pose_schemes", []):
                    if scheme.get("id") == self.pending_pose_scheme_id:
                        scheme["skin_source"] = dict(self.pending_skin_context)
                save_project(self.current_project.project_dir, self.current_project.data)
            elif result.mode in {"mesh-repack", "atlas-repack"} and self.pending_repack_id:
                scheme = self.pose_scheme_for_psd(self.pending_repack_source)
                source_context = (scheme or {}).get("skin_source")
                if source_context is None:
                    source = Path(self.pending_repack_source).resolve()
                    source_context = next((item.get("skin_source") for item in self.current_project.data.get("psd_exports", [])
                                           if item.get("psd") and resolve_project_path(self.current_project, item["psd"]) == source), None)
                for entry in self.current_project.data.get("repack_history", []):
                    if entry.get("id") == self.pending_repack_id:
                        entry["skin_source"] = dict(source_context) if source_context else None
                for pose in self.current_project.data.get("pose_schemes", []):
                    for version in pose.get("versions", []):
                        if version.get("id") == self.pending_repack_id:
                            version["skin_source"] = dict(source_context) if source_context else None
                save_project(self.current_project.project_dir, self.current_project.data)
            self.refresh_project_ui(self.pending_repack_id or None)
            self.refresh_project_combo(select_project_file=str(self.current_project.project_file))
            self._project_dirty = False
            self._update_project_header()
            self.projectChanged.emit(self.current_project)
        except Exception as exc:
            self.append_log(tr("psd.warning_prefix", message=str(exc)))
            raise

    def create_pose_scheme_from_preview(self, payload: dict):
        """Save a named parameter preset captured by the unified preview."""
        try:
            project_file = str(payload.get("project_file") or "")
            if not self.current_project or str(self.current_project.project_file) != str(Path(project_file).resolve()):
                if self._compact:
                    self.projectRequested.emit(project_file)
                    if not self.current_project:
                        return
                else:
                    self.set_current_project(load_project(project_file))
            name = str(payload.get("name") or "").strip()
            parameters = {
                str(key): float(value)
                for key, value in dict(payload.get("parameters") or {}).items()
            }
            self.current_project, preset = create_parameter_preset(
                self.current_project,
                name,
                parameters,
            )
            self.projectChanged.emit(self.current_project)
            self.set_workflow("export")
            self.refresh_parameter_preset_controls(str(preset.get("id") or ""))
            self.refresh_project_combo(
                select_project_file=str(self.current_project.project_file)
            )
            self.append_log(
                tr(
                    "psd.parameter_preset.saved",
                    name=str(preset.get("name") or name),
                    count=len(parameters),
                )
            )
            InfoBar.success(
                title=tr("common.success"),
                content=tr(
                    "psd.parameter_preset.saved_hint",
                    name=str(preset.get("name") or name),
                ),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )
        except Exception as exc:
            InfoBar.error(
                title=tr("common.error"),
                content=str(exc),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=6000,
            )

    def load_selected_preview_images(self):
        if not self.current_project:
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("psd.preview.no_project"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return
        image_paths = self.selected_preview_image_paths()
        if not image_paths:
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("psd.preview.no_images"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return
        viewer_mode = self.settings_manager.get_texture_viewer_mode()
        if viewer_mode == "system":
            for path in image_paths:
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            self.append_log(
                tr("psd.preview.external_opened", count=len(image_paths))
            )
            return
        if viewer_mode == "custom":
            executable = self.settings_manager.get_image_viewer_executable()
            if executable:
                QProcess.startDetached(executable, image_paths)
                self.append_log(
                    tr("psd.preview.external_opened", count=len(image_paths))
                )
                return
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("psd.preview.image_viewer_missing"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3500,
            )
        if self._task_embedded:
            self.texturePreviewRequested.emit(image_paths)
            return
        self.close_project_preview(update_placeholder=False)
        self.set_preview_visible(True, stop_process=False)
        self.preview_placeholder_label.setVisible(False)
        self.preview_image_panel.setVisible(True)
        self.preview_image_panel.load_images(image_paths)
        self.preview_title_label.setText(tr("psd.preview.images_title", count=len(image_paths)))

    def load_selected_preview_live2d(self):
        token = self.preview_source_token()
        if token.startswith(("repack:", "composite:")):
            self.start_project_preview("current", token.split(":", 1)[1])
        else:
            self.start_project_preview("original")

    def start_project_preview(self, mode: str, version_id: str = ""):
        if self.is_busy():
            return
        if not self.current_project:
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("psd.preview.no_project"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return

        if mode == "original":
            self.unifiedPreviewRequested.emit(
                str(self.current_project.base_model_json),
                str(self.current_project.project_file),
            )
            return

        version_id = version_id or self.preview_source_repack_id() or self.selected_repack_id()
        repack_entry = find_repack_entry(self.current_project, version_id or None)
        if version_id and (
            not repack_entry or str(repack_entry.get("id") or "") != version_id
        ):
            repack_entry = find_composite_entry(self.current_project, version_id)
        if not repack_entry:
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("psd.preview.no_repack"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return
        version_id = str(repack_entry.get("id") or version_id)

        self._begin_task("preview", tr("psd.preview.preparing_current"))
        if not self._compact:
            if self.preview_dialog is None:
                self._build_preview_dialog()
            self.clear_live2d_preview_widget()
            self.live2d_preview_host.setVisible(False)
            self.preview_image_panel.setVisible(False)
            self.preview_placeholder_label.setVisible(True)
            self.preview_placeholder_label.setText(tr("psd.preview.preparing_current"))
        self.preview_image_button.setEnabled(False)
        self.preview_live2d_button.setEnabled(False)
        self.preview_prepare_worker = PsdPreviewPrepareThread(self.current_project, version_id)
        self.preview_prepare_worker.progressUpdated.connect(self.append_log)
        self.preview_prepare_worker.previewReady.connect(self.on_preview_workspace_ready)
        self.preview_prepare_worker.previewError.connect(self.on_preview_workspace_error)
        self.preview_prepare_worker.finished.connect(self.on_preview_workspace_finished)
        self.preview_prepare_worker.start()

    def on_preview_workspace_ready(self, model_json_path: str, version_id: str):
        if self.current_project:
            self.set_preview_visible(False, stop_process=False)
            self._task_state = "succeeded"
            self._task_progress = 100
            self._task_message = tr("psd.stage.done")
            self._pending_preview_ready = (model_json_path, str(self.current_project.project_file))

    def on_preview_workspace_error(self, error: str):
        if hasattr(self, "preview_placeholder_label"):
            self.preview_placeholder_label.setText(tr("psd.preview.failed", error=error))
        self.on_reconstruction_error(error)

    def on_preview_workspace_finished(self):
        self.preview_prepare_worker = None
        self._on_worker_stopped()

    def _launch_preview_process(self, model_json_path: str, mode: str, repack_id: str):
        model_path = Path(model_json_path)
        if not model_path.is_file():
            InfoBar.error(
                title=tr("common.error"),
                content=tr("preview_window.error_model_missing", path=str(model_path)),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )
            return
        try:
            preview_model_path = prepare_model_json_for_preview(model_path)
        except Exception as exc:
            InfoBar.error(
                title=tr("common.error"),
                content=tr("psd.preview.failed", error=str(exc)),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )
            return

        try:
            self.set_preview_visible(True, stop_process=False)
            self.clear_live2d_preview_widget()
            self.preview_mode = mode
            self.preview_repack_id = repack_id
            self.preview_image_panel.setVisible(False)
            self.preview_placeholder_label.setVisible(True)
            self.preview_placeholder_label.setText(tr("psd.preview.preparing_current"))
            self.live2d_preview_host.setVisible(False)
            self.preview_title_label.setText(tr("psd.preview.title"))

            preview_window = Live2DPreviewWindow(
                str(preview_model_path),
                parent=self.live2d_preview_host,
                embedded=True,
            )
            self.live2d_preview_window = preview_window
            self.refresh_motion_controls(preview_model_path)
            preview_window.apply_settings(
                {
                    "show_controls": False,
                    "selected_motion_on_click": True,
                }
            )
            self.send_selected_motion_to_preview()
            self.live2d_preview_host_layout.addWidget(preview_window, 1)
            self.preview_placeholder_label.setVisible(False)
            self.live2d_preview_host.setVisible(True)
            preview_window.show()
            if self.current_project:
                self.current_project = set_preview_state(self.current_project, mode, repack_id)
            if mode == "current":
                self.preview_title_label.setText(tr("psd.preview.current_running", version=repack_id or "-"))
            else:
                self.preview_title_label.setText(tr("psd.preview.original_running"))
        except Exception as exc:
            self.close_project_preview(update_placeholder=False)
            self.preview_placeholder_label.setVisible(True)
            self.preview_image_panel.setVisible(False)
            self.live2d_preview_host.setVisible(False)
            self.preview_placeholder_label.setText(tr("psd.preview.failed", error=str(exc)))
            InfoBar.error(
                title=tr("common.error"),
                content=str(exc),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )

    def _send_preview_command(self, payload: dict) -> bool:
        if self._compact and self.preview_command_handler:
            return bool(self.preview_command_handler(payload))
        window = self.live2d_preview_window
        if window is None:
            return False
        try:
            command_type = str(payload.get("type") or "")
            if command_type == "play_motion":
                window.play_motion(str(payload.get("group") or ""), int(payload.get("index") or 0))
            elif command_type == "set_selected_motion":
                window.set_selected_motion(str(payload.get("group") or ""), int(payload.get("index") or 0))
            elif command_type == "settings":
                window.apply_settings(payload.get("settings") or {})
            elif command_type == "close":
                window.close()
            else:
                return False
            return True
        except Exception:
            return False

    def clear_live2d_preview_widget(self):
        window = self.live2d_preview_window
        self.live2d_preview_window = None
        if window is None:
            return
        try:
            self.live2d_preview_host_layout.removeWidget(window)
            window.close()
            window.deleteLater()
        except Exception:
            pass

    def _preview_dock_rect(self) -> dict | None:
        if not (self.preview_dialog is not None and self.preview_dialog.isVisible()):
            return None
        if hasattr(self, "preview_image_panel") and self.preview_image_panel.isVisible():
            rect = self.preview_image_panel.preview_rect()
            if rect:
                return rect
        target = self.preview_placeholder_label
        if target.width() <= 0 or target.height() <= 0:
            return None
        origin = target.mapToGlobal(target.rect().topLeft())
        margin = 4
        return {
            "x": int(origin.x() + margin),
            "y": int(origin.y() + margin),
            "w": max(240, int(target.width() - margin * 2)),
            "h": max(240, int(target.height() - margin * 2)),
        }

    def _send_preview_dock_geometry(self, force: bool = False):
        rect = self._preview_dock_rect()
        if not rect:
            return
        if not force and rect == self._last_preview_dock_rect:
            return
        if self._send_preview_command({"type": "dock", "rect": rect}):
            self._last_preview_dock_rect = rect

    def close_project_preview(self, update_placeholder: bool = True):
        self._preview_dock_timer.stop()
        self._last_preview_dock_rect = None
        self.preview_mode = ""
        self.preview_repack_id = ""
        self.clear_live2d_preview_widget()
        if hasattr(self, "live2d_preview_host"):
            self.live2d_preview_host.setVisible(False)
        if update_placeholder and hasattr(self, "preview_placeholder_label"):
            self.preview_placeholder_label.setText(tr("psd.preview.closed"))

    def close_preview_panel(self):
        self.close_project_preview()
        self.set_preview_visible(False, stop_process=False)
        if self._compact:
            self.previewClosed.emit()

    def toggle_preview_panel(self):
        self.set_preview_visible(not (self.preview_dialog is not None and self.preview_dialog.isVisible()))

    def _on_preview_dialog_closed(self, _result: int):
        self.close_project_preview()
        self.preview_toggle_button.setText(tr("psd.preview.show"))

    def set_preview_visible(self, visible: bool, stop_process: bool = True):
        if visible and self.preview_dialog is None:
            self._build_preview_dialog()
            self.preview_dialog.setWindowTitle(tr("psd.preview.title"))
        if self.preview_dialog is None:
            return
        if visible:
            if self.preview_image_panel.isHidden() and self.live2d_preview_host.isHidden():
                self.preview_placeholder_label.setVisible(True)
            self.preview_dialog.show()
            self.preview_dialog.raise_()
            self.preview_dialog.activateWindow()
        else:
            self.preview_dialog.close()
            if stop_process:
                self.close_project_preview()
        self.preview_toggle_button.setText(tr("psd.preview.hide") if visible else tr("psd.preview.show"))

    def _configure_responsive_controls(self):
        """Reserve enough room for translated labels at the active font size."""
        for button in self.findChildren(QAbstractButton):
            if isinstance(button, (ComboBox, EditableComboBox)):
                continue
            if not isinstance(button, (PushButton, PrimaryPushButton, QPushButton)):
                continue
            button.setMinimumHeight(max(36, button.fontMetrics().lineSpacing() + 16))
            button.setMaximumHeight(MAX_WIDGET_SIZE)
            if button is self.log_toggle_button:
                button.setMinimumWidth(52)
                button.setMaximumWidth(64)
                button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            else:
                button.setMinimumWidth(max(74, button.fontMetrics().horizontalAdvance(button.text()) + 28))
                button.setMaximumWidth(MAX_WIDGET_SIZE)
                button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        for control_type in (QComboBox, QLineEdit, QAbstractSpinBox):
            for control in self.findChildren(control_type):
                control.setMinimumWidth(0)
                control_height = max(34, control.fontMetrics().lineSpacing() + 14)
                control.setMinimumHeight(control_height)
                control.setMaximumHeight(MAX_WIDGET_SIZE)
                control.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        for label in self.findChildren(CaptionLabel):
            if label.wordWrap():
                label.setMinimumWidth(0)
                label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.repack_card_title.setMinimumWidth(0)
        self.repack_card_title.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.mesh_canvas_label.setWordWrap(True)
        self.mesh_canvas_label.setMinimumWidth(0)
        self.mesh_canvas_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.mode_combo.setMinimumWidth(0)
        self.mode_combo.setSizePolicy(QSizePolicy.Expanding if self._compact else QSizePolicy.Ignored, QSizePolicy.Fixed)
        if self._compact:
            self.output_edit.setReadOnly(True)
            for control_type in (ComboBox, EditableComboBox, LineEdit):
                for control in self.findChildren(control_type):
                    control.setMinimumWidth(0)
                    control.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def _arrange_button_rows(self):
        """Keep each action legible when translated text needs another row."""
        if not hasattr(self, "preview_action_layout"):
            return
        if self._compact and hasattr(self, "_compact_scrolls"):
            grids = [(self.project_button_layout, (self.new_project_button, self.save_project_button, self.open_project_folder_button)),
                     (self.preview_action_layout, (self.preview_image_button, self.preview_live2d_button, self.preview_close_button))]
            grids.extend(self._compact_button_grids)
            for layout, buttons in grids:
                for button in buttons:
                    layout.removeWidget(button)
                width = max(1, self.width() - 34)
                needed = sum(button.minimumWidth() for button in buttons) + 16
                for index, button in enumerate(buttons):
                    layout.addWidget(button, 0 if needed <= width else index, index if needed <= width else 0)
            return
        available = max(
            1,
            self.left_scroll.viewport().width()
            - self.left_panel_layout.contentsMargins().right()
            - self.preview_control_layout.contentsMargins().left()
            - self.preview_control_layout.contentsMargins().right(),
        )

        def fits(buttons: tuple[QAbstractButton, ...], layout: QGridLayout, width: int = available) -> bool:
            needed = sum(
                max(button.minimumWidth(), button.fontMetrics().horizontalAdvance(button.text()) + 28)
                for button in buttons
            )
            return needed + max(8, layout.horizontalSpacing()) * (len(buttons) - 1) <= width

        projects = (
            self.new_project_button,
            self.save_project_button,
            self.open_project_folder_button,
        )
        for button in projects:
            self.project_button_layout.removeWidget(button)
        if fits(projects, self.project_button_layout):
            for column, button in enumerate(projects):
                self.project_button_layout.addWidget(button, 0, column)
        else:
            self.project_button_layout.addWidget(projects[0], 0, 0)
            self.project_button_layout.addWidget(projects[1], 0, 1)
            self.project_button_layout.addWidget(projects[2], 1, 0, 1, 2)

        flows = (self.export_flow_button, self.repack_flow_button)
        for button in flows:
            self.workflow_segment_layout.removeWidget(button)
        if fits(flows, self.workflow_segment_layout):
            self.workflow_segment_layout.addWidget(flows[0], 0, 0)
            self.workflow_segment_layout.addWidget(flows[1], 0, 1)
        else:
            self.workflow_segment_layout.addWidget(flows[0], 0, 0)
            self.workflow_segment_layout.addWidget(flows[1], 1, 0)

        preview_actions = (
            self.preview_image_button,
            self.preview_live2d_button,
            self.preview_close_button,
        )
        for button in preview_actions:
            self.preview_action_layout.removeWidget(button)
        if fits(preview_actions, self.preview_action_layout):
            for column, button in enumerate(preview_actions):
                self.preview_action_layout.addWidget(button, 0, column)
        else:
            self.preview_action_layout.addWidget(preview_actions[0], 0, 0)
            self.preview_action_layout.addWidget(preview_actions[2], 0, 1)
            self.preview_action_layout.addWidget(preview_actions[1], 1, 0, 1, 2)

        right_available = max(1, self.right_scroll.viewport().width() - self.right_panel_layout.contentsMargins().left())
        actions = (
            self.reconstruct_button,
            self.artmesh_inspector_button,
            self.open_output_button,
            self.preview_toggle_button,
        )
        for button in actions:
            self.action_layout.removeWidget(button)
        if fits(actions[:2], self.action_layout, right_available) and fits(actions[2:], self.action_layout, right_available):
            self.action_layout.addWidget(actions[0], 0, 0)
            self.action_layout.addWidget(actions[1], 0, 1)
            self.action_layout.addWidget(actions[2], 1, 0)
            self.action_layout.addWidget(actions[3], 1, 1)
        else:
            self.action_layout.addWidget(actions[0], 0, 0)
            self.action_layout.addWidget(actions[3], 0, 1)
            self.action_layout.addWidget(actions[2], 1, 0, 1, 2)
            self.action_layout.addWidget(actions[1], 2, 0, 1, 2)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange_timer.start(0)

    def _update_log_toggle_button(self):
        if not hasattr(self, "log_toggle_button"):
            return
        self.log_toggle_button.setText("-" if self._log_expanded else "+")

    def _set_log_expanded(self, expanded: bool):
        if self._compact:
            self._log_expanded = bool(expanded)
            self.log_text.setVisible(self._log_expanded)
            self.log_text.setMinimumHeight(180 if expanded else 0)
            self.log_frame.setMinimumHeight(0)
            self.log_frame.setMaximumHeight(MAX_WIDGET_SIZE if expanded else 60)
            self._update_log_toggle_button()
            return
        if self._log_expanded == bool(expanded) and self.log_text.isHidden() != bool(expanded):
            return
        sizes = self.workspace_splitter.sizes()
        if self._log_expanded and sizes:
            self._expanded_log_height = max(150, sizes[1])
        self._log_expanded = bool(expanded)
        self.log_text.setVisible(self._log_expanded)
        self.log_frame.setMinimumHeight(150 if self._log_expanded else 0)
        self.log_frame.setMaximumHeight(MAX_WIDGET_SIZE if self._log_expanded else 72)
        self._update_log_toggle_button()
        total = max(1, sum(sizes) or self.workspace_splitter.height())
        log_height = min(self._expanded_log_height, total // 2) if self._log_expanded else 72
        self.workspace_splitter.setSizes([max(240, total - log_height), log_height])

    def toggle_log_panel(self):
        self._set_log_expanded(not self._log_expanded)

    def append_log(self, text: str, *, expand: bool = False):
        self.log_text.append(text)
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        if expand:
            self._set_log_expanded(True)
        if self._task_id:
            self._emit_task_state()

    @staticmethod
    def _is_supported_path(path: str) -> bool:
        if not path:
            return False
        p = Path(path)
        return p.is_dir() or p.suffix.lower() in {".json", ".moc3", ".psd"}

    def updateUIScale(self, window_width, window_height):
        self._configure_responsive_controls()
        self._arrange_button_rows()
        font = QApplication.instance().font()
        for label in self.findChildren(SubtitleLabel):
            label_font = label.font()
            label_font.setPointSize(font.pointSize() + 2)
            label.setFont(label_font)
        if self._compact:
            self._compact_control_text()
            self._arrange_button_rows()
