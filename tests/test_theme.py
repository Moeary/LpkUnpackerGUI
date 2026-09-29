from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QWidget

    import app.gui.theme as theme_module
    from app.gui.theme import apply_application_theme
except ImportError:  # pragma: no cover - the lightweight system env omits Qt.
    QApplication = None
    QWidget = None
    apply_application_theme = None
    Qt = None
    theme_module = None


@unittest.skipIf(QApplication is None, "PySide6 is optional in the lightweight test environment")
class ThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_custom_palette_styles_switch_between_light_and_dark(self):
        widget = QWidget()
        widget.setStyleSheet(
            "QWidget { background: palette(base); color: palette(text); }"
        )
        widget.resize(20, 20)
        widget.show()

        apply_application_theme("light", widget)
        self.app.processEvents()
        self.assertEqual(widget.palette().color(widget.backgroundRole()).name(), "#ffffff")
        self.assertEqual(widget.grab().toImage().pixelColor(2, 2).name(), "#ffffff")

        apply_application_theme("dark", widget)
        self.app.processEvents()
        self.assertEqual(widget.palette().color(widget.backgroundRole()).name(), "#272c34")
        self.assertEqual(widget.grab().toImage().pixelColor(2, 2).name(), "#272c34")

        widget.close()

    def test_auto_theme_uses_qt_color_scheme(self):
        style_hints = self.app.styleHints()
        if not hasattr(style_hints, "setColorScheme"):
            self.skipTest("Qt does not expose a controllable color scheme")
        if style_hints.colorScheme() == Qt.ColorScheme.Unknown:
            self.skipTest("Qt platform does not expose the system color scheme")

        previous = style_hints.colorScheme()
        try:
            style_hints.setColorScheme(Qt.ColorScheme.Dark)
            self.assertEqual(apply_application_theme("auto"), self._theme("dark"))
            style_hints.setColorScheme(Qt.ColorScheme.Light)
            self.assertEqual(apply_application_theme("auto"), self._theme("light"))
        finally:
            if previous.name != "Unknown":
                style_hints.setColorScheme(previous)

    def test_auto_theme_resolution_uses_qt_scheme_when_available(self):
        class FakeStyleHints:
            @staticmethod
            def colorScheme():
                return Qt.ColorScheme.Dark

        with patch.object(theme_module.QGuiApplication, "styleHints", return_value=FakeStyleHints()):
            self.assertEqual(apply_application_theme("auto"), self._theme("dark"))

    @staticmethod
    def _theme(value):
        from qfluentwidgets import Theme

        return Theme.DARK if value == "dark" else Theme.LIGHT
