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
from Core.image_extractor import ImageExtractor

class UnityExtractorPage(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('unityExtractorPage')
        
        # Enable drop
        self.setAcceptDrops(True)
        
        # Configuration manager
        self.config_manager = ConfigManager()
        
        # Default output directory
        self.default_output_dir = os.path.join(os.getcwd(), "output", "unity_images")
        
        # Track selected files
        self.selected_files = []
        
        # Track last output directory
        self.last_output_dir = self.default_output_dir
        
        self.setupUI()
        
        # 响应窗口大小变化
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        
    def setupUI(self):
        # Create main layout
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(20, 20, 20, 20)
        self.main_layout.setSpacing(10)
        
        # Add title
        self.title_label = SubtitleLabel("Unity Image Extractor", self)
        self.main_layout.addWidget(self.title_label)
        
        # File selection
        self.file_layout = QHBoxLayout()
        self.file_label = SubtitleLabel("Files/Folders:", self)
        self.file_edit = LineEdit(self)
        self.file_edit.setPlaceholderText("Drag & drop Unity files or folders here...")
        self.file_edit.setReadOnly(True)
        
        self.file_button = PushButton("Files", self)
        self.file_button.setIcon(FluentIcon.FOLDER)
        self.file_button.clicked.connect(self.browse_files)
        
        self.folder_button = PushButton("Folders", self)
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
        self.output_edit.setText(self.default_output_dir)
        self.output_button = PushButton("Browse", self)
        self.output_button.setIcon(FluentIcon.FOLDER)
        self.output_button.clicked.connect(self.browse_output)
        self.output_layout.addWidget(self.output_label)
        self.output_layout.addWidget(self.output_edit, 1)
        self.output_layout.addWidget(self.output_button)
        self.main_layout.addLayout(self.output_layout)
        
        # Progress bar
        self.progress_bar = ProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.main_layout.addWidget(self.progress_bar)
        
        # Extract button
        self.extract_button = PushButton("Extract Images", self)
        self.extract_button.setIcon(FluentIcon.IMAGE_EXPORT)
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
        
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
                    
    def dropEvent(self, event: QDropEvent):
        files = []
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if os.path.exists(path):
                files.append(path)
        
        if files:
            self.selected_files = files
            self.update_file_display()
                
        event.acceptProposedAction()
    
    def update_file_display(self):
        if not self.selected_files:
            self.file_edit.clear()
        elif len(self.selected_files) == 1:
            self.file_edit.setText(self.selected_files[0])
        else:
            self.file_edit.setText(f"{len(self.selected_files)} items selected")
            
    def browse_files(self):
        file_paths, _ = QFileDialog.getOpenFileNames(
            self, "Select Unity Files", "", "All Files (*.*)"
        )
        if file_paths:
            self.selected_files = [os.path.abspath(f) for f in file_paths]
            self.update_file_display()

    def browse_folder(self):
        dir_path = QFileDialog.getExistingDirectory(self, "Select Folder Containing Unity Files")
        if dir_path:
            self.selected_files = [os.path.abspath(dir_path)]
            self.update_file_display()
            
    def browse_output(self):
        dir_path = QFileDialog.getExistingDirectory(self, "Select Output Directory", self.output_edit.text())
        if dir_path:
            self.output_edit.setText(os.path.abspath(dir_path))
            
    def open_output_folder(self):
        output_dir = self.output_edit.text()
        if os.path.exists(output_dir):
            QDesktopServices.openUrl(QUrl.fromLocalFile(output_dir))

    def log_info(self, message):
        self.log_text.append(f"<span style='color: white;'>{message}</span>")
        
    def log_error(self, message):
        self.log_text.append(f"<span style='color: red;'>{message}</span>")
        
    def start_extraction(self):
        if not self.selected_files:
            InfoBar.error(
                title="No files selected",
                content="Please select or drag Unity files/folders first.",
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self
            )
            return
            
        output_dir = self.output_edit.text()
        os.makedirs(output_dir, exist_ok=True)
        
        # Collect all files if folders were selected
        actual_files = []
        for path in self.selected_files:
            if os.path.isfile(path):
                actual_files.append(path)
            elif os.path.isdir(path):
                for root, dirs, files in os.walk(path):
                    for filename in files:
                        actual_files.append(os.path.join(root, filename))
        
        if not actual_files:
            self.log_error("No files found in the selection.")
            return

        self.extract_button.setEnabled(False)
        self.log_text.clear()
        self.progress_bar.setValue(0)
        
        # Use existing ExtractorThread but with images_only=True
        # We might need to filter only Unity files for this thread or let it detect them
        # ImageExtractor.detect_file_type handles no-extension files by checking UnityPy load
        
        self.thread = ExtractorThread(actual_files, [], output_dir, extract_images_only=True)
        self.thread.progressUpdated.connect(self.progress_bar.setValue)
        self.thread.logMessage.connect(lambda level, msg: self.log_info(f"[{level}] {msg}"))
        self.thread.extractionFinished.connect(self.on_finished)
        self.thread.extractionError.connect(self.on_error)
        self.thread.start()
        
    def on_finished(self, output_dir):
        self.extract_button.setEnabled(True)
        self.open_folder_button.setEnabled(True)
        InfoBar.success(
            title="Success",
            content=f"Extraction completed to {output_dir}",
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=3000,
            parent=self
        )
        
    def on_error(self, error_msg):
        self.extract_button.setEnabled(True)
        self.log_error(f"Error: {error_msg}")
        InfoBar.error(
            title="Extraction failed",
            content=error_msg,
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=5000,
            parent=self
        )
