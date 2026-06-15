import logging
import os
import shutil
import tempfile

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QSizePolicy,
    QSplitter,
    QTableWidgetItem,
    QVBoxLayout,
)
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    CaptionLabel,
    CheckBox,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    MessageBox,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    SubtitleLabel,
    TableWidget,
    TextEdit,
)

from app.core.assetstudio_cli import AssetStudioCLI
from app.core.config_manager import ConfigManager
from app.core.extract import ExtractSourceType, ExtractTaskPlan, analyze_sources, scan_package_folder
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


class DropPathLineEdit(LineEdit):
    pathsDropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.toLocalFile()]
        if paths:
            self.pathsDropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()


class UnityDeepAnalysisThread(QThread):
    itemAnalyzed = Signal(int, str, object, str, int)
    analysisFinished = Signal()

    def __init__(self, items, parent=None):
        super().__init__(parent)
        self.items = list(items)

    def run(self):
        for row, path in self.items:
            temp_dir = tempfile.mkdtemp(prefix="lpk_unity_probe_")
            try:
                result = AssetStudioCLI().export_live2d(path, temp_dir)
                if _has_live2d_export(result.exported_files):
                    self.itemAnalyzed.emit(
                        row,
                        "extractor.deep_live2d_confirmed",
                        {"key": "extractor.deep_note_confirmed", "count": result.exported_count},
                        "extractor.task_status_ready",
                        Qt.Checked.value,
                    )
                else:
                    self.itemAnalyzed.emit(
                        row,
                        "extractor.deep_live2d_rejected",
                        {"key": "extractor.deep_note_rejected"},
                        "extractor.task_status_skipped",
                        Qt.Unchecked.value,
                    )
            except Exception as exc:
                self.itemAnalyzed.emit(
                    row,
                    "extractor.deep_live2d_failed",
                    {"key": "extractor.deep_note_failed", "error": str(exc)},
                    "extractor.task_status_ready",
                    -1,
                )
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
        self.analysisFinished.emit()


def _has_live2d_export(paths):
    for path in paths:
        name = path.name.lower()
        suffix = path.suffix.lower()
        if name.endswith(".model3.json") or name == "model.json":
            return True
        if suffix in {".moc", ".moc3"}:
            return True
    return False


class ExtractorPage(QFrame):
    CONTROL_HEIGHT = 42
    ACTION_HEIGHT = 44
    PROGRESS_HEIGHT = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("extractorPage")
        self.setAcceptDrops(True)

        self.config_manager = ConfigManager()
        self.default_output_dir = self.config_manager.get_last_output_dir()
        os.makedirs(self.default_output_dir, exist_ok=True)
        self.i18n = get_i18n()

        self.selected_files = []
        self.selected_configs = []
        self.task_plans: list[ExtractTaskPlan] = []
        self.last_output_dir = self.default_output_dir
        self.deep_analysis_thread = None

        self.setupUI()
        self.retranslate_ui()
        self.configure_logging()
        self.i18n.languageChanged.connect(self.retranslate_ui)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def setupUI(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(16, 16, 16, 16)
        self.main_layout.setSpacing(12)

        self.header_layout = QHBoxLayout()
        self.header_layout.setSpacing(10)
        self.title_label = SubtitleLabel("", self)
        self.header_desc_label = CaptionLabel("", self)
        self.header_desc_label.setWordWrap(True)
        self.header_text_layout = QVBoxLayout()
        self.header_text_layout.setSpacing(2)
        self.header_text_layout.addWidget(self.title_label)
        self.header_text_layout.addWidget(self.header_desc_label)
        self.header_layout.addLayout(self.header_text_layout, 1)
        self.main_layout.addLayout(self.header_layout)

        self.splitter = QSplitter(Qt.Horizontal, self)
        self.splitter.setHandleWidth(8)
        self.main_layout.addWidget(self.splitter, 1)

        self.left_panel = QFrame(self.splitter)
        self.left_layout = QVBoxLayout(self.left_panel)
        self.left_layout.setContentsMargins(0, 0, 10, 0)
        self.left_layout.setSpacing(10)

        self.top_panel = QFrame(self.left_panel)
        self.top_panel_layout = QVBoxLayout(self.top_panel)
        self.top_panel_layout.setContentsMargins(0, 0, 0, 0)
        self.top_panel_layout.setSpacing(10)

        self.input_card = CardWidget(self.top_panel)
        self.input_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        input_layout = QVBoxLayout(self.input_card)
        input_layout.setContentsMargins(14, 14, 14, 14)
        input_layout.setSpacing(10)

        self.file_layout = QHBoxLayout()
        self.file_layout.setAlignment(Qt.AlignVCenter)
        self.file_label = SubtitleLabel("", self)
        self.file_edit = DropPathLineEdit(self)
        self.file_edit.setReadOnly(True)
        self.file_edit.pathsDropped.connect(self.analyze_input_paths)
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
        input_layout.addLayout(self.file_layout)

        self.output_layout = QHBoxLayout()
        self.output_layout.setAlignment(Qt.AlignVCenter)
        self.output_label = SubtitleLabel("", self)
        self.output_edit = LineEdit(self)
        self.output_edit.setText(self.default_output_dir)
        self.output_edit.setCursorPosition(0)
        self.output_button = PushButton("", self)
        self.output_button.setIcon(FluentIcon.FOLDER)
        self.output_button.clicked.connect(self.browse_output)
        self.output_layout.addWidget(self.output_label)
        self.output_layout.addWidget(self.output_edit, 1)
        self.output_layout.addWidget(self.output_button)
        input_layout.addLayout(self.output_layout)

        self.images_only_checkbox = CheckBox("", self)
        self.images_only_checkbox.setChecked(self.config_manager.get_extract_images_only())
        self.images_only_checkbox.stateChanged.connect(self.on_images_only_changed)
        input_layout.addWidget(self.images_only_checkbox)

        self.top_panel_layout.addWidget(self.input_card)

        self.action_card = CardWidget(self.top_panel)
        self.action_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        action_layout = QVBoxLayout(self.action_card)
        action_layout.setContentsMargins(14, 14, 14, 14)
        action_layout.setSpacing(10)

        self.progress_bar = ProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(self.PROGRESS_HEIGHT)
        action_layout.addWidget(self.progress_bar)

        self.summary_label = SubtitleLabel("", self)
        self.summary_label.setVisible(False)
        action_layout.addWidget(self.summary_label)

        self.action_buttons_layout = QHBoxLayout()
        self.extract_button = PrimaryPushButton("", self)
        self.extract_button.setIcon(FluentIcon.PLAY)
        self.extract_button.clicked.connect(self.start_extraction)
        self.action_buttons_layout.addWidget(self.extract_button)

        self.clear_tasks_button = PushButton("", self)
        self.clear_tasks_button.setIcon(FluentIcon.REMOVE)
        self.clear_tasks_button.clicked.connect(self.clear_tasks)
        self.action_buttons_layout.addWidget(self.clear_tasks_button)

        self.open_folder_button = PushButton("", self)
        self.open_folder_button.setIcon(FluentIcon.FOLDER)
        self.open_folder_button.clicked.connect(self.open_output_folder)
        self.open_folder_button.setEnabled(True)
        self.open_folder_button.hide()

        action_layout.addLayout(self.action_buttons_layout)

        self.top_panel_layout.addWidget(self.action_card)
        self.top_panel_layout.addStretch(1)

        self.left_layout.addWidget(self.top_panel, 0)

        self.log_card = CardWidget(self.left_panel)
        self.log_card_layout = QVBoxLayout(self.log_card)
        self.log_card_layout.setContentsMargins(14, 14, 14, 14)
        self.log_card_layout.setSpacing(10)
        self.log_header_layout = QHBoxLayout()
        self.log_header_layout.setSpacing(10)
        self.log_label = SubtitleLabel("", self.log_card)
        self.clear_log_button = PushButton("", self)
        self.clear_log_button.setIcon(FluentIcon.REMOVE)
        self.log_header_layout.addWidget(self.log_label, 1)
        self.log_header_layout.addWidget(self.clear_log_button)
        self.log_card_layout.addLayout(self.log_header_layout)

        self.log_text = TextEdit(self.log_card)
        self.log_text.setReadOnly(True)
        self.log_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.clear_log_button.clicked.connect(self.log_text.clear)
        self.log_card_layout.addWidget(self.log_text, 1)
        self.left_layout.addWidget(self.log_card, 1)

        self.right_panel = QFrame(self.splitter)
        self.right_layout = QVBoxLayout(self.right_panel)
        self.right_layout.setContentsMargins(10, 0, 0, 0)
        self.right_layout.setSpacing(10)

        self.task_card = CardWidget(self.right_panel)
        task_card_layout = QVBoxLayout(self.task_card)
        task_card_layout.setContentsMargins(14, 14, 14, 14)
        task_card_layout.setSpacing(10)

        self.task_header_layout = QHBoxLayout()
        self.task_header_layout.setSpacing(10)
        self.task_title_label = SubtitleLabel("", self.right_panel)
        self.task_header_layout.addWidget(self.task_title_label, 1)

        self.deep_analyze_button = PushButton("", self.right_panel)
        self.deep_analyze_button.setIcon(FluentIcon.SEARCH)
        self.deep_analyze_button.clicked.connect(self.start_deep_analysis)
        self.task_header_layout.addWidget(self.deep_analyze_button)
        task_card_layout.addLayout(self.task_header_layout)

        self.task_table = TableWidget(self.right_panel)
        self.task_table.setColumnCount(5)
        self.task_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.task_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.task_table.setAlternatingRowColors(True)
        self.task_table.verticalHeader().setVisible(False)
        self.task_table.setWordWrap(False)
        self.task_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.task_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.task_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.task_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.task_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.task_table.itemChanged.connect(self.on_task_item_changed)
        if hasattr(self.task_table, "setBorderVisible"):
            self.task_table.setBorderVisible(True)
        if hasattr(self.task_table, "setBorderRadius"):
            self.task_table.setBorderRadius(6)
        task_card_layout.addWidget(self.task_table, 1)

        self.task_hint_label = BodyLabel("", self.right_panel)
        self.task_hint_label.setWordWrap(True)
        task_card_layout.addWidget(self.task_hint_label)
        self.right_layout.addWidget(self.task_card, 1)

        self.splitter.addWidget(self.left_panel)
        self.splitter.addWidget(self.right_panel)
        self.splitter.setSizes([620, 980])
        self.setStyleSheet(
            """
            QSplitter::handle {
                background: #eef2f7;
                border-radius: 3px;
            }
            QSplitter::handle:hover {
                background: #d5dde8;
            }
            QProgressBar {
                min-height: 6px;
                max-height: 6px;
                border-radius: 3px;
            }
            QProgressBar::chunk {
                border-radius: 3px;
            }
            """
        )

        self.apply_compact_metrics()

    def apply_compact_metrics(self):
        input_controls = (
            self.file_edit,
            self.file_button,
            self.folder_button,
            self.output_edit,
            self.output_button,
        )
        for widget in input_controls:
            widget.setFixedHeight(self.CONTROL_HEIGHT)

        self.file_button.setMinimumWidth(112)
        self.folder_button.setMinimumWidth(126)
        self.output_button.setMinimumWidth(112)

        for button in (
            self.extract_button,
            self.clear_tasks_button,
            self.open_folder_button,
            self.clear_log_button,
            self.deep_analyze_button,
        ):
            button.setFixedHeight(self.ACTION_HEIGHT)

        for button in self.findChildren(PushButton):
            button.setMinimumWidth(96)

        self.progress_bar.setFixedHeight(self.PROGRESS_HEIGHT)

    def retranslate_ui(self):
        self.title_label.setText(tr("extractor.title", default="LPK/WPK File Extractor"))
        self.header_desc_label.setText(tr("extractor.header_desc"))
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
        self.clear_tasks_button.setText(tr("extractor.clear_tasks"))
        self.open_folder_button.setText(tr("extractor.open_output_folder"))
        self.log_label.setText(tr("extractor.log"))
        self.clear_log_button.setText(tr("extractor.clear_log"))
        self.task_title_label.setText(tr("extractor.task_list"))
        self.deep_analyze_button.setText(tr("extractor.deep_analyze_unity"))
        self.task_hint_label.setText(tr("extractor.task_hint"))
        self.task_table.setHorizontalHeaderLabels(
            [
                tr("extractor.task_enabled"),
                tr("extractor.task_path"),
                tr("extractor.task_analysis"),
                tr("extractor.task_status"),
                tr("extractor.task_output"),
            ]
        )

    def on_images_only_changed(self, state):
        self.config_manager.set_extract_images_only(state == Qt.Checked)
        self.refresh_task_modes()

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.toLocalFile()]
        self.analyze_input_paths(paths)

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
            self.analyze_input_paths(file_paths)

    def browse_folder(self):
        folder_path = QFileDialog.getExistingDirectory(
            self,
            tr("dialog.select_package_folder", default="Select Folder Containing LPK/WPK Files"),
        )
        if folder_path:
            self.analyze_input_paths([folder_path])

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
            self.file_edit.setCursorPosition(0)
        else:
            self.file_edit.setText(
                tr("extractor.selected_count", default="{count} files selected", count=len(self.selected_files))
            )
            self.file_edit.setCursorPosition(0)

    def analyze_input_paths(self, paths):
        tasks, configs = analyze_sources(paths)
        if not tasks and not configs:
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("extractor.warning_no_supported_tasks"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return

        self.task_plans = tasks
        self.selected_configs = [str(path) for path in configs]
        self.refresh_task_table()
        self.update_selected_files_from_table()
        self.log_text.clear()
        logging.info("Analyzed %s task(s), found %s config file(s)", len(tasks), len(configs))

    def refresh_task_table(self):
        self.task_table.blockSignals(True)
        self.task_table.setRowCount(0)
        for row, task in enumerate(self.task_plans):
            self.task_table.insertRow(row)

            enabled_item = QTableWidgetItem("")
            enabled_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            enabled_item.setCheckState(Qt.Checked if task.runnable else Qt.Unchecked)
            if not task.runnable:
                enabled_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.task_table.setItem(row, 0, enabled_item)

            path_item = QTableWidgetItem(str(task.path))
            path_item.setToolTip(str(task.path))
            self.task_table.setItem(row, 1, path_item)

            analysis_item = QTableWidgetItem(self.analysis_label(task))
            analysis_item.setToolTip(self.analysis_tooltip(task))
            self.task_table.setItem(row, 2, analysis_item)

            status_item = QTableWidgetItem(tr("extractor.task_status_ready") if task.runnable else tr("extractor.task_status_skipped"))
            status_item.setToolTip(task.note)
            self.task_table.setItem(row, 3, status_item)
            self.set_task_output_button(row, None)
            self.task_table.setRowHeight(row, 46)

        self.task_table.blockSignals(False)

    def set_task_output_button(self, row: int, output_dir):
        button = PushButton("", self.task_table)
        button.setIcon(FluentIcon.FOLDER)
        button.setFixedHeight(30)
        button.setMinimumWidth(72)
        button.setText(tr("extractor.open_task_output"))
        button.setEnabled(bool(output_dir))
        button.setProperty("output_dir", str(output_dir) if output_dir else "")
        if output_dir:
            button.setToolTip(str(output_dir))
        else:
            button.setToolTip(tr("extractor.open_task_output_disabled"))
        button.clicked.connect(lambda _checked=False, b=button: self.open_task_output_folder(b))
        self.task_table.setCellWidget(row, 4, button)

    def open_task_output_folder(self, button):
        output_dir = button.property("output_dir")
        if not output_dir:
            return
        os.makedirs(output_dir, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(output_dir))

    def on_task_item_changed(self, item):
        if item.column() == 0:
            self.update_selected_files_from_table()

    def update_selected_files_from_table(self):
        selected = []
        for row, task in enumerate(self.task_plans):
            item = self.task_table.item(row, 0)
            if task.runnable and item and item.checkState() == Qt.Checked:
                selected.append(str(task.path))
        self.selected_files = selected
        self.update_file_display()

    def current_mode_label(self, task: ExtractTaskPlan):
        if not task.runnable:
            return tr("extractor.task_mode_skip")
        if self.images_only_checkbox.isChecked():
            return tr("extractor.task_mode_textures")
        if task.source_type.name == "UNITY":
            return tr("extractor.task_mode_live2d")
        return tr("extractor.task_mode_full")

    def refresh_task_modes(self):
        for row, task in enumerate(self.task_plans):
            item = self.task_table.item(row, 2)
            if item:
                item.setText(self.analysis_label(task))
                item.setToolTip(self.analysis_tooltip(task))

    def analysis_label(self, task: ExtractTaskPlan):
        return (
            f"{task.display_type} | {task.live2d_status} | {self.current_mode_label(task)}"
        )

    def analysis_tooltip(self, task: ExtractTaskPlan):
        config = str(task.config_path) if task.config_path else tr("extractor.task_config_none")
        return (
            f"{tr('extractor.task_type')}: {task.display_type}\n"
            f"{tr('extractor.task_live2d')}: {task.live2d_status}\n"
            f"{tr('extractor.task_config')}: {config}\n"
            f"{tr('extractor.task_mode')}: {self.current_mode_label(task)}\n"
            f"{tr('extractor.task_note')}: {task.note}"
        )

    def clear_tasks(self):
        if self.deep_analysis_thread and self.deep_analysis_thread.isRunning():
            return
        self.task_plans = []
        self.selected_files = []
        self.selected_configs = []
        self.task_table.setRowCount(0)
        self.file_edit.clear()
        self.progress_bar.setValue(0)
        self.summary_label.clear()
        self.summary_label.setVisible(False)
        self.open_folder_button.setEnabled(True)
        self.log_text.clear()

    def start_deep_analysis(self):
        items = []
        for row, task in enumerate(self.task_plans):
            enabled_item = self.task_table.item(row, 0)
            if task.source_type != ExtractSourceType.UNITY:
                continue
            if enabled_item and enabled_item.checkState() != Qt.Checked:
                continue
            items.append((row, str(task.path)))

        if not items:
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("extractor.deep_no_unity_tasks"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return

        self.deep_analyze_button.setEnabled(False)
        self.extract_button.setEnabled(False)
        self.file_button.setEnabled(False)
        self.folder_button.setEnabled(False)
        self.output_button.setEnabled(False)
        self.clear_tasks_button.setEnabled(False)
        self.progress_bar.setRange(0, 0)

        for row, _path in items:
            status_item = self.task_table.item(row, 3)
            if status_item:
                status_item.setText(tr("extractor.task_status_analyzing"))
                status_item.setToolTip(tr("extractor.deep_note_running"))

        logging.info("Starting deep Unity analysis for %s task(s)", len(items))
        self.deep_analysis_thread = UnityDeepAnalysisThread(items, self)
        self.deep_analysis_thread.itemAnalyzed.connect(self.on_deep_analysis_item)
        self.deep_analysis_thread.analysisFinished.connect(self.on_deep_analysis_finished)
        self.deep_analysis_thread.start()

    def on_deep_analysis_item(self, row: int, live2d_key: str, note_payload, status_key: str, check_state: int):
        if row < 0 or row >= self.task_table.rowCount():
            return
        analysis_item = self.task_table.item(row, 2)
        status_item = self.task_table.item(row, 3)
        enabled_item = self.task_table.item(row, 0)

        if analysis_item:
            old = analysis_item.text()
            mode = old.split("|")[-1].strip() if "|" in old else ""
            analysis_item.setText(f"{tr(live2d_key)} | {mode}" if mode else tr(live2d_key))
        if status_item:
            status_item.setText(tr(status_key))
            status_item.setToolTip(self._format_deep_note(note_payload))
        if enabled_item and check_state >= 0:
            enabled_item.setCheckState(Qt.CheckState(check_state))
        self.update_selected_files_from_table()

    def on_deep_analysis_finished(self):
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.deep_analyze_button.setEnabled(True)
        self.extract_button.setEnabled(True)
        self.file_button.setEnabled(True)
        self.folder_button.setEnabled(True)
        self.output_button.setEnabled(True)
        self.clear_tasks_button.setEnabled(True)
        logging.info("Deep Unity analysis finished")

    def _format_deep_note(self, payload):
        if isinstance(payload, dict):
            key = payload.get("key", "")
            values = {k: v for k, v in payload.items() if k != "key"}
            if key:
                return tr(key, **values)
        return str(payload or "")

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
        self.update_selected_files_from_table()
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
        self.clear_tasks_button.setEnabled(False)
        self.deep_analyze_button.setEnabled(False)
        self.task_table.setEnabled(False)

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
        self.mark_selected_tasks_running()

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
        self.clear_tasks_button.setEnabled(True)
        self.deep_analyze_button.setEnabled(True)
        self.task_table.setEnabled(True)
        self.progress_bar.setValue(100)
        self.open_folder_button.setEnabled(True)
        self.last_output_dir = output_dir
        self.summary_label.setText(self.format_result_summary(result))
        self.summary_label.setVisible(True)
        self.update_task_results(result)

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
        self.clear_tasks_button.setEnabled(True)
        self.deep_analyze_button.setEnabled(True)
        self.task_table.setEnabled(True)
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

    def mark_selected_tasks_running(self):
        for row, task in enumerate(self.task_plans):
            enabled_item = self.task_table.item(row, 0)
            status_item = self.task_table.item(row, 3)
            if not status_item:
                continue
            self.set_task_output_button(row, None)
            if task.runnable and enabled_item and enabled_item.checkState() == Qt.Checked:
                status_item.setText(tr("extractor.task_status_running"))
            elif task.runnable:
                status_item.setText(tr("extractor.task_status_skipped"))

    def update_task_results(self, result):
        result_by_source = {}
        if hasattr(result, "items"):
            for item in result.items:
                result_by_source[str(item.source.resolve())] = item

        for row, task in enumerate(self.task_plans):
            status_item = self.task_table.item(row, 3)
            if not status_item:
                continue
            item = result_by_source.get(str(task.path.resolve()))
            if not item:
                current = status_item.text()
                if current == tr("extractor.task_status_running"):
                    status_item.setText(tr("extractor.task_status_skipped"))
                continue

            if item.success:
                status_item.setText(tr("extractor.task_status_success"))
                status_item.setToolTip(item.message or task.note)
                self.set_task_output_button(row, self.result_output_dir(item))
            else:
                status_item.setText(tr("extractor.task_status_failed"))
                status_item.setToolTip(item.error or item.message or task.note)
                self.set_task_output_button(row, None)

    def result_output_dir(self, item):
        child_dirs = [
            child.output_dir
            for child in getattr(item, "children", [])
            if child.success and child.output_dir
        ]
        unique_child_dirs = []
        for path in child_dirs:
            if path not in unique_child_dirs:
                unique_child_dirs.append(path)
        if len(unique_child_dirs) == 1:
            return unique_child_dirs[0]
        return item.output_dir

    def open_output_folder(self):
        output_dir = self.last_output_dir or self.output_edit.text()

        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(output_dir))

    def updateUIScale(self, window_width, window_height):
        scale_factor = max(1.0, window_width / 1000.0)
        self.apply_compact_metrics()

        font = QApplication.instance().font()
        for label in self.findChildren(SubtitleLabel):
            label_font = label.font()
            label_font.setPointSize(font.pointSize() + 2)
            label.setFont(label_font)

        self.log_text.setMinimumHeight(int(200 * scale_factor))
        self.progress_bar.setFixedHeight(self.PROGRESS_HEIGHT)
