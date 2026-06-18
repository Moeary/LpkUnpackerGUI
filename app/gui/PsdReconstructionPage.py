import os
import re
from pathlib import Path

from PySide6.QtCore import QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QFileDialog, QFrame, QHBoxLayout, QSizePolicy, QVBoxLayout
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
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
from app.core.settings_manager import SettingsManager
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

        self.setupUI()
        self.retranslate_ui()
        self.i18n.languageChanged.connect(self.retranslate_ui)

    def setupUI(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(20, 20, 20, 20)
        self.main_layout.setSpacing(12)

        self.title_label = SubtitleLabel("", self)
        self.main_layout.addWidget(self.title_label)

        self.desc_label = CaptionLabel("", self)
        self.desc_label.setWordWrap(True)
        self.main_layout.addWidget(self.desc_label)

        self.drop_frame = QFrame(self)
        self.drop_frame.setObjectName("psdDropFrame")
        self.drop_frame.setMinimumHeight(96)
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
        drop_layout.setContentsMargins(16, 14, 16, 14)
        drop_layout.setSpacing(4)
        self.drop_main_label = BodyLabel("", self.drop_frame)
        self.drop_sub_label = CaptionLabel("", self.drop_frame)
        self.drop_sub_label.setWordWrap(True)
        drop_layout.addWidget(self.drop_main_label)
        drop_layout.addWidget(self.drop_sub_label)
        self.main_layout.addWidget(self.drop_frame)

        self.workflow_layout = QHBoxLayout()
        self.workflow_layout.setSpacing(10)
        self.workflow_label = SubtitleLabel("", self)
        self.workflow_segment = QFrame(self)
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
        self.main_layout.addLayout(self.workflow_layout)

        self.source_layout = QHBoxLayout()
        self.source_label = SubtitleLabel("", self)
        self.source_edit = LineEdit(self)
        self.source_edit.setReadOnly(True)
        self.source_file_button = PushButton("", self)
        self.source_file_button.clicked.connect(self.browse_source_file)
        self.source_folder_button = PushButton("", self)
        self.source_folder_button.clicked.connect(self.browse_source_folder)
        self.source_layout.addWidget(self.source_label)
        self.source_layout.addWidget(self.source_edit, 1)
        self.source_layout.addWidget(self.source_file_button)
        self.source_layout.addWidget(self.source_folder_button)
        self.main_layout.addLayout(self.source_layout)

        self.mode_frame = QFrame(self)
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
        self.main_layout.addWidget(self.mode_frame)

        self.metadata_frame = QFrame(self)
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
        self.main_layout.addWidget(self.metadata_frame)

        self.output_layout = QHBoxLayout()
        self.output_label = SubtitleLabel("", self)
        self.output_edit = LineEdit(self)
        self.output_edit.setText(self.last_output_dir)
        self.output_button = PushButton("", self)
        self.output_button.clicked.connect(self.browse_output)
        self.output_layout.addWidget(self.output_label)
        self.output_layout.addWidget(self.output_edit, 1)
        self.output_layout.addWidget(self.output_button)
        self.main_layout.addLayout(self.output_layout)

        self.action_layout = QHBoxLayout()
        self.reconstruct_button = PrimaryPushButton("", self)
        self.reconstruct_button.clicked.connect(self.start_reconstruction)
        self.open_output_button = PushButton("", self)
        self.open_output_button.setEnabled(False)
        self.open_output_button.clicked.connect(self.open_output_folder)
        self.action_layout.addWidget(self.reconstruct_button)
        self.action_layout.addWidget(self.open_output_button)
        self.action_layout.addStretch(1)
        self.main_layout.addLayout(self.action_layout)

        self.progress_layout = QHBoxLayout()
        self.progress_bar = ProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.stage_label = CaptionLabel("", self)
        self.stage_label.setMinimumWidth(220)
        self.stage_label.setWordWrap(True)
        self.progress_layout.addWidget(self.progress_bar, 1)
        self.progress_layout.addWidget(self.stage_label)
        self.main_layout.addLayout(self.progress_layout)

        self.log_label = SubtitleLabel("", self)
        self.main_layout.addWidget(self.log_label)

        self.log_text = TextEdit(self)
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(220)
        self.log_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.main_layout.addWidget(self.log_text, 1)

        for button in self.findChildren(PushButton):
            button.setMinimumSize(104, 32)
            button.setMaximumHeight(36)
        for line_edit in self.findChildren(LineEdit):
            line_edit.setMinimumHeight(32)
            line_edit.setMaximumHeight(36)
        self.export_flow_button.setMinimumWidth(150)
        self.repack_flow_button.setMinimumWidth(176)
        self._apply_static_styles()

    def retranslate_ui(self):
        self.title_label.setText(tr("psd.title"))
        self.desc_label.setText(tr("psd.description"))
        self.drop_main_label.setText(tr("psd.drop_main"))
        self.drop_sub_label.setText(tr("psd.drop_sub"))
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

        if not source:
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("psd.warning_no_source"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return

        self.last_output_dir = os.path.abspath(output_dir)
        self.output_edit.setText(self.last_output_dir)
        metadata_path = None
        if self.workflow == "repack":
            if Path(source).suffix.lower() != ".psd":
                InfoBar.warning(
                    title=tr("common.warning"),
                    content=tr("psd.warning_no_psd_source"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=3000,
                )
                return
            output_dir = str(Path(source).resolve().parent)
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
        self.output_button.setEnabled(not busy and self.workflow != "repack")
        self.output_edit.setReadOnly(self.workflow == "repack")
        self.mode_combo.setEnabled(not busy)
        self.export_flow_button.setEnabled(not busy)
        self.repack_flow_button.setEnabled(not busy)
        self.metadata_button.setEnabled(not busy)
        self.metadata_default_button.setEnabled(not busy)

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
