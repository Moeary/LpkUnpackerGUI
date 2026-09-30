from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.font_helper import (
    DEFAULT_FONT_SIZE,
    MAX_FONT_SIZE,
    MIN_FONT_SIZE,
    apply_application_font,
    clamp_font_size,
    recommended_font_family,
)
from app.core.settings_manager import SettingsManager


class FontPreferencePersistenceTests(unittest.TestCase):
    def test_settings_round_trip_and_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            manager = SettingsManager(settings_path)

            self.assertEqual(manager.get_font_family(), "")
            self.assertEqual(manager.get_font_size(), DEFAULT_FONT_SIZE)

            manager.set_font_preferences("Noto Sans CJK SC", 17)
            saved = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["font"]["family"], "Noto Sans CJK SC")
            self.assertEqual(saved["font"]["size"], 17)

            restored = SettingsManager(settings_path)
            self.assertEqual(restored.get_font_family(), "Noto Sans CJK SC")
            self.assertEqual(restored.get_font_size(), 17)

            restored.reset_font_preferences()
            self.assertEqual(restored.get_font_family(), "")
            self.assertEqual(restored.get_font_size(), DEFAULT_FONT_SIZE)

    def test_size_is_clamped(self):
        self.assertEqual(clamp_font_size("not-a-number"), DEFAULT_FONT_SIZE)
        self.assertEqual(clamp_font_size(float("inf")), DEFAULT_FONT_SIZE)
        self.assertEqual(clamp_font_size(MIN_FONT_SIZE - 4), MIN_FONT_SIZE)
        self.assertEqual(clamp_font_size(MAX_FONT_SIZE + 4), MAX_FONT_SIZE)

    def test_recommended_family_prefers_cjk_candidates(self):
        self.assertEqual(
            recommended_font_family(
                ["Arial", "Microsoft YaHei", "Noto Sans CJK SC"]
            ),
            "Microsoft YaHei",
        )


try:
    from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget
    from qfluentwidgets import BodyLabel, PushButton, SubtitleLabel
except (ImportError, OSError):  # pragma: no cover - optional desktop dependency
    QApplication = None


@unittest.skipIf(QApplication is None, "PySide6 is optional in the lightweight test environment")
class LiveFontApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_live_apply_preserves_hierarchy_and_does_not_compound(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        body = BodyLabel("body", root)
        subtitle = SubtitleLabel("subtitle", root)
        button = PushButton("button", root)
        plain = QLabel("plain", root)
        for widget in (body, subtitle, button, plain):
            layout.addWidget(widget)

        apply_application_font(self.app, "", 16, root=root)
        body_at_16 = body.font().pointSizeF()
        subtitle_at_16 = subtitle.font().pointSizeF()
        plain_at_16 = plain.font().pointSizeF()
        self.assertGreater(body_at_16, 0)
        self.assertGreater(subtitle_at_16, body_at_16)
        self.assertAlmostEqual(plain_at_16, 16, delta=0.5)

        apply_application_font(self.app, "", 8, root=root)
        body_at_8 = body.font().pointSizeF()
        subtitle_at_8 = subtitle.font().pointSizeF()
        self.assertAlmostEqual(body_at_8, body_at_16 / 2, delta=0.5)
        self.assertAlmostEqual(subtitle_at_8, subtitle_at_16 / 2, delta=0.5)

        # A QFluentWidgets control created after the live change receives the
        # same family and base scale through the application event filter.
        new_plain = QLabel("new", root)
        layout.addWidget(new_plain)
        self.app.processEvents()
        self.assertAlmostEqual(new_plain.font().pointSizeF(), 8, delta=0.5)

        root.close()
        root.deleteLater()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
