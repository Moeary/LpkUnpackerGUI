import os

from PySide6.QtCore import QThread, QUrl
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QFileDialog, QFrame, QHBoxLayout, QSizePolicy, QVBoxLayout
from qfluentwidgets import (
    ComboBox,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    ProgressBar,
    PushButton,
    SubtitleLabel,
    TextEdit,
)
from PySide6.QtCore import Signal

from app.core.assetstudio_cli import AssetStudioCLI, AssetStudioCLIError
from app.i18n import get_i18n, tr


class AssetStudioExportThread(QThread):
    progressUpdated = Signal(int)
    logMessage = Signal(str, str)
    extractionFinished = Signal(str)
    extractionError = Signal(str)

    def __init__(self, inputs: list[str], output_dir: str, mode: str):
        super().__init__()
        self.inputs = inputs
        self.output_dir = output_dir
        self.mode = mode

    def run(self):
        try:
            cli = AssetStudioCLI()
            self.logMessage.emit("INFO", f"AssetStudio CLI: {cli.executable}")

            total_count = 0
            for index, input_path in enumerate(self.inputs):
                self.logMessage.emit("INFO", f"Processing: {input_path}")
                if self.mode == "live2d":
                    result = cli.export_live2d(input_path, self.output_dir)
                else:
                    result = cli.export_textures(input_path, self.output_dir)

                total_count += result.exported_count
                self._emit_cli_output(result.stdout)
                self._emit_cli_output(result.stderr, level="WARNING")
                self.logMessage.emit(
                    "INFO",
                    f"Exported {result.exported_count} file(s) from {os.path.basename(input_path)}",
                )
                self.progressUpdated.emit(int((index + 1) / max(1, len(self.inputs)) * 100))

            self.logMessage.emit("INFO", f"Total exported files: {total_count}")
            self.extractionFinished.emit(self.output_dir)
        except AssetStudioCLIError as exc:
            self.extractionError.emit(str(exc))
        except Exception as exc:
            self.extractionError.emit(str(exc))

    def _emit_cli_output(self, text: str, level: str = "INFO"):
        for line in text.splitlines():
            line = line.strip()
            if line:
                self.logMessage.emit(level, line)


class UnityExtractorPage(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("unityExtractorPage")
        self.setAcceptDrops(True)

        self.i18n = get_i18n()
        self.default_output_dir = os.path.join(os.getcwd(), "output", "unity_assets")
        self.selected_paths: list[str] = []
        self.last_output_dir = self.default_output_dir
        self.thread: AssetStudioExportThread | None = None

        self.setupUI()
        self.retranslate_ui()
        self.i18n.languageChanged.connect(self.retranslate_ui)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def setupUI(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(20, 20, 20, 20)
        self.main_layout.setSpacing(10)

        self.title_label = SubtitleLabel("", self)
        self.main_layout.addWidget(self.title_label)

        self.file_layout = QHBoxLayout()
        self.file_label = SubtitleLabel("", self)
        self.file_edit = LineEdit(self)
        self.file_edit.setReadOnly(True)
        self.file_button = PushButton("", self)
        self.file_button.setIcon(FluentIcon.FOLDER)
        self.file_button.clicked.connect(self.browse_files)
        self.folder_button = PushButton("", self)
        self.folder_button.setIcon(FluentIcon.FOLDER_ADD)
        self.folder_button.clicked.connect(self.browse_folder)
        self.file_layout.addWidget(self.file_label)
        self.file_layout.addWidget(self.file_edit, 1)
        self.file_layout.addWidget(self.file_button)
        self.file_layout.addWidget(self.folder_button)
        self.main_layout.addLayout(self.file_layout)

        self.mode_layout = QHBoxLayout()
        self.mode_label = SubtitleLabel("", self)
        self.mode_combo = ComboBox(self)
        self.mode_combo.addItem("", userData="textures")
        self.mode_combo.addItem("", userData="live2d")
        self.mode_layout.addWidget(self.mode_label)
        self.mode_layout.addWidget(self.mode_combo, 1)
        self.main_layout.addLayout(self.mode_layout)

        self.output_layout = QHBoxLayout()
        self.output_label = SubtitleLabel("", self)
        self.output_edit = LineEdit(self)
        self.output_edit.setText(self.default_output_dir)
        self.output_button = PushButton("", self)
        self.output_button.setIcon(FluentIcon.FOLDER)
        self.output_button.clicked.connect(self.browse_output)
        self.output_layout.addWidget(self.output_label)
        self.output_layout.addWidget(self.output_edit, 1)
        self.output_layout.addWidget(self.output_button)
        self.main_layout.addLayout(self.output_layout)

        self.progress_bar = ProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.main_layout.addWidget(self.progress_bar)

        self.extract_button = PushButton("", self)
        self.extract_button.setIcon(FluentIcon.IMAGE_EXPORT)
        self.extract_button.clicked.connect(self.start_extraction)
        self.main_layout.addWidget(self.extract_button)

        self.open_folder_button = PushButton("", self)
        self.open_folder_button.setIcon(FluentIcon.FOLDER)
        self.open_folder_button.clicked.connect(self.open_output_folder)
        self.open_folder_button.setEnabled(False)
        self.main_layout.addWidget(self.open_folder_button)

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
        self.title_label.setText(tr("unity.title"))
        self.file_label.setText(tr("unity.files_or_folders"))
        self.file_edit.setPlaceholderText(tr("unity.placeholder_files"))
        self.file_button.setText(tr("unity.browse_files"))
        self.folder_button.setText(tr("unity.browse_folder"))
        self.mode_label.setText(tr("unity.mode"))
        self.mode_combo.setItemText(0, tr("unity.mode.textures"))
        self.mode_combo.setItemText(1, tr("unity.mode.live2d"))
        self.output_label.setText(tr("unity.output_directory"))
        self.output_edit.setPlaceholderText(tr("unity.placeholder_output"))
        self.output_button.setText(tr("common.browse"))
        self.extract_button.setText(tr("unity.extract_button"))
        self.open_folder_button.setText(tr("unity.open_output_folder"))
        self.log_label.setText(tr("unity.log"))

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        paths = []
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if os.path.exists(path):
                paths.append(os.path.abspath(path))

        if paths:
            self.selected_paths = paths
            self.update_file_display()
        event.acceptProposedAction()

    def update_file_display(self):
        if not self.selected_paths:
            self.file_edit.clear()
        elif len(self.selected_paths) == 1:
            self.file_edit.setText(self.selected_paths[0])
        else:
            self.file_edit.setText(tr("unity.selected_count", count=len(self.selected_paths)))

    def browse_files(self):
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            tr("dialog.select_unity_files"),
            "",
            tr("dialog.filter_unity_files"),
        )
        if file_paths:
            self.selected_paths = [os.path.abspath(path) for path in file_paths]
            self.update_file_display()

    def browse_folder(self):
        dir_path = QFileDialog.getExistingDirectory(
            self,
            tr("dialog.select_unity_folder"),
        )
        if dir_path:
            self.selected_paths = [os.path.abspath(dir_path)]
            self.update_file_display()

    def browse_output(self):
        dir_path = QFileDialog.getExistingDirectory(
            self,
            tr("dialog.select_output_directory"),
            self.output_edit.text(),
        )
        if dir_path:
            self.output_edit.setText(os.path.abspath(dir_path))

    def open_output_folder(self):
        output_dir = self.output_edit.text()
        if os.path.exists(output_dir):
            QDesktopServices.openUrl(QUrl.fromLocalFile(output_dir))

    def start_extraction(self):
        if not self.selected_paths:
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("unity.warning_no_selection"),
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        output_dir = os.path.abspath(self.output_edit.text() or self.default_output_dir)
        os.makedirs(output_dir, exist_ok=True)
        self.last_output_dir = output_dir
        self.output_edit.setText(output_dir)

        mode = self.mode_combo.currentData() or "textures"
        self.set_busy(True)
        self.log_text.clear()
        self.progress_bar.setValue(0)
        self.append_log(tr("unity.started"))

        self.thread = AssetStudioExportThread(self.selected_paths, output_dir, mode)
        self.thread.progressUpdated.connect(self.progress_bar.setValue)
        self.thread.logMessage.connect(self.on_log_message)
        self.thread.extractionFinished.connect(self.on_finished)
        self.thread.extractionError.connect(self.on_error)
        self.thread.start()

    def on_log_message(self, level: str, message: str):
        prefix = f"[{level}] " if level else ""
        self.append_log(prefix + message)

    def on_finished(self, output_dir: str):
        self.set_busy(False)
        self.progress_bar.setValue(100)
        self.open_folder_button.setEnabled(True)
        InfoBar.success(
            title=tr("common.success"),
            content=tr("unity.success_content", output=output_dir),
            position=InfoBarPosition.TOP,
            duration=4000,
            parent=self,
        )

    def on_error(self, error_msg: str):
        self.set_busy(False)
        self.append_log(tr("unity.error_log", error=error_msg))
        InfoBar.error(
            title=tr("common.error"),
            content=error_msg,
            position=InfoBarPosition.TOP,
            duration=6000,
            parent=self,
        )

    def set_busy(self, busy: bool):
        self.extract_button.setEnabled(not busy)
        self.file_button.setEnabled(not busy)
        self.folder_button.setEnabled(not busy)
        self.output_button.setEnabled(not busy)

    def append_log(self, message: str):
        self.log_text.append(message)
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def updateUIScale(self, window_width, window_height):
        scale_factor = max(1.0, window_width / 1000.0)
        button_height = int(32 * scale_factor)
        for button in self.findChildren(PushButton):
            button.setMinimumHeight(button_height)
        for line_edit in self.findChildren(LineEdit):
            line_edit.setMinimumHeight(button_height)

        font = QApplication.instance().font()
        for label in self.findChildren(SubtitleLabel):
            label_font = label.font()
            label_font.setPointSize(font.pointSize() + 2)
            label.setFont(label_font)
