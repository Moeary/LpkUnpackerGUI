from __future__ import annotations

import copy
import hashlib
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication, QSplitter

from app.core.live2d_editor_session import Live2DEditorSession
from app.core.psd_project import create_project_from_source, load_project
from app.gui.PsdReconstructionPage import PsdReconstructionPage
from app.i18n import tr
from tests import test_live2d_editor_session as session_fixture
from tests.test_selection_psd_page import _Settings


class _GatedRequest:
    def __init__(self, request, *, error=None):
        self.request = request
        self.error = error
        self.started = threading.Event()
        self.release = threading.Event()
        self.worker_ident = None

    def __deepcopy__(self, memo):
        raise AssertionError("Detached snapshot leases must not be deep-copied.")

    def write(self, output_dir):
        self.worker_ident = threading.get_ident()
        self.started.set()
        if not self.release.wait(5):
            raise TimeoutError("Test failed to release PSD preparation")
        if self.error:
            Path(output_dir).mkdir()
            (Path(output_dir) / "partial.bin").write_bytes(b"private partial output")
            raise RuntimeError(self.error)
        return self.request.write(output_dir)


class PsdTaskWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="psd-worker-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.model = session_fixture.make_model(self.root / "source")
        self.session = Live2DEditorSession(self.model)
        self.addCleanup(self.session.close)
        self.page = PsdReconstructionPage(compact=True, settings=_Settings(self.root))
        self.page.resize(430, 760)
        self.page.show()
        self.project = create_project_from_source(self.model, "Task", output_root=self.root / "projects")
        self.page.bind_project(self.project)
        self.page.set_skin_context({"id": self.session.active_skin_id, "name": "captured"})
        self.app.processEvents()
        self.addCleanup(self.close_page)

    def close_page(self):
        self.page.shutdown()
        self.page.close()
        self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def events_until(self, condition, seconds=5):
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.001)
        self.app.processEvents()
        self.assertTrue(condition(), self.page.log_text.toPlainText())

    def hashes(self, root):
        return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in root.rglob("*") if path.is_file() and ".psd-staging" not in path.parts}

    def export_gated(self, error=None):
        gate = _GatedRequest(self.session.capture_export_snapshot(), error=error)
        self.addCleanup(gate.request.close)
        self.addCleanup(gate.release.set)
        provider_threads = []

        def capture():
            self.assertTrue(self.page.is_busy(), "Preparation must be busy before capture")
            provider_threads.append(threading.get_ident())
            return gate

        self.page.export_snapshot_request_provider = capture
        with patch.object(self.page, "_prompt_non_empty_name", return_value="Selected face"):
            started = self.page.export_selected_artmeshes(
                ["ArtMeshFace"], {"parameters": {"ParamAngleY": 12}},
                mesh_data=copy.deepcopy(self.session.mesh_data))
        self.assertTrue(started)
        self.assertEqual(provider_threads, [threading.get_ident()])
        self.events_until(gate.started.is_set)
        self.assertNotEqual(gate.worker_ident, threading.get_ident())
        return gate

    def test_compact_builds_task_cards_without_standalone_shell_or_preview_dialog(self):
        self.assertFalse(hasattr(self.page, "workspace_splitter"))
        self.assertFalse(hasattr(self.page, "left_scroll"))
        self.assertFalse(self.page.findChildren(QSplitter))
        self.assertIsNone(self.page.preview_dialog)
        self.assertEqual(self.page.workflow_tabs.count(), 4)
        self.assertEqual(self.page.mode_combo.count(), 2)
        self.page.configure_task_embedding()
        for index, task in enumerate(("export", "repack", "history", "advanced")):
            self.page.show_task(task)
            self.app.processEvents()
            self.assertEqual(self.page.workflow_tabs.currentIndex(), index)
        self.assertFalse(self.page.task_selector.isVisible())
        self.assertFalse(self.page.progress_bar.isVisible())
        self.assertFalse(self.page.log_frame.isVisible())
        self.assertIsNone(self.page.preview_dialog)

    def test_show_task_resynchronizes_workflow_when_stack_index_is_unchanged(self):
        for index, task, opposite, label in (
            (0, "export", "repack", "psd.export_button"),
            (1, "repack", "export", "psd.repack.single_button"),
        ):
            self.page.show_task(task)
            self.page.set_workflow(opposite)
            self.assertEqual(self.page.workflow_tabs.currentIndex(), index)
            self.assertEqual(self.page.workflow, opposite)
            with patch.object(self.page, "mark_project_dirty", wraps=self.page.mark_project_dirty) as dirty:
                self.page.show_task(task)
                self.assertEqual(self.page.workflow, task)
                self.assertEqual(self.page.reconstruct_button.text(), tr(label))
                synchronized_count = dirty.call_count
                self.assertGreater(synchronized_count, 0)
                self.page.show_task(task)
                self.assertEqual(dirty.call_count, synchronized_count)

    def test_gui_timer_runs_during_detached_preparation_and_success_unlocks_after_worker(self):
        before = self.hashes(self.model.parent)
        pending = (copy.deepcopy(self.session.project.modified), copy.deepcopy(self.session._undo))
        states, completed, results = [], [], []
        self.page.taskStateChanged.connect(states.append)
        self.page.taskFinished.connect(completed.append)
        gate = self.export_gated()
        self.page.worker.reconstructionFinished.connect(results.append)
        timer = QTimer()
        ticks = []
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start(5)
        try:
            self.events_until(lambda: len(ticks) >= 3)
            self.assertTrue(self.page.is_busy())
            self.assertFalse(self.page.reconstruct_button.isEnabled())
            self.assertEqual(pending, (self.session.project.modified, self.session._undo))
            # Leased static files remain available after the session closes.
            self.session.close()
            gate.release.set()
            self.events_until(lambda: not self.page.is_busy())
        finally:
            timer.stop()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[-1]["state"], "succeeded")
        self.assertFalse(completed[-1]["busy"])
        self.assertEqual(completed[-1]["progress"], 100)
        self.assertTrue(any(state["state"] == "preparing" and state["busy"] for state in states))
        self.assertTrue(all(isinstance(state["details"], str) for state in states))
        self.assertTrue(results[0].psd_path.is_file())
        self.assertNotIn(".psd-staging", str(results[0].metadata_path))
        self.assertEqual(before, self.hashes(self.model.parent))
        self.assertEqual(len(load_project(self.page.current_project.project_file).data["pose_schemes"]), 1)

    def test_failed_preparation_discards_partial_files_and_preserves_existing_export(self):
        first = self.export_gated()
        first.release.set()
        self.events_until(lambda: not self.page.is_busy())
        before = self.hashes(self.project.project_dir)
        source = self.hashes(self.model.parent)
        failed = []
        self.page.taskFailed.connect(failed.append)
        gate = self.export_gated(error="private preparation fault")
        gate.release.set()
        self.events_until(lambda: not self.page.is_busy())
        self.assertEqual(failed, ["private preparation fault"])
        self.assertEqual(self.page._task_state, "failed")
        self.assertTrue(self.page.reconstruct_button.isEnabled())
        self.assertEqual(before, self.hashes(self.project.project_dir))
        self.assertEqual(source, self.hashes(self.model.parent))
        self.assertFalse(list((self.project.project_dir / ".psd-staging").iterdir()))

    def test_close_during_preparation_keeps_running_thread_then_cancels_without_history(self):
        before = self.project.project_file.read_bytes()
        gate = self.export_gated()
        worker = self.page.worker
        self.assertFalse(self.page.close())
        self.assertIs(worker, self.page.worker)
        self.assertTrue(worker.isRunning())
        self.assertTrue(self.page.isVisible())
        gate.release.set()
        self.events_until(lambda: not self.page.is_busy())
        self.assertEqual(self.page._task_state, "cancelled")
        self.assertFalse(self.page.current_project.data["pose_schemes"])
        self.assertEqual(before, self.project.project_file.read_bytes())
        self.assertFalse(list((self.project.project_dir / ".psd-staging").iterdir()))

    def test_shutdown_cancels_with_total_short_wait_and_keeps_busy_worker_alive(self):
        gate = self.export_gated()
        started = time.monotonic()
        self.assertFalse(self.page.shutdown())
        self.assertLess(time.monotonic() - started, .5)
        self.assertTrue(self.page.worker.isRunning())
        gate.release.set()
        self.events_until(lambda: not self.page.is_busy())
        self.assertEqual(self.page._task_state, "cancelled")
        self.assertFalse(self.page.current_project.data["pose_schemes"])

    def test_repack_registry_failure_reports_failure_and_never_applies_skin(self):
        gate = self.export_gated()
        gate.release.set()
        self.events_until(lambda: not self.page.is_busy())
        self.page.set_workflow("repack")
        psd = self.page.selected_repack_psd_path()
        before = self.page.current_project.project_file.read_bytes()
        ready, failed, completed = [], [], []
        self.page.repackSkinReady.connect(ready.append)
        self.page.taskFailed.connect(failed.append)
        self.page.taskFinished.connect(completed.append)
        with patch("app.gui.PsdReconstructionPage.record_repack", side_effect=OSError("repack registry fault")):
            self.page.start_reconstruction(psd)
            self.events_until(lambda: not self.page.is_busy())
        self.assertEqual(failed, ["repack registry fault"])
        self.assertEqual(self.page._task_state, "failed")
        self.assertFalse(ready)
        self.assertFalse(completed)
        self.assertFalse(self.page.current_project.data["repack_history"])
        self.assertEqual(before, self.page.current_project.project_file.read_bytes())


if __name__ == "__main__":
    unittest.main()
