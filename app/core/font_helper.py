"""Application font selection and live application helpers.

Qt's application font is not enough for this project because QFluentWidgets
assigns an explicit font to many of its controls during construction.  This
module keeps the application font and those explicit widget fonts in sync
while changing only the family and the requested base size.  Existing font
weights and the size hierarchy used by QFluentWidgets are retained.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QPlainTextEdit,
    QTextEdit,
    QWidget,
)

logger = logging.getLogger("ApplicationFont")

DEFAULT_FONT_SIZE = 10
MIN_FONT_SIZE = 8
MAX_FONT_SIZE = 32

# The order is intentional: these families are common on Windows, macOS and
# Linux and provide broad Simplified Chinese/Japanese glyph coverage.  The
# first installed candidate becomes the automatic default; the application
# still falls back to Qt's system font when none of these is installed.
SENSIBLE_CJK_FONT_FAMILIES = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "Noto Sans CJK SC",
    "Noto Sans CJK JP",
    "Source Han Sans SC",
    "Source Han Sans CN",
    "PingFang SC",
    "Yu Gothic UI",
    "Meiryo UI",
    "WenQuanYi Zen Hei",
)

_BASE_SIZE_PROPERTY = "_lpk_font_base_point_size"
_CODE_FONT_PROPERTY = "lpkCodeFont"
_FONT_FILTER_PROPERTY = "lpkFontExcluded"


def clamp_font_size(value: object, default: int = DEFAULT_FONT_SIZE) -> int:
    """Return a safe integer point size for settings and Qt widgets."""

    try:
        size = int(float(value))
    except (TypeError, ValueError, OverflowError):
        size = int(default)
    return max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, size))


def installed_font_families() -> list[str]:
    """Return the sorted installed font families when Qt is available."""

    try:
        families = {str(family).strip() for family in QFontDatabase.families()}
    except Exception:
        return []
    return sorted((family for family in families if family), key=str.casefold)


def recommended_font_family(
    families: Optional[Iterable[str]] = None,
    fallback: str = "",
) -> str:
    """Choose an installed font with broad CJK coverage.

    ``families`` is injectable so settings and unit tests can resolve the
    default without depending on the fonts installed on the test machine.
    """

    available = list(families) if families is not None else installed_font_families()
    by_key = {str(family).casefold(): str(family) for family in available if str(family).strip()}
    for candidate in SENSIBLE_CJK_FONT_FAMILIES:
        match = by_key.get(candidate.casefold())
        if match:
            return match

    if fallback:
        fallback_key = str(fallback).casefold()
        if fallback_key in by_key:
            return by_key[fallback_key]
        return str(fallback).strip()

    try:
        system_family = QFontDatabase.systemFont(
            QFontDatabase.SystemFont.GeneralFont
        ).family()
    except Exception:
        system_family = ""
    return str(system_family or "").strip()


def resolve_font_family(family: object = "") -> str:
    """Resolve a persisted family, treating an empty value as automatic."""

    requested = str(family or "").strip()
    families = installed_font_families()
    if not requested:
        return recommended_font_family(families)

    by_key = {name.casefold(): name for name in families}
    # A font removed after it was selected should resolve back to the same
    # automatic CJK-aware choice as the reset action.  Passing the stale name
    # through would make Qt silently perform the very fallback this setting is
    # intended to reduce.
    return by_key.get(requested.casefold(), recommended_font_family(families))


def _font_point_size(font: QFont) -> float:
    point_size = float(font.pointSizeF())
    if point_size > 0:
        return point_size

    # Pixel-only fonts are used by QFluentWidgets.  Keep a stable conversion
    # for the normal 96-DPI case and let Qt scale the resulting point size on
    # high-DPI displays.
    pixel_size = float(font.pixelSize())
    if pixel_size > 0:
        return pixel_size * 72.0 / 96.0
    return float(DEFAULT_FONT_SIZE)


def _font_matches(left: QFont, right: QFont) -> bool:
    """Compare the inherited portions of two fonts without DPI assumptions."""

    return (
        left.families() == right.families()
        and abs(_font_point_size(left) - _font_point_size(right)) < 0.05
        and left.weight() == right.weight()
        and left.italic() == right.italic()
    )


def _is_icon_font(font: QFont) -> bool:
    """Identify common icon font names that must never be replaced."""

    for family in font.families():
        key = str(family).casefold().replace(" ", "")
        if (
            "fluent" in key and "icon" in key
            or "systemicons" in key
            or "mdl2assets" in key
            or "fontawesome" in key
        ):
            return True
    return False


def _should_exclude_widget(widget: QWidget) -> bool:
    if bool(widget.property(_FONT_FILTER_PROPERTY)):
        return True
    if bool(widget.property(_CODE_FONT_PROPERTY)):
        return True
    if _is_icon_font(widget.font()):
        return True

    # Preserve explicitly fixed-pitch editors (for example an integrator's
    # code/log widget).  Ordinary text editors inherit the selected family.
    if isinstance(widget, (QPlainTextEdit, QTextEdit)) and widget.font().fixedPitch():
        return True
    return False


def mark_font_excluded(widget: QWidget, excluded: bool = True) -> None:
    """Mark a widget whose font family belongs to an independent subsystem."""

    widget.setProperty(_FONT_FILTER_PROPERTY, bool(excluded))


def _apply_widget_font(
    widget: QWidget,
    family: str,
    size: int,
    inherited_font: Optional[QFont] = None,
) -> bool:
    if _should_exclude_widget(widget):
        return False

    font = widget.font()
    base_size = widget.property(_BASE_SIZE_PROPERTY)
    try:
        base_size = float(base_size)
    except (TypeError, ValueError, OverflowError):
        # A widget that simply inherits QApplication.font() has no independent
        # hierarchy size.  Treat it as the 10-point base even when the current
        # application font was previously set to 14/16 points.
        if inherited_font is not None and _font_matches(font, inherited_font):
            base_size = float(DEFAULT_FONT_SIZE)
        else:
            base_size = _font_point_size(font)
        widget.setProperty(_BASE_SIZE_PROPERTY, base_size)

    # Preserve the widget's relative hierarchy (caption/body/subtitle) while
    # allowing the user-controlled application size to scale the hierarchy.
    scaled_size = max(1.0, base_size * size / DEFAULT_FONT_SIZE)
    font.setFamilies([family])
    font.setPointSizeF(scaled_size)
    widget.setFont(font)
    return True


class _FontApplicationFilter(QObject):
    """Apply the current family to QFluentWidgets created after startup."""

    def __init__(self, application: QApplication, family: str, size: int):
        super().__init__(application)
        self.application = application
        self.family = family
        self.size = size

    def update(self, family: str, size: int) -> None:
        self.family = family
        self.size = size

    def eventFilter(self, watched, event):  # noqa: N802 - Qt virtual method
        if event.type() in (QEvent.Type.Polish, QEvent.Type.Show):
            if isinstance(watched, QWidget):
                _apply_widget_font(
                    watched,
                    self.family,
                    self.size,
                    self.application.font(),
                )
        return super().eventFilter(watched, event)


def _font_filter(application: QApplication) -> _FontApplicationFilter:
    existing = getattr(application, "_lpk_font_filter", None)
    if isinstance(existing, _FontApplicationFilter):
        return existing
    created = _FontApplicationFilter(application, "", DEFAULT_FONT_SIZE)
    application._lpk_font_filter = created
    application.installEventFilter(created)
    return created


def _configure_qfluent_font_family(family: str) -> None:
    """Keep QFluentWidgets' explicit fonts aligned for future widgets."""

    try:
        from qfluentwidgets import setFontFamilies

        # Do not persist qfluent's own config file; the project's settings
        # manager is the single source of truth for this preference.
        setFontFamilies([family], save=False)
    except Exception as exc:  # pragma: no cover - qfluent is optional in CI
        logger.debug("Unable to configure QFluentWidgets font family: %s", exc)


def apply_application_font(
    application: Optional[QApplication] = None,
    family: object = "",
    size: object = DEFAULT_FONT_SIZE,
    root: Optional[QWidget] = None,
) -> tuple[str, int]:
    """Apply a family and size to the application and existing text widgets.

    The returned tuple contains the resolved installed family and normalized
    point size.  ``root`` may limit the recursive refresh to one window; the
    application font and QFluentWidgets' future-widget defaults are always
    updated globally.
    """

    app = application or QApplication.instance()
    normalized_size = clamp_font_size(size)
    resolved_family = resolve_font_family(family)
    if not resolved_family:
        # A QApplication may be unavailable in a lightweight import test.  A
        # family-less QFont still gives Qt a valid platform default later.
        resolved_family = str(family or "").strip()

    if app is None:
        return resolved_family, normalized_size

    if root is None:
        widgets = list(app.allWidgets())
    else:
        widgets = [root]
        widgets.extend(root.findChildren(QWidget))

    # Capture baselines before QApplication.setFont() updates inherited
    # widgets.  Without this step, a second live change would scale from the
    # previous user value and quickly compound the requested size.
    previous_app_font = app.font()
    for widget in widgets:
        if widget.property(_BASE_SIZE_PROPERTY) is None:
            current = widget.font()
            baseline = (
                float(DEFAULT_FONT_SIZE)
                if _font_matches(current, previous_app_font)
                else _font_point_size(current)
            )
            widget.setProperty(_BASE_SIZE_PROPERTY, baseline)

    app_font = app.font()
    app_font.setFamilies([resolved_family] if resolved_family else [])
    app_font.setPointSize(normalized_size)
    app.setFont(app_font)
    _configure_qfluent_font_family(resolved_family)

    font_filter = _font_filter(app)
    font_filter.update(resolved_family, normalized_size)

    for widget in widgets:
        _apply_widget_font(widget, resolved_family, normalized_size, previous_app_font)
    return resolved_family, normalized_size


__all__ = [
    "DEFAULT_FONT_SIZE",
    "MAX_FONT_SIZE",
    "MIN_FONT_SIZE",
    "SENSIBLE_CJK_FONT_FAMILIES",
    "apply_application_font",
    "clamp_font_size",
    "installed_font_families",
    "mark_font_excluded",
    "recommended_font_family",
    "resolve_font_family",
]
