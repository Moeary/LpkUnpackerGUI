from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.assetstudio_cli import AssetStudioCLIError, AssetStudioResult, validate_unityfs_bundle
from app.core.cubism_core import CubismCoreError
from app.core.extract.models import ExtractMode
from app.core.extract.unity import (
    extract_unity, validate_live2d_export, _materialize_spine_text_assets,
)


def _write_unityfs(path: Path, declared_size: int, payload: bytes = b"") -> None:
    header = (
        b"UnityFS\x00"
        + struct.pack(">I", 8)
        + b"5.x.x\x00"
        + b"0.0.0\x00"
        + struct.pack(">Q", declared_size)
        + struct.pack(">II", 0, 0)
        + b"CAB-example.resS\x00"
    )
    path.write_bytes(header + payload)


def _write_model(root: Path, textures: list[str]) -> tuple[Path, Path]:
    moc = root / "chaijun_6.moc3"
    moc.write_bytes(b"MOC3\x05\x00\x00\x00")
    model = root / "chaijun_6.model3.json"
    model.write_text(
        json.dumps(
            {
                "Version": 3,
                "FileReferences": {
                    "Moc": moc.name,
                    "Textures": textures,
                },
            }
        ),
        encoding="utf-8",
    )
    return model, moc


class UnityExtractionTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="unity-extraction-test-")
        self.root = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()

    def test_validate_unityfs_bundle_rejects_truncated_source(self):
        source = self.root / "chaijun_6"
        _write_unityfs(source, declared_size=4096, payload=b"partial")

        with self.assertRaisesRegex(AssetStudioCLIError, "truncated") as context:
            validate_unityfs_bundle(source)

        self.assertIn("CAB-example.resS", str(context.exception))
        self.assertIn("obtain the complete UnityFS", str(context.exception))

    def test_validate_unityfs_bundle_warns_but_allows_trailing_bytes(self):
        source = self.root / "wrapped_bundle"
        _write_unityfs(source, declared_size=1, payload=b"wrapper")

        warning = validate_unityfs_bundle(source)

        self.assertIsNotNone(warning)
        self.assertIn("trailing byte", warning or "")

    def test_full_unity_export_can_resolve_spine_text_asset(self):
        source = self.root / "spine_bundle"
        source.mkdir()
        output = self.root / "output"

        class FakeCLI:
            executable = self.root / "AssetStudioModCLI.exe"

            def export_live2d(self, _source, target):
                target = Path(target)
                target.mkdir(parents=True, exist_ok=True)
                return AssetStudioResult(
                    command=[], output_dir=target, exported_files=[],
                    stdout="", stderr="", returncode=0,
                )

            def export_spine(self, _source, target):
                target = Path(target)
                target.mkdir(parents=True, exist_ok=True)
                skeleton = target / "hero.txt"
                skeleton.write_text(
                    json.dumps(
                        {
                            "skeleton": {"spine": "4.0.37"},
                            "bones": [{"name": "root"}],
                            "slots": [],
                            "skins": {},
                            "animations": {"idle": {}},
                        }
                    ),
                    encoding="utf-8",
                )
                atlas = target / "hero_atlas.txt"
                atlas.write_text(
                    "page.png\nsize: 1,1\nformat: RGBA8888\nregion\n  xy: 0,0\n  size: 1,1\n",
                    encoding="utf-8",
                )
                (target / "page.png").write_bytes(b"png")
                return AssetStudioResult(
                    command=[], output_dir=target,
                    exported_files=[skeleton, atlas, target / "page.png"],
                    stdout="", stderr="", returncode=0,
                )

        with patch("app.core.extract.unity.AssetStudioCLI", return_value=FakeCLI()):
            result = extract_unity(
                source,
                output,
                ExtractMode.LIVE2D,
                include_spine=True,
            )

        self.assertTrue(result.success)
        self.assertIn("Spine", result.message)
        self.assertEqual(len(result.extracted_dirs), 1)
        self.assertTrue((result.extracted_dirs[0] / "hero.json").is_file())
        self.assertTrue(result.extracted_dirs[0].is_relative_to(output))

    def test_binary_text_asset_is_copied_without_utf8_roundtrip(self):
        raw = b"\x91\xff\x004.0.37\x00\xfe\x80"
        source = self.root / "hero.bytes"
        source.write_bytes(raw)
        restored = _materialize_spine_text_assets(self.root, [source])
        self.assertEqual(restored, [self.root / "hero.skel"])
        self.assertEqual(restored[0].read_bytes(), raw)
        self.assertEqual(source.read_bytes(), raw)

    def test_text_asset_does_not_scan_old_files_when_export_is_empty(self):
        (self.root / "old.txt").write_text(json.dumps({
            "skeleton": {"spine": "3.8.75"}, "bones": [], "slots": [],
        }), encoding="utf-8")
        self.assertEqual(_materialize_spine_text_assets(self.root, []), [])
        self.assertFalse((self.root / "old.json").exists())

    def test_extract_unity_rejects_truncated_source_before_cli(self):
        source = self.root / "chaijun_6"
        _write_unityfs(source, declared_size=4096, payload=b"partial")
        output = self.root / "output"

        with patch("app.core.extract.unity.AssetStudioCLI") as cli_type:
            result = extract_unity(source, output, ExtractMode.LIVE2D)

        self.assertFalse(result.success)
        self.assertIn("truncated", result.error or "")
        cli_type.assert_not_called()

    def test_live2d_export_rejects_model_without_textures(self):
        model, _moc = _write_model(self.root, [])

        with patch(
            "app.core.extract.unity.CubismCore",
            side_effect=CubismCoreError("Live2DCubismCore.dll was not found"),
        ):
            with self.assertRaisesRegex(AssetStudioCLIError, "no texture references"):
                validate_live2d_export(self.root, [model])

    def test_live2d_export_rejects_native_inconsistent_moc(self):
        texture = self.root / "texture_00.png"
        texture.write_bytes(b"png")
        model, moc = _write_model(self.root, [texture.name])

        class FakeCore:
            def validate_moc(self, path: Path) -> int:
                assert path == moc.resolve()
                raise CubismCoreError("csmHasMocConsistency returned false")

        with patch("app.core.extract.unity.CubismCore", return_value=FakeCore()):
            with self.assertRaisesRegex(AssetStudioCLIError, "invalid MOC3"):
                validate_live2d_export(self.root, [model])

    def test_live2d_export_returns_warning_when_core_is_unavailable(self):
        texture = self.root / "texture_00.png"
        texture.write_bytes(b"png")
        model, _moc = _write_model(self.root, [texture.name])

        with patch(
            "app.core.extract.unity.CubismCore",
            side_effect=CubismCoreError("Live2DCubismCore.dll was not found"),
        ):
            warnings = validate_live2d_export(self.root, [model])

        self.assertEqual(len(warnings), 1)
        self.assertIn("consistency validation was skipped", warnings[0])
