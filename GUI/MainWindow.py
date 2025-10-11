from PyQt5.QtWidgets import QApplication, QFrame, QHBoxLayout, QSizePolicy
from PyQt5.QtCore import Qt, QEvent
from PyQt5.QtGui import QIcon, QFont
from qfluentwidgets import NavigationItemPosition, FluentWindow, setTheme, Theme
from qfluentwidgets import FluentIcon as FIF

# Import pages
try:
    from GUI.ExtractorPage import ExtractorPage
except Exception as e:
    import traceback
    print(f"Error importing ExtractorPage: {e}")
    traceback.print_exc()
    class ExtractorPage(QFrame):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setObjectName('extractorPage')
            QHBoxLayout(self).addWidget(QFrame(self))

try:
    from GUI.Live2DModPage import Live2DModPage
except Exception as e:
    import traceback
    print(f"Error importing Live2DModPage: {e}")
    traceback.print_exc()
    class Live2DModPage(QFrame):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setObjectName('live2dModPage')
            QHBoxLayout(self).addWidget(QFrame(self))

try:
    from GUI.SettingsPage import SettingsPage
except Exception as e:
    import traceback
    print(f"Error importing SettingsPage: {e}")
    traceback.print_exc()
    class SettingsPage(QFrame):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setObjectName('settingsPage')
            QHBoxLayout(self).addWidget(QFrame(self))


class MainWindow(FluentWindow):
    """ Main Window with Navigation """
    
    def __init__(self):
        super().__init__()
        
        # Create sub-interfaces
        try:
            self.extractorPage = ExtractorPage(self)
        except Exception as e:
            print(f"Error creating ExtractorPage: {e}")
            self.extractorPage = QFrame(self)
            self.extractorPage.setObjectName('extractorPage')
            
        try:
            self.live2dModPage = Live2DModPage(self)
        except Exception as e:
            print(f"Error creating Live2DModPage: {e}")
            self.live2dModPage = QFrame(self)
            self.live2dModPage.setObjectName('live2dModPage')
            
        try:
            self.settingsPage = SettingsPage(self)
        except Exception as e:
            print(f"Error creating SettingsPage: {e}")
            self.settingsPage = QFrame(self)
            self.settingsPage.setObjectName('settingsPage')

        self.initWindow()
        self.initNavigation()
        
        # Set theme
        setTheme(Theme.AUTO)
        
        # Set font
        self.updateFontSize()
        
        # Install event filter for scaling
        self.installEventFilter(self)
        
    def initWindow(self):
        self.resize(1000, 700)
        self.setWindowTitle('LPK Unpacker GUI')
        
    def initNavigation(self):
        # Add main extractor page
        try:
            self.addSubInterface(self.extractorPage, FIF.ZIP_FOLDER, 'LPK Extractor')
        except Exception as e:
            print(f"Error adding ExtractorPage to navigation: {e}")
        
        self.navigationInterface.addSeparator()
        
        # Add Live2D mod tool
        try:
            self.addSubInterface(self.live2dModPage, FIF.EDIT, 'Live2D Mod Tool',
                              NavigationItemPosition.SCROLL)
        except Exception as e:
            print(f"Error adding Live2DModPage to navigation: {e}")
            
        # Add settings at bottom
        try:
            self.addSubInterface(self.settingsPage, FIF.SETTING, 'Settings',
                               NavigationItemPosition.BOTTOM)
        except Exception as e:
            print(f"Error adding SettingsPage to navigation: {e}")
            
    def eventFilter(self, obj, event):
        # Monitor window resize events to adjust UI
        if obj is self and event.type() == QEvent.Resize:
            self.updateFontSize()
        
        return super().eventFilter(obj, event)
    
    def updateFontSize(self):
        """Update font size based on window size"""
        width = self.width()
        
        # Calculate font size based on window width
        base_size = 9
        if width > 1600:
            font_size = base_size + 3
        elif width > 1200:
            font_size = base_size + 2
        elif width > 800:
            font_size = base_size + 1
        else:
            font_size = base_size
            
        # Set application font
        app = QApplication.instance()
        font = app.font()
        font.setPointSize(font_size)
        app.setFont(font)
        
        # Notify sub-pages about font update
        for page in [self.extractorPage, self.live2dModPage, self.settingsPage]:
            if hasattr(page, 'updateUIScale'):
                page.updateUIScale(self.width(), self.height())