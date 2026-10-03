import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from app.core.spine_converter import SpineConversionResult
from app.core.spine_preview import load_spine_asset
from app.core.spine_preview_conversion import (
    UNIFIED_PREVIEW_VERSION,
    SpinePreviewConversionError,
    prepare_spine_preview,
)
from app.core.preview.session import prepare_spine_preview_import


class SpinePreviewConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="spine-preview-conversion-"))
        self.source = self.temp / "source"
        self.source.mkdir()
        Image.new("RGBA", (16, 16), (255, 0, 0, 255)).save(self.source / "page.png")
        (self.source / "sample.atlas").write_text(
            "page.png\n"
            "size: 16, 16\n"
            "format: RGBA8888\n"
            "region\n"
            "  xy: 0, 0\n"
            "  size: 16, 16\n\n",
            encoding="utf-8",
        )
        (self.source / "skeleton.json").write_text(
            json.dumps(
                {
                    "skeleton": {"spine": "4.0.37"},
                    "bones": [{"name": "root"}],
                    "slots": [],
                    "skins": {"default": {}},
                    "animations": {"idle": {}},
                }
            ),
            encoding="utf-8",
        )
        (self.source / "model0.json").write_text(
            json.dumps(
                {
                    "skeleton": "skeleton.json",
                    "atlases": [{"atlas": "sample.atlas", "textures": ["page.png"]}],
                }
            ),
            encoding="utf-8",
        )
        self.converter = self.temp / "lpk_spine_converter.dll"
        self.converter.write_bytes(b"converter-v1")
        self.calls = 0

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def _convert(self, source, output_dir, **_kwargs):
        self.calls += 1
        destination = Path(output_dir) / "converted"
        destination.mkdir(parents=True)
        skeleton = destination / "skeleton.json"
        skeleton.write_text(
            json.dumps(
                {
                    "skeleton": {"spine": UNIFIED_PREVIEW_VERSION},
                    "bones": [{"name": "root"}],
                    "slots": [],
                    "skins": {"default": {}},
                    "animations": {"idle": {}},
                }
            ),
            encoding="utf-8",
        )
        shutil.copy2(self.source / "sample.atlas", destination / "sample.atlas")
        shutil.copy2(self.source / "page.png", destination / "page.png")
        report = destination / "spine_conversion_report.json"
        report.write_text("{}\n", encoding="utf-8")
        return SpineConversionResult(
            output_dir=destination,
            skeleton_path=skeleton,
            report_path=report,
        )

    def _prepare(self, source_root=None):
        root = Path(source_root or self.source)
        asset = load_spine_asset(root / "model0.json")
        return prepare_spine_preview(
            asset,
            self.temp / "cache",
            unify_version=True,
        )

    def test_non_target_version_uses_cached_copy_and_is_path_independent(self):
        with mock.patch(
            "app.core.spine_preview_conversion.discover_native_converter",
            return_value=self.converter,
        ), mock.patch(
            "app.core.spine_preview_conversion.convert_spine",
            side_effect=self._convert,
        ):
            first = self._prepare()
            self.assertTrue(first.conversion.converted)
            self.assertEqual(first.conversion.source_version, "4.0.37")
            self.assertEqual(first.conversion.target_version, UNIFIED_PREVIEW_VERSION)
            self.assertEqual(first.asset.spine_version, UNIFIED_PREVIEW_VERSION)

            other = self.temp / "other-source"
            shutil.copytree(self.source, other)
            second = self._prepare(other)
            self.assertEqual(second.asset.spine_version, UNIFIED_PREVIEW_VERSION)
            self.assertEqual(self.calls, 1)

    def test_corrupt_cache_is_rebuilt(self):
        with mock.patch(
            "app.core.spine_preview_conversion.discover_native_converter",
            return_value=self.converter,
        ), mock.patch(
            "app.core.spine_preview_conversion.convert_spine",
            side_effect=self._convert,
        ):
            first = self._prepare()
            first.asset.skeleton_path.write_text("corrupt", encoding="utf-8")
            second = self._prepare()
            self.assertEqual(second.asset.spine_version, UNIFIED_PREVIEW_VERSION)
            self.assertEqual(self.calls, 2)

    def test_conversion_failure_is_explicit_and_does_not_modify_source(self):
        source_bytes = (self.source / "skeleton.json").read_bytes()
        with mock.patch(
            "app.core.spine_preview_conversion.discover_native_converter",
            return_value=self.converter,
        ), mock.patch(
            "app.core.spine_preview_conversion.convert_spine",
            side_effect=RuntimeError("synthetic converter failure"),
        ):
            with self.assertRaises(SpinePreviewConversionError):
                self._prepare()
        self.assertEqual((self.source / "skeleton.json").read_bytes(), source_bytes)
        self.assertFalse(list((self.temp / "cache").glob(".spine_preview_build_*")))

    def test_unify_disabled_keeps_source_and_does_not_call_converter(self):
        asset = load_spine_asset(self.source / "model0.json")
        with mock.patch("app.core.spine_preview_conversion.convert_spine") as convert, mock.patch(
            "app.core.spine_preview_conversion.discover_native_converter"
        ) as discover:
            prepared = prepare_spine_preview(asset, self.temp / "cache", unify_version=False)
        self.assertEqual(prepared.asset.spine_version, "4.0.37")
        convert.assert_not_called()
        discover.assert_not_called()

    def test_exact_target_keeps_source_and_does_not_call_converter(self):
        skeleton = self.source / "skeleton.json"
        data = json.loads(skeleton.read_text(encoding="utf-8"))
        data["skeleton"]["spine"] = UNIFIED_PREVIEW_VERSION
        skeleton.write_text(json.dumps(data), encoding="utf-8")
        asset = load_spine_asset(self.source / "model0.json")
        with mock.patch("app.core.spine_preview_conversion.convert_spine") as convert, mock.patch(
            "app.core.spine_preview_conversion.discover_native_converter"
        ) as discover:
            prepared = prepare_spine_preview(asset, self.temp / "cache", unify_version=True)
        self.assertEqual(prepared.asset.spine_version, UNIFIED_PREVIEW_VERSION)
        self.assertFalse(prepared.conversion.converted)
        convert.assert_not_called()
        discover.assert_not_called()

    def test_texture_content_change_invalidates_cache(self):
        with mock.patch(
            "app.core.spine_preview_conversion.discover_native_converter",
            return_value=self.converter,
        ), mock.patch(
            "app.core.spine_preview_conversion.convert_spine",
            side_effect=self._convert,
        ):
            self._prepare()
            Image.new("RGBA", (16, 16), (0, 255, 0, 255)).save(self.source / "page.png")
            self._prepare()
        self.assertEqual(self.calls, 2)

    def test_model_config_content_change_invalidates_cache(self):
        with mock.patch(
            "app.core.spine_preview_conversion.discover_native_converter",
            return_value=self.converter,
        ), mock.patch(
            "app.core.spine_preview_conversion.convert_spine",
            side_effect=self._convert,
        ):
            self._prepare()
            config = self.source / "model0.json"
            data = json.loads(config.read_text(encoding="utf-8"))
            data["preview_metadata"] = {"revision": 2}
            config.write_text(json.dumps(data), encoding="utf-8")
            self._prepare()
        self.assertEqual(self.calls, 2)

    def test_session_conversion_failure_removes_owned_extraction_temp(self):
        extracted = self.temp / "extracted"
        extracted.mkdir()
        asset = load_spine_asset(self.source / "model0.json")
        session_temp = self.temp / "session-temp"
        session_temp.mkdir()
        result = SimpleNamespace(asset=asset, temp_dir=extracted)
        with mock.patch(
            "app.core.preview.session._prepare_spine_preview_import",
            return_value=result,
        ), mock.patch(
            "app.core.spine_preview_conversion.convert_spine",
            side_effect=RuntimeError("synthetic converter failure"),
        ):
            with self.assertRaises(SpinePreviewConversionError):
                prepare_spine_preview_import(
                    "synthetic-source",
                    session_temp,
                    unify_version=True,
                )
        self.assertFalse(extracted.exists())


if __name__ == "__main__":
    unittest.main()
