"""Application theme helpers shared by the Qt pages.

QFluentWidgets updates its own widget style sheets when the theme changes, but
it does not install a matching :class:`QPalette`.  The custom panels in this
application use palette roles in their style sheets, so keeping the palette in
sync is what makes those panels follow the same light/dark setting.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget
from qfluentwidgets import Theme, setTheme


_LIGHT_COLORS = {
    "window": "#f7f9fc",
    "window_text": "#1f2328",
    "base": "#ffffff",
    "alternate_base": "#f1f4f8",
    "text": "#1f2328",
    "button": "#ffffff",
    "button_text": "#1f2328",
    "mid": "#c7d0dc",
    "highlight": "#00a6b3",
    "highlighted_text": "#ffffff",
    "placeholder_text": "#68707d",
    "link": "#0078d4",
}

_DARK_COLORS = {
    "window": "#20242b",
    "window_text": "#f2f4f7",
    "base": "#272c34",
    "alternate_base": "#303641",
    "text": "#f2f4f7",
    "button": "#303641",
    "button_text": "#f2f4f7",
    "mid": "#596575",
    "highlight": "#00a6b3",
    "highlighted_text": "#ffffff",
    "placeholder_text": "#a4afbf",
    "link": "#6ec8ff",
}


def normalize_theme(theme: str | Theme | None) -> Theme:
    """Return a concrete QFluentWidgets theme for a persisted setting."""

    if isinstance(theme, Theme):
        if theme in (Theme.LIGHT, Theme.DARK):
            return theme
        return _system_theme()
    value = str(theme or "").strip().lower()
    if value == "dark":
        return Theme.DARK
    if value == "auto":
        return _system_theme()
    return Theme.LIGHT


def _system_theme() -> Theme:
    """Resolve the OS appearance using Qt's native color scheme."""

    app = QGuiApplication.instance()
    if app is not None:
        try:
            scheme = app.styleHints().colorScheme()
            if scheme == Qt.ColorScheme.Dark:
                return Theme.DARK
            if scheme == Qt.ColorScheme.Light:
                return Theme.LIGHT
        except (AttributeError, RuntimeError):
            pass

    # Qt 6.6 on some older Windows configurations reports Unknown.  Keep the
    # same fallback used by QFluentWidgets in that case when available.
    try:
        import darkdetect

        return Theme.DARK if str(darkdetect.theme() or "").lower() == "dark" else Theme.LIGHT
    except Exception:
        return Theme.LIGHT


def _set_color(palette: QPalette, role: QPalette.ColorRole, value: str) -> None:
    palette.setColor(role, QColor(value))


def palette_for_theme(theme: str | Theme | None) -> QPalette:
    """Build the application palette used by custom page controls."""

    selected = normalize_theme(theme)
    colors = _DARK_COLORS if selected is Theme.DARK else _LIGHT_COLORS
    palette = QPalette()

    roles = {
        QPalette.ColorRole.Window: "window",
        QPalette.ColorRole.WindowText: "window_text",
        QPalette.ColorRole.Base: "base",
        QPalette.ColorRole.AlternateBase: "alternate_base",
        QPalette.ColorRole.ToolTipBase: "base",
        QPalette.ColorRole.ToolTipText: "text",
        QPalette.ColorRole.Text: "text",
        QPalette.ColorRole.Button: "button",
        QPalette.ColorRole.ButtonText: "button_text",
        QPalette.ColorRole.BrightText: "highlighted_text",
        QPalette.ColorRole.Highlight: "highlight",
        QPalette.ColorRole.HighlightedText: "highlighted_text",
        QPalette.ColorRole.Link: "link",
        QPalette.ColorRole.Mid: "mid",
        QPalette.ColorRole.PlaceholderText: "placeholder_text",
    }
    for role, key in roles.items():
        _set_color(palette, role, colors[key])

    # Disabled text and controls need a deliberate contrast in dark mode.  A
    # separate disabled group keeps qfluent controls readable without forcing
    # every page to add its own disabled colour rule.
    disabled_text = QColor(colors["placeholder_text"])
    disabled_button = QColor(colors["alternate_base"])
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled_text)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled_text)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled_text)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button, disabled_button)
    return palette


def _repolish(widget: QWidget) -> None:
    """Re-evaluate palette(...) expressions in custom style sheets."""

    # Calling QStyle.unpolish/polish recursively is unsafe for native and
    # QFluentWidgets-owned children on Windows.  Reassigning only a widget's
    # own stylesheet asks Qt to re-evaluate palette(...) without taking over
    # the lifetime of the style object.  Widgets without custom QSS already
    # receive the QApplication palette change and QFluentWidgets refresh.
    for target in (widget, *widget.findChildren(QWidget)):
        sheet = target.styleSheet()
        if sheet:
            target.setStyleSheet(sheet)
        target.update()


def apply_application_theme(theme: str | Theme | None, root: QWidget | None = None) -> Theme:
    """Apply a QFluentWidgets theme and matching Qt palette.

    ``root`` is normally the main window.  Passing it also refreshes existing
    custom widgets after a setting is changed while the application is open.
    """

    selected = normalize_theme(theme)
    app = QApplication.instance()
    if app is None:
        return selected

    # Install the palette before QFluentWidgets repaints its controls.  The
    # second step below refreshes custom style sheets that were already set
    # during page construction.
    app.setPalette(palette_for_theme(selected))
    setTheme(selected)
    if root is not None:
        _repolish(root)
        auto_requested = theme is Theme.AUTO or str(theme or "").strip().lower() == "auto"
        _sync_system_theme_listener(root, auto_requested)
    return selected


def _sync_system_theme_listener(root: QWidget, enabled: bool) -> None:
    """Refresh an open main window when Windows changes its color scheme."""

    try:
        signal = QGuiApplication.styleHints().colorSchemeChanged
    except (AttributeError, RuntimeError):
        return

    slot = getattr(root, "_theme_auto_slot", None)
    connected = bool(getattr(root, "_theme_auto_connected", False))
    if enabled and not connected:
        slot = lambda *_args: apply_application_theme("auto", root)
        root._theme_auto_slot = slot
        signal.connect(slot)
        root._theme_auto_connected = True
    elif not enabled and connected:
        try:
            signal.disconnect(slot)
        except (TypeError, RuntimeError):
            pass
        root._theme_auto_connected = False


__all__ = [
    "apply_application_theme",
    "normalize_theme",
    "palette_for_theme",
]
