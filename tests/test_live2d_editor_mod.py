from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PIL import Image
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
from qfluentwidgets import ComboBox, DoubleSpinBox, Pivot, TableWidget

from app.core.live2d_editor_mod_preview import mapped_mod_skin_preview
from app.core.live2dviewer_mod_project import (
    Live2DViewerModProjectError, add_model_to_project, create_empty_project,
    create_project_from_base_source, export_live2dviewer_mod,
)
from app.gui.Live2DEditorPage import Live2DEditorPage
from tests.test_live2d_editor_page import _Preview
from tests.test_live2d_editor_session import make_model
from tests.test_live2d_mod_page_smoke import _TestSettings


class Live2DEditorModTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="live2d-mod-editor-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.model = make_model(self.root / "source")
        document = json.loads(self.model.read_text(encoding="utf-8"))
        document["HitAreas"] = [{"Id": "ArtMeshFace", "Name": "Face"},
                                {"Id": "ArtMeshBound", "Motion": "TapFace"}]
        self.model.write_text(json.dumps(document), encoding="utf-8")
        self.original_hashes = self.hashes(self.model.parent)
        self.settings = _TestSettings(self.root)
        self.settings_patch = patch("app.gui.Live2DModPage.SettingsManager", return_value=self.settings)
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.page = Live2DEditorPage()
        self.addCleanup(self.cleanup_page)
        self.mod = self.page.mod_panel
        self.errors = []
        self.mod.show_error = self.errors.append

    @staticmethod
    def hashes(root):
        return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in root.rglob("*") if path.is_file()}

    def cleanup_page(self):
        self.page.shutdown()
        self.page.close()
        self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def wait_worker(self):
        deadline = time.monotonic() + 10
        while self.mod.worker or self.mod.pending_sources:
            self.assertLess(time.monotonic(), deadline)
            self.app.processEvents()
            time.sleep(.005)
        self.assertFalse(self.errors, self.errors)

    def new_project(self):
        project = create_project_from_base_source(self.model, project_name="TestMOD", output_root=self.root / "projects")
        self.mod.set_current_project(project)
        return project

    def open(self, model=None):
        with patch("app.gui.Live2DPreviewWindow.Live2DPreviewWindow", _Preview):
            self.assertTrue(self.page.open_source(str(model or self.model)), self.page.status_label.text())

    def add_skin(self):
        skin = self.root / "Rose.png"
        Image.new("RGBA", (32, 32), (220, 30, 70, 255)).save(skin)
        self.mod.set_current_project(add_model_to_project(self.mod.current_project, skin, skin_name="Rose"))
        return skin

    def test_fluent_controls_and_recoverable_panels(self):
        self.open()
        self.assertIsInstance(self.page.tabs.pivot, Pivot)
        self.assertIsInstance(self.page.parameter_table, TableWidget)
        self.assertIsInstance(self.page.parameter_spin, DoubleSpinBox)
        self.assertIsInstance(self.page.motion_combo, ComboBox)
        self.assertIsInstance(self.mod.workflow_tabs.pivot, Pivot)
        self.assertEqual(self.mod.workflow_tabs.count(), 3)
        self.assertIs(self.mod.project_combo.parent(), self.mod)
        self.page.timeline.set_track([{"time": 0, "value": 0}], 2, ["value"])
        self.page.timeline.set_playing(True)
        self.page.workspace.set_panel_visible("timeline", False)
        self.assertFalse(self.page.timeline.is_playing)
        self.page.workspace.set_panel_visible("preview", False)
        self.assertFalse(self.page.preview.active)
        self.page.workspace.set_panel_visible("details", False)
        self.page.workspace.reset_layout()
        self.assertTrue(all(self.page.workspace.is_panel_visible(name) for name in ("preview", "details", "timeline")))

    def test_edited_copy_ui_import_trigger_export_and_reopen(self):
        self.open()
        self.page.session.create_motion("Pose", 2)
        self.page.session.set_keyframes("Pose", "ParamAngleY", [{"time": 0, "value": -10}, {"time": 2, "value": 10}])
        self.mod.set_current_project(create_empty_project("EditedMOD", output_root=self.root / "projects"))
        with patch("app.gui.Live2DEditorPage.QFileDialog.getExistingDirectory", return_value=str(self.root / "copies")):
            self.page._use_edited_for_mod()
        self.wait_worker()
        self.assertEqual(len(self.mod.current_project.models), 1)
        self.assertTrue(Path(self.page.last_saved_copy["model_path"]).is_file())
        self.add_skin()
        # The unit fixture intentionally has an invalid MOC. Give its imported
        # package the matching static geometry sidecar; real native validation
        # obtains the same data directly from Cubism Core.
        (self.mod.current_project.base_model_json.parent / "drawables.json").write_bytes(
            (self.model.parent / "drawables.json").read_bytes())
        self.page.tabs.setCurrentWidget(self.page.mod_tab)
        self.mod.preview_main_model()
        self.page.preview.live2d_canvas.modelPointClicked.emit(.5, .5)
        self.assertIs(self.page.tabs.currentWidget(), self.page.mod_tab)
        self.assertEqual(self.mod.current_project.data["selected_artmesh_id"], "ArtMeshFace")
        self.assertFalse(self.mod.select_trigger("ArtMeshBound"))
        self.mod.rename_skin(str(self.mod.current_project.models[1]["id"]), "Evening")
        self.assertTrue(self.mod.save_current_project(silent=True))
        self.mod.start_worker("export", project=self.mod.current_project, destination=str(self.root / "exports"))
        self.wait_worker()
        exported = Path(self.mod.current_project.data["last_export_path"])
        self.assertTrue((exported / "skin_manifest.json").is_file())
        self.assertTrue((exported / "texture_mapping.json").is_file())
        self.mod.preview_export_model()
        self.assertIn("Pose[0]", self.page.session.project.motions)
        self.assertEqual(self.page.session.keyframes("Pose[0]", "ParamAngleY")[-1]["value"], 10)
        self.assertEqual(self.original_hashes, self.hashes(self.model.parent))

    def test_texture_only_skin_preview_and_canceled_dirty_switch(self):
        self.new_project()
        skin = self.add_skin()
        self.open()
        self.mod.preview_main_model()
        original_session = self.page.session
        self.page._parameter_spins["ParamAngleY"].setValue(20)
        with patch("app.gui.Live2DEditorPage.QMessageBox.warning", return_value=QMessageBox.Cancel):
            self.mod.model_selector.setCurrentIndex(1)
        self.assertIs(self.page.session, original_session)
        self.assertEqual(self.mod.model_selector.currentIndex(), 0)
        self.page.undo()
        self.mod.model_selector.setCurrentIndex(1)
        self.assertIsNot(self.page.session, original_session)
        self.assertEqual(self.page.session.texture_paths[0].read_bytes(), skin.read_bytes())
        self.assertTrue(self.mod.selected_mapping_button.isEnabled())
        self.assertEqual(self.original_hashes, self.hashes(self.model.parent))

    def test_mapping_preview_matches_export_and_rejects_invalid_mapping(self):
        project = self.new_project()
        skin = self.add_skin()
        project = self.mod.current_project
        project.data["selected_artmesh_id"] = "ArtMeshFace"
        model_id = str(project.models[1]["id"])
        built, output = export_live2dviewer_mod(project, self.root / "exports")
        with mapped_mod_skin_preview(project, model_id) as preview_path:
            preview = json.loads(preview_path.read_text(encoding="utf-8"))
            generated = json.loads((output / "model1.json").read_text(encoding="utf-8"))
            texture = preview_path.parent / preview["FileReferences"]["Textures"][0]
            exported_texture = output / generated["FileReferences"]["Textures"][0]
            self.assertEqual(texture.read_bytes(), exported_texture.read_bytes())
            self.assertEqual(texture.read_bytes(), skin.read_bytes())
            self.assertEqual((preview_path.parent / preview["FileReferences"]["Moc"]).read_bytes(),
                             (self.model.parent / "model.moc3").read_bytes())
        self.assertFalse(preview_path.exists())
        project.models[1]["texture_mappings"] = []
        with self.assertRaises(Live2DViewerModProjectError):
            with mapped_mod_skin_preview(project, model_id):
                self.fail("Invalid mappings must not generate a skin preview")

    def test_project_actions_and_texture_dialogs_remain_accessible(self):
        self.new_project()
        self.add_skin()
        self.assertTrue(self.mod.rename_project_button.isEnabled())
        self.assertTrue(self.mod.delete_project_button.isEnabled())
        self.assertTrue(self.mod.save_project_button.isEnabled())
        with patch("app.gui.Live2DModPage.ProjectNameDialog") as dialog:
            dialog.return_value.exec.return_value = QDialog.Accepted
            dialog.return_value.project_name = "RenamedMOD"
            self.mod.rename_current_project()
        self.assertEqual(self.mod.current_project.project_name, "RenamedMOD")
        self.mod.clear_current_project()
        self.mod.project_combo.setText("RenamedMOD")
        self.mod.open_project_file()
        self.assertIsNotNone(self.mod.current_project)
        skin_id = str(self.mod.current_project.models[1]["id"])
        with patch("app.gui.Live2DModPage.TextureGalleryDialog.exec", return_value=QDialog.Accepted) as gallery:
            self.mod.view_model_textures(skin_id)
            gallery.assert_called_once()
        with patch("app.gui.Live2DModPage.TextureMappingDialog") as dialog:
            dialog.return_value.exec.return_value = QDialog.Accepted
            dialog.return_value.mappings.return_value = [(0, self.mod.resolve_project_path(self.mod.current_project.models[1]["textures"][0]))]
            self.mod.edit_model_mapping(skin_id)
        self.assertFalse(self.errors, self.errors)
        directory = self.mod.current_project.project_dir
        with patch("app.gui.Live2DModPage.DeleteProjectDialog") as dialog:
            dialog.return_value.exec.return_value = QDialog.Accepted
            dialog.return_value.delete_files = False
            self.mod.delete_current_project()
        self.assertIsNone(self.mod.current_project)
        self.assertTrue(directory.is_dir())
        self.assertEqual(self.original_hashes, self.hashes(self.model.parent))


if __name__ == "__main__":
    unittest.main()
