from __future__ import annotations

import json
import hashlib
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
from app.core.utils import decrypt, genkey, guess_type, hashed_filename, is_spine_atlas_data, normalize

_PACK_ID = "com.example.pack"
_MODEL_A = "a" * 32 + ".bin"
_MODEL_B = "b" * 32 + ".bin"
_TEXTURE = "c" * 32 + ".bin"
_PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


def _write_std2_lpk(path: Path, characters: list, models: dict) -> Path:
    """Write a minimal encrypted STD2_0 package; LPK encryption is symmetric."""

    def encrypt(name: str, data: bytes) -> bytes:
        return decrypt(genkey(_PACK_ID + name), data)

    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr(
            hashed_filename("config.mlve"),
            json.dumps({"type": "STD2_0", "id": _PACK_ID, "list": characters}),
        )
        for name, model in models.items():
            bundle.writestr(name, encrypt(name, json.dumps(model).encode()))
        bundle.writestr(_TEXTURE, encrypt(_TEXTURE, _PNG))
    return path


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
    def test_decrypt_keeps_lpk_block_reset_compatibility(self):
        # The LPK stream key resets at every 1024 bytes.  This fixture spans
        # that boundary so a single-stream optimization cannot pass silently.
        payload = bytes(range(251)) * 7 + bytes(range(17))
        decrypted = decrypt(0x12345678, payload)
        self.assertEqual(len(decrypted), len(payload))
        self.assertEqual(
            hashlib.sha256(decrypted).hexdigest(),
            "46b610f4e177105d1eed77eee7a3eeb3e5463b32d4aaf2aea5d86e5c3685c005",
        )

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


class LpkExtractTests(unittest.TestCase):
    def test_std2_package_extracts_without_steam_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lpk = _write_std2_lpk(
                root / "pack.lpk",
                [{"character": "hero", "costume": [{"path": _MODEL_A}]}],
                {_MODEL_A: {"textures": [_TEXTURE]}},
            )
            created = LpkLoader(str(lpk), None).extract(str(root / "out"))

            self.assertEqual(created, [str(root / "out" / "hero")])
            model = json.loads((root / "out/hero/model0.json").read_text(encoding="utf-8"))
            self.assertEqual(model["textures"], ["textures_0_0.png"])
            self.assertEqual((root / "out/hero/textures_0_0.png").read_bytes(), _PNG)

    def test_model_keys_cannot_write_outside_output_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lpk = _write_std2_lpk(
                root / "pack.lpk",
                [{"character": "hero", "costume": [{"path": _MODEL_A}]}],
                {_MODEL_A: {"../../escaped": _TEXTURE}},
            )
            output = root / "nested" / "out"
            with self.assertRaisesRegex(ValueError, "escapes the output directory"):
                LpkLoader(str(lpk), None).extract(str(output))

            self.assertEqual(list(root.glob("escaped*")), [])
            self.assertEqual(list((root / "nested").glob("escaped*")), [])

    def test_each_character_directory_is_self_contained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lpk = _write_std2_lpk(
                root / "pack.lpk",
                [
                    {"character": "first", "costume": [{"path": _MODEL_A}]},
                    {"character": "second", "costume": [{"path": _MODEL_B}]},
                ],
                {_MODEL_A: {"textures": [_TEXTURE]}, _MODEL_B: {"textures": [_TEXTURE]}},
            )
            LpkLoader(str(lpk), None).extract(str(root / "out"))

            for character in ("first", "second"):
                folder = root / "out" / character
                self.assertEqual(
                    sorted(path.name for path in folder.iterdir()),
                    ["model0.json", "textures_0_0.png"],
                )
                model = json.loads((folder / "model0.json").read_text(encoding="utf-8"))
                self.assertTrue((folder / model["textures"][0]).is_file())

    def test_dot_only_names_do_not_resolve_to_parent_directories(self):
        self.assertEqual(normalize(".."), "unnamed")
        self.assertEqual(normalize("../.."), "unnamed")
        self.assertEqual(normalize(" . "), "unnamed")
        self.assertEqual(normalize("v1.0"), "v1.0")


if __name__ == "__main__":
    unittest.main()
