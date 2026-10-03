from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication

from app.core.live2d_editor_session import Live2DEditorSession
from app.core.psd_project import create_project_from_source, load_project
from app.core.psd_worker import publish_output, run_psd_job, worker_command
from app.gui.PsdReconstructionPage import PsdReconstructionPage, PsdReconstructionThread
from tests import test_live2d_editor_session as fixture
from tests.test_selection_psd_page import _Settings


def hard_exit_command(job_file):
    # A native process exit cannot be caught by Python's task try/except.
    script = """
import ctypes, os, pickle, sys
from pathlib import Path
jobfile = Path(sys.argv[1])
with jobfile.open('rb') as f:
    job = pickle.load(f)
if job.get('operation') == 'initialize':
    from app.core.psd_worker import _execute
    _execute(job, jobfile.parent)
else:
    stage = jobfile.parent / 'export'
    stage.mkdir()
    (stage / 'partial.bin').write_bytes(b'task-owned partial output')
if sys.platform == 'win32':
    ctypes.windll.kernel32.ExitProcess(0xC0000374)
os._exit(71)
"""
    return [sys.executable, "-u", "-c", script, str(job_file)]


class PsdWorkerProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="psd-process-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.model = fixture.make_model(self.root / "source")
        self.session = Live2DEditorSession(self.model)
        self.addCleanup(self.session.close)
        self.page = PsdReconstructionPage(compact=True, settings=_Settings(self.root))
        self.project = create_project_from_source(self.model, "Process", output_root=self.root / "projects")
        self.page.bind_project(self.project)
        self.page.show()
        self.page.export_snapshot_request_provider = self.session.capture_export_snapshot
        self.addCleanup(self.close_page)

    def close_page(self):
        self.page.shutdown()
        self.page.close()
        self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def wait(self, condition, seconds=8):
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.002)
        self.app.processEvents()
        self.assertTrue(condition(), self.page.log_text.toPlainText())

    def hashes(self, directory):
        return {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in directory.rglob("*") if p.is_file() and ".psd-staging" not in p.parts}

    def export(self):
        with patch.object(self.page, "_prompt_non_empty_name", return_value="Face"):
            self.assertTrue(self.page.export_selected_artmeshes(["ArtMeshFace"],
                {"parameters": {}, "parts": {}, "drawables": {}}, mesh_data=self.session.mesh_data))
        self.wait(lambda: not self.page.is_busy())

    def test_native_exit_isolated_keeps_history_and_gui_alive_then_retries(self):
        self.export()
        self.assertEqual(self.page._task_state, "succeeded")
        previous = self.hashes(self.project.project_dir)
        source = self.hashes(self.model.parent)
        failed = []
        self.page.taskFailed.connect(failed.append)
        with patch("app.core.psd_worker.worker_command", side_effect=hard_exit_command):
            self.export()
        self.assertEqual(self.page._task_state, "failed")
        self.assertEqual(len(failed), 1)
        if sys.platform == "win32":
            self.assertIn("0xC0000374", failed[0])
        self.assertIn("PSD diagnostics:", failed[0])
        self.assertEqual(self.hashes(self.project.project_dir), previous)
        self.assertEqual(self.hashes(self.model.parent), source)
        self.assertFalse(list((self.project.project_dir / ".psd-staging").iterdir()))
        timer, ticks = QTimer(), []
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start(3)
        try:
            self.wait(lambda: len(ticks) >= 3)
        finally:
            timer.stop()
        self.export()
        self.assertEqual(self.page._task_state, "succeeded")
        self.assertEqual(len(load_project(self.page.current_project.project_file).data["pose_schemes"]), 2)

    def test_initialization_writes_immutable_base_once_and_keeps_defaults_visible(self):
        self.page.bind_project(None)
        self.page.configure_task_embedding()
        self.page.show_task("export")
        self.app.processEvents()
        self.assertTrue(self.page.mode_combo.isVisible())
        self.assertTrue(self.page.mesh_canvas_spin.isVisible())
        self.assertEqual(self.page.mode_combo.count(), 3)
        self.assertEqual(self.page.mesh_canvas_spin.value(), 2048)
        before = (copy.deepcopy(self.session.project.modified), copy.deepcopy(self.session._undo), self.hashes(self.model.parent))
        target = self.session.root / "psd-new"
        request = self.session.capture_export_snapshot()
        with patch("app.gui.PsdReconstructionPage.InfoBar.success") as success_notice:
            self.assertTrue(self.page.begin_editor_project_preparation(request, target))
            self.assertTrue(self.page.is_busy())
            self.assertFalse(self.page.reconstruct_button.isEnabled())
            self.wait(lambda: not self.page.is_busy())
        success_notice.assert_not_called()
        self.assertNotIn(str(target), self.page._task_message)
        self.assertEqual(self.page._task_state, "succeeded")
        self.assertEqual(self.page.current_project.project_dir, target)
        self.assertEqual(self.page.current_project.base_model_json, target / "live2d/model.json")
        self.assertFalse((target / ".psd-init-owner").exists())
        self.assertEqual(self.page.current_project.data["selected_parameter_preset"], "default")
        self.assertEqual(before, (self.session.project.modified, self.session._undo, self.hashes(self.model.parent)))

    def test_initialization_native_exit_after_publish_removes_only_owned_base_then_retries(self):
        self.page.bind_project(None)
        target = self.session.root / "psd-new"
        source = self.hashes(self.model.parent)
        with patch("app.core.psd_worker.worker_command", side_effect=hard_exit_command):
            self.assertTrue(self.page.begin_editor_project_preparation(self.session.capture_export_snapshot(), target))
            self.wait(lambda: not self.page.is_busy())
        self.assertEqual(self.page._task_state, "failed")
        self.assertIsNone(self.page.current_project)
        self.assertFalse(target.exists())
        self.assertEqual(self.hashes(self.model.parent), source)
        self.assertTrue(self.page.begin_editor_project_preparation(self.session.capture_export_snapshot(), target))
        self.wait(lambda: not self.page.is_busy())
        self.assertEqual(self.page._task_state, "succeeded")

    def test_initialization_cancel_after_child_publish_discards_new_base(self):
        self.page.bind_project(None)
        target = self.session.root / "psd-new"
        def interrupt_after_result(*args, **kwargs):
            result = run_psd_job(*args, **kwargs)
            self.page.project_worker.requestInterruption()
            return result
        with patch("app.gui.PsdReconstructionPage.run_psd_job", side_effect=interrupt_after_result):
            self.assertTrue(self.page.begin_editor_project_preparation(self.session.capture_export_snapshot(), target))
            self.wait(lambda: not self.page.is_busy())
        self.assertEqual(self.page._task_state, "cancelled")
        self.assertFalse(target.exists())
        self.assertIsNone(self.page.current_project)

    def test_initialization_bind_failure_discards_owned_base_and_can_retry(self):
        self.page.bind_project(None)
        target = self.session.root / "psd-new"
        with patch.object(self.page, "set_current_project", side_effect=RuntimeError("GUI bind fault")):
            self.assertTrue(self.page.begin_editor_project_preparation(self.session.capture_export_snapshot(), target))
            self.wait(lambda: not self.page.is_busy())
        self.assertEqual(self.page._task_state, "failed")
        self.assertFalse(target.exists())
        self.assertIsNone(self.page.current_project)
        self.assertTrue(self.page.begin_editor_project_preparation(self.session.capture_export_snapshot(), target))
        self.wait(lambda: not self.page.is_busy())
        self.assertEqual(self.page._task_state, "succeeded")

    def test_standalone_output_is_same_volume_and_cancel_after_publish_is_success(self):
        output = self.root / "different-output" / "existing"
        output.mkdir(parents=True)
        (output / "repeat_mesh_pose.psd").write_bytes(b"previous PSD")
        (output / "keep.txt").write_text("unrelated", encoding="utf-8")
        worker = PsdReconstructionThread(str(self.model), str(output), "mesh", output_name="repeat")
        completed, cancelled = [], []
        worker.reconstructionFinished.connect(completed.append)
        worker.reconstructionCancelled.connect(lambda: cancelled.append(1))
        def publish_then_cancel(*args):
            self.assertEqual(worker._job_dir.parent, output.parent)
            publish_output(*args)
            worker.requestInterruption()
        with patch("app.gui.PsdReconstructionPage.publish_output", side_effect=publish_then_cancel):
            worker.start()
            self.assertTrue(worker.wait(8000))
            self.app.processEvents()
        self.assertEqual(len(completed), 1)
        self.assertFalse(cancelled)
        self.assertNotEqual((output / "repeat_mesh_pose.psd").read_bytes(), b"previous PSD")
        self.assertEqual((output / "keep.txt").read_text(), "unrelated")
        worker.cleanup_job()

    def test_standalone_publish_failure_restores_preexisting_entries(self):
        target, stage, backup = self.root / "existing", self.root / "stage", self.root / "backup"
        target.mkdir()
        stage.mkdir()
        for name in ("a.psd", "b.json"):
            (target / name).write_bytes(b"old " + name.encode())
            (stage / name).write_bytes(b"new " + name.encode())
        before = self.hashes(target)
        original = os.rename
        calls = []
        def fail_second(source, destination):
            calls.append((source, destination))
            if len(calls) == 2:
                raise OSError("publication fault")
            return original(source, destination)
        with patch("app.core.psd_worker.os.rename", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "publication fault"):
                publish_output(stage, target, backup)
        self.assertEqual(self.hashes(target), before)
        self.assertEqual({p.name for p in stage.iterdir()}, {"a.psd", "b.json"})

    def test_packaged_worker_uses_early_dispatch(self):
        with patch.object(sys, "frozen", True, create=True):
            self.assertEqual(worker_command(Path("task/request.pickle"))[:2], [sys.executable, "--psd-worker"])

    def test_worker_log_open_write_or_close_failure_still_drains_large_pipe(self):
        script = """
import json, sys
from pathlib import Path
jobfile = Path(sys.argv[1])
for _ in range(1024):
    print('x' * 1024, flush=True)
(jobfile.parent / 'result.json').write_text(json.dumps({'completed': True}))
"""
        original_open = Path.open
        for fault in ("open", "write", "close"):
            with self.subTest(fault=fault):
                job_dir = self.root / ("log-fault-" + fault)
                job_dir.mkdir()
                class FailingLog:
                    def write(self, line):
                        if fault == "write":
                            raise OSError("log write fault")
                    def flush(self):
                        pass
                    def close(self):
                        raise OSError("log close fault")
                def log_open(path, *args, **kwargs):
                    if path.name == "worker.log":
                        if fault == "open":
                            raise OSError("log open fault")
                        return FailingLog()
                    return original_open(path, *args, **kwargs)
                command = lambda job_file: [sys.executable, "-u", "-c", script, str(job_file)]
                started = time.monotonic()
                with patch("app.core.psd_worker.worker_command", side_effect=command), \
                        patch.object(Path, "open", log_open), \
                        self.assertLogs("app.core.psd_worker", level="ERROR"):
                    result = run_psd_job({}, job_dir, cancelled=lambda: time.monotonic() - started > 5)
                self.assertEqual(result, {"completed": True})


if __name__ == "__main__":
    unittest.main()
