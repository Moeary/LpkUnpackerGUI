from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from app.core.model.importer import _resolve_local_live2d_package
from app.core.model.resolver import Live2DPackageError


class Live2DModelImporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _make_model(self, root: Path, name: str = "model") -> tuple[Path, Path]:
        textures = root / "textures"
        textures.mkdir(parents=True)
        texture = textures / "texture_00.png"
        Image.new("RGBA", (32, 32), (220, 80, 90, 255)).save(texture)
        (root / f"{name}.moc3").write_bytes(b"moc")
        model_json = root / f"{name}.model3.json"
        model_json.write_text(
            json.dumps(
                {
                    "Version": 3,
                    "FileReferences": {
                        "Moc": f"{name}.moc3",
                        "Textures": ["textures/texture_00.png"],
                    },
                }
            ),
            encoding="utf-8",
        )
        return model_json, texture

    def test_extensionless_unity_file_does_not_reuse_parent_model(self):
        unrelated_dir = self.root / "already_extracted"
        self._make_model(unrelated_dir, "unrelated")
        bundle = self.root / "dafeng_7"
        bundle.write_bytes(b"UnityFS\0fake")

        with self.assertRaises(Live2DPackageError):
            _resolve_local_live2d_package(bundle)

    def test_nested_texture_resolves_only_its_direct_ancestor_model(self):
        model_dir = self.root / "character"
        model_json, texture = self._make_model(model_dir, "character")
        another_dir = self.root / "another_character"
        self._make_model(another_dir, "another")

        package = _resolve_local_live2d_package(texture)

        self.assertEqual(package.model_json, model_json.resolve())
        self.assertEqual(package.root_dir, model_dir.resolve())


if __name__ == "__main__":
    unittest.main()
