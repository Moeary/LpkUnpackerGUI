"""Keyboard zoom shared by every preview surface.

Ctrl+= (or Ctrl++) zooms in, Ctrl+- zooms out and Ctrl+0 returns to the
starting view.  Mouse zoom is Ctrl+wheel and panning is a middle-button drag;
the editors route the same keys through their editable shortcut table.
"""
from __future__ import annotations

from PySide6.QtCore import Qt

ZOOM_STEP = 1.25

_KEYS = {
    Qt.Key.Key_Equal: "in", Qt.Key.Key_Plus: "in",
    Qt.Key.Key_Minus: "out", Qt.Key.Key_Underscore: "out",
    Qt.Key.Key_0: "reset",
}


def zoom_key_action(event) -> str | None:
    """Return "in", "out" or "reset" for a view-zoom key event, else None."""
    # Shift is ignored so Ctrl++ (Shift+= on most layouts) works; the
    # keypad modifier is ignored so the numeric keypad works too.
    # Integer masks: PySide6's ~ on a Qt flag does not reliably clear it.
    ignored = Qt.KeyboardModifier.ShiftModifier.value | Qt.KeyboardModifier.KeypadModifier.value
    if event.modifiers().value & ~ignored != Qt.KeyboardModifier.ControlModifier.value:
        return None
    return _KEYS.get(Qt.Key(event.key()))
