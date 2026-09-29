from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.assetstudio_cli import AssetStudioCLIError, validate_unityfs_bundle
from app.core.cubism_core import CubismCoreError
from app.core.extract.models import ExtractMode
from app.core.extract.unity import extract_unity, validate_live2d_export


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
