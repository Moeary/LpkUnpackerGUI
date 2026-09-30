from __future__ import annotations

import importlib
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog
from qfluentwidgets import ComboBox, ListWidget, PlainTextEdit, SpinBox

from app.core.mcp_launch import animation_mcp_endpoint
from app.core.settings_manager import SettingsManager


class MCPSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mcp-qt-lifecycle-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.settings_file = self.base / "settings.json"
        self.workspace = self.base / "outputs"
        self.workspace.mkdir()
        settings_file = self.settings_file

        class TempSettings(SettingsManager):
            def __init__(self, *args, **kwargs):
                super().__init__(settings_file)

        self.module = importlib.import_module("app.gui.SettingsPage")
        with patch.object(self.module, "SettingsManager", TempSettings):
            self.page = self.module.SettingsPage()
        self.addCleanup(self._dispose)
        self.page.mcp_workspace_edit.setText(str(self.workspace))
        self.page.select_settings_category("ai")
        self.page.resize(1040, 760)
        self.page.show()
        self.app.processEvents()

    def _dispose(self):
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()

    def _wait(self, condition):
        deadline = time.monotonic() + 12
        while not condition() and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertTrue(condition(), self.page.mcp_log.toPlainText())

    def test_transport_widgets_and_persisted_port_match_connection(self):
        page = self.page
        self.assertIsInstance(page.category_list, ListWidget)
        self.assertIsInstance(page.mcp_transport_combo, ComboBox)
        self.assertIsInstance(page.mcp_port_spin, SpinBox)
        page.mcp_port_spin.setValue(8933)
        self.assertEqual(page.mcp_endpoint_edit.text(), animation_mcp_endpoint(8933))
        self.assertEqual(SettingsManager(self.settings_file).get("mcp.port"), 8933)
        page.mcp_transport_combo.setCurrentIndex(page._mcp_transport_values.index("stdio"))
        self.assertTrue(page.mcp_http_options.isHidden())
        self.assertTrue(page.mcp_start_button.isHidden())
        self.assertTrue(page.mcp_endpoint_row.isHidden())
        self.assertFalse(page.mcp_stdio_hint.isHidden())
        self.assertFalse(page.start_animation_mcp())
        self.assertEqual(SettingsManager(self.settings_file).get("mcp.transport"), "stdio")

    def test_application_owned_http_process_stops_when_settings_window_closes(self):
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        self.page.mcp_port_spin.setValue(port)
        self.assertTrue(self.page.start_animation_mcp())
        self._wait(lambda: self.page._mcp_status_key == "settings.mcp.running")
        self.assertFalse(self.page.mcp_port_spin.isEnabled())
        self.assertTrue(self.page.mcp_stop_button.isEnabled())
        self.assertTrue(self.page.close())
        self.assertIsNone(self.page._mcp_process)
        with socket.socket() as released:
            released.bind(("127.0.0.1", port))

    def test_occupied_port_returns_readable_error_and_allows_retry(self):
        with socket.socket() as occupying:
            occupying.bind(("127.0.0.1", 0))
            occupying.listen()
            port = occupying.getsockname()[1]
            self.page.mcp_port_spin.setValue(port)
            self.assertTrue(self.page.start_animation_mcp())
            self._wait(lambda: self.page._mcp_process is None)
        self.assertEqual(self.page._mcp_status_key, "settings.mcp.failed")
        self.assertIn(f"127.0.0.1:{port}", self.page.mcp_status.text())
        self.assertIn("port is in use", self.page.mcp_status.text())
        self.assertTrue(self.page.mcp_port_spin.isEnabled())
        self.assertTrue(self.page.mcp_start_button.isEnabled())

    def test_guide_view_copy_and_save_preserve_actual_markdown(self):
        self.page.mcp_port_spin.setValue(8934)
        guide = self.page._animation_mcp_guide_text()
        self.assertIn(animation_mcp_endpoint(8934), guide)
        self.assertNotIn("{{", guide)
        self.page.copy_animation_mcp_guide()
        self.assertEqual(QApplication.clipboard().text(), guide)
        output = self.base / "portable-ai-guide.md"
        with patch.object(self.module.QFileDialog, "getSaveFileName", return_value=(str(output), "")):
            self.page.save_animation_mcp_guide()
        self.assertEqual(output.read_bytes(), guide.encode("utf-8"))
        viewed = []

        def close_guide_dialog():
            dialog = self.page.findChild(QDialog)
            if dialog is not None:
                editor = dialog.findChild(PlainTextEdit)
                viewed.append((editor.isReadOnly(), editor.toPlainText()))
                dialog.accept()

        QTimer.singleShot(0, self.page, close_guide_dialog)
        self.page.show_animation_mcp_guide()
        self.assertEqual(viewed, [(True, guide)])


if __name__ == "__main__":
    unittest.main()
