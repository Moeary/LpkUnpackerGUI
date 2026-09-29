from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication

    import app.gui.SpineConverterPage as converter_page_module
    from app.gui.SpineConverterPage import SpineConverterPage
    from app.core.settings_manager import SettingsManager
except (ImportError, OSError):  # pragma: no cover - optional desktop dependency
    QApplication = None
    QCoreApplication = None
    QEvent = None
    converter_page_module = None
    SpineConverterPage = None


@unittest.skipIf(QApplication is None, "PySide6 is optional in the lightweight test environment")
class SpineConverterPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _wait(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("timed out waiting for Spine converter worker")

    def _dispose(self, page):
        if page is None:
            return
        if page.is_conversion_running():
            self.fail("converter worker was still running during test cleanup")
        page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def test_conversion_passes_controls_and_exposes_result_actions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "skeleton.json"
            output = root / "converted"
            source.write_text("{}", encoding="utf-8")
            output.mkdir()
            result = SimpleNamespace(
                output_dir=output,
                skeleton_path=output / "skeleton.json",
                report_path=output / "report.json",
                warnings=["curve warning"],
            )
            calls = []

            def fake_convert(*args, **kwargs):
                calls.append((args, kwargs))
                return result

            with patch.object(
                converter_page_module,
                "discover_converter",
                return_value=Path("C:/tools/SpineSkeletonDataConverter.exe"),
            ), patch.object(converter_page_module, "convert_spine", side_effect=fake_convert):
                page = SpineConverterPage(settings_manager=SettingsManager(root / "settings.json"))
                try:
                    self.assertEqual(page.target_version_combo.currentText(), "3.8.75")
                    page.source_edit.setText(str(source))
                    page.output_edit.setText(str(output))
                    page.target_version_combo.setCurrentText("3.8.75")
                    page.output_format_combo.setCurrentText("skel")
                    page.remove_curve_checkbox.setChecked(True)
                    previewed = []
                    page.previewRequested.connect(previewed.append)
                    page.start_conversion()
                    self._wait(lambda: page._status_state == "success" and page._worker is None)

                    self.assertEqual(len(calls), 1)
                    args, kwargs = calls[0]
                    self.assertEqual(args, (str(source), str(output)))
                    self.assertEqual(kwargs["target_version"], "3.8.75")
                    self.assertEqual(kwargs["output_format"], "skel")
                    self.assertEqual(
                        kwargs["converter_path"],
                        str(Path("C:/tools/SpineSkeletonDataConverter.exe")),
                    )
                    self.assertTrue(kwargs["remove_curve"])
                    self.assertTrue(page.open_output_button.isEnabled())
                    self.assertTrue(page.preview_button.isEnabled())
                    page.preview_result()
                    self.assertEqual(previewed, [str(output / "skeleton.json")])
                    self.assertIn("curve warning", page.result_label.toPlainText())
                finally:
                    self._dispose(page)

    def test_close_does_not_destroy_running_conversion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "skeleton.skel"
            output = root / "converted"
            source.write_bytes(b"skeleton")
            output.mkdir()
            started = threading.Event()
            release = threading.Event()
            result = SimpleNamespace(
                output_dir=output,
                skeleton_path=output / "skeleton.skel",
                report_path="",
                warnings=[],
            )

            def slow_convert(*_args, **_kwargs):
                started.set()
                release.wait(3.0)
                return result

            with patch.object(converter_page_module, "discover_converter", return_value=None), \
                    patch.object(converter_page_module, "convert_spine", side_effect=slow_convert):
                page = SpineConverterPage()
                try:
                    page.source_edit.setText(str(source))
                    page.output_edit.setText(str(output))
                    page.show()
                    page.start_conversion()
                    self._wait(started.is_set)
                    worker = page._worker
                    page.close()
                    self.assertIs(page._worker, worker)
                    self.assertTrue(page.is_conversion_running())
                    release.set()
                    self._wait(lambda: page._status_state == "success" and page._worker is None)
                    page.close()
                finally:
                    release.set()
                    self._wait(lambda: not page.is_conversion_running(), timeout=4.0)
                    self._dispose(page)

    def test_conversion_error_restores_controls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "skeleton.json"
            output = root / "converted"
            source.write_text("{}", encoding="utf-8")
            output.mkdir()

            def failing_convert(*_args, **_kwargs):
                raise RuntimeError("synthetic converter failure")

            with patch.object(converter_page_module, "discover_converter", return_value=None), \
                    patch.object(converter_page_module, "convert_spine", side_effect=failing_convert):
                page = SpineConverterPage()
                try:
                    page.source_edit.setText(str(source))
                    page.output_edit.setText(str(output))
                    page.start_conversion()
                    self._wait(lambda: page._status_state == "error" and page._worker is None)
                    self.assertIn("synthetic converter failure", page.status_label.text())
                    self.assertTrue(page.convert_button.isEnabled())
                    self.assertFalse(page.open_output_button.isEnabled())
                finally:
                    self._dispose(page)


if __name__ == "__main__":
    unittest.main()
