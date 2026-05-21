import os
from pathlib import Path

from PySide6.QtCore import QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QFileDialog, QFrame, QHBoxLayout, QSizePolicy, QVBoxLayout
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    ProgressBar,
    PushButton,
    SubtitleLabel,
    TextEdit,
)

from app.core.psd_reconstructor import ReconstructionResult, reconstruct_live2d_psd
from app.i18n import get_i18n, tr


class PsdReconstructionThread(QThread):
    progressUpdated = Signal(int, str)
    reconstructionFinished = Signal(object)
    reconstructionError = Signal(str)

    def __init__(self, source_path: str, output_dir: str):
        super().__init__()
        self.source_path = source_path
        self.output_dir = output_dir

    def run(self):
        try:
            result = reconstruct_live2d_psd(
                self.source_path,
                self.output_dir,
                progress=lambda value, message: self.progressUpdated.emit(value, message),
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
        self.selected_source = ""
        self.last_output_dir = os.path.abspath(os.path.join(os.getcwd(), "output", "psd"))
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

        self.source_layout = QHBoxLayout()
        self.source_label = SubtitleLabel("", self)
        self.source_edit = LineEdit(self)
        self.source_edit.setReadOnly(True)
        self.source_file_button = PushButton("", self)
        self.source_file_button.setIcon(FluentIcon.DOCUMENT)
        self.source_file_button.clicked.connect(self.browse_source_file)
        self.source_folder_button = PushButton("", self)
        self.source_folder_button.setIcon(FluentIcon.FOLDER)
        self.source_folder_button.clicked.connect(self.browse_source_folder)
        self.source_layout.addWidget(self.source_label)
        self.source_layout.addWidget(self.source_edit, 1)
        self.source_layout.addWidget(self.source_file_button)
        self.source_layout.addWidget(self.source_folder_button)
        self.main_layout.addLayout(self.source_layout)

        self.output_layout = QHBoxLayout()
        self.output_label = SubtitleLabel("", self)
        self.output_edit = LineEdit(self)
        self.output_edit.setText(self.last_output_dir)
        self.output_button = PushButton("", self)
        self.output_button.setIcon(FluentIcon.FOLDER)
        self.output_button.clicked.connect(self.browse_output)
        self.output_layout.addWidget(self.output_label)
        self.output_layout.addWidget(self.output_edit, 1)
        self.output_layout.addWidget(self.output_button)
        self.main_layout.addLayout(self.output_layout)

        self.action_layout = QHBoxLayout()
        self.reconstruct_button = PushButton("", self)
        self.reconstruct_button.setIcon(FluentIcon.SAVE)
        self.reconstruct_button.clicked.connect(self.start_reconstruction)
        self.open_output_button = PushButton("", self)
        self.open_output_button.setIcon(FluentIcon.FOLDER)
        self.open_output_button.setEnabled(False)
        self.open_output_button.clicked.connect(self.open_output_folder)
        self.action_layout.addWidget(self.reconstruct_button)
        self.action_layout.addWidget(self.open_output_button)
        self.action_layout.addStretch(1)
        self.main_layout.addLayout(self.action_layout)

        self.progress_bar = ProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.main_layout.addWidget(self.progress_bar)

        self.log_label = SubtitleLabel("", self)
        self.main_layout.addWidget(self.log_label)

        self.log_text = TextEdit(self)
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(220)
        self.log_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.main_layout.addWidget(self.log_text, 1)

        for button in self.findChildren(PushButton):
            button.setMinimumSize(112, 32)
        for line_edit in self.findChildren(LineEdit):
            line_edit.setMinimumHeight(32)

    def retranslate_ui(self):
        self.title_label.setText(tr("psd.title"))
        self.desc_label.setText(tr("psd.description"))
        self.drop_main_label.setText(tr("psd.drop_main"))
        self.drop_sub_label.setText(tr("psd.drop_sub"))
        self.source_label.setText(tr("psd.source"))
        self.source_edit.setPlaceholderText(tr("psd.placeholder_source"))
        self.source_file_button.setText(tr("psd.browse_file"))
        self.source_folder_button.setText(tr("psd.browse_folder"))
        self.output_label.setText(tr("psd.output_directory"))
        self.output_edit.setPlaceholderText(tr("psd.placeholder_output"))
        self.output_button.setText(tr("common.browse"))
        self.reconstruct_button.setText(tr("psd.reconstruct_button"))
        self.open_output_button.setText(tr("psd.open_output_folder"))
        self.log_label.setText(tr("psd.log"))

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
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("dialog.select_live2d_psd_source"),
            "",
            tr("dialog.filter_live2d_psd_sources"),
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

    def browse_output(self):
        path = QFileDialog.getExistingDirectory(
            self,
            tr("dialog.select_output_directory"),
            self.output_edit.text(),
        )
        if path:
            self.output_edit.setText(os.path.abspath(path))

    def set_source(self, path: str):
        self.selected_source = os.path.abspath(path)
        self.source_edit.setText(self.selected_source)
        self.append_log(tr("psd.selected_source", path=self.selected_source))

    def start_reconstruction(self):
        source = self.source_edit.text().strip()
        output_dir = self.output_edit.text().strip() or self.last_output_dir

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
        self.set_busy(True)
        self.progress_bar.setValue(0)
        self.log_text.clear()
        self.append_log(tr("psd.started"))

        self.worker = PsdReconstructionThread(source, self.last_output_dir)
        self.worker.progressUpdated.connect(self.on_progress_updated)
        self.worker.reconstructionFinished.connect(self.on_reconstruction_finished)
        self.worker.reconstructionError.connect(self.on_reconstruction_error)
        self.worker.start()

    def on_progress_updated(self, value: int, message: str):
        self.progress_bar.setValue(value)
        if message:
            self.append_log(message)

    def on_reconstruction_finished(self, result: ReconstructionResult):
        self.set_busy(False)
        self.progress_bar.setValue(100)
        self.open_output_button.setEnabled(True)

        for warning in result.warnings:
            self.append_log(tr("psd.warning_prefix", message=warning))

        self.append_log(
            tr(
                "psd.finished_log",
                path=str(result.psd_path),
                count=result.layer_count,
                mode=result.mode,
            )
        )
        InfoBar.success(
            title=tr("common.success"),
            content=tr("psd.success_content", path=str(result.psd_path)),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000,
        )

    def on_reconstruction_error(self, error: str):
        self.set_busy(False)
        self.progress_bar.setValue(0)
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
        self.output_button.setEnabled(not busy)

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
        return p.is_dir() or p.suffix.lower() in {".json", ".moc3"}

    def updateUIScale(self, window_width, window_height):
        scale_factor = max(1.0, window_width / 1000.0)
        button_height = int(32 * scale_factor)
        for button in self.findChildren(PushButton):
            button.setMinimumHeight(button_height)
        self.log_text.setMinimumHeight(int(220 * scale_factor))
        font = QApplication.instance().font()
        for label in self.findChildren(SubtitleLabel):
            label_font = label.font()
            label_font.setPointSize(font.pointSize() + 2)
            label.setFont(label_font)
