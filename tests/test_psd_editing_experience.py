import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from app.core.psd_project import create_project_from_source
from app.gui.PsdReconstructionPage import PsdReconstructionPage
from app.gui.psd_comparison_dialog import PsdComparisonDialog
from app.gui.psd_export_dialog import SelectionPsdDialog
from tests import test_selection_psd as fixture
from tests.test_selection_psd_page import _Settings


class PsdEditingExperienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.helper = fixture.SelectedPosePsdTests()
        source = self.root / "source"
        source.mkdir()
        self.model, self.textures, self.mesh = self.helper.make_source(source)
        self.page = PsdReconstructionPage(compact=True, settings=_Settings(self.root))
        self.addCleanup(self.close_page)
        self.page.bind_project(create_project_from_source(self.model, "edit", output_root=self.root / "projects"))
        self.page.resize(480, 760)
        self.page.show()
        self.app.processEvents()

    def close_page(self):
        self.page.shutdown()
        self.page.close()
        self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def wait_until(self, condition):
        deadline = time.monotonic() + 20
        while condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.app.processEvents()
        self.assertFalse(condition())

    def test_selected_atlas_worker_roundtrip_exposes_edit_and_compare(self):
        page = self.page
        self.assertEqual([page.mode_combo.itemData(i) for i in range(page.mode_combo.count())],
                         ["mesh", "atlas-components"])
        self.assertTrue(page.export_selected_artmeshes(["Head", "EarLeft"], {}, mesh_data=self.mesh,
                                                      mode="atlas-components", output_name="Parts", atlas_layout="packed"))
        self.wait_until(page.is_busy)
        self.assertEqual(page._task_state, "succeeded", page.log_text.toPlainText())
        self.assertTrue(page.open_photoshop_button.isVisible())
        self.assertTrue(page.open_photoshop_button.isEnabled())
        self.assertTrue(page.mode_hint_label.isVisible())
        self.assertTrue(Path(page.last_psd_path).is_file())
        exported = json.loads(Path(page.last_psd_path).with_suffix(".lpkpsd.json").read_text(encoding="utf-8"))
        self.assertEqual(exported["selection"]["layout"], "packed")
        page.set_workflow("repack")
        page.start_reconstruction(page.last_psd_path)
        self.wait_until(page.is_busy)
        self.assertEqual(page._task_state, "succeeded", page.log_text.toPlainText())
        self.assertTrue(page.compare_repack_button.isEnabled())
        request = page._last_comparison_request
        self.assertTrue(Path(request["metadata_path"]).is_file())
        self.assertEqual(set(request["texture_outputs"]), {0, 1})
        page.bind_project(None)
        self.assertFalse(page.compare_repack_button.isEnabled())
        self.assertIsNone(page._last_comparison_request)

    def test_edit_psd_uses_default_app_without_photoshop_and_reports_failure(self):
        page = self.page
        exported = self.helper.export(self.root, self.model, self.mesh)
        page.last_psd_path = str(exported.psd_path)
        with patch.object(page.settings_manager, "get_photoshop_executable", return_value="", create=True), \
             patch("app.gui.PsdReconstructionPage.QDesktopServices.openUrl", return_value=True) as open_url:
            page.open_current_psd_in_photoshop()
            self.assertEqual(Path(open_url.call_args.args[0].toLocalFile()), exported.psd_path)
        with patch.object(page.settings_manager, "get_photoshop_executable", return_value="", create=True), \
             patch("app.gui.PsdReconstructionPage.QDesktopServices.openUrl", return_value=False), \
             patch("app.gui.PsdReconstructionPage.InfoBar.warning") as warning:
            page.open_current_psd_in_photoshop()
            warning.assert_called_once()

    def test_old_export_preference_maps_to_atlas_without_removing_history(self):
        project = self.page.current_project
        project.data["ui_state"] = {"export_mode": "atlas-artmesh"}
        self.page.set_current_project(project)
        self.assertEqual(self.page.mode_combo.currentData(), "atlas-components")

    def test_selection_dialog_chooses_native_atlas_layout(self):
        dialog = SelectionPsdDialog(3, self.page)
        dialog.show()
        self.app.processEvents()
        self.assertFalse(dialog.packed.isVisible())
        dialog.mode_combo.setCurrentIndex(1)
        self.assertTrue(dialog.packed.isVisible())
        self.assertEqual(dialog.options()["atlas_layout"], "packed")
        dialog.packed.setChecked(False)
        self.assertEqual(dialog.options()["atlas_layout"], "original")
        dialog.close()
        dialog.deleteLater()

    def test_comparison_dialog_loads_child_process_images_and_view_modes(self):
        exported = self.helper.export(self.root, self.model, self.mesh)
        dialog = PsdComparisonDialog({"metadata_path": str(exported.metadata_path),
                                      "texture_outputs": {i: str(path) for i, path in enumerate(self.textures)},
                                      "mesh_data": self.mesh}, self.page)
        dialog.show()
        try:
            self.wait_until(dialog.worker.isRunning)
            self.assertIsNotNone(dialog.report, dialog.status.text())
            self.assertEqual(dialog.view_combo.count(), 3)
            self.assertEqual(dialog.report["warnings"], [])
            for index, mode in enumerate(("side", "wipe", "difference")):
                dialog.mode_combo.setCurrentIndex(index)
                self.app.processEvents()
                self.assertEqual(dialog.canvas.mode, mode)
                self.assertFalse(dialog.canvas.grab().isNull())
            dialog.view_combo.setCurrentIndex(2)
            self.assertEqual(dialog.canvas.view["label"], "pose")
        finally:
            dialog.worker.requestInterruption()
            self.wait_until(dialog.worker.isRunning)
            dialog.close()
            dialog.deleteLater()

    def test_comparison_cancel_waits_for_child_process_before_closing(self):
        exported = self.helper.export(self.root, self.model, self.mesh)
        dialog = PsdComparisonDialog({"metadata_path": str(exported.metadata_path),
                                      "texture_outputs": {i: str(path) for i, path in enumerate(self.textures)}}, self.page)
        dialog.show()
        dialog.reject()
        self.wait_until(dialog.worker.isRunning)
        self.assertFalse(dialog.isVisible())
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
