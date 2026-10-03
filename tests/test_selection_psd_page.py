from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from app.core.psd_project import create_project_from_source, load_project
from app.gui.PsdReconstructionPage import PsdReconstructionPage
from tests.test_live2d_mod_page_smoke import _TestSettings
from tests import test_selection_psd as selection_fixture


class _Settings(_TestSettings):
    def get_output_root(self):
        return str(self.root / "output")

    def get_texture_viewer_mode(self):
        return "builtin"


class SelectedPosePsdPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = self.root / "source"
        source.mkdir()
        self.fixture = selection_fixture.SelectedPosePsdTests()
        self.model, self.textures, self.mesh = self.fixture.make_source(source, shared=True)
        self.page = PsdReconstructionPage(compact=True, settings=_Settings(self.root))
        self.page.resize(420, 760)
        self.page.show()
        self.app.processEvents()
        self.addCleanup(self.close_page)

    def close_page(self):
        self.page.shutdown()
        if self.page.current_project:
            self.page.save_current_project()
        self.page.close()
        self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def wait_worker(self):
        deadline = time.monotonic() + 10
        while self.page.is_busy() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.app.processEvents()
        self.assertFalse(self.page.is_busy(), self.page.log_text.toPlainText())

    def bind_project(self):
        project = create_project_from_source(self.model, "Selected", output_root=self.root / "projects")
        self.page.bind_project(project)
        return project

    def test_shared_option_defaults_off_resets_per_metadata_and_disables_for_multi(self):
        self.bind_project()
        exported = self.fixture.export(self.root, self.model, self.mesh, ["Head"])
        self.page._set_manual_repack_psd(str(exported.psd_path))
        self.page.set_workflow("repack")
        self.page.workflow_tabs.setCurrentIndex(1)
        self.page.metadata_edit.setText(str(exported.metadata_path))
        self.app.processEvents()
        checkbox = self.page.shared_uv_checkbox
        self.assertTrue(checkbox.isVisible())
        self.assertTrue(checkbox.isEnabled())
        self.assertFalse(checkbox.isChecked())
        self.assertIn("UnselectedShared", checkbox.toolTip())
        checkbox.setChecked(True)
        self.page._refresh_shared_uv_option()
        self.assertTrue(checkbox.isChecked())
        self.page.set_busy(True)
        self.assertFalse(checkbox.isEnabled())
        self.page.set_busy(False)
        alternate = self.root / "alternate.lpkpsd.json"
        alternate.write_bytes(exported.metadata_path.read_bytes())
        self.page.metadata_edit.setText(str(alternate))
        self.assertFalse(checkbox.isChecked())
        checkbox.setChecked(True)
        with patch.object(self.page, "project_psd_choices", return_value=[]):
            self.page.start_multi_repack()
        self.assertFalse(checkbox.isChecked())
        self.assertFalse(checkbox.isEnabled())
        self.page._refresh_shared_uv_option()
        self.assertFalse(checkbox.isEnabled())
        metadata = json.loads(alternate.read_text(encoding="utf-8"))
        metadata.pop("selection")
        alternate.write_text(json.dumps(metadata), encoding="utf-8")
        self.page._refresh_shared_uv_option()
        self.assertFalse(checkbox.isVisible())
        self.assertFalse(checkbox.isEnabled())

    def test_cancelled_selection_does_not_create_pose_scheme_or_snapshot(self):
        project = self.bind_project()
        original = project.project_file.read_bytes()
        with patch.object(self.page, "_prompt_non_empty_name", return_value=""), \
                patch.object(self.page, "export_snapshot_provider") as snapshot:
            self.assertFalse(self.page.export_selected_artmeshes(["Head"], {}, mesh_data=self.mesh))
        snapshot.assert_not_called()
        self.assertEqual(original, project.project_file.read_bytes())
        self.assertFalse(self.page.current_project.data["pose_schemes"])
        self.assertIsNone(self.page.worker)

    def test_part_pose_without_exact_snapshot_is_rejected_before_export(self):
        project = self.bind_project()
        original = project.project_file.read_bytes()
        with patch.object(self.page, "_prompt_non_empty_name") as prompt:
            self.assertFalse(self.page.export_selected_artmeshes(["Head"], {"parameters": {}, "parts": {"Hair": .4}}))
        prompt.assert_not_called()
        self.assertIn("exact drawable pose snapshot", self.page.log_text.toPlainText())
        self.assertEqual(original, project.project_file.read_bytes())

    def test_actual_worker_repack_records_actual_shared_ids_in_history_and_manifest(self):
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.model.parent.iterdir()}
        self.bind_project()
        results = []
        with patch.object(self.page, "_prompt_non_empty_name", return_value="Head pose"):
            self.assertTrue(self.page.export_selected_artmeshes(
                ["Head"], {"parameters": {"Angle": 100}, "parts": {"Hair": .4}},
                {"x": 49, "y": 33, "width": 1, "height": 1}, mesh_data=self.mesh))
        self.page.worker.reconstructionFinished.connect(results.append)
        self.wait_worker()
        self.assertEqual(len(results), 1, self.page.log_text.toPlainText())
        exported = results[0]
        metadata = json.loads(exported.metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["selection"]["pose"]["parts"], {"Hair": .4})
        edited = self.fixture.edit_layers(self.root, exported, {
            "Head": lambda pixels: pixels.__setitem__((3, 3, slice(0, 3)), (240, 0, 0)),
        })
        shutil.copyfile(edited, exported.psd_path)
        self.page._set_manual_repack_psd(str(exported.psd_path))
        self.page.set_workflow("repack")
        self.page.metadata_edit.setText(str(exported.metadata_path))
        self.assertFalse(self.page.shared_uv_checkbox.isChecked())
        self.page.shared_uv_checkbox.setChecked(True)
        skin_tokens = []
        self.page.repackSkinReady.connect(skin_tokens.append)
        self.page.start_reconstruction(str(exported.psd_path))
        self.assertTrue(self.page.worker.allow_shared_uv)
        self.wait_worker()
        self.assertEqual(len(skin_tokens), 1, self.page.log_text.toPlainText())
        loaded = load_project(self.page.current_project.project_file)
        entry = loaded.data["repack_history"][-1]
        version = loaded.data["pose_schemes"][-1]["versions"][-1]
        manifest = json.loads((loaded.project_dir / entry["textures_dir"] / "manifest.json").read_text(encoding="utf-8"))
        for item in (entry, version, manifest):
            self.assertTrue(item["allow_shared_uv"])
            self.assertEqual(item["affected_unselected_ids"], ["UnselectedShared"])
        self.assertEqual(before, {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.model.parent.iterdir()})


if __name__ == "__main__":
    unittest.main()
