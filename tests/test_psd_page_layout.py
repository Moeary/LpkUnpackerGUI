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

    def _close_page(self, page):
        page.close()
        page.deleteLater()
        self.app.processEvents()

    def test_two_columns_hold_operations_and_preview_uses_dialog(self):
        page = self._page()
        try:
            self.assertIs(page.left_scroll.widget(), page.left_panel)
            self.assertIs(page.right_scroll.widget(), page.right_panel)
            self.assertIs(page.project_frame.parentWidget(), page.left_panel)
            self.assertIs(page.preview_control_frame.parentWidget(), page.left_panel)
            self.assertIs(page.export_card.parentWidget(), page.right_panel)
            self.assertIs(page.repack_card.parentWidget(), page.right_panel)
            self.assertIs(page.reconstruct_button.parentWidget(), page.right_footer)
            self.assertIs(page.progress_bar.parentWidget(), page.right_footer)
            self.assertTrue(page.reconstruct_button.isVisible())
            self.assertGreater(page.right_footer.y(), page.right_scroll.y())
            self.assertIs(page.preview_frame.parentWidget(), page.preview_dialog)
            self.assertFalse(page.preview_dialog.isVisible())

            left_width = page.left_scroll.viewport().width()
            right_width = page.right_scroll.viewport().width()
            self.assertLessEqual(abs(left_width - right_width), max(left_width, right_width) * 0.1)
            self.assertLessEqual(page.left_panel.minimumSizeHint().width(), left_width)
            self.assertLessEqual(page.right_panel.minimumSizeHint().width(), right_width)

            page.preview_dialog.resize(820, 560)
            page.toggle_preview_panel()
            self.app.processEvents()
            self.assertTrue(page.preview_dialog.isVisible())
            self.assertGreaterEqual(page.preview_dialog.width(), 820)
            page.close_preview_panel()
            self.app.processEvents()
            self.assertFalse(page.preview_dialog.isVisible())
        finally:
            self._close_page(page)

    def test_bottom_log_can_be_dragged_when_expanded(self):
        page = self._page()
        try:
            self.assertIs(page.log_frame.parentWidget(), page.workspace_splitter)
            self.assertFalse(page._log_expanded)
            self.assertFalse(page.log_text.isVisible())
            self.assertLessEqual(page.log_frame.height(), 80)

            page.append_log("error from layout test", expand=True)
            self.app.processEvents()
            self.assertTrue(page.log_text.isVisible())
            self.assertGreaterEqual(page.log_frame.height(), 150)
            expanded_height = page.log_frame.height()
            page.workspace_splitter.setSizes([260, 280])
            self.app.processEvents()
            self.assertGreater(page.log_frame.height(), expanded_height)

            page.toggle_log_panel()
            self.app.processEvents()
            self.assertFalse(page.log_text.isVisible())
            self.assertLessEqual(page.log_frame.height(), 80)
        finally:
            self._close_page(page)

    def test_chinese_large_font_controls_fit_in_both_workflows(self):
        original_font = self.app.font()
        i18n = get_i18n()
        original_language = i18n.language
        font = QFont(original_font)
        font.setPointSize(16)
        self.app.setFont(font)
        i18n.set_language("zh_CN")
        page = self._page()
        try:
            for workflow in ("export", "repack"):
                page.set_workflow(workflow)
                page.updateUIScale(1000, 720)
                self.app.processEvents()
                self.assertLessEqual(
                    page.left_panel.minimumSizeHint().width(),
                    page.left_scroll.viewport().width(),
                )
                self.assertLessEqual(
                    page.right_panel.minimumSizeHint().width(),
                    page.right_scroll.viewport().width(),
                )
                for button in (
                    page.new_project_button,
                    page.save_project_button,
                    page.export_flow_button,
                    page.repack_flow_button,
                    page.reconstruct_button,
                    page.artmesh_inspector_button,
                    page.open_output_button,
                    page.preview_image_button,
                    page.preview_live2d_button,
                ):
                    self.assertGreaterEqual(
                        button.width(),
                        button.fontMetrics().horizontalAdvance(button.text()) + 16,
                        button.text(),
                    )
                    self.assertGreaterEqual(
                        button.height(), button.fontMetrics().lineSpacing() + 12,
                        button.text(),
                    )
        finally:
            self._close_page(page)
            self.app.setFont(original_font)
            i18n.set_language(original_language)
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
