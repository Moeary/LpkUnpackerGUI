"""Shared text styles that sit between the stock QFluentWidgets labels."""

from __future__ import annotations

from PySide6.QtGui import QFont
from qfluentwidgets import SubtitleLabel, getFont


class CardTitleLabel(SubtitleLabel):
    """Heading of a card or section: one step below the page title.

    Page titles use SubtitleLabel (20 px) and body text 14 px; card titles at
    the same 20 px made every page read flat.  16 px semibold keeps a clear
    page > card > field ladder.  It stays a SubtitleLabel so code that scales
    or finds headings by that type keeps working.
    """

    def getFont(self):  # noqa: N802
        return getFont(16, QFont.Weight.DemiBold)


__all__ = ["CardTitleLabel"]
