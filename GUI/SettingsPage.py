import os
from PyQt5.QtWidgets import QFrame, QVBoxLayout, QHBoxLayout, QLabel, QApplication, QSizePolicy
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFont
from qfluentwidgets import (
    SubtitleLabel, SettingCardGroup, SettingCard,
    ComboBox, FluentIcon, InfoBar, InfoBarPosition
)
from Core.config_manager import ConfigManager

class LanguageSettingCard(SettingCard):
    """Custom language setting card"""
    
    languageChanged = pyqtSignal(str)
    
    def __init__(self, icon, title, content, parent=None):
        super().__init__(icon, title, content, parent)
        
        self.comboBox = ComboBox(self)
        self.comboBox.addItems(["English (en-US)", "简体中文 (zh-CN)"])
        self.comboBox.currentIndexChanged.connect(self.onLanguageChanged)
        
        self.hBoxLayout.addWidget(self.comboBox, 0, Qt.AlignRight)
        self.hBoxLayout.addSpacing(16)
    
    def onLanguageChanged(self, index):
        """Emit signal when language changes"""
        language_map = {0: "en-US", 1: "zh-CN"}
        new_language = language_map.get(index, "en-US")
        self.languageChanged.emit(new_language)

class SettingsPage(QFrame):
    languageChanged = pyqtSignal(str)  # Signal when language changes
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('settingsPage')  # Required for navigation
        
        self.config_manager = ConfigManager()
        
        self.setupUI()
        
        # Response to window size changes
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        
    def setupUI(self):
        # Main layout
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(20, 20, 20, 20)
        self.main_layout.setSpacing(15)
        
        # Title
        self.title_label = SubtitleLabel("Settings", self)
        self.main_layout.addWidget(self.title_label)
        
        # General settings group
        self.general_group = SettingCardGroup("General Settings", self)
        
        # Language setting card
        self.language_card = LanguageSettingCard(
            icon=FluentIcon.LANGUAGE,
            title="Language",
            content="Select your preferred language",
            parent=self
        )
        
        # Set current language
        current_lang = self.config_manager.get_language()
        if current_lang == "zh-CN":
            self.language_card.comboBox.setCurrentIndex(1)
        else:
            self.language_card.comboBox.setCurrentIndex(0)
        
        self.language_card.languageChanged.connect(self.onLanguageChanged)
        self.general_group.addSettingCard(self.language_card)
        
        # Add group to main layout
        self.main_layout.addWidget(self.general_group)
        
        # Add stretch to push everything to the top
        self.main_layout.addStretch(1)
    
    def onLanguageChanged(self, new_language):
        """Handle language change"""
        self.config_manager.set_language(new_language)
        
        # Emit signal
        self.languageChanged.emit(new_language)
        
        # Show info message
        if new_language == "zh-CN":
            title = "语言已更改"
            content = "请重启应用程序以使更改生效。"
        else:
            title = "Language Changed"
            content = "Please restart the application for changes to take effect."
        
        InfoBar.success(
            title=title,
            content=content,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000
        )
    
    def updateUIScale(self, window_width, window_height):
        """Adjust UI elements according to window size"""
        # Calculate scale factor
        scale_factor = max(1.0, window_width / 1000.0)
        
        # Adjust font sizes
        font = QApplication.instance().font()
        for label in self.findChildren(SubtitleLabel):
            label_font = label.font()
            label_font.setPointSize(font.pointSize() + 2)
            label.setFont(label_font)
