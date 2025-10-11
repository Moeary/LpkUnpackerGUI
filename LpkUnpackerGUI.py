import sys
import os
from PyQt5.QtCore import Qt, QCoreApplication, QTranslator
from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QIcon

# Enable high DPI scaling
QCoreApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
QCoreApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

def run_application():
    """Main application entry point"""
    # Create QApplication instance
    app = QApplication(sys.argv)
    
    # Set application icon
    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Img", "icon.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    
    # Load translator for internationalization
    translator = QTranslator()
    from Core.config_manager import ConfigManager
    config_manager = ConfigManager()
    language = config_manager.get_language()
    
    if language == "zh-CN":
        translator_file = os.path.join(os.path.dirname(__file__), "translations", "zh_CN.qm")
        if os.path.exists(translator_file):
            translator.load(translator_file)
            app.installTranslator(translator)
    
    # Set base font
    font = app.font()
    font.setPointSize(10)
    app.setFont(font)
    
    try:
        # Import and create main window
        from GUI.MainWindow import MainWindow
        
        window = MainWindow()
        window.show()
        
        # Start event loop
        return app.exec_()
    except Exception as e:
        import traceback
        print(f"Error initializing application: {e}")
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(run_application())