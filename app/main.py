import sys
from PySide6.QtCore import Qt, QCoreApplication
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon
from app.paths import APP_ICON
from app.core.app_logging import setup_runtime_logging
from app.core.settings_manager import SettingsManager
from app.i18n import get_i18n, normalize_language_code

# Qt 6 enables high-DPI scaling by default. Keep shared OpenGL contexts for Live2D.
if hasattr(Qt.ApplicationAttribute, "AA_ShareOpenGLContexts"):
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

def run_application():
    log_path = setup_runtime_logging()
    print(f"Runtime log: {log_path}")

    # 创建QApplication实例
    app = QApplication(sys.argv)

    # 在创建窗口前初始化语言
    settings_manager = SettingsManager()
    language = normalize_language_code(settings_manager.get("language", "en_US"))
    get_i18n().set_language(language)
    
    # 设置应用程序图标 - 这会影响任务栏图标
    app_icon = QIcon(str(APP_ICON))
    app.setWindowIcon(app_icon)
    
    # 设置全局字体缩放因子
    font = app.font()
    font.setPointSize(10)  # 设置一个基础字号大小
    app.setFont(font)
    
    try:
        # 导入主窗口类
        from app.gui.MainWindow import MainWindow
        
        # 创建主窗口
        window = MainWindow()
        # 确保窗口也使用相同的图标
        window.setWindowIcon(app_icon)
        window.show()
        
        # 启动应用程序事件循环
        return app.exec()
    except Exception as e:
        import traceback
        print(f"Error initializing application: {e}")
        traceback.print_exc()
        return 1

def main():
    if "--preview-process" in sys.argv:
        args = [arg for arg in sys.argv[1:] if arg != "--preview-process"]
        from app.preview_process import run_preview_process

        return run_preview_process(args)
    return run_application()

if __name__ == "__main__":
    sys.exit(main())
