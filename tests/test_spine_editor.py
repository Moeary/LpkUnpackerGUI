from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.core.spine_editor import (
    SpineEditorProcessError,
    create_spine_editor_project,
    discover_spine_editor,
)


class SpineEditorProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="spine-editor-test-", dir=Path.cwd()))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _fixture(self) -> tuple[Path, Path, Path]:
        output = self.root / "converted"
        output.mkdir()
        source = output / "skeleton.json"
        source_data = {
            "skeleton": {"spine": "3.8.75", "images": "./tex/"},
            "bones": [{"name": "root"}],
            "slots": [{"name": "slot", "bone": "root"}],
            "skins": [
                {
                    "name": "default",
                    "attachments": {
                        "slot": {
                            "hero": {"type": "region", "path": "hero"},
                            "cloth": {
                                "type": "mesh",
                                "path": "cloth",
                                "uvs": [0.1, 0.2, 0.9, 0.2, 0.9, 0.8, 0.1, 0.8],
                                "triangles": [0, 1, 2, 2, 3, 0],
                                "vertices": [-1, -1, 1, -1, 1, 1, -1, 1],
                                "hull": 4,
                            },
                        }
                    },
                }
            ],
            "animations": {},
        }
        source.write_text(json.dumps(source_data), encoding="utf-8")

        page = output / "page.png"
        image = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        image.paste((220, 70, 40, 255), (1, 1, 2, 2))
        image.paste((40, 120, 220, 255), (4, 1, 2, 2))
        image.save(page)
        atlas = output / "main.atlas"
        atlas.write_text(
            "\n".join(
                [
                    "page.png",
                    "size: 8, 8",
                    "format: RGBA8888",
                    "hero",
                    "bounds: 1, 1, 2, 2",
                    "cloth",
                    "bounds: 4, 1, 2, 2",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        editor = self.root / "Spine.com"
        editor.write_bytes(b"fixture editor")
        return source, atlas, editor

    def test_project_rewrites_paths_and_preserves_mesh_uvs(self) -> None:
        source, atlas, editor = self._fixture()
        original_source = source.read_bytes()

        def fake_cli(command, *, cwd):
            command = list(command)
            if "-o" in command:
                project = Path(command[command.index("-o") + 1])
                project.write_bytes(b"real project fixture")
            return subprocess.CompletedProcess(
                command,
                0,
                "Spine version: 3.8.75\nSkeleton: skeleton\n",
                "",
            )

        with patch("app.core.spine_editor.discover_spine_editor", return_value=editor), \
                patch("app.core.spine_editor._run_editor_command", side_effect=fake_cli):
            result = create_spine_editor_project(
                source,
                [atlas],
                source.parent,
                editor_path=editor,
            )

        self.assertTrue(result.project_path.is_file())
        self.assertTrue(result.report_path.is_file())
        self.assertEqual(source.read_bytes(), original_source)
        imported = json.loads(result.import_json_path.read_text(encoding="utf-8"))
        self.assertEqual(imported["skeleton"]["images"], "./images/")
        attachments = imported["skins"][0]["attachments"]["slot"]
        self.assertEqual(attachments["hero"]["path"], "atlas_00/regions/p00_hero_i-1")
        self.assertEqual(attachments["cloth"]["path"], "atlas_00/regions/p00_cloth_i-1")
        self.assertEqual(attachments["cloth"]["uvs"], [0.1, 0.2, 0.9, 0.2, 0.9, 0.8, 0.1, 0.8])
        self.assertTrue(all(not item["path"].casefold().endswith(".png") for item in attachments.values()))
        self.assertTrue((result.images_dir / "atlas_00" / "regions" / "p00_hero_i-1.png").is_file())
        self.assertTrue((result.images_dir / "atlas_00" / "regions" / "p00_cloth_i-1.png").is_file())

    def test_existing_package_gets_numbered_sibling(self) -> None:
        source, atlas, editor = self._fixture()
        (source.parent / "editor_project").mkdir()
        with patch("app.core.spine_editor.discover_spine_editor", return_value=editor), \
                patch("app.core.spine_editor._run_editor_command", side_effect=self._fake_cli):
            result = create_spine_editor_project(source, [atlas], source.parent, editor_path=editor)
        self.assertEqual(result.project_path.parent.name, "editor_project_2")

    @staticmethod
    def _fake_cli(command, *, cwd):
        command = list(command)
        if "-o" in command:
            Path(command[command.index("-o") + 1]).write_bytes(b"project")
        return subprocess.CompletedProcess(
            command, 0, "Spine version: 3.8.75\nSkeleton: skeleton\n", ""
        )

    def test_editor_cli_timeout_is_bounded(self) -> None:
        with patch(
            "app.core.spine_editor.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["Spine.com"], 120),
        ):
            with self.assertRaisesRegex(SpineEditorProcessError, "timed out after 120 seconds"):
                from app.core.spine_editor import _run_editor_command

                _run_editor_command(["Spine.com", "-i", "input.json"], cwd=self.root)

    def test_explicit_editor_path_is_authoritative(self) -> None:
        _, _, editor = self._fixture()
        self.assertEqual(discover_spine_editor(editor), editor.resolve())
        self.assertIsNone(discover_spine_editor(self.root / "missing" / "Spine.com"))


if __name__ == "__main__":
    unittest.main()
