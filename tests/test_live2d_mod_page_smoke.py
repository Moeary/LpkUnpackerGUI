from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from PIL import Image


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication


class _TestSettings:
    def __init__(self, root: Path):
        self.root = root
        self.values = {
            "live2dviewer_mod.last_project_file": "",
            "live2dviewer_mod.project_files": [],
        }

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def get_output_dir(self, output_type="live2d"):
        path = self.root / "output" / output_type
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def get_temp_dir(self):
        path = self.root / "temp"
        path.mkdir(parents=True, exist_ok=True)
        return str(path)


class Live2DModPageSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_page_constructs_with_card_workflow(self):
        module = importlib.import_module("app.gui.Live2DModPage")
        with tempfile.TemporaryDirectory() as directory:
            settings = _TestSettings(Path(directory))
            original = module.SettingsManager
            module.SettingsManager = lambda: settings
            try:
                page = module.Live2DModPage()
                self.assertFalse(page.export_button.isEnabled())
                self.assertEqual(page.models_layout.count(), 2)
                project_dir = Path(directory) / "project"
                texture = project_dir / "sources" / "main" / "texture.png"
                texture.parent.mkdir(parents=True)
                Image.new("RGBA", (32, 32), (255, 255, 255, 255)).save(texture)
                model_json = texture.parent / "main.model3.json"
                model_json.write_text(
                    json.dumps(
                        {
                            "Version": 3,
                            "HitAreas": [
                                {"Name": "Body", "Id": "ArtMeshBody"}
                            ],
                            "FileReferences": {
                                "Moc": "main.moc3",
                                "Textures": ["texture.png"],
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                project = module.Live2DViewerModProject(
                    project_dir,
                    project_dir / module.PROJECT_FILE_NAME,
                    {
                        "format": "LpkUnpacker.Live2DViewerModProject",
                        "version": 3,
                        "project_name": "project",
                        "models": [
                            {
                                "id": "main",
                                "skin_name": "Original",
                                "source_path": "main.model3.json",
                                "workspace_path": "sources/main",
                                "model_json": "sources/main/main.model3.json",
                                "textures": ["sources/main/texture.png"],
                                "texture_mappings": [],
                            },
                            {
                                "id": "model-blue",
                                "skin_name": "Blue",
                                "source_path": "blue.png",
                                "workspace_path": "sources/model-blue",
                                "model_json": "",
                                "textures": ["sources/main/texture.png"],
                                "texture_mappings": [
                                    {
                                        "source_texture": "sources/main/texture.png",
                                        "target_index": 0,
                                        "target_texture": "sources/main/texture.png",
                                    }
                                ],
                            },
                        ],
                        "artmesh_areas": [
                            {"id": "ArtMeshBody", "name": "Body"}
                        ],
                        "selected_artmesh_id": "ArtMeshBody",
                    },
                )
                page.set_current_project(project)
                self.assertEqual(len(page.findChildren(module.ModelCard)), 2)
                self.assertIsInstance(
                    page.models_container,
                    module.ModelListContainer,
                )
                card_buttons = [
                    button.text()
                    for card in page.findChildren(module.ModelCard)
                    for button in card.findChildren(module.PushButton)
                ]
                self.assertNotIn(module.tr("mod.models.move_up"), card_buttons)
                self.assertNotIn(
                    module.tr("mod.models.move_down"),
                    card_buttons,
                )
                self.assertIn(
                    module.tr("mod.models.make_main"),
                    card_buttons,
                )
                self.assertTrue(page.export_button.isEnabled())
                self.assertEqual(
                    page.artmesh_combo.currentData(),
                    "ArtMeshBody",
                )
                preview_paths = []
                page.previewModelRequested.connect(preview_paths.append)
                page.preview_model("main")
                self.assertEqual(preview_paths, [str(model_json.resolve())])
                page.deleteLater()
            finally:
                module.SettingsManager = original

    def test_project_search_opens_exact_and_unique_matches(self):
        module = importlib.import_module("app.gui.Live2DModPage")
        with tempfile.TemporaryDirectory() as directory:
            settings = _TestSettings(Path(directory))
            original = module.SettingsManager
            module.SettingsManager = lambda: settings
            try:
                page = module.Live2DModPage()
                alpha_path = str(
                    Path(directory) / "Alpha" / module.PROJECT_FILE_NAME
                )
                beta_path = str(
                    Path(directory) / "Beta Project" / module.PROJECT_FILE_NAME
                )
                page._project_combo_refreshing = True
                page.project_combo.blockSignals(True)
                page.project_combo.clear()
                page.project_combo.addItem("Alpha", userData=alpha_path)
                page.project_combo.addItem(
                    "Beta Project",
                    userData=beta_path,
                )
                page.project_combo.setCurrentIndex(-1)
                page.project_combo.blockSignals(False)
                page._project_combo_refreshing = False

                loaded = []
                page.load_project_file = loaded.append
                page.project_combo.setText("Alpha")
                self.assertEqual(loaded, [alpha_path])

                page.project_combo.setCurrentIndex(-1)
                page.project_combo.setText("Beta")
                self.assertEqual(loaded, [alpha_path])
                page.open_project_file()
                self.assertEqual(loaded, [alpha_path, beta_path])
                page.deleteLater()
            finally:
                module.SettingsManager = original


if __name__ == "__main__":
    unittest.main()
