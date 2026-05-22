import logging
import os

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QFileDialog, QFrame, QHBoxLayout, QSizePolicy, QVBoxLayout
from qfluentwidgets import (
    CheckBox,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    MessageBox,
    ProgressBar,
    PushButton,
    SubtitleLabel,
    TextEdit,
)

from app.core.config_manager import ConfigManager
from app.core.extract import scan_package_folder
from app.core.extractor_thread import ExtractorThread
from app.i18n import get_i18n, tr


UNITY_SOURCE_EXTENSIONS = {
    "",
    ".assets",
    ".sharedassets",
    ".bundle",
    ".unity3d",
    ".resource",
    ".ress",
    ".resss",
}


class QTextEditLogger(logging.Handler):
    def __init__(self, textEdit):
        super().__init__()
        self.textEdit = textEdit
        self.textEdit.setReadOnly(True)
        self.formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    def emit(self, record):
        msg = self.formatter.format(record)
        self.textEdit.append(msg)
        scrollbar = self.textEdit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())


class ExtractorPage(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("extractorPage")
        self.setAcceptDrops(True)

        self.config_manager = ConfigManager()
        self.default_output_dir = self.config_manager.get_last_output_dir()
        self.i18n = get_i18n()

        self.selected_files = []
        self.selected_configs = []
        self.last_output_dir = self.default_output_dir

        self.setupUI()
        self.retranslate_ui()
        self.configure_logging()
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

        self.images_only_checkbox = CheckBox("", self)
        self.images_only_checkbox.setChecked(self.config_manager.get_extract_images_only())
        self.images_only_checkbox.stateChanged.connect(self.on_images_only_changed)
        self.main_layout.addWidget(self.images_only_checkbox)

        self.progress_bar = ProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.main_layout.addWidget(self.progress_bar)

        self.summary_label = SubtitleLabel("", self)
        self.summary_label.setVisible(False)
        self.main_layout.addWidget(self.summary_label)

        self.extract_button = PushButton("", self)
        self.extract_button.setIcon(FluentIcon.PLAY)
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
        self.log_text.setMinimumHeight(200)
        self.log_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.main_layout.addWidget(self.log_text)

        for button in self.findChildren(PushButton):
            button.setMinimumSize(100, 30)

        for line_edit in self.findChildren(LineEdit):
            line_edit.setMinimumHeight(30)

    def retranslate_ui(self):
        self.title_label.setText(tr("extractor.title", default="LPK/WPK File Extractor"))
        self.file_label.setText(tr("extractor.files_or_folders", default="Files/Folders:"))
        self.file_edit.setPlaceholderText(
            tr(
                "extractor.placeholder_files",
                default="Select LPK/WPK files or folders, or drag and drop here...",
            )
        )
        self.file_button.setText(tr("extractor.browse_files", default="Browse Files"))
        self.folder_button.setText(tr("extractor.browse_folder", default="Browse Folder"))

        self.output_label.setText(tr("extractor.output_directory"))
        self.output_edit.setPlaceholderText(tr("extractor.placeholder_output"))
        self.output_button.setText(tr("common.browse"))

        self.images_only_checkbox.setText(
            tr("extractor.extract_images_only", default="Extract Images Only")
        )
        self.extract_button.setText(tr("extractor.extract_button"))
        self.open_folder_button.setText(tr("extractor.open_output_folder"))
        self.log_label.setText(tr("extractor.log"))

    def on_images_only_changed(self, state):
        self.config_manager.set_extract_images_only(state == Qt.Checked)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        files = []
        configs = []

        for url in event.mimeData().urls():
            path = url.toLocalFile()

            if os.path.isdir(path):
                folder_files, folder_configs = self.scan_folder(path)
                if folder_files:
                    files.extend(folder_files)
                else:
                    files.append(path)
                configs.extend(folder_configs)
            elif os.path.isfile(path):
                ext = os.path.splitext(path)[1].lower()
                if self.is_supported_source_file(path):
                    files.append(path)
                    config_path = os.path.join(os.path.dirname(path), "config.json")
                    if os.path.exists(config_path) and config_path not in configs:
                        configs.append(config_path)
                elif ext == ".json" and os.path.basename(path).lower() == "config.json":
                    if path not in configs:
                        configs.append(path)

        if files:
            self.selected_files = files
            self.selected_configs = configs
            self.update_file_display()

        event.acceptProposedAction()

    def browse_files(self):
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            tr("dialog.select_package_files", default="Select LPK/WPK Files"),
            "",
            tr(
                "dialog.filter_package_files",
                default=(
                    "Live2D Sources (*.lpk *.wpk *.assets *.sharedassets *.bundle *.unity3d);;"
                    "All Files (*.*)"
                ),
            ),
        )
        if file_paths:
            self.selected_files = [os.path.abspath(f) for f in file_paths]
            self.selected_configs = []

            for file_path in self.selected_files:
                dir_name = os.path.dirname(file_path)
                config_path = os.path.join(dir_name, "config.json")
                if os.path.exists(config_path) and config_path not in self.selected_configs:
                    self.selected_configs.append(config_path)

            self.update_file_display()

    def browse_folder(self):
        folder_path = QFileDialog.getExistingDirectory(
            self,
            tr("dialog.select_package_folder", default="Select Folder Containing LPK/WPK Files"),
        )
        if folder_path:
            files, configs = self.scan_folder(folder_path)
            self.selected_files = files or [os.path.abspath(folder_path)]
            self.selected_configs = configs
            self.update_file_display()

    def scan_folder(self, folder_path):
        return scan_package_folder(folder_path)

    def is_supported_source_file(self, file_path):
        ext = os.path.splitext(file_path)[1].lower()
        return ext in {".lpk", ".wpk"} or ext in UNITY_SOURCE_EXTENSIONS

    def update_file_display(self):
        if not self.selected_files:
            self.file_edit.clear()
        elif len(self.selected_files) == 1:
            self.file_edit.setText(self.selected_files[0])
        else:
            self.file_edit.setText(
                tr("extractor.selected_count", default="{count} files selected", count=len(self.selected_files))
            )

    def configure_logging(self):
        self.log_handler = QTextEditLogger(self.log_text)
        self.log_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))

        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(self.log_handler)

    def browse_output(self):
        dir_path = QFileDialog.getExistingDirectory(
            self,
            tr("dialog.select_output_directory"),
        )
        if dir_path:
            self.output_edit.setText(os.path.abspath(dir_path))

    def start_extraction(self):
        output_dir = self.output_edit.text()

        if not self.selected_files:
            InfoBar.error(
                title=tr("common.error"),
                content=tr(
                    "extractor.error_no_files",
                    default="Please select LPK/WPK files or folders.",
                ),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return

        if output_dir:
            output_dir = os.path.abspath(output_dir)
        else:
            output_dir = os.path.abspath(self.default_output_dir)
            self.output_edit.setText(output_dir)

        if not os.path.exists(output_dir):
            try:
                os.makedirs(output_dir)
            except OSError as e:
                InfoBar.error(
                    title=tr("common.error"),
                    content=tr("extractor.error_create_output_failed", error=str(e)),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=3000,
                )
                return

        self.last_output_dir = output_dir
        self.config_manager.set_last_output_dir(output_dir)

        self.extract_button.setEnabled(False)
        self.file_button.setEnabled(False)
        self.folder_button.setEnabled(False)
        self.output_button.setEnabled(False)

        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.summary_label.clear()
        self.summary_label.setVisible(False)
        self.log_text.clear()

        self.extractor_thread = ExtractorThread(
            self.selected_files,
            self.selected_configs,
            output_dir,
            self.images_only_checkbox.isChecked(),
        )
        self.extractor_thread.progressUpdated.connect(self.on_progress_updated)
        self.extractor_thread.extractionFinished.connect(self.extraction_finished)
        self.extractor_thread.extractionError.connect(self.extraction_error)
        self.extractor_thread.logMessage.connect(self.on_log_message)
        self.extractor_thread.start()

        logging.info(f"Starting extraction of {len(self.selected_files)} file(s) to {output_dir}")

    def on_progress_updated(self, value):
        self.progress_bar.setValue(value)

    def on_log_message(self, level, message):
        level_map = {
            "DEBUG": logging.DEBUG,
            "INFO": logging.INFO,
            "WARNING": logging.WARNING,
            "ERROR": logging.ERROR,
        }

        log_level = level_map.get(level, logging.INFO)
        logging.getLogger().log(log_level, message)

    def extraction_finished(self, result):
        output_dir = str(result.output_dir) if hasattr(result, "output_dir") else str(result)
        self.extract_button.setEnabled(True)
        self.file_button.setEnabled(True)
        self.folder_button.setEnabled(True)
        self.output_button.setEnabled(True)
        self.progress_bar.setValue(100)
        self.open_folder_button.setEnabled(True)
        self.last_output_dir = output_dir
        self.summary_label.setText(self.format_result_summary(result))
        self.summary_label.setVisible(True)

        if hasattr(result, "failed_items"):
            self.log_failed_items(result.failed_items)

        if hasattr(result, "has_failures") and result.has_failures:
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr(
                    "extractor.partial_batch_extracted",
                    default="Extraction finished with {failed} failed item(s). Files saved to {output}",
                    failed=result.failure_count,
                    output=output_dir,
                ),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=6000,
            )
            logging.warning(
                "Extraction finished with failures. Success: %s/%s, output: %s",
                result.success_count,
                result.total_count,
                output_dir,
            )
            return

        InfoBar.success(
            title=tr("common.success"),
            content=tr(
                "extractor.success_batch_extracted",
                default="Extraction completed successfully. Files saved to {output}",
                output=output_dir,
            ),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000,
        )

        logging.info(f"All extractions completed successfully. Files saved to {output_dir}")

    def extraction_error(self, error_message):
        self.extract_button.setEnabled(True)
        self.file_button.setEnabled(True)
        self.folder_button.setEnabled(True)
        self.output_button.setEnabled(True)
        self.progress_bar.setValue(0)

        MessageBox(
            tr("extractor.error_title"),
            error_message,
            self,
        ).exec()

        logging.error(f"Extraction failed: {error_message}")

    def format_result_summary(self, result):
        if not hasattr(result, "total_count"):
            return ""
        return tr(
            "extractor.summary",
            default=(
                "Summary: {success}/{total} succeeded, {failed} failed, "
                "{exported} exported, {skipped} skipped"
            ),
            success=result.success_count,
            total=result.total_count,
            failed=result.failure_count,
            exported=result.exported_count,
            skipped=result.skipped_count,
        )

    def log_failed_items(self, failed_items):
        for item in failed_items:
            logging.error(
                "Extraction failed for %s: %s",
                item.source,
                item.error or item.message,
            )

    def open_output_folder(self):
        output_dir = self.last_output_dir or self.output_edit.text()

        if os.path.exists(output_dir):
            QDesktopServices.openUrl(QUrl.fromLocalFile(output_dir))
        else:
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("extractor.warning_output_missing"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )

    def updateUIScale(self, window_width, window_height):
        scale_factor = max(1.0, window_width / 1000.0)

        button_height = int(30 * scale_factor)
        for button in self.findChildren(PushButton):
            button.setMinimumHeight(button_height)

        for line_edit in self.findChildren(LineEdit):
            line_edit.setMinimumHeight(button_height)

        font = QApplication.instance().font()
        for label in self.findChildren(SubtitleLabel):
            label_font = label.font()
            label_font.setPointSize(font.pointSize() + 2)
            label.setFont(label_font)

        self.log_text.setMinimumHeight(int(200 * scale_factor))
        self.progress_bar.setMinimumHeight(int(20 * scale_factor))
