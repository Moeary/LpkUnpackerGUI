from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from app.gui.PsdReconstructionPage import PsdReconstructionPage
    from app.i18n import get_i18n
except (ImportError, OSError):  # pragma: no cover - optional desktop dependency
    QApplication = None
    PsdReconstructionPage = None
    QFont = None
    get_i18n = None


@unittest.skipIf(
    QApplication is None,
    "PySide6 is optional in the lightweight test environment",
)
class PsdPageLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _page(self, width: int = 1000, height: int = 720):
        page = PsdReconstructionPage()
        page.resize(width, height)
        page.show()
        self.app.processEvents()
        return page

    def test_log_is_compact_bottom_panel_and_expands_on_error(self):
        page = self._page()
        try:
            self.assertIs(page.log_frame.parentWidget(), page)
            self.assertEqual(page.right_panel_layout.indexOf(page.log_frame), -1)
            self.assertFalse(page._log_expanded)
            self.assertFalse(page.log_text.isVisible())
            self.assertLessEqual(page.log_frame.height(), 80)

            page.append_log("error from layout test", expand=True)
            self.app.processEvents()
            self.assertTrue(page._log_expanded)
            self.assertTrue(page.log_text.isVisible())
            self.assertLessEqual(page.log_text.maximumHeight(), 280)

            page.toggle_log_panel()
            self.app.processEvents()
            self.assertFalse(page._log_expanded)
            self.assertFalse(page.log_text.isVisible())
        finally:
            page.close()
            page.deleteLater()
            self.app.processEvents()

    def test_sidebar_controls_fit_at_small_window_and_large_font(self):
        original_font = self.app.font()
        i18n = get_i18n()
        original_language = i18n.language
        font = QFont(original_font)
        font.setPointSize(16)
        self.app.setFont(font)
        i18n.set_language("zh_CN")
        page = self._page()
        try:
            self.assertLessEqual(
                page.left_panel.minimumSizeHint().width(),
                page.left_scroll.viewport().width(),
            )
            page.set_workflow("repack")
            self.app.processEvents()
            self.assertLessEqual(
                page.left_panel.minimumSizeHint().width(),
                page.left_scroll.viewport().width(),
            )
            for widget in (
                page.source_file_button,
                page.source_folder_button,
                page.reconstruct_button,
                page.artmesh_inspector_button,
                page.open_output_button,
                page.preview_toggle_button,
            ):
                self.assertGreater(widget.width(), 0)
                self.assertGreaterEqual(
                    widget.maximumHeight(), widget.fontMetrics().lineSpacing() + 12
                )
            for widget in (
                page.export_name_edit,
                page.mode_combo,
                page.output_edit,
                page.output_button,
                page.preview_image_button,
                page.preview_live2d_button,
                page.preview_close_button,
            ):
                self.assertLessEqual(
                    widget.geometry().right(), widget.parentWidget().width()
                )
            for combo in (page.export_name_edit, page.mode_combo):
                self.assertGreaterEqual(
                    combo.width(), combo.fontMetrics().horizontalAdvance(combo.currentText())
                )
        finally:
            page.close()
            page.deleteLater()
            self.app.setFont(original_font)
            i18n.set_language(original_language)
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
