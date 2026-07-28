import json
import os
import re
from pathlib import Path

from PySide6.QtCore import Qt, QProcess, QThread, QTimer, QUrl, Signal, QStringListModel
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QCompleter,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    CaptionLabel,
    ComboBox,
    EditableComboBox,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    SpinBox,
    SingleDirectionScrollArea,
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
    create_pose_scheme,
    create_parameter_preset,
    create_repack_dir,
    compose_pose_versions,
    find_composite_entry,
    find_pose_scheme,
    find_parameter_preset,
    find_repack_entry,
    load_project,
    prepare_repack_preview_workspace,
    record_psd_export,
    record_pose_scheme_export,
    record_repack,
    resolve_project_path,
    save_project,
    select_pose_scheme,
    select_parameter_preset,
    select_repack,
    set_preview_state,
    sanitize_project_name,
    set_pose_scheme_priority,
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

    def __init__(
        self,
        source_path: str,
        output_dir: str,
        mode: str,
        metadata_path: str | None = None,
        parameter_values: dict[str, float] | None = None,
        pose_name: str | None = None,
        output_name: str | None = None,
    ):
        super().__init__()
        self.source_path = source_path
        self.output_dir = output_dir
        self.mode = mode
        self.metadata_path = metadata_path
        self.parameter_values = parameter_values
        self.pose_name = pose_name
        self.output_name = output_name

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
                    parameter_values=self.parameter_values,
                    pose_name=self.pose_name,
                    output_name=self.output_name,
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
    unifiedPreviewRequested = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("psdReconstructionPage")
        self.setAcceptDrops(True)

        self.i18n = get_i18n()
        self.settings_manager = SettingsManager()
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
        self._project_combo_refreshing = False
        self._pose_scheme_refreshing = False
        self._parameter_preset_refreshing = False
        self._ui_refreshing = False
        self._project_dirty = False
        self._is_busy = False
        self._motion_items: list[dict] = []
        self.live2d_preview_window: Live2DPreviewWindow | None = None
        self.preview_mode = ""
        self.preview_repack_id = ""
        self._last_preview_dock_rect: dict | None = None
        self.pending_repack_id = ""
        self.pending_repack_dir = ""
        self.pending_repack_source = ""
        self.pending_repack_metadata = ""
        self.pending_repack_name = ""
        self.pending_pose_scheme_id = ""
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

        self.header_layout = QHBoxLayout()
        self.title_label = SubtitleLabel("", self)
        self.current_project_title_label = BodyLabel("", self)
        self.current_project_title_label.setStyleSheet(
            "color: #5B6472; font-weight: 600;"
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

        self.left_scroll = SingleDirectionScrollArea(
            orient=Qt.Vertical,
            parent=self.content_splitter,
        )
        self.left_scroll.setWidgetResizable(True)
        self.left_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.left_scroll.enableTransparentBackground()
        self.left_scroll.setMinimumWidth(390)
        self.left_panel = QWidget()
        self.left_panel_layout = QVBoxLayout(self.left_panel)
        self.left_panel_layout.setContentsMargins(0, 0, 10, 0)
        self.left_panel_layout.setSpacing(10)
        self.left_scroll.setWidget(self.left_panel)
        self.content_splitter.addWidget(self.left_scroll)

        self.right_panel = QWidget(self.content_splitter)
        self.right_panel_layout = QVBoxLayout(self.right_panel)
        self.right_panel_layout.setContentsMargins(8, 0, 0, 0)
        self.right_panel_layout.setSpacing(10)
        self.content_splitter.addWidget(self.right_panel)
        self.content_splitter.setSizes([420, 980])

        self.project_frame = CardWidget(self.left_panel)
        self.project_frame.setObjectName("psdProjectFrame")
        self.project_layout = QVBoxLayout(self.project_frame)
        self.project_layout.setContentsMargins(12, 12, 12, 12)
        self.project_layout.setSpacing(8)
        self.project_title_label = SubtitleLabel("", self.project_frame)
        self.project_layout.addWidget(self.project_title_label)

        self.project_name_edit = LineEdit(self.project_frame)
        self.project_name_edit.setPlaceholderText("")
        self.project_name_edit.setVisible(False)

        self.project_combo = EditableComboBox(self.project_frame)
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

        self.export_card = CardWidget(self.left_panel)
        self.export_card.setObjectName("psdExportCard")
        self.export_card_layout = QVBoxLayout(self.export_card)
        self.export_card_layout.setContentsMargins(14, 14, 14, 14)
        self.export_card_layout.setSpacing(10)
        self.export_card_title = SubtitleLabel("", self.export_card)
        self.export_card_layout.addWidget(self.export_card_title)

        self.source_layout = QHBoxLayout()
        self.source_label = BodyLabel("", self.export_card)
        self.source_edit = LineEdit(self.export_card)
        self.source_edit.setReadOnly(True)
        self.source_file_button = PushButton("", self.export_card)
        self.source_file_button.clicked.connect(self.browse_source_file)
        self.source_folder_button = PushButton("", self.export_card)
        self.source_folder_button.clicked.connect(self.browse_source_folder)
        self.source_layout.addWidget(self.source_label)
        self.source_layout.addWidget(self.source_edit, 1)
        self.source_layout.addWidget(self.source_file_button)
        self.source_layout.addWidget(self.source_folder_button)
        self.export_card_layout.addLayout(self.source_layout)

        self.export_name_layout = QHBoxLayout()
        self.export_name_label = BodyLabel("", self.export_card)
        self.export_name_edit = EditableComboBox(self.export_card)
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
        self.export_name_layout.addWidget(self.export_name_edit, 1)
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
        self.export_card_layout.addWidget(self.mode_frame)

        self.output_layout = QHBoxLayout()
        self.output_label = BodyLabel("", self.export_card)
        self.output_edit = LineEdit(self.export_card)
        self.output_edit.setText(self.last_output_dir)
        self.output_edit.textChanged.connect(self.mark_project_dirty)
        self.output_button = PushButton("", self.export_card)
        self.output_button.clicked.connect(self.browse_output)
        self.output_layout.addWidget(self.output_label)
        self.output_layout.addWidget(self.output_edit, 1)
        self.output_layout.addWidget(self.output_button)
        self.export_card_layout.addLayout(self.output_layout)
        self.left_panel_layout.addWidget(self.export_card)

        self.repack_card = CardWidget(self.left_panel)
        self.repack_card.setObjectName("psdRepackCard")
        self.repack_card_layout = QVBoxLayout(self.repack_card)
        self.repack_card_layout.setContentsMargins(14, 14, 14, 14)
        self.repack_card_layout.setSpacing(10)
        self.repack_card_title = SubtitleLabel("", self.repack_card)
        self.repack_card_layout.addWidget(self.repack_card_title)

        self.repack_psd_layout = QHBoxLayout()
        self.pose_scheme_label = BodyLabel("", self.repack_card)
        self.pose_scheme_combo = EditableComboBox(self.repack_card)
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
        self.repack_psd_button.clicked.connect(self.browse_repack_psd_file)
        self.repack_psd_layout.addWidget(self.pose_scheme_label)
        self.repack_psd_layout.addWidget(self.pose_scheme_combo, 1)
        self.repack_psd_layout.addWidget(self.repack_psd_button)
        self.repack_card_layout.addLayout(self.repack_psd_layout)

        self.metadata_frame = QFrame(self.repack_card)
        self.metadata_layout = QHBoxLayout(self.metadata_frame)
        self.metadata_layout.setContentsMargins(0, 0, 0, 0)
        self.metadata_label = BodyLabel("", self.metadata_frame)
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
        self.repack_card_layout.addWidget(self.metadata_frame)

        self.texture_name_layout = QHBoxLayout()
        self.texture_name_label = BodyLabel("", self.repack_card)
        self.texture_name_edit = LineEdit(self.repack_card)
        self.texture_name_edit.textChanged.connect(self.on_texture_name_changed)
        self.texture_name_layout.addWidget(self.texture_name_label)
        self.texture_name_layout.addWidget(self.texture_name_edit, 1)
        self.repack_card_layout.addLayout(self.texture_name_layout)

        self.pose_priority_layout = QHBoxLayout()
        self.pose_priority_label = BodyLabel("", self.repack_card)
        self.pose_priority_spin = SpinBox(self.repack_card)
        self.pose_priority_spin.setRange(-9999, 9999)
        self.pose_priority_spin.editingFinished.connect(
            lambda: self.on_pose_priority_changed(self.pose_priority_spin.value())
        )
        self.pose_priority_layout.addWidget(self.pose_priority_label)
        self.pose_priority_layout.addWidget(self.pose_priority_spin)
        self.pose_priority_layout.addStretch(1)
        self.repack_card_layout.addLayout(self.pose_priority_layout)

        self.repack_output_layout = QHBoxLayout()
        self.repack_output_label = BodyLabel("", self.repack_card)
        self.repack_output_edit = LineEdit(self.repack_card)
        self.repack_output_edit.setReadOnly(True)
        self.repack_output_layout.addWidget(self.repack_output_label)
        self.repack_output_layout.addWidget(self.repack_output_edit, 1)
        self.repack_card_layout.addLayout(self.repack_output_layout)
        self.left_panel_layout.addWidget(self.repack_card)

        self.action_layout = QHBoxLayout()
        self.reconstruct_button = PrimaryPushButton("", self.left_panel)
        self.reconstruct_button.clicked.connect(self.start_reconstruction)
        self.open_output_button = PushButton("", self.left_panel)
        self.open_output_button.setEnabled(False)
        self.open_output_button.clicked.connect(self.open_output_folder)
        self.open_photoshop_button = PushButton("", self.left_panel)
        self.open_photoshop_button.setEnabled(False)
        self.open_photoshop_button.clicked.connect(self.open_current_psd_in_photoshop)
        self.preview_toggle_button = PushButton("", self.left_panel)
        self.preview_toggle_button.clicked.connect(self.toggle_preview_panel)
        self.action_layout.addWidget(self.reconstruct_button)
        self.action_layout.addWidget(self.open_output_button)
        self.action_layout.addWidget(self.open_photoshop_button)
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

        self.preview_control_frame = CardWidget(self.left_panel)
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

        self.motion_frame = CardWidget(self.left_panel)
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
        self.motion_frame.hide()

        self.left_panel_layout.addStretch(1)

        self.preview_frame = CardWidget(self.right_panel)
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

        self.log_frame = CardWidget(self.right_panel)
        self.log_layout = QVBoxLayout(self.log_frame)
        self.log_layout.setContentsMargins(16, 16, 16, 16)
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
        self.pose_scheme_label.setText(tr("psd.pose.scheme"))
        self.export_card_title.setText(tr("psd.export.card_title"))
        self.repack_card_title.setText(tr("psd.repack.card_title"))
        self.export_name_label.setText(tr("psd.export.name"))
        self.export_name_edit.setPlaceholderText(tr("psd.export.name_placeholder"))
        self.export_preset_hint.setText(tr("psd.export.preset_hint"))
        self.repack_psd_button.setText(tr("psd.repack.browse_psd"))
        self.texture_name_label.setText(tr("psd.repack.texture_name"))
        self.texture_name_edit.setPlaceholderText(
            tr("psd.repack.texture_name_placeholder")
        )
        self.pose_priority_label.setText(tr("psd.repack.priority"))
        self.repack_output_label.setText(tr("psd.repack.output"))
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
        self.open_photoshop_button.setText(tr("psd.pose.open_photoshop"))
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
        self._update_project_header()

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
            self.mark_project_dirty()
            self.append_log(tr("psd.selected_metadata", path=self.selected_metadata))

    def clear_metadata_file(self):
        self.selected_metadata = ""
        self.metadata_edit.clear()
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
        value, accepted = QInputDialog.getText(
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

    def start_reconstruction(self):
        self.pending_repack_id = ""
        self.pending_repack_dir = ""
        self.pending_repack_source = ""
        self.pending_repack_metadata = ""
        self.pending_repack_name = ""
        self.pending_pose_scheme_id = ""

        metadata_path = None
        parameter_values = None
        pose_name = None
        output_name = None

        if self.workflow == "repack":
            source = self.selected_repack_psd_path()
            if not source:
                InfoBar.warning(
                    title=tr("common.warning"),
                    content=tr("psd.warning_no_psd_source"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=3000,
                )
                return

            texture_name = self.texture_name_edit.text().strip()
            if not texture_name:
                texture_name = self._prompt_non_empty_name(
                    "psd.repack.name_dialog_title",
                    "psd.repack.name_dialog_prompt",
                    Path(source).stem,
                )
                if not texture_name:
                    return
                self.texture_name_edit.setText(texture_name)

            scheme = self.selected_pose_scheme()
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
            metadata_path = self.metadata_edit.text().strip() or None
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
                try:
                    self.current_project, scheme, scheme_dir = create_pose_scheme(
                        self.current_project,
                        pose_name,
                        0,
                        parameter_values,
                        pose_source=pose_source,
                        parameter_preset_id=preset_id,
                    )
                except Exception as exc:
                    InfoBar.warning(
                        title=tr("common.warning"),
                        content=str(exc),
                        parent=self,
                        position=InfoBarPosition.TOP,
                        duration=4000,
                    )
                    return
                self.pending_pose_scheme_id = str(scheme["id"])
                output_name = str(scheme["id"])
                output_dir = str(scheme_dir)
            else:
                output_dir = (
                    self.output_edit.text().strip()
                    or self.default_output_dir(source)
                )
                output_name = sanitize_project_name(pose_name)
            self.output_edit.setText(os.path.abspath(output_dir))

        self.last_output_dir = os.path.abspath(output_dir)
        self.set_busy(True)
        self.progress_bar.setValue(0)
        self.stage_label.setText(tr("psd.stage.starting"))
        self.log_text.clear()
        self.append_log(tr("psd.started", mode=mode_text))
        if mode == "repack-atlas" and metadata_path:
            self.append_log(tr("psd.metadata_log", path=metadata_path))
        elif mode == "repack-atlas":
            self.append_log(tr("psd.metadata_default_log"))

        self.worker = PsdReconstructionThread(
            source,
            self.last_output_dir,
            mode,
            metadata_path,
            parameter_values=parameter_values,
            pose_name=pose_name,
            output_name=output_name,
        )
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
        if result.mode in {"mesh", "atlas-components"} and Path(result.psd_path).is_file():
            self.last_psd_path = str(Path(result.psd_path).resolve())
        self.open_photoshop_button.setEnabled(
            result.mode in {"mesh", "atlas-components"} and Path(result.psd_path).is_file()
        )

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
        self._is_busy = bool(busy)
        self.reconstruct_button.setEnabled(not busy)
        self.source_file_button.setEnabled(not busy)
        self.source_folder_button.setEnabled(not busy)
        self.export_name_edit.setEnabled(not busy)
        self.repack_psd_button.setEnabled(not busy)
        self.texture_name_edit.setEnabled(not busy)
        self.new_project_button.setEnabled(not busy)
        self.save_project_button.setEnabled(not busy)
        self.open_project_folder_button.setEnabled(not busy and self.current_project is not None)
        self.project_combo.setEnabled(not busy)
        self.pose_scheme_combo.setEnabled(not busy)
        self.pose_priority_spin.setEnabled(
            not busy and self.selected_pose_scheme() is not None
        )
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
        self._update_action_availability()

    def set_workflow(self, workflow: str):
        self.workflow = "repack" if workflow == "repack" else "export"
        if self.workflow == "repack":
            self._sync_repack_output_dir()
        self._update_workflow_ui()
        self.mark_project_dirty()

    def on_mode_changed(self, *_args):
        self.update_mode_hint()
        self.mark_project_dirty()

    def _update_workflow_ui(self):
        is_repack = self.workflow == "repack"
        self.export_card.setVisible(not is_repack)
        self.repack_card.setVisible(is_repack)
        self.reconstruct_button.setText(tr("psd.repack_button") if is_repack else tr("psd.export_button"))
        self._style_workflow_button(self.export_flow_button, not is_repack)
        self._style_workflow_button(self.repack_flow_button, is_repack)
        self.update_mode_hint()
        self._update_action_availability()

    def _update_action_availability(self, *_args):
        if not hasattr(self, "reconstruct_button"):
            return
        if self._is_busy:
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
        source = self.selected_repack_psd_path()
        if not source:
            self.selected_metadata = ""
            self.metadata_edit.clear()
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
        if "source resolved" in lower or "resolv" in lower:
            return tr("psd.stage.resolve_model")
        return message

    def open_output_folder(self):
        if os.path.isdir(self.last_output_dir):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.last_output_dir))

    def open_current_psd_in_photoshop(self):
        psd_path = Path(self.last_psd_path) if self.last_psd_path else None
        if not psd_path or not psd_path.is_file():
            scheme = find_pose_scheme(self.current_project) if self.current_project else None
            value = str((scheme or {}).get("psd") or "")
            psd_path = resolve_project_path(self.current_project, value) if value and self.current_project else None
        photoshop = Path(self.settings_manager.get_photoshop_executable())
        if not photoshop.is_file():
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("psd.pose.photoshop_not_configured"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=4000,
            )
            return
        if not psd_path or not psd_path.is_file():
            return
        QProcess.startDetached(str(photoshop), [str(psd_path)])

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
        self.pose_priority_spin.blockSignals(True)
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
        current = self.selected_pose_scheme()
        self.pose_priority_spin.setValue(int((current or {}).get("priority") or 0))
        self.pose_scheme_combo.blockSignals(False)
        self.pose_priority_spin.blockSignals(False)
        self.pose_priority_spin.setEnabled(bool(current))
        self.open_photoshop_button.setEnabled(
            bool(self.selected_repack_psd_path())
        )
        self._pose_scheme_refreshing = False
        self._sync_default_metadata()
        self._sync_repack_output_dir()

    def on_pose_scheme_changed(self, *_args):
        if self._pose_scheme_refreshing:
            return
        scheme = self.selected_pose_scheme()
        if not scheme:
            self.pose_priority_spin.setEnabled(False)
            path = self.selected_repack_psd_path()
            self.last_psd_path = path
            self.open_photoshop_button.setEnabled(bool(path))
            self._sync_default_metadata()
            self._sync_repack_output_dir()
            return
        try:
            self.current_project = select_pose_scheme(
                self.current_project,
                str(scheme.get("id") or ""),
            )
        except Exception as exc:
            self.append_log(tr("psd.warning_prefix", message=str(exc)))
            return
        self._pose_scheme_refreshing = True
        self.pose_priority_spin.setValue(int(scheme.get("priority") or 0))
        self.pose_priority_spin.setEnabled(True)
        self._pose_scheme_refreshing = False
        self.last_psd_path = str(resolve_project_path(self.current_project, scheme.get("psd", "")))
        self.open_photoshop_button.setEnabled(Path(self.last_psd_path).is_file())
        self._sync_default_metadata()
        self._sync_repack_output_dir()

    def on_pose_priority_changed(self, value: int):
        if self._pose_scheme_refreshing or not self.current_project:
            return
        scheme_id = str(self.pose_scheme_combo.currentData() or "")
        if not scheme_id:
            return
        try:
            self.current_project = set_pose_scheme_priority(
                self.current_project,
                scheme_id,
                int(value),
            )
            if any(
                scheme.get("versions")
                for scheme in self.current_project.data.get("pose_schemes", [])
            ):
                self.current_project, composite = compose_pose_versions(
                    self.current_project,
                    log=self.append_log,
                )
                self.append_log(
                    tr("psd.pose.composite_created", version=str(composite.get("id") or ""))
                )
                self.refresh_preview_source_combo()
        except Exception as exc:
            self.append_log(tr("psd.warning_prefix", message=str(exc)))

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
        default_name = self.source_output_name(source)
        project_name = self._prompt_non_empty_name(
            "psd.project.new_dialog_title",
            "psd.project.new_dialog_prompt",
            default_name,
        )
        if not project_name:
            return
        self.set_busy(True)
        self.project_status_label.setText(tr("psd.project.creating"))
        self.append_log(tr("psd.project.new_save_hint", name=project_name))
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
        self._project_combo_refreshing = True
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        paths = self.discover_project_files()
        completion_labels: list[str] = []
        for path in paths:
            label = Path(path).parent.name
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
        mode_index = self._combo_index_by_data(
            self.mode_combo,
            str(ui_state.get("export_mode") or "mesh"),
        )
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
        scheme = find_pose_scheme(self.current_project)
        if scheme and scheme.get("psd"):
            return scheme
        exports = self.current_project.data.get("psd_exports") or []
        return exports[-1] if exports else None

    def update_project_after_result(self, result: ReconstructionResult):
        if not self.current_project:
            return
        try:
            if result.mode in {"mesh", "atlas-components"} and self.pending_pose_scheme_id:
                self.current_project = record_pose_scheme_export(
                    self.current_project,
                    self.pending_pose_scheme_id,
                    result.psd_path,
                    result.metadata_path,
                    result.mode,
                    result.layer_count,
                )
            elif result.mode in {"mesh", "atlas-components"}:
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
                    scheme_id=self.pending_pose_scheme_id,
                    display_name=self.pending_repack_name,
                )
                if self.pending_pose_scheme_id:
                    self.current_project, composite = compose_pose_versions(
                        self.current_project,
                        log=self.append_log,
                    )
                    self.append_log(
                        tr("psd.pose.composite_created", version=str(composite.get("id") or ""))
                    )
            self.refresh_project_ui(self.pending_repack_id or None)
            self.refresh_project_combo(select_project_file=str(self.current_project.project_file))
            self._project_dirty = False
            self._update_project_header()
        except Exception as exc:
            self.append_log(tr("psd.warning_prefix", message=str(exc)))

    def create_pose_scheme_from_preview(self, payload: dict):
        """Save a named parameter preset captured by the unified preview."""
        try:
            project_file = str(payload.get("project_file") or "")
            if not self.current_project or str(self.current_project.project_file) != str(Path(project_file).resolve()):
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
        if self.current_project:
            self.unifiedPreviewRequested.emit(
                model_json_path,
                str(self.current_project.project_file),
            )

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
