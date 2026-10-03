"""Desktop acceptance for settings layout and live theme changes.

Uses isolated settings and saves light/dark screenshots under runtime/validation.
"""
from pathlib import Path
import json
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication


def main():
    root = Path("runtime/validation/settings-theme").resolve()
    root.mkdir(parents=True, exist_ok=True)
    sandbox = Path(tempfile.mkdtemp(prefix="run-", dir=root))
    import app.core.settings_manager as sm
    original = sm.SettingsManager

    class DiagnosticSettings(original):
        def __init__(self, settings_file=None):
            super().__init__(settings_file or sandbox / "settings.json")

    sm.SettingsManager = DiagnosticSettings
    DiagnosticSettings().set("language", "zh_CN")
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication([])
    from app.gui.MainWindow import MainWindow
    window = MainWindow()
    window.showMaximized()
    window.switchTo(window.settingsPage)
    report = {}
    try:
        if hasattr(window.settingsPage, "font_size_spin"):
            font_sizes = []
            for size in (12, 16, 10):
                window.settingsPage.font_size_spin.setValue(size)
                until = time.monotonic() + .25
                while time.monotonic() < until:
                    app.processEvents()
                    time.sleep(.01)
                assert app.font().pointSize() == size
                assert DiagnosticSettings().get_font_size() == size
                font_sizes.append(size)
            report["font_sizes_applied"] = font_sizes
            report["font_family"] = app.font().family()
        for theme in ("light", "dark", "light"):
            window.settings_manager.set("theme", theme)
            window.apply_theme()
            until = time.monotonic() + .8
            while time.monotonic() < until:
                app.processEvents()
                time.sleep(.01)
            page = window.settingsPage
            shot = page.grab()
            shot.save(str(root / f"settings-{theme}.png"))
            available = window.screen().availableGeometry()
            assert window.height() <= available.height() + 16
            assert window.width() <= available.width() + 16
            from qfluentwidgets import isDarkTheme
            assert isDarkTheme() == (theme == "dark")
            report[theme] = {"window": [window.width(), window.height()],
                             "dpr": window.devicePixelRatioF(),
                             "screenshot": str(root / f"settings-{theme}.png")}
            window.switchTo(window.spineConverterPage)
            until = time.monotonic() + .3
            while time.monotonic() < until:
                app.processEvents()
                time.sleep(.01)
            window.spineConverterPage.grab().save(str(root / f"converter-{theme}.png"))
            window.switchTo(window.settingsPage)
        assert not hasattr(window, "spineAtlasPage")
        report["ok"] = True
    finally:
        window.close()
        app.processEvents()
        (root / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
