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
        self.assertEqual(widget.palette().color(widget.backgroundRole()).name(), "#323232")
        self.assertEqual(widget.grab().toImage().pixelColor(2, 2).name(), "#323232")

        widget.close()

    def test_native_tool_tips_follow_the_theme(self):
        from PySide6.QtGui import QPalette
        from PySide6.QtWidgets import QToolTip

        try:
            apply_application_theme("dark")
            palette = QToolTip.palette()
            self.assertEqual(palette.color(QPalette.ColorRole.ToolTipBase).name(), "#323232")
            self.assertEqual(palette.color(QPalette.ColorRole.ToolTipText).name(), "#f3f3f3")
            apply_application_theme("light")
            self.assertEqual(QToolTip.palette().color(QPalette.ColorRole.ToolTipBase).name(), "#ffffff")
        finally:
            apply_application_theme("light")

    def test_image_preview_uses_fluent_scroll_bars_and_middle_drag_pans(self):
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QMouseEvent, QPixmap
        from qfluentwidgets import ScrollBar
        from app.gui.ImagePreviewPanel import ImageZoomScrollArea

        area = ImageZoomScrollArea()
        area.resize(200, 150)
        label = QWidget()
        label.resize(800, 600)
        area.setWidget(label)
        area.show()
        self.app.processEvents()
        self.assertEqual(area.horizontalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        bars = area.findChildren(ScrollBar)
        self.assertEqual(len(bars), 2)
        self.assertTrue(all(bar.isVisible() for bar in bars))

        def mouse(kind, x, y):
            button = Qt.MouseButton.MiddleButton
            buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else button
            return QMouseEvent(kind, QPointF(x, y), QPointF(x, y), button, buttons, Qt.KeyboardModifier.NoModifier)

        area.horizontalScrollBar().setValue(100)
        area.verticalScrollBar().setValue(100)
        area.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, 100, 80))
        area.mouseMoveEvent(mouse(QEvent.Type.MouseMove, 70, 50))
        area.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, 70, 50))
        self.assertEqual((area.horizontalScrollBar().value(), area.verticalScrollBar().value()), (130, 130))

        from PySide6.QtGui import QKeyEvent
        zooms, fits = [], []
        area.zoomRequested.connect(lambda factor, _point: zooms.append(round(factor, 3)))
        area.fitRequested.connect(lambda: fits.append(True))
        for key in (Qt.Key.Key_Equal, Qt.Key.Key_Minus, Qt.Key.Key_0):
            area.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.ControlModifier))
        self.assertEqual((zooms, fits), ([1.25, 0.8], [True]))
        area.close()

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
