import json
import os
import re
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    EditableComboBox,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    SubtitleLabel,
    TextEdit,
)

from app.core.psd_reconstructor import (
    ReconstructionResult,
    reconstruct_live2d_psd,
    repack_atlas_png_from_psd,
)
from app.core.psd_project import (
    Live2DPSDProject,
    PROJECT_FILE_NAME,
    create_project_from_source,
    create_repack_dir,
    find_repack_entry,
    load_project,
    prepare_repack_preview_workspace,
    record_psd_export,
    record_repack,
    resolve_project_path,
    save_project,
    select_repack,
    set_preview_state,
    sanitize_project_name,
)
from app.core.model import prepare_model_json_for_preview, resolve_live2d_package
from app.core.model.motions import load_live2d_motions
from app.core.settings_manager import SettingsManager
from app.gui.Live2DPreviewWindow import Live2DPreviewWindow
from app.gui.PreviewPage import ImagePreviewPanel
from app.i18n import get_i18n, tr


class PsdReconstructionThread(QThread):
    progressUpdated = Signal(int, str)
    reconstructionFinished = Signal(object)
    reconstructionError = Signal(str)

    def __init__(self, source_path: str, output_dir: str, mode: str, metadata_path: str | None = None):
        super().__init__()
        self.source_path = source_path
        self.output_dir = output_dir
        self.mode = mode
        self.metadata_path = metadata_path

    def run(self):
        try:
            if self.mode == "repack-atlas":
                result = repack_atlas_png_from_psd(
                    self.source_path,
                    self.output_dir,
                    metadata_path=self.metadata_path or None,
                    progress=lambda value, message: self.progressUpdated.emit(value, message),
                )
            else:
                result = reconstruct_live2d_psd(
                    self.source_path,
                    self.output_dir,
                    progress=lambda value, message: self.progressUpdated.emit(value, message),
                    mode=self.mode,
                )
            self.reconstructionFinished.emit(result)
        except Exception as exc:
            self.reconstructionError.emit(str(exc))


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
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("psdReconstructionPage")
        self.setAcceptDrops(True)

        self.i18n = get_i18n()
        self.settings_manager = SettingsManager()
        self.selected_source = ""
        self.selected_metadata = ""
        self.workflow = "export"
        self.output_manually_selected = False
        self.last_output_dir = self.default_output_dir()
        self.worker: PsdReconstructionThread | None = None
        self.project_worker: PsdProjectImportThread | None = None
        self.preview_prepare_worker: PsdPreviewPrepareThread | None = None
        self.current_project: Live2DPSDProject | None = None
        self._project_combo_refreshing = False
        self._motion_items: list[dict] = []
        self.live2d_preview_window: Live2DPreviewWindow | None = None
        self.preview_mode = ""
        self.preview_repack_id = ""
        self._last_preview_dock_rect: dict | None = None
        self.pending_repack_id = ""
        self.pending_repack_dir = ""
        self.pending_repack_source = ""
        self.pending_repack_metadata = ""
        self._preview_dock_timer = QTimer(self)
        self._preview_dock_timer.setInterval(700)
        self._preview_dock_timer.timeout.connect(self._send_preview_dock_geometry)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.close_project_preview)

        self.setupUI()
        self.retranslate_ui()
        self.i18n.languageChanged.connect(self.retranslate_ui)
        QTimer.singleShot(0, lambda: self.load_last_project(silent=True))

    def setupUI(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(20, 20, 20, 20)
        self.main_layout.setSpacing(12)

        self.title_label = SubtitleLabel("", self)
        self.main_layout.addWidget(self.title_label)

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
                border: 1px dashed #8a8a8a;
                border-radius: 6px;
                background: #fafafa;
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

        self.content_splitter = QSplitter(Qt.Horizontal, self)
        self.content_splitter.setChildrenCollapsible(False)
        self.main_layout.addWidget(self.content_splitter, 1)

        self.left_panel = QWidget(self.content_splitter)
        self.left_panel.setMinimumWidth(320)
        self.left_panel_layout = QVBoxLayout(self.left_panel)
        self.left_panel_layout.setContentsMargins(0, 0, 8, 0)
        self.left_panel_layout.setSpacing(10)
        self.content_splitter.addWidget(self.left_panel)

        self.right_panel = QWidget(self.content_splitter)
        self.right_panel_layout = QVBoxLayout(self.right_panel)
        self.right_panel_layout.setContentsMargins(8, 0, 0, 0)
        self.right_panel_layout.setSpacing(10)
        self.content_splitter.addWidget(self.right_panel)
        self.content_splitter.setSizes([420, 980])

        self.project_frame = QFrame(self.left_panel)
        self.project_frame.setObjectName("psdProjectFrame")
        self.project_layout = QVBoxLayout(self.project_frame)
        self.project_layout.setContentsMargins(12, 12, 12, 12)
        self.project_layout.setSpacing(8)
        self.project_title_label = SubtitleLabel("", self.project_frame)
        self.project_layout.addWidget(self.project_title_label)

        self.project_name_edit = LineEdit(self.project_frame)
        self.project_name_edit.setPlaceholderText("")
        self.project_layout.addWidget(self.project_name_edit)

        self.project_combo = EditableComboBox(self.project_frame)
        self.project_combo.setPlaceholderText("")
        self.project_combo.textChanged.connect(self.on_project_combo_text_changed)
        self.project_combo.currentIndexChanged.connect(self.on_project_combo_changed)
        self.project_layout.addWidget(self.project_combo)

        self.project_button_layout = QHBoxLayout()
        self.project_button_layout.setSpacing(8)
        self.new_project_button = PushButton("", self.project_frame)
        self.new_project_button.clicked.connect(self.create_project_from_current_source)
        self.save_project_button = PushButton("", self.project_frame)
        self.save_project_button.clicked.connect(self.save_current_project)
        self.open_project_folder_button = PushButton("", self.project_frame)
        self.open_project_folder_button.setEnabled(False)
        self.open_project_folder_button.clicked.connect(self.open_current_project_folder)
        self.project_button_layout.addWidget(self.new_project_button)
        self.project_button_layout.addWidget(self.save_project_button)
        self.project_button_layout.addWidget(self.open_project_folder_button)
        self.project_layout.addLayout(self.project_button_layout)

        self.project_status_label = CaptionLabel("", self.project_frame)
        self.project_status_label.setWordWrap(True)
        self.project_layout.addWidget(self.project_status_label)
        self.left_panel_layout.addWidget(self.project_frame)

        self.workflow_layout = QHBoxLayout()
        self.workflow_layout.setSpacing(10)
        self.workflow_label = SubtitleLabel("", self.left_panel)
        self.workflow_segment = QFrame(self.left_panel)
        self.workflow_segment.setObjectName("psdWorkflowSegment")
        self.workflow_segment_layout = QHBoxLayout(self.workflow_segment)
        self.workflow_segment_layout.setContentsMargins(3, 3, 3, 3)
        self.workflow_segment_layout.setSpacing(3)
        self.export_flow_button = PushButton("", self.workflow_segment)
        self.export_flow_button.setObjectName("psdWorkflowButton")
        self.export_flow_button.clicked.connect(lambda: self.set_workflow("export"))
        self.repack_flow_button = PushButton("", self.workflow_segment)
        self.repack_flow_button.setObjectName("psdWorkflowButton")
        self.repack_flow_button.clicked.connect(lambda: self.set_workflow("repack"))
        self.workflow_segment_layout.addWidget(self.export_flow_button)
        self.workflow_segment_layout.addWidget(self.repack_flow_button)
        self.workflow_layout.addWidget(self.workflow_label)
        self.workflow_layout.addWidget(self.workflow_segment)
        self.workflow_layout.addStretch(1)
        self.left_panel_layout.addLayout(self.workflow_layout)

        self.source_layout = QHBoxLayout()
        self.source_label = SubtitleLabel("", self.left_panel)
        self.source_edit = LineEdit(self.left_panel)
        self.source_edit.setReadOnly(True)
        self.source_file_button = PushButton("", self.left_panel)
        self.source_file_button.clicked.connect(self.browse_source_file)
        self.source_folder_button = PushButton("", self.left_panel)
        self.source_folder_button.clicked.connect(self.browse_source_folder)
        self.source_layout.addWidget(self.source_label)
        self.source_layout.addWidget(self.source_edit, 1)
        self.source_layout.addWidget(self.source_file_button)
        self.source_layout.addWidget(self.source_folder_button)
        self.left_panel_layout.addLayout(self.source_layout)

        self.mode_frame = QFrame(self.left_panel)
        self.mode_container_layout = QVBoxLayout(self.mode_frame)
        self.mode_container_layout.setContentsMargins(0, 0, 0, 0)
        self.mode_container_layout.setSpacing(6)
        self.mode_layout = QHBoxLayout()
        self.mode_label = SubtitleLabel("", self.mode_frame)
        self.mode_combo = ComboBox(self.mode_frame)
        self.mode_combo.addItem("", userData="mesh")
        self.mode_combo.addItem("", userData="atlas-components")
        self.mode_combo.currentIndexChanged.connect(self.on_mode_changed)
        self.mode_layout.addWidget(self.mode_label)
        self.mode_layout.addWidget(self.mode_combo, 1)
        self.mode_hint_label = CaptionLabel("", self.mode_frame)
        self.mode_hint_label.setWordWrap(True)
        self.mode_container_layout.addLayout(self.mode_layout)
        self.mode_container_layout.addWidget(self.mode_hint_label)
        self.left_panel_layout.addWidget(self.mode_frame)

        self.metadata_frame = QFrame(self.left_panel)
        self.metadata_layout = QHBoxLayout(self.metadata_frame)
        self.metadata_layout.setContentsMargins(0, 0, 0, 0)
        self.metadata_label = SubtitleLabel("", self.metadata_frame)
        self.metadata_edit = LineEdit(self.metadata_frame)
        self.metadata_edit.setReadOnly(True)
        self.metadata_button = PushButton("", self.metadata_frame)
        self.metadata_button.clicked.connect(self.browse_metadata_file)
        self.metadata_default_button = PushButton("", self.metadata_frame)
        self.metadata_default_button.clicked.connect(self.clear_metadata_file)
        self.metadata_layout.addWidget(self.metadata_label)
        self.metadata_layout.addWidget(self.metadata_edit, 1)
        self.metadata_layout.addWidget(self.metadata_button)
        self.metadata_layout.addWidget(self.metadata_default_button)
        self.left_panel_layout.addWidget(self.metadata_frame)

        self.output_layout = QHBoxLayout()
        self.output_label = SubtitleLabel("", self.left_panel)
        self.output_edit = LineEdit(self.left_panel)
        self.output_edit.setText(self.last_output_dir)
        self.output_button = PushButton("", self.left_panel)
        self.output_button.clicked.connect(self.browse_output)
        self.output_layout.addWidget(self.output_label)
        self.output_layout.addWidget(self.output_edit, 1)
        self.output_layout.addWidget(self.output_button)
        self.left_panel_layout.addLayout(self.output_layout)

        self.action_layout = QHBoxLayout()
        self.reconstruct_button = PrimaryPushButton("", self.left_panel)
        self.reconstruct_button.clicked.connect(self.start_reconstruction)
        self.open_output_button = PushButton("", self.left_panel)
        self.open_output_button.setEnabled(False)
        self.open_output_button.clicked.connect(self.open_output_folder)
        self.preview_toggle_button = PushButton("", self.left_panel)
        self.preview_toggle_button.clicked.connect(self.toggle_preview_panel)
        self.action_layout.addWidget(self.reconstruct_button)
        self.action_layout.addWidget(self.open_output_button)
        self.action_layout.addWidget(self.preview_toggle_button)
        self.action_layout.addStretch(1)
        self.left_panel_layout.addLayout(self.action_layout)

        self.progress_layout = QHBoxLayout()
        self.progress_bar = ProgressBar(self.left_panel)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.stage_label = CaptionLabel("", self.left_panel)
        self.stage_label.setMinimumWidth(160)
        self.stage_label.setWordWrap(True)
        self.progress_layout.addWidget(self.progress_bar, 1)
        self.progress_layout.addWidget(self.stage_label)
        self.left_panel_layout.addLayout(self.progress_layout)

        self.preview_control_frame = QFrame(self.left_panel)
        self.preview_control_frame.setObjectName("psdPreviewControlFrame")
        self.preview_control_layout = QVBoxLayout(self.preview_control_frame)
        self.preview_control_layout.setContentsMargins(12, 12, 12, 12)
        self.preview_control_layout.setSpacing(8)
        self.preview_control_title_label = SubtitleLabel("", self.preview_control_frame)
        self.preview_control_layout.addWidget(self.preview_control_title_label)

        self.preview_source_layout = QHBoxLayout()
        self.preview_source_label = BodyLabel("", self.preview_control_frame)
        self.preview_source_combo = ComboBox(self.preview_control_frame)
        self.preview_source_combo.currentIndexChanged.connect(self.on_preview_source_changed)
        self.preview_source_layout.addWidget(self.preview_source_label)
        self.preview_source_layout.addWidget(self.preview_source_combo, 1)
        self.preview_control_layout.addLayout(self.preview_source_layout)

        self.preview_texture_layout = QHBoxLayout()
        self.preview_texture_label = BodyLabel("", self.preview_control_frame)
        self.preview_texture_combo = ComboBox(self.preview_control_frame)
        self.preview_texture_layout.addWidget(self.preview_texture_label)
        self.preview_texture_layout.addWidget(self.preview_texture_combo, 1)
        self.preview_control_layout.addLayout(self.preview_texture_layout)

        self.preview_action_layout = QHBoxLayout()
        self.preview_action_layout.setSpacing(8)
        self.preview_image_button = PrimaryPushButton("", self.preview_control_frame)
        self.preview_image_button.clicked.connect(self.load_selected_preview_images)
        self.preview_live2d_button = PushButton("", self.preview_control_frame)
        self.preview_live2d_button.clicked.connect(self.load_selected_preview_live2d)
        self.preview_close_button = PushButton("", self.preview_control_frame)
        self.preview_close_button.clicked.connect(self.close_preview_panel)
        self.preview_action_layout.addWidget(self.preview_image_button, 1)
        self.preview_action_layout.addWidget(self.preview_live2d_button, 1)
        self.preview_action_layout.addWidget(self.preview_close_button, 1)
        self.preview_control_layout.addLayout(self.preview_action_layout)

        self.preview_hint_label = CaptionLabel("", self.preview_control_frame)
        self.preview_hint_label.setWordWrap(True)
        self.preview_control_layout.addWidget(self.preview_hint_label)
        self.left_panel_layout.addWidget(self.preview_control_frame)

        self.motion_frame = QFrame(self.left_panel)
        self.motion_frame.setObjectName("psdMotionFrame")
        self.motion_layout = QVBoxLayout(self.motion_frame)
        self.motion_layout.setContentsMargins(12, 12, 12, 12)
        self.motion_layout.setSpacing(8)
        self.motion_title_label = SubtitleLabel("", self.motion_frame)
        self.motion_layout.addWidget(self.motion_title_label)
        self.motion_row_layout = QHBoxLayout()
        self.motion_label = BodyLabel("", self.motion_frame)
        self.motion_combo = ComboBox(self.motion_frame)
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
        self.left_panel_layout.addWidget(self.motion_frame)

        self.left_panel_layout.addStretch(1)

        self.preview_frame = QFrame(self.right_panel)
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
        self.right_panel_layout.addWidget(self.preview_frame, 1)
        self.preview_frame.setVisible(False)

        self.log_frame = QFrame(self.right_panel)
        self.log_layout = QVBoxLayout(self.log_frame)
        self.log_layout.setContentsMargins(0, 0, 0, 0)
        self.log_layout.setSpacing(8)
        self.log_label = SubtitleLabel("", self.log_frame)
        self.log_layout.addWidget(self.log_label)

        self.log_text = TextEdit(self.log_frame)
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(220)
        self.log_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.log_layout.addWidget(self.log_text, 1)
        self.right_panel_layout.addWidget(self.log_frame, 1)

        for button in self.findChildren(PushButton):
            button.setMinimumSize(104, 32)
            button.setMaximumHeight(36)
        for line_edit in self.findChildren(LineEdit):
            line_edit.setMinimumHeight(32)
            line_edit.setMaximumHeight(36)
        for button in (
            self.new_project_button,
            self.save_project_button,
            self.open_project_folder_button,
            self.preview_image_button,
            self.preview_live2d_button,
            self.preview_close_button,
            self.motion_play_button,
        ):
            button.setMinimumWidth(86)
        self.export_flow_button.setMinimumWidth(150)
        self.repack_flow_button.setMinimumWidth(176)
        self._apply_static_styles()

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
        self.source_label.setText(tr("psd.source"))
        self.source_edit.setPlaceholderText(tr("psd.placeholder_source"))
        self.source_file_button.setText(tr("psd.browse_file"))
        self.source_folder_button.setText(tr("psd.browse_folder"))
        self.mode_label.setText(tr("psd.mode"))
        self.mode_combo.setItemText(0, tr("psd.mode.mesh_pose"))
        self.mode_combo.setItemText(1, tr("psd.mode.editable_atlas"))
        self.output_label.setText(tr("psd.output_directory"))
        self.output_edit.setPlaceholderText(tr("psd.placeholder_output"))
        self.output_button.setText(tr("common.browse"))
        self.reconstruct_button.setText(tr("psd.reconstruct_button"))
        self.open_output_button.setText(tr("psd.open_output_folder"))
        self.preview_toggle_button.setText(
            tr("psd.preview.hide") if not self.preview_frame.isHidden() else tr("psd.preview.show")
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
        self.preview_title_label.setText(tr("psd.preview.title"))
        self.preview_placeholder_label.setText(tr("psd.preview.placeholder"))
        self.preview_image_panel.retranslate_ui()
        self.log_label.setText(tr("psd.log"))
        self.workflow_label.setText(tr("psd.workflow"))
        self.export_flow_button.setText(tr("psd.workflow.export"))
        self.repack_flow_button.setText(tr("psd.workflow.repack"))
        self.metadata_label.setText(tr("psd.metadata_file"))
        self.metadata_edit.setPlaceholderText(tr("psd.placeholder_metadata"))
        self.metadata_button.setText(tr("psd.browse_metadata"))
        self.metadata_default_button.setText(tr("psd.use_default_metadata"))
        self.stage_label.setText(tr("psd.stage.idle"))
        self._update_workflow_ui()
        self.refresh_project_ui()
        self.refresh_project_combo()
        self.refresh_preview_source_combo()

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
        if self.workflow == "repack":
            title = tr("dialog.select_psd_file")
            file_filter = tr("dialog.filter_psd_files")
        else:
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
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("dialog.select_psd_metadata_file"),
            str(Path(self.source_edit.text()).parent) if self.source_edit.text().strip() else "",
            tr("dialog.filter_lpkpsd_metadata_files"),
        )
        if path:
            self.selected_metadata = os.path.abspath(path)
            self.metadata_edit.setText(self.selected_metadata)
            self.append_log(tr("psd.selected_metadata", path=self.selected_metadata))

    def clear_metadata_file(self):
        self.selected_metadata = ""
        self.metadata_edit.clear()
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
        self.selected_source = os.path.abspath(path)
        self.source_edit.setText(self.selected_source)
        if Path(self.selected_source).suffix.lower() == ".psd":
            self.set_workflow("repack")
            self._sync_default_metadata()
            self._sync_repack_output_dir()
        else:
            self.set_workflow("export")
        if self.workflow != "repack" and not self.output_manually_selected:
            self.last_output_dir = self.default_output_dir(self.selected_source)
            self.output_edit.setText(self.last_output_dir)
        self.append_log(tr("psd.selected_source", path=self.selected_source))

    def start_reconstruction(self):
        source = self.source_edit.text().strip()
        output_dir = self.output_edit.text().strip() or self.default_output_dir(source)

        if self.current_project and self.workflow == "export":
            source = str(self.current_project.base_model_json)
            output_dir = str((self.current_project.project_dir / "psd").resolve())
            self.source_edit.setText(source)
            self.output_edit.setText(output_dir)

        if not source:
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("psd.warning_no_source"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return

        metadata_path = None
        self.pending_repack_id = ""
        self.pending_repack_dir = ""
        self.pending_repack_source = ""
        self.pending_repack_metadata = ""
        if self.workflow == "repack":
            if self.current_project and Path(source).suffix.lower() != ".psd":
                latest_export = self.latest_project_export()
                if latest_export:
                    source = str((self.current_project.project_dir / latest_export["psd"]).resolve())
                    metadata = str(latest_export.get("metadata") or "")
                    metadata_path = (
                        str((self.current_project.project_dir / metadata).resolve())
                        if metadata
                        else None
                    )
                    self.source_edit.setText(source)
                    if metadata_path:
                        self.metadata_edit.setText(metadata_path)
            if Path(source).suffix.lower() != ".psd":
                InfoBar.warning(
                    title=tr("common.warning"),
                    content=tr("psd.warning_no_psd_source"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=3000,
                )
                return
            if self.current_project:
                version_id, version_dir = create_repack_dir(self.current_project)
                output_dir = str(version_dir)
                self.pending_repack_id = version_id
                self.pending_repack_dir = output_dir
            else:
                output_dir = str(Path(source).resolve().parent)
            metadata_path = metadata_path or self.metadata_edit.text().strip() or None
            if metadata_path and not os.path.isfile(metadata_path):
                InfoBar.warning(
                    title=tr("common.warning"),
                    content=tr("psd.warning_metadata_missing", path=metadata_path),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=3000,
                )
                return
            mode = "repack-atlas"
            mode_text = tr("psd.workflow.repack")
            self.pending_repack_source = source
            self.pending_repack_metadata = metadata_path or ""
        else:
            if Path(source).suffix.lower() == ".psd":
                InfoBar.warning(
                    title=tr("common.warning"),
                    content=tr("psd.warning_export_needs_model"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=3000,
                )
                return
            mode = self.mode_combo.currentData() or "mesh"
            mode_text = self.mode_combo.currentText()

        self.last_output_dir = os.path.abspath(output_dir)
        self.output_edit.setText(self.last_output_dir)
        self.set_busy(True)
        self.progress_bar.setValue(0)
        self.stage_label.setText(tr("psd.stage.starting"))
        self.log_text.clear()
        self.append_log(tr("psd.started", mode=mode_text))
        if mode == "repack-atlas" and metadata_path:
            self.append_log(tr("psd.metadata_log", path=metadata_path))
        elif mode == "repack-atlas":
            self.append_log(tr("psd.metadata_default_log"))

        self.worker = PsdReconstructionThread(source, self.last_output_dir, mode, metadata_path)
        self.worker.progressUpdated.connect(self.on_progress_updated)
        self.worker.reconstructionFinished.connect(self.on_reconstruction_finished)
        self.worker.reconstructionError.connect(self.on_reconstruction_error)
        self.worker.start()

    def on_progress_updated(self, value: int, message: str):
        self.progress_bar.setValue(value)
        stage_text = self.stage_text_from_message(message)
        if stage_text:
            self.stage_label.setText(stage_text)
        if message:
            self.append_log(message)

    def on_reconstruction_finished(self, result: ReconstructionResult):
        self.set_busy(False)
        self.progress_bar.setValue(100)
        self.stage_label.setText(tr("psd.stage.done"))
        self.open_output_button.setEnabled(True)

        for warning in result.warnings:
            self.append_log(tr("psd.warning_prefix", message=warning))

        if result.metadata_path:
            self.append_log(tr("psd.metadata_log", path=str(result.metadata_path)))
        if result.output_paths:
            self.append_log(tr("psd.output_count_log", count=len(result.output_paths)))

        self.update_project_after_result(result)

        self.append_log(
            tr(
                "psd.finished_log",
                path=str(result.primary_path),
                count=result.layer_count,
                mode=result.mode,
            )
        )
        InfoBar.success(
            title=tr("common.success"),
            content=tr("psd.success_content", path=str(result.primary_path)),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000,
        )

    def on_reconstruction_error(self, error: str):
        self.set_busy(False)
        self.progress_bar.setValue(0)
        self.stage_label.setText(tr("psd.stage.failed"))
        self.append_log(tr("psd.error_log", error=error))
        InfoBar.error(
            title=tr("common.error"),
            content=error,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=6000,
        )

    def set_busy(self, busy: bool):
        self.reconstruct_button.setEnabled(not busy)
        self.source_file_button.setEnabled(not busy)
        self.source_folder_button.setEnabled(not busy)
        self.new_project_button.setEnabled(not busy)
        self.save_project_button.setEnabled(not busy)
        self.open_project_folder_button.setEnabled(not busy and self.current_project is not None)
        self.project_combo.setEnabled(not busy)
        self.output_button.setEnabled(not busy and self.workflow != "repack")
        self.output_edit.setReadOnly(self.workflow == "repack")
        self.mode_combo.setEnabled(not busy)
        self.export_flow_button.setEnabled(not busy)
        self.repack_flow_button.setEnabled(not busy)
        self.metadata_button.setEnabled(not busy)
        self.metadata_default_button.setEnabled(not busy)
        self.preview_image_button.setEnabled(not busy and self.current_project is not None)
        self.motion_play_button.setEnabled(not busy and bool(self._motion_items))
        self.update_preview_controls()

    def set_workflow(self, workflow: str):
        self.workflow = "repack" if workflow == "repack" else "export"
        if self.workflow == "repack" and Path(self.selected_source).suffix.lower() == ".psd":
            self._sync_repack_output_dir()
        self._update_workflow_ui()

    def on_mode_changed(self, *_args):
        self.update_mode_hint()

    def _update_workflow_ui(self):
        is_repack = self.workflow == "repack"
        self.mode_frame.setVisible(not is_repack)
        self.metadata_frame.setVisible(is_repack)
        self.source_folder_button.setVisible(not is_repack)
        self.source_label.setText(tr("psd.psd_source") if is_repack else tr("psd.source"))
        self.source_edit.setPlaceholderText(
            tr("psd.placeholder_psd_source") if is_repack else tr("psd.placeholder_source")
        )
        self.reconstruct_button.setText(tr("psd.repack_button") if is_repack else tr("psd.export_button"))
        self.output_button.setEnabled(not is_repack)
        self.output_edit.setReadOnly(is_repack)
        self._style_workflow_button(self.export_flow_button, not is_repack)
        self._style_workflow_button(self.repack_flow_button, is_repack)
        self.update_mode_hint()

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
                    background: #DDE2EA;
                    color: #8B95A1;
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
                    color: #30343B;
                    padding: 0 14px;
                }
                PushButton#psdWorkflowButton:hover {
                    background: #EEF2F7;
                }
                PushButton#psdWorkflowButton:disabled {
                    background: transparent;
                    color: #A0A7B0;
                }
                """
            )

    def _apply_static_styles(self):
        self.workflow_segment.setStyleSheet(
            """
            QFrame#psdWorkflowSegment {
                border: 1px solid #DDE2EA;
                border-radius: 9px;
                background: #F7F9FC;
            }
            """
        )
        self.drop_frame.setStyleSheet(
            """
            QFrame#psdDropFrame {
                border: 1px dashed #B8C1CC;
                border-radius: 8px;
                background: #FAFBFD;
            }
            """
        )
        self.project_frame.setStyleSheet(
            """
            QFrame#psdProjectFrame {
                border: 1px solid #E3E8EF;
                border-radius: 8px;
                background: #FFFFFF;
            }
            """
        )
        self.preview_control_frame.setStyleSheet(
            """
            QFrame#psdPreviewControlFrame {
                border: 1px solid #E3E8EF;
                border-radius: 8px;
                background: #FFFFFF;
            }
            """
        )
        self.motion_frame.setStyleSheet(
            """
            QFrame#psdMotionFrame {
                border: 1px solid #E3E8EF;
                border-radius: 8px;
                background: #FFFFFF;
            }
            """
        )
        self.preview_frame.setStyleSheet(
            """
            QFrame#psdPreviewFrame {
                border: 1px solid #DDE2EA;
                border-radius: 8px;
                background: #FAFBFD;
            }
            """
        )
        self.live2d_preview_host.setStyleSheet(
            """
            QFrame#psdLive2DPreviewHost {
                border: 1px solid #DDE2EA;
                border-radius: 8px;
                background: #FFFFFF;
            }
            """
        )

    def update_mode_hint(self):
        mode = self.mode_combo.currentData() or "mesh"
        if self.workflow == "repack":
            self.mode_hint_label.setText("")
        elif mode == "atlas-components":
            self.mode_hint_label.setText(tr("psd.mode_hint.atlas_components"))
        else:
            self.mode_hint_label.setText(tr("psd.mode_hint.mesh_pose"))

    def _sync_default_metadata(self):
        if Path(self.selected_source).suffix.lower() != ".psd":
            self.selected_metadata = ""
            self.metadata_edit.clear()
            return
        metadata_path = Path(self.selected_source).with_suffix(".lpkpsd.json")
        if metadata_path.is_file():
            self.selected_metadata = str(metadata_path.resolve())
            self.metadata_edit.setText(self.selected_metadata)
        else:
            self.selected_metadata = ""
            self.metadata_edit.clear()

    def _sync_repack_output_dir(self):
        if Path(self.selected_source).suffix.lower() != ".psd":
            return
        output_path = str(Path(self.selected_source).resolve().parent)
        self.last_output_dir = output_path
        self.output_edit.setText(output_path)

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
        if "source resolved" in lower or "resolv" in lower:
            return tr("psd.stage.resolve_model")
        return message

    def open_output_folder(self):
        if os.path.isdir(self.last_output_dir):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.last_output_dir))

    def create_project_from_current_source(self):
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
        project_name = self.project_name_edit.text().strip()
        self.set_busy(True)
        self.project_status_label.setText(tr("psd.project.creating"))
        self.project_worker = PsdProjectImportThread(source, project_name)
        self.project_worker.progressUpdated.connect(self.append_log)
        self.project_worker.projectFinished.connect(self.on_project_created)
        self.project_worker.projectError.connect(self.on_project_error)
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
        query = ""
        if hasattr(self, "project_combo"):
            query = self.project_combo.text().strip().lower()

        self._project_combo_refreshing = True
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        matched = []
        for path in self.discover_project_files():
            label = Path(path).parent.name
            haystack = f"{label} {path}".lower()
            if query and query not in haystack:
                continue
            matched.append(path)
            self.project_combo.addItem(label, userData=path)
        if not matched:
            self.project_combo.addItem(tr("psd.project.no_projects"), userData="")
        index = self._combo_index_by_data(self.project_combo, selected)
        self.project_combo.setCurrentIndex(index if index >= 0 else 0)
        self.project_combo.blockSignals(False)
        self._project_combo_refreshing = False

    def on_project_combo_text_changed(self, *_args):
        if self._project_combo_refreshing:
            return
        self.refresh_project_combo()

    def on_project_combo_changed(self, *_args):
        if self._project_combo_refreshing:
            return
        path = str(self.project_combo.currentData() or "")
        if not path:
            return
        if self.current_project and str(self.current_project.project_file) == path:
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

    def save_current_project(self):
        if not self.current_project:
            return
        data = dict(self.current_project.data)
        data["project_name"] = sanitize_project_name(
            self.project_name_edit.text() or self.current_project.project_name
        )
        project_file = save_project(self.current_project.project_dir, data)
        self.current_project = Live2DPSDProject(self.current_project.project_dir, project_file, data)
        self.remember_project_file(self.current_project)
        self.refresh_project_ui()
        self.refresh_project_combo(select_project_file=str(project_file))
        self.append_log(tr("psd.project.saved", path=str(project_file)))

    def open_current_project_folder(self):
        if not self.current_project:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.current_project.project_dir.resolve())))

    def on_project_created(self, project: Live2DPSDProject):
        self.set_busy(False)
        self.set_current_project(project)
        self.append_log(tr("psd.project.created", path=str(project.project_file)))
        InfoBar.success(
            title=tr("common.success"),
            content=tr("psd.project.created", path=str(project.project_file)),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=3500,
        )

    def on_project_error(self, error: str):
        self.set_busy(False)
        self.project_status_label.setText(tr("psd.project.no_project"))
        self.append_log(tr("psd.error_log", error=error))
        InfoBar.error(
            title=tr("common.error"),
            content=error,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=6000,
        )

    def set_current_project(self, project: Live2DPSDProject):
        self.close_project_preview()
        self.current_project = project
        self.remember_project_file(project)
        self.project_name_edit.setText(project.project_name)
        self.selected_source = str(project.base_model_json)
        self.source_edit.setText(self.selected_source)
        self.workflow = "export"
        self.output_manually_selected = False
        self.last_output_dir = str((project.project_dir / "psd").resolve())
        self.output_edit.setText(self.last_output_dir)
        self._update_workflow_ui()
        self.refresh_project_ui()
        self.refresh_project_combo(select_project_file=str(project.project_file))
        self.refresh_preview_source_combo()
        self.refresh_motion_controls(project.base_model_json)

    def refresh_project_ui(self, selected_id: str | None = None):
        if not self.current_project:
            if hasattr(self, "open_project_folder_button"):
                self.open_project_folder_button.setEnabled(False)
            self.project_status_label.setText(tr("psd.project.no_project"))
            self.refresh_preview_source_combo()
            self.refresh_motion_controls(None)
            return
        if hasattr(self, "open_project_folder_button"):
            self.open_project_folder_button.setEnabled(True)
        self.project_status_label.setText(
            tr("psd.project.current", path=str(self.current_project.project_file))
        )
        self.refresh_preview_source_combo(selected_id)

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
            if self.preview_mode == "current" and not self.preview_frame.isHidden():
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
                        tr("psd.preview.repack_source", version=version_id),
                        userData=f"repack:{version_id}",
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
        if token.startswith("repack:") and self.current_project:
            try:
                self.current_project = select_repack(self.current_project, token.split(":", 1)[1])
            except Exception as exc:
                self.append_log(tr("psd.warning_prefix", message=str(exc)))
        self.refresh_preview_texture_combo()
        self.update_preview_controls()

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
        if not hasattr(self, "preview_live2d_button"):
            return
        has_project = self.current_project is not None
        self.preview_image_button.setEnabled(has_project)
        self.preview_live2d_button.setEnabled(has_project)
        self.motion_play_button.setEnabled(has_project and bool(self._motion_items))
        self.preview_hint_label.setText(tr("psd.preview.hint_live2d_enabled"))

    @staticmethod
    def _combo_index_by_data(combo: ComboBox, value: str) -> int:
        for index in range(combo.count()):
            if str(combo.itemData(index) or "") == value:
                return index
        return -1

    def latest_project_export(self) -> dict | None:
        if not self.current_project:
            return None
        exports = self.current_project.data.get("psd_exports") or []
        return exports[-1] if exports else None

    def update_project_after_result(self, result: ReconstructionResult):
        if not self.current_project:
            return
        try:
            if result.mode in {"mesh", "atlas-components"}:
                self.current_project = record_psd_export(
                    self.current_project,
                    result.psd_path,
                    result.metadata_path,
                    result.mode,
                    result.layer_count,
                )
            elif result.mode in {"mesh-repack", "atlas-repack"} and self.pending_repack_id:
                self.current_project = record_repack(
                    self.current_project,
                    self.pending_repack_id,
                    self.pending_repack_source,
                    self.pending_repack_metadata or result.metadata_path,
                    self.pending_repack_dir,
                    result.output_paths,
                )
            self.refresh_project_ui(self.pending_repack_id or None)
            self.refresh_project_combo(select_project_file=str(self.current_project.project_file))
        except Exception as exc:
            self.append_log(tr("psd.warning_prefix", message=str(exc)))

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
        self.close_project_preview(update_placeholder=False)
        self.set_preview_visible(True, stop_process=False)
        self.preview_placeholder_label.setVisible(False)
        self.preview_image_panel.setVisible(True)
        self.preview_image_panel.load_images(image_paths)
        self.preview_title_label.setText(tr("psd.preview.images_title", count=len(image_paths)))

    def load_selected_preview_live2d(self):
        token = self.preview_source_token()
        if token.startswith("repack:"):
            self.start_project_preview("current", token.split(":", 1)[1])
        else:
            self.start_project_preview("original")

    def start_project_preview(self, mode: str, version_id: str = ""):
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
            self._launch_preview_process(str(self.current_project.base_model_json), "original", "")
            return

        version_id = version_id or self.preview_source_repack_id() or self.selected_repack_id()
        repack_entry = find_repack_entry(self.current_project, version_id or None)
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

        self.set_preview_visible(True, stop_process=False)
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
        if self.preview_frame.isHidden():
            return
        self._launch_preview_process(model_json_path, "current", version_id)

    def on_preview_workspace_error(self, error: str):
        self.preview_placeholder_label.setText(tr("psd.preview.failed", error=error))
        self.append_log(tr("psd.error_log", error=error))
        InfoBar.error(
            title=tr("common.error"),
            content=error,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000,
        )

    def on_preview_workspace_finished(self):
        self.preview_prepare_worker = None
        self.update_preview_controls()

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
        if self.preview_frame.isHidden():
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

    def toggle_preview_panel(self):
        self.set_preview_visible(self.preview_frame.isHidden())

    def set_preview_visible(self, visible: bool, stop_process: bool = True):
        if not visible and stop_process:
            self.close_project_preview()
        self.preview_frame.setVisible(visible)
        self.log_frame.setVisible(not visible)
        if visible and self.preview_image_panel.isHidden() and self.live2d_preview_host.isHidden():
            self.preview_placeholder_label.setVisible(True)
        self.preview_toggle_button.setText(tr("psd.preview.hide") if visible else tr("psd.preview.show"))

    def append_log(self, text: str):
        self.log_text.append(text)
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    @staticmethod
    def _is_supported_path(path: str) -> bool:
        if not path:
            return False
        p = Path(path)
        return p.is_dir() or p.suffix.lower() in {".json", ".moc3", ".psd"}

    def updateUIScale(self, window_width, window_height):
        button_height = 32
        for button in self.findChildren(PushButton):
            button.setMinimumHeight(button_height)
            button.setMaximumHeight(36)
        for line_edit in self.findChildren(LineEdit):
            line_edit.setMinimumHeight(32)
            line_edit.setMaximumHeight(36)
        self.log_text.setMinimumHeight(220)
        font = QApplication.instance().font()
        for label in self.findChildren(SubtitleLabel):
            label_font = label.font()
            label_font.setPointSize(font.pointSize() + 2)
            label.setFont(label_font)
