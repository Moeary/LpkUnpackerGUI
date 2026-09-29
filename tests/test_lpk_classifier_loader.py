from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.core.extract.package_classifier import (
    PackageContentKind,
    _classify_archive_names,
    _classify_entry_json,
)
from app.core.lpk_loader import LpkDecryptError, LpkLoader
from app.core.utils import guess_type, is_spine_atlas_data


class LpkClassifierTests(unittest.TestCase):
    def test_archive_with_live2d_and_spine_entries_is_mixed(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "mixed.zip"
            with zipfile.ZipFile(archive, "w") as zip_file:
                zip_file.writestr(
                    "models/character.model3.json",
                    json.dumps(
                        {
                            "Version": 3,
                            "FileReferences": {
                                "Moc": "character.moc3",
                                "Textures": ["character.png"],
                            },
                        }
                    ),
                )
                zip_file.writestr(
                    "spine/character.atlas",
                    "\npage.png\n"
                    "size: 16,16\nformat: RGBA8888\n"
                    "filter: Linear,Linear\nrepeat: none\n"
                    "region\n  xy: 0,0\n",
                )

            self.assertEqual(_classify_archive_names(archive), PackageContentKind.MIXED)

    def test_spine_json_and_mixed_entry_are_classified(self):
        self.assertEqual(
            _classify_entry_json(
                {
                    "skeleton": {"spine": "3.8.99"},
                    "bones": [],
                    "slots": [],
                    "skins": {},
                }
            ),
            PackageContentKind.SPINE,
        )
        self.assertEqual(
            _classify_entry_json(
                {
                    "FileReferences": {
                        "Moc": "model.moc3",
                        "Textures": ["texture.png"],
                    },
                    "skeleton": {"spine": "3.8.99"},
                    "bones": [],
                    "slots": [],
                }
            ),
            PackageContentKind.MIXED,
        )
        self.assertEqual(
            _classify_entry_json(
                {
                    "FileReferences": {
                        "Moc": "0123456789abcdef0123456789abcdef.bin3",
                        "Textures": ["fedcba9876543210fedcba9876543210.bin3"],
                    }
                }
            ),
            PackageContentKind.LIVE2D,
        )


class LpkLoaderTests(unittest.TestCase):
    def test_guess_type_recognizes_spine_atlas_and_binary_skeleton(self):
        atlas = (
            b"\npage.png\nsize: 16,16\nformat: RGBA8888\n"
            b"filter: Linear,Linear\nrepeat: none\n"
            b"region\n  xy: 0,0\n"
        )
        skeleton = b"hash-value\x073.8.99\x00root\x00"
        self.assertEqual(guess_type(atlas), ".atlas")
        self.assertEqual(guess_type(skeleton), ".skel")

        # Newer atlas writers may omit size/format while retaining page
        # metadata and bounds.
        minimal_atlas = b"page.png\npma: true\nregion\n  bounds: 0,0,1,1\n"
        self.assertTrue(is_spine_atlas_data(minimal_atlas))
        self.assertEqual(guess_type(minimal_atlas), ".atlas")

    def test_failed_check_decrypt_raises_without_reading_stdin(self):
        loader = object.__new__(LpkLoader)
        loader.config = {"fileId": "wrong", "lpkFile": "12345.lpk"}
        loader.lpkpath = "12345.lpk"
        loader.last_decrypt_error = None

        def fail(_filename):
            raise UnicodeDecodeError("utf-8", b"x", 0, 1, "bad key")

        loader.decrypt_file = fail
        with patch("builtins.input", side_effect=AssertionError("input() called")):
            with self.assertRaises(LpkDecryptError) as context:
                loader.check_decrypt("entry.bin")

        error = context.exception
        self.assertEqual(error.filename, "entry.bin")
        self.assertEqual(error.attempted_file_ids, ("wrong", "12345"))
        self.assertIs(loader.last_decrypt_error, error)

    def test_atlas_page_header_can_be_rewritten_to_extracted_texture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            atlas_path = root / "model.atlas"
            atlas_path.write_text(
                "\noriginal.png\n"
                "size: 16,16\nformat: RGBA8888\n"
                "filter: Linear,Linear\nrepeat: none\n"
                "region\n  xy: 0,0\n",
                encoding="utf-8",
            )
            loader = object.__new__(LpkLoader)
            loader.trans = {"atlas.bin": "model.atlas", "texture.bin": "texture_0.png"}
            loader._trans_normalized = {
                "atlas.bin": "model.atlas",
                "texture.bin": "texture_0.png",
            }
            loader._rewrite_atlas_pages(
                {
                    "atlases": [
                        {
                            "atlas": "atlas.bin",
                            "textures": ["texture.bin"],
                        }
                    ]
                },
                str(root),
            )
            self.assertEqual(
                atlas_path.read_text(encoding="utf-8").splitlines()[1],
                "texture_0.png",
            )

    def test_atlas_page_rewrite_handles_two_pages_and_region_index(self):
        atlas = (
            "page-one.png\n"
            "size: 16,16\n"
            "filter: Linear,Linear\n"
            "region-one\n"
            "  xy: 0,0\n"
            "  index: -1\n"
            "\n"
            "page-two.png\n"
            "size: 32,32\n"
            "filter: Linear,Linear\n"
            "region-two\n"
            "  xy: 0,0\n"
            "  index: -1\n"
        )
        rewritten = LpkLoader._replace_atlas_page_headers(
            atlas,
            ["texture-one.png", "texture-two.png"],
        )
        lines = rewritten.splitlines()
        self.assertEqual(lines[0], "texture-one.png")
        self.assertEqual(lines[7], "texture-two.png")

    def test_atlas_page_rewrite_handles_two_pages_without_page_metadata(self):
        atlas = (
            "page-one.png\n"
            "\n"
            "region-one\n"
            "  bounds: 0,0,4,4\n"
            "  index: -1\n"
            "\n"
            "page-two.png\n"
            "\n"
            "region-two\n"
            "  bounds: 0,0,8,8\n"
            "  index: -1\n"
        )
        rewritten = LpkLoader._replace_atlas_page_headers(
            atlas,
            ["texture-one.png", "texture-two.png"],
        )
        lines = rewritten.splitlines()
        self.assertEqual(lines[0], "texture-one.png")
        self.assertEqual(lines[6], "texture-two.png")


if __name__ == "__main__":
    unittest.main()
