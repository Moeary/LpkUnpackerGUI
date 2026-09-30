import sys


def _configure_qt_environment():
    from PySide6.QtCore import Qt, QCoreApplication
    from PySide6.QtWidgets import QApplication

    # Keep the stdio MCP entry point free of GUI imports and startup output.
    # Qt 6 enables DPI scaling by default; Live2D requires shared GL contexts.
    if hasattr(Qt.ApplicationAttribute, "AA_ShareOpenGLContexts"):
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

def run_application():
    _configure_qt_environment()
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QIcon
    from app.paths import APP_ICON
    from app.core.app_logging import setup_runtime_logging
    from app.core.font_helper import apply_application_font
    from app.core.settings_manager import SettingsManager
    from app.i18n import get_i18n, normalize_language_code

    log_path = setup_runtime_logging()
    print(f"Runtime log: {log_path}")

    # 创建QApplication实例
    app = QApplication(sys.argv)
    app.setApplicationName("Live2D_MOD_Helper")
    app.setApplicationDisplayName("Live2D_MOD_Helper")
    app.setOrganizationName("Live2D_MOD_Helper")

    # 在创建窗口前初始化语言
    settings_manager = SettingsManager()
    language = normalize_language_code(settings_manager.get("language", "en_US"))
    get_i18n().set_language(language)
    
    # 设置应用程序图标 - 这会影响任务栏图标
    app_icon = QIcon(str(APP_ICON))
    app.setWindowIcon(app_icon)
    
    # Set the persisted font before importing/constructing QFluentWidgets.
    # QFluentWidgets assigns explicit fonts in widget constructors, so the
    # helper also refreshes the completed window after MainWindow is built.
    apply_application_font(
        app,
        settings_manager.get_font_family(),
        settings_manager.get_font_size(),
    )
    
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
    if "--mcp-animation" in sys.argv:
        args = [arg for arg in sys.argv[1:] if arg != "--mcp-animation"]
        from app.mcp_server import main as run_mcp

        return run_mcp(args)
    if "--preview-process" in sys.argv:
        _configure_qt_environment()
        args = [arg for arg in sys.argv[1:] if arg != "--preview-process"]
        from app.preview_process import run_preview_process

        return run_preview_process(args)
    return run_application()

if __name__ == "__main__":
    sys.exit(main())
