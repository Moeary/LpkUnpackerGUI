from __future__ import annotations

import os
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QCoreApplication, QEvent, QThread, Signal

    import app.gui.PreviewPage as preview_page_module
    from app.gui.PreviewPage import PreviewPage
    from app.gui.SpinePreviewWidget import SpinePreviewWidget, WEBENGINE_AVAILABLE
except (ImportError, OSError):  # pragma: no cover - optional desktop dependency
    QApplication = None
    QCoreApplication = None
    QEvent = None
    QThread = None
    Signal = None
    preview_page_module = None
    PreviewPage = None
    SpinePreviewWidget = None
    WEBENGINE_AVAILABLE = False


@unittest.skipIf(
    QApplication is None or not WEBENGINE_AVAILABLE,
    "PySide6 QtWebEngine is optional in the lightweight test environment",
)
class SpinePreviewWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _wait(self, predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("timed out waiting for QtWebEngine")

    def test_embedded_page_loads_and_clear_stops_navigation(self):
        widget = SpinePreviewWidget()
        events = []
        ready = []
        failed = []
        widget.documentLoaded.connect(lambda url: events.append("document"))
        widget.previewReady.connect(ready.append)
        widget.previewFailed.connect(failed.append)
        widget.show()
        widget.open_url(
            "data:text/html,<html><body data-preview-state='ready'><span id='status'>ready</span>"
            "<canvas id='canvas'></canvas><div id='atlas' class='hidden'></div>"
            "<button id='exportPsd' class='hidden'></button></body></html>"
        )
        self._wait(lambda: bool(ready) or bool(failed))
        self.assertFalse(failed, failed)
        self.assertTrue(ready)
        self.assertEqual(events, ["document"])
        self.assertEqual(widget.current_url.split(":", 1)[0], "data")

        widget.clear_preview()
        self.app.processEvents()
        self.assertEqual(widget.current_url, "")
        self.assertFalse(widget.isVisible())
        widget.shutdown()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def test_page_state_error_is_forwarded_after_document_load(self):
        widget = SpinePreviewWidget()
        failures = []
        widget.previewFailed.connect(failures.append)
        widget.show()
        widget.open_url(
            "data:text/html,<html><body data-preview-state='error' "
            "data-preview-error='synthetic runtime error'><span id='status' "
            "class='error'>synthetic runtime error</span><canvas id='canvas'></canvas>"
            "<div id='atlas' class='hidden'></div><button id='exportPsd' "
            "class='hidden'></button></body></html>"
        )
        self._wait(lambda: bool(failures))
        self.assertIn("synthetic runtime error", failures[0])
        widget.shutdown()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def test_preview_page_surfaces_web_error_without_active_plan(self):
        page = PreviewPage()
        try:
            page._on_spine_preview_web_error("synthetic WebEngine failure")
            self.assertIn("synthetic WebEngine failure", page.preview_placeholder.text())
            self.assertFalse(page.spine_preview.isVisible())
            self.assertFalse(page._spine_mode)
        finally:
            page.close_preview_window()
            page._destroy_embedded_spine()
            page._destroy_embedded_live2d()
            page.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            self.app.processEvents()

    def test_spine_import_switch_clears_finished_worker_and_cancels_active_one(self):
        """A rapid switch must tolerate a deleted QThread wrapper and close cleanly."""

        class MemorySettings:
            def __init__(self):
                self.values = {}

            def get(self, key, default=None):
                return self.values.get(key, default)

            def set(self, key, value):
                self.values[key] = value

            def get_temp_dir(self):
                return os.getcwd()

            def get_output_dir(self, _kind):
                return os.getcwd()

        class FakeSpineImportThread(QThread):
            previewReady = Signal(object, str)
            failed = Signal(str, str)

            def __init__(self, source_path, temp_root, runtime_root="", parent=None):
                super().__init__(parent)
                self.source_path = source_path
                self.result = None

            def run(self):
                if self.source_path == "first":
                    return
                while not self.isInterruptionRequested():
                    time.sleep(0.005)

        with patch.object(preview_page_module, "SettingsManager", MemorySettings), \
                patch.object(preview_page_module, "SpinePreviewImportThread", FakeSpineImportThread):
            page = PreviewPage()
            try:
                page.start_spine_preview_import("first")
                self._wait(lambda: not page._spine_preview_workers, timeout=2.0)
                self.assertIsNone(page._spine_preview_thread)

                page.start_spine_preview_import("second")
                self._wait(
                    lambda: page._spine_preview_thread is not None
                    and page._spine_preview_thread.isRunning(),
                    timeout=2.0,
                )
                generation = page._spine_preview_generation
                page.close_preview_window()
                self.assertGreater(page._spine_preview_generation, generation)
                self._wait(lambda: not page._spine_preview_workers, timeout=2.0)
                self.assertIsNone(page._spine_preview_thread)
            finally:
                page.close_preview_window()
                page._destroy_embedded_spine()
                page._destroy_embedded_live2d()
                page.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
