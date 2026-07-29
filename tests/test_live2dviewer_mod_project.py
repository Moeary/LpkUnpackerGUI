from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image

from app.core.live2dviewer_mod_project import (
    BUILD_DIR,
    MAPPING_TABLE_FILE,
    PROJECT_FILE_NAME,
    SKIN_MANIFEST_FILE,
    Live2DViewerModProjectError,
    add_model_to_project,
    create_project_from_base_source,
    delete_project_directory,
    export_live2dviewer_mod,
    generate_live2dviewer_mod,
    load_project,
    move_model_in_project,
    remove_model_from_project,
    rename_model_skin,
    rename_project,
    save_project,
)


class Live2DViewerModProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.main_source = self._make_model("main_source", (128, 128), (255, 0, 0, 255))
        self.skin_source = self._make_model("skin_source", (128, 128), (0, 0, 255, 255))

    def tearDown(self):
        self.temp.cleanup()

    def _make_model(
        self,
        name: str,
        size: tuple[int, int],
        color: tuple[int, int, int, int],
        texture_count: int = 2,
        include_hit_areas: bool = True,
    ) -> Path:
        root = self.root / name
        texture_dir = root / "textures"
        texture_dir.mkdir(parents=True)
        (root / f"{name}.moc3").write_bytes(b"test-moc")
        texture_refs = []
        for index in range(texture_count):
            texture_name = f"texture_{index:02d}.png"
            texture_refs.append(f"textures/{texture_name}")
            texture_size = (
                size
                if index == 0
                else (max(1, size[0] // 2), max(1, size[1] // 2))
            )
            Image.new("RGBA", texture_size, color).save(
                texture_dir / texture_name
            )
        model_json = root / f"{name}.model3.json"
        model_json.write_text(
            json.dumps(
                {
                    "Version": 3,
                    "HitAreas": (
                        [
                            {"Name": "Body", "Id": "ArtMeshBody"},
                            {"Name": "Head", "Id": "ArtMeshHead"},
                        ]
                        if include_hit_areas
                        else []
                    ),
                    "FileReferences": {
                        "Moc": f"{name}.moc3",
                        "Textures": texture_refs,
                    },
                }
            ),
            encoding="utf-8",
        )
        return model_json

    def test_project_add_reorder_rename_remove_and_export(self):
        project = create_project_from_base_source(
            self.main_source.parent / "textures" / "texture_00.png",
            project_name="Example Project",
            output_root=self.root / "projects",
        )
        self.assertTrue(project.project_file.is_file())
        self.assertEqual(project.project_file.name, PROJECT_FILE_NAME)
        self.assertEqual(len(project.models), 1)
        self.assertTrue(project.base_model_json.is_file())

        project = add_model_to_project(
            project,
            self.skin_source.parent / "textures" / "texture_00.png",
            skin_name="Blue Skin",
        )
        self.assertEqual(len(project.models), 2)
        self.assertEqual(len(project.models[1]["texture_mappings"]), 2)
        imported_workspace = (
            project.project_dir / project.models[1]["workspace_path"]
        ).resolve()
        self.assertTrue(imported_workspace.is_dir())

        project = rename_model_skin(
            project,
            project.models[1]["id"],
            "Night Blue",
        )
        self.assertEqual(project.models[1]["skin_name"], "Night Blue")

        project = move_model_in_project(
            project,
            project.models[1]["id"],
            0,
        )
        self.assertEqual(project.models[0]["skin_name"], "Night Blue")
        self.assertEqual(len(project.models[1]["texture_mappings"]), 2)
        self.assertEqual(len(project.data["artmesh_areas"]), 2)
        project.data["selected_artmesh_id"] = "ArtMeshBody"
        project = load_project(
            save_project(project.project_dir, project.data)
        )

        archive = self.root / "result.zip"
        project, archive = export_live2dviewer_mod(project, archive)
        self.assertTrue(archive.is_file())
        self.assertTrue((project.project_dir / BUILD_DIR / SKIN_MANIFEST_FILE).is_file())
        self.assertTrue((project.project_dir / BUILD_DIR / MAPPING_TABLE_FILE).is_file())
        with zipfile.ZipFile(archive) as package:
            names = set(package.namelist())
        self.assertIn(SKIN_MANIFEST_FILE, names)
        self.assertIn(MAPPING_TABLE_FILE, names)
        self.assertTrue(any(name.endswith(".model3.json") for name in names))
        manifest = json.loads(
            (project.project_dir / BUILD_DIR / SKIN_MANIFEST_FILE).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            manifest["artmesh_trigger"]["id"],
            "ArtMeshBody",
        )
        mapping_table = json.loads(
            (project.project_dir / BUILD_DIR / MAPPING_TABLE_FILE).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            len(mapping_table["models"][1]["texture_mappings"]),
            2,
        )
        for model_path in project.data["generated_model_json_paths"]:
            generated = json.loads(
                (project.project_dir / model_path).read_text(encoding="utf-8")
            )
            selected_area = next(
                item
                for item in generated["HitAreas"]
                if item["Id"] == "ArtMeshBody"
            )
            self.assertEqual(selected_area["Motion"], "TapSwitchSkin")

        removed_id = project.models[1]["id"]
        removed_workspace = (
            project.project_dir / project.models[1]["workspace_path"]
        ).resolve()
        project = remove_model_from_project(project, removed_id)
        self.assertEqual(len(project.models), 1)
        self.assertFalse(removed_workspace.exists())
        self.assertEqual(load_project(project.project_file).models, project.models)

    def test_manual_artmesh_id_is_rejected_when_model_has_no_hit_areas(self):
        source = self._make_model(
            "no_hit_areas",
            (64, 64),
            (120, 120, 120, 255),
            texture_count=1,
            include_hit_areas=False,
        )
        project = create_project_from_base_source(
            source,
            output_root=self.root / "manual-projects",
        )
        self.assertEqual(project.data["artmesh_areas"], [])
        project.data["selected_artmesh_id"] = "ArtMeshCustomButton"
        project = load_project(save_project(project.project_dir, project.data))
        self.assertEqual(project.data["selected_artmesh_id"], "")

    def test_bound_artmesh_is_rejected_for_skin_switching(self):
        project = create_project_from_base_source(
            self.main_source,
            output_root=self.root / "bound-projects",
        )
        project = add_model_to_project(project, self.skin_source)
        project.data["artmesh_areas"][0]["motion"] = "TapBody"
        project.data["selected_artmesh_id"] = "ArtMeshBody"
        project = load_project(save_project(project.project_dir, project.data))
        with self.assertRaisesRegex(
            Live2DViewerModProjectError,
            "already has an event",
        ):
            generate_live2dviewer_mod(project)

    def test_project_can_be_renamed_and_deleted_safely(self):
        project = create_project_from_base_source(
            self.main_source,
            project_name="Before",
            output_root=self.root / "crud-projects",
        )
        old_dir = project.project_dir
        project = rename_project(project, "After")
        self.assertFalse(old_dir.exists())
        self.assertEqual(project.project_name, "After")
        self.assertEqual(project.project_dir.name, "After")
        self.assertEqual(load_project(project.project_file).project_name, "After")
        removed = delete_project_directory(project)
        self.assertEqual(removed.name, "After")
        self.assertFalse(removed.exists())

    def test_legacy_project_is_migrated_to_models(self):
        project_dir = self.root / "legacy"
        project_dir.mkdir()
        project_file = project_dir / PROJECT_FILE_NAME
        project_file.write_text(
            json.dumps(
                {
                    "format": "LpkUnpacker.Live2DViewerModProject",
                    "version": 1,
                    "project_name": "legacy",
                    "selected_hit_area": "ArtMeshBody",
                    "hit_areas": [{"id": "ArtMeshBody", "name": "Body"}],
                    "base_source": {"path": "old.lpk", "name": "Original"},
                    "base_model_json": "workspace/base/model.model3.json",
                    "imported_workspace_paths": {"base": "workspace/base"},
                    "skins": [
                        {
                            "id": "skin_1",
                            "name": "Legacy Skin",
                            "source": "skin.png",
                            "texture_replacements": [
                                {
                                    "source": "workspace/imports/skin.png",
                                    "target_index": 0,
                                    "target_texture": "workspace/base/texture.png",
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        project = load_project(project_file)
        self.assertEqual(project.data["version"], 3)
        self.assertEqual(len(project.models), 2)
        self.assertEqual(project.models[1]["skin_name"], "Legacy Skin")
        self.assertEqual(project.data["selected_artmesh_id"], "ArtMeshBody")
        self.assertEqual(project.data["artmesh_areas"][0]["id"], "ArtMeshBody")
        self.assertNotIn("skins", project.data)


if __name__ == "__main__":
    unittest.main()
