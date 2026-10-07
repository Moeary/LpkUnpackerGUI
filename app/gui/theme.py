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
from qfluentwidgets import Theme, ThemeColor, isDarkTheme, setTheme


# Surfaces follow FluentWindow's own backgrounds: the light window is a cool
# #f0f4f9 under a half-white page, the dark window a neutral #202020 under a
# #272727 page.  "base" matches a Fluent CardWidget drawn on that page, and
# "alternate_base" is a recessed well (canvas, log, splitter handle).
_LIGHT_COLORS = {
    "window": "#f7f9fc",
    "window_text": "#1f2328",
    "base": "#ffffff",
    "alternate_base": "#f1f4f8",
    "text": "#1f2328",
    "button": "#ffffff",
    "button_text": "#1f2328",
    "mid": "#c7d0dc",
    "border": "#e1e6ed",
    "highlighted_text": "#ffffff",
    "placeholder_text": "#68707d",
    "link": "#0078d4",
    "danger": "#c42b1c",
}

_DARK_COLORS = {
    "window": "#272727",
    "window_text": "#f3f3f3",
    "base": "#323232",
    "alternate_base": "#2b2b2b",
    "text": "#f3f3f3",
    "button": "#3b3b3b",
    "button_text": "#f3f3f3",
    "mid": "#5a5a5a",
    "border": "#3d3d3d",
    "highlighted_text": "#000000",
    "placeholder_text": "#a0a0a0",
    "link": "#6ec8ff",
    "danger": "#ff99a4",
}


_DARK_ACCENT_SATURATION = 0.8
_DARK_ACCENT_VALUE = 0.82
_DARK_ACCENT_SHADES = {
    # (saturation factor, value factor) relative to the dark primary.
    ThemeColor.DARK_1: (1.0, 0.9),
    ThemeColor.DARK_2: (0.977, 0.82),
    ThemeColor.DARK_3: (0.95, 0.7),
    ThemeColor.LIGHT_1: (0.92, 1.07),
    ThemeColor.LIGHT_2: (0.78, 1.14),
    ThemeColor.LIGHT_3: (0.65, 1.2),
}
_stock_theme_color = getattr(ThemeColor.color, "_lpk_stock", ThemeColor.color)


def _fluent_theme_color(self: ThemeColor) -> QColor:
    """QFluentWidgets' accent shades, with a calmer dark-mode primary.

    The stock dark transform forces full brightness, which turns the teal
    accent into a neon #29f1ff behind black text.  Light mode is unchanged;
    dark mode keeps the stock hover/pressed ordering around a mid teal.
    """

    if not isDarkTheme():
        return _stock_theme_color(self)
    from qfluentwidgets import qconfig

    hue, saturation, _value, _alpha = QColor(qconfig.get(qconfig._cfg.themeColor)).getHsvF()
    s_factor, v_factor = _DARK_ACCENT_SHADES.get(self, (1.0, 1.0))
    return QColor.fromHsvF(
        hue,
        min(saturation * _DARK_ACCENT_SATURATION * s_factor, 1),
        min(_DARK_ACCENT_VALUE * v_factor, 1),
    )


_fluent_theme_color._lpk_stock = _stock_theme_color
ThemeColor.color = _fluent_theme_color


def _current_or(theme: str | Theme | None) -> Theme:
    if theme is None:
        return Theme.DARK if isDarkTheme() else Theme.LIGHT
    return normalize_theme(theme)


def accent_color(theme: str | Theme | None = None) -> QColor:
    """Return the primary accent the Fluent controls use for ``theme``."""

    from qfluentwidgets import qconfig

    color = QColor(qconfig.get(qconfig._cfg.themeColor))
    if _current_or(theme) is Theme.DARK:
        hue, saturation, _value, _alpha = color.getHsvF()
        color = QColor.fromHsvF(hue, saturation * _DARK_ACCENT_SATURATION, _DARK_ACCENT_VALUE)
    return color


def theme_token(name: str, theme: str | Theme | None = None) -> QColor:
    """Return a named surface/text token (see the colour tables) for painting."""

    selected = _current_or(theme)
    if name == "highlight":
        return accent_color(selected)
    colors = _DARK_COLORS if selected is Theme.DARK else _LIGHT_COLORS
    return QColor(colors[name])


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
        QPalette.ColorRole.HighlightedText: "highlighted_text",
        QPalette.ColorRole.Link: "link",
        QPalette.ColorRole.Mid: "mid",
        QPalette.ColorRole.PlaceholderText: "placeholder_text",
    }
    for role, key in roles.items():
        _set_color(palette, role, colors[key])
    # Custom QSS uses palette(highlight); keep it identical to the accent that
    # QFluentWidgets paints its own primary controls with.
    palette.setColor(QPalette.ColorRole.Highlight, accent_color(selected))

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


def transparent_scroll_area(scroll) -> None:
    """Let a scroll area show the surface it sits on.

    QScrollArea.setWidget() turns on autoFillBackground for its content, and
    Fluent's enableTransparentBackground() only clears the frame itself, so a
    scroll area on a card otherwise paints a page-coloured (palette window)
    block.  Call this after setWidget().
    """

    enable = getattr(scroll, "enableTransparentBackground", None)
    if callable(enable):
        enable()
    else:
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
    scroll.viewport().setAutoFillBackground(False)
    content = scroll.widget()
    if content is not None:
        content.setAutoFillBackground(False)


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
    "accent_color",
    "apply_application_theme",
    "normalize_theme",
    "palette_for_theme",
    "theme_token",
    "transparent_scroll_area",
]
