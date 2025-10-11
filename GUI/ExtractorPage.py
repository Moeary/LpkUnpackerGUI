import os
import re
import logging
from PyQt5.QtCore import pyqtSignal, QThread, QUrl, Qt
from PyQt5.QtWidgets import (QWidget, QFrame, QVBoxLayout, QHBoxLayout, QFileDialog, 
                            QApplication, QSizePolicy, QCheckBox)
from PyQt5.QtGui import QDesktopServices, QFont, QDragEnterEvent, QDropEvent
from qfluentwidgets import (
    PushButton, LineEdit, ComboBox, ProgressBar, TextEdit, SubtitleLabel,
    FluentIcon, InfoBar, InfoBarPosition, MessageBox, CheckBox
)
from Core.extractor_thread import ExtractorThread
from Core.config_manager import ConfigManager

# Logger class for GUI output
class QTextEditLogger(logging.Handler):
    def __init__(self, textEdit):
        super().__init__()
        self.textEdit = textEdit
        self.textEdit.setReadOnly(True)
        self.formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        
    def emit(self, record):
        msg = self.formatter.format(record)
        self.textEdit.append(msg)
        # Auto-scroll to the bottom
        scrollbar = self.textEdit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

class ExtractorPage(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        # Set object name - required for FluentWindow navigation
        self.setObjectName('extractorPage')
        
        # Enable drop
        self.setAcceptDrops(True)
        
        # Configuration manager
        self.config_manager = ConfigManager()
        
        # Default output directory
        self.default_output_dir = os.path.join(os.getcwd(), "output")
        
        # Track selected files and folders
        self.selected_files = []
        self.selected_configs = []
        
        # Track last output directory
        self.last_output_dir = self.default_output_dir
        
        self.setupUI()
        self.configure_logging()
        
        # 响应窗口大小变化
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        
    def setupUI(self):
        # Create main layout
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(20, 20, 20, 20)
        self.main_layout.setSpacing(10)
        
        # Add title
        self.title_label = SubtitleLabel("LPK/WPK File Extractor", self)
        self.main_layout.addWidget(self.title_label)
        
        # File selection (supports multiple files)
        self.file_layout = QHBoxLayout()
        self.file_label = SubtitleLabel("Files/Folders:", self)
        self.file_edit = LineEdit(self)
        self.file_edit.setPlaceholderText("Select LPK/WPK files or folders, or drag & drop here...")
        self.file_edit.setReadOnly(True)
        self.file_button = PushButton("Browse Files", self)
        self.file_button.setIcon(FluentIcon.FOLDER)
        self.file_button.clicked.connect(self.browse_files)
        
        self.folder_button = PushButton("Browse Folder", self)
        self.folder_button.setIcon(FluentIcon.FOLDER_ADD)
        self.folder_button.clicked.connect(self.browse_folder)
        
        self.file_layout.addWidget(self.file_label)
        self.file_layout.addWidget(self.file_edit, 1)
        self.file_layout.addWidget(self.file_button)
        self.file_layout.addWidget(self.folder_button)
        self.main_layout.addLayout(self.file_layout)
        
        # Output directory selection
        self.output_layout = QHBoxLayout()
        self.output_label = SubtitleLabel("Output Directory:", self)
        self.output_edit = LineEdit(self)
        self.output_edit.setPlaceholderText("Select output directory...")
        # Set default output directory
        self.output_edit.setText(self.default_output_dir)
        self.output_button = PushButton("Browse", self)
        self.output_button.setIcon(FluentIcon.FOLDER)
        self.output_button.clicked.connect(self.browse_output)
        self.output_layout.addWidget(self.output_label)
        self.output_layout.addWidget(self.output_edit, 1)
        self.output_layout.addWidget(self.output_button)
        self.main_layout.addLayout(self.output_layout)
        
        # Extract images only checkbox
        self.images_only_checkbox = CheckBox("Extract Images Only", self)
        self.images_only_checkbox.setChecked(self.config_manager.get_extract_images_only())
        self.images_only_checkbox.stateChanged.connect(self.on_images_only_changed)
        self.main_layout.addWidget(self.images_only_checkbox)
        
        # Progress bar
        self.progress_bar = ProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.main_layout.addWidget(self.progress_bar)
        
        # Extract button
        self.extract_button = PushButton("Extract", self)
        self.extract_button.setIcon(FluentIcon.PLAY)
        self.extract_button.clicked.connect(self.start_extraction)
        self.main_layout.addWidget(self.extract_button)
        
        # Open output folder button
        self.open_folder_button = PushButton("Open Output Folder", self)
        self.open_folder_button.setIcon(FluentIcon.FOLDER)
        self.open_folder_button.clicked.connect(self.open_output_folder)
        self.open_folder_button.setEnabled(False)
        self.main_layout.addWidget(self.open_folder_button)
        
        # Log output
        self.log_label = SubtitleLabel("Log:", self)
        self.main_layout.addWidget(self.log_label)
        
        self.log_text = TextEdit(self)
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(200)
        self.main_layout.addWidget(self.log_text)
        
        # 确保所有按钮有合理的最小尺寸
        for button in self.findChildren(PushButton):
            button.setMinimumSize(100, 30)
            
        # 设置LineEdit的最小高度
        for line_edit in self.findChildren(LineEdit):
            line_edit.setMinimumHeight(30)
            
        # 设置TextEdit的响应式尺寸
        self.log_text.setMinimumHeight(200)
        self.log_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    
    def on_images_only_changed(self, state):
        """Handle images only checkbox state change"""
        self.config_manager.set_extract_images_only(state == Qt.Checked)
        
    # Drag & Drop support
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            # Accept files and folders
            event.acceptProposedAction()
                    
    def dropEvent(self, event: QDropEvent):
        # Process the dropped files/folders
        files = []
        configs = []
        
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            
            if os.path.isdir(path):
                # Process folder
                folder_files, folder_configs = self.scan_folder(path)
                files.extend(folder_files)
                configs.extend(folder_configs)
            elif os.path.isfile(path):
                ext = os.path.splitext(path)[1].lower()
                if ext in ['.lpk', '.wpk']:
                    files.append(path)
                    # Look for config in same directory
                    config_path = os.path.join(os.path.dirname(path), 'config.json')
                    if os.path.exists(config_path) and config_path not in configs:
                        configs.append(config_path)
                elif ext == '.json' and os.path.basename(path).lower() == 'config.json':
                    if path not in configs:
                        configs.append(path)
        
        if files:
            self.selected_files = files
            self.selected_configs = configs
            self.update_file_display()
                
        event.acceptProposedAction()
    
    def browse_files(self):
        """Browse for multiple files"""
        file_paths, _ = QFileDialog.getOpenFileNames(
            self, "Select LPK/WPK Files", "", "Package Files (*.lpk *.wpk);;All Files (*.*)"
        )
        if file_paths:
            self.selected_files = [os.path.abspath(f) for f in file_paths]
            self.selected_configs = []
            
            # Try to find config files
            for file_path in self.selected_files:
                dir_name = os.path.dirname(file_path)
                config_path = os.path.join(dir_name, "config.json")
                if os.path.exists(config_path) and config_path not in self.selected_configs:
                    self.selected_configs.append(config_path)
            
            self.update_file_display()
    
    def browse_folder(self):
        """Browse for a folder"""
        folder_path = QFileDialog.getExistingDirectory(
            self, "Select Folder Containing LPK/WPK Files"
        )
        if folder_path:
            files, configs = self.scan_folder(folder_path)
            self.selected_files = files
            self.selected_configs = configs
            self.update_file_display()
    
    def scan_folder(self, folder_path):
        """Scan folder for LPK/WPK files and configs"""
        files = []
        configs = []
        
        for root, dirs, filenames in os.walk(folder_path):
            for filename in filenames:
                full_path = os.path.join(root, filename)
                ext = os.path.splitext(filename)[1].lower()
                
                if ext in ['.lpk', '.wpk']:
                    files.append(full_path)
                elif filename.lower() == 'config.json':
                    configs.append(full_path)
        
        return files, configs
    
    def update_file_display(self):
        """Update the file display text"""
        if not self.selected_files:
            self.file_edit.clear()
        elif len(self.selected_files) == 1:
            self.file_edit.setText(self.selected_files[0])
        else:
            self.file_edit.setText(f"{len(self.selected_files)} files selected")
        
    def configure_logging(self):
        # Setup logging to capture to text widget
        self.log_handler = QTextEditLogger(self.log_text)
        self.log_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(self.log_handler)
            
    def browse_output(self):
        dir_path = QFileDialog.getExistingDirectory(
            self, "Select Output Directory"
        )
        if dir_path:
            # Use absolute path
            self.output_edit.setText(os.path.abspath(dir_path))
            
    def start_extraction(self):
        # Validate inputs
        output_dir = self.output_edit.text()
        
        if not self.selected_files:
            InfoBar.error(
                title="Error",
                content="Please select LPK/WPK files or folders.",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )
            return
        
        # Convert to absolute paths
        if output_dir:
            output_dir = os.path.abspath(output_dir)
        else:
            # Use default output directory
            output_dir = os.path.abspath(self.default_output_dir)
            self.output_edit.setText(output_dir)
            
        # Create output directory if it doesn't exist
        if not os.path.exists(output_dir):
            try:
                os.makedirs(output_dir)
            except OSError as e:
                InfoBar.error(
                    title="Error",
                    content=f"Failed to create output directory: {str(e)}",
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=3000
                )
                return
        
        # Save last output directory
        self.last_output_dir = output_dir
        self.config_manager.set_last_output_dir(output_dir)
        
        # Disable controls during extraction
        self.extract_button.setEnabled(False)
        self.file_button.setEnabled(False)
        self.folder_button.setEnabled(False)
        
        # Reset progress bar
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        
        # Clear log
        self.log_text.clear()
        
        # Get extract images only setting
        extract_images_only = self.images_only_checkbox.isChecked()
        
        # Start extraction in a separate thread
        self.extractor_thread = ExtractorThread(
            self.selected_files,
            self.selected_configs,
            output_dir,
            extract_images_only
        )
        self.extractor_thread.progressUpdated.connect(self.on_progress_updated)
        self.extractor_thread.extractionFinished.connect(self.extraction_finished)
        self.extractor_thread.extractionError.connect(self.extraction_error)
        self.extractor_thread.logMessage.connect(self.on_log_message)
        self.extractor_thread.start()
        
        # Log start of extraction
        logging.info(f"Starting extraction of {len(self.selected_files)} file(s) to {output_dir}")
    
    def on_progress_updated(self, value):
        """Handle progress update from thread"""
        self.progress_bar.setValue(value)
    
    def on_log_message(self, level, message):
        """Handle log message from thread"""
        # Map level to logging level
        level_map = {
            "DEBUG": logging.DEBUG,
            "INFO": logging.INFO,
            "WARNING": logging.WARNING,
            "ERROR": logging.ERROR
        }
        
        log_level = level_map.get(level, logging.INFO)
        logger = logging.getLogger()
        logger.log(log_level, message)
        
    def extraction_finished(self, output_dir):
        # Re-enable controls
        self.extract_button.setEnabled(True)
        self.file_button.setEnabled(True)
        self.folder_button.setEnabled(True)
        self.progress_bar.setValue(100)
        self.open_folder_button.setEnabled(True)
        
        # Update last output directory
        self.last_output_dir = output_dir
        
        # Show success message
        InfoBar.success(
            title="Success",
            content=f"Extraction completed successfully",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000
        )
        
        logging.info(f"All extractions completed successfully. Files saved to {output_dir}")
        
    def extraction_error(self, error_message):
        # Re-enable controls
        self.extract_button.setEnabled(True)
        self.file_button.setEnabled(True)
        self.folder_button.setEnabled(True)
        self.progress_bar.setValue(0)
        
        # Show error message
        MessageBox(
            "Extraction Error",
            error_message,
            self
        ).exec_()
        
        logging.error(f"Extraction failed: {error_message}")
        
    def open_output_folder(self):
        # Use the last output directory
        output_dir = self.last_output_dir or self.output_edit.text()
        
        if os.path.exists(output_dir):
            QDesktopServices.openUrl(QUrl.fromLocalFile(output_dir))
        else:
            InfoBar.warning(
                title="Warning",
                content="Output directory does not exist.",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )
            
    def updateUIScale(self, window_width, window_height):
        """根据窗口大小调整UI元素"""
        # 计算比例因子
        scale_factor = max(1.0, window_width / 1000.0)
        
        # 调整按钮大小
        button_height = int(30 * scale_factor)
        for button in self.findChildren(PushButton):
            button.setMinimumHeight(button_height)
            
        # 调整输入框高度
        for line_edit in self.findChildren(LineEdit):
            line_edit.setMinimumHeight(button_height)
            
        # 调整字体大小
        font = QApplication.instance().font()
        for label in self.findChildren(SubtitleLabel):
            label_font = label.font()
            label_font.setPointSize(font.pointSize() + 2)  # 标题字体比正常字体大2点
            label.setFont(label_font)
            
        # 日志窗口自适应
        self.log_text.setMinimumHeight(int(200 * scale_factor))
        
        # 进度条高度
        self.progress_bar.setMinimumHeight(int(20 * scale_factor))
