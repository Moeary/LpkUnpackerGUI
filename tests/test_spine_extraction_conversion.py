from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.extract import batch
from app.core.extract.models import ExtractItemResult, ExtractMode, ExtractSourceType
from app.core.spine_converter import (
    SpineConversionError,
    SpineConversionOptions,
    SpineConversionResult,
    SpineSourceError,
    discover_spine_conversion_sources,
)


class SpineExtractionConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="spine-extraction-conversion-")
        self.root = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _model_config(self, root: Path, name: str = "model0.json") -> Path:
        skeleton = root / "skeleton_0.skel4.0.37"
        skeleton.write_bytes(b"fixture skeleton")
        (root / "main.atlas").write_text("page.png\nsize:1,1\n", encoding="utf-8")
        config = root / name
        config.write_text(
            json.dumps(
                {
                    "type": 9,
                    "skeleton": skeleton.name,
                    "atlases": [{"atlas": "main.atlas", "textures": ["page.png"]}],
                }
            ),
            encoding="utf-8",
        )
        return config

    def test_discovery_prefers_model_config_deduplicates_skeleton_and_skips_old_output(self) -> None:
        config = self._model_config(self.root)
        previous = self.root / "Spine_Converted" / "old"
        previous.mkdir(parents=True)
        shutil.copy2(config, previous / config.name)
        shutil.copy2(self.root / "skeleton_0.skel4.0.37", previous / "skeleton_0.skel4.0.37")

        discovered = discover_spine_conversion_sources(self.root)

        self.assertEqual(discovered, [config.resolve()])

    def test_discovery_returns_unconfigured_skeleton_as_second_model(self) -> None:
        config = self._model_config(self.root)
        other = self.root / "standalone.skel"
        other.write_bytes(b"fixture standalone")

        discovered = discover_spine_conversion_sources(self.root)

        self.assertEqual(discovered, [config.resolve(), other.resolve()])

    def test_discovery_includes_real_json_skeleton_without_model_wrapper(self) -> None:
        skeleton = self.root / "standalone.json"
        skeleton.write_text(
            json.dumps(
                {
                    "skeleton": {"spine": "4.0.37"},
                    "bones": [{"name": "root"}],
                    "slots": [],
                    "skins": [],
                    "animations": {},
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(discover_spine_conversion_sources(self.root), [skeleton.resolve()])

    def test_discovery_rejects_model_wrapper_with_missing_skeleton(self) -> None:
        (self.root / "model0.json").write_text(
            json.dumps({"skeleton": "missing.skel", "atlases": []}),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(SpineSourceError, "Skeleton reference not found"):
            discover_spine_conversion_sources(self.root)

    def _successful_lpk(self, target: Path, source: Path) -> ExtractItemResult:
        target.mkdir(parents=True, exist_ok=True)
        return ExtractItemResult(
            source=source,
            source_type=ExtractSourceType.LPK,
            success=True,
            output_dir=target,
            exported_count=1,
            message="fake extraction",
            extracted_dirs=[target],
        )

    def test_enabled_full_lpk_conversion_is_a_child_in_independent_directory(self) -> None:
        source = self.root / "source.lpk"
        source.write_bytes(b"lpk")
        extracted_root = self.root / "extracted"
        model = extracted_root / "model0.json"
        converted_root = extracted_root / "spine_converted"
        converted_dir = converted_root / "model_to_3.8.75_json"
        converted_dir.mkdir(parents=True)
        converted = SpineConversionResult(
            output_dir=converted_dir,
            skeleton_path=converted_dir / "skeleton.json",
            report_path=converted_dir / "spine_conversion_report.json",
            warnings=("cross-family warning",),
        )
        options = SpineConversionOptions(
            enabled=True,
            target_version="3.8.75",
            output_format="json",
            converter_path="converter.exe",
        )

        with patch.object(batch, "detect_source_type", return_value=ExtractSourceType.LPK), \
                patch.object(batch, "extract_lpk", side_effect=lambda *_args: self._successful_lpk(extracted_root, source)), \
                patch.object(batch, "discover_spine_conversion_sources", return_value=[model]) as discover, \
                patch.object(batch, "convert_spine", return_value=converted) as convert:
            result = batch.run_extraction_batch(
                [source],
                self.root / "batch-output",
                ExtractMode.FULL,
                spine_conversion=options,
            )

        self.assertFalse(result.has_failures)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(len(result.items[0].children), 1)
        child = result.items[0].children[0]
        self.assertTrue(child.success)
        self.assertEqual(child.output_dir, converted_dir)
        discover.assert_called_once_with(extracted_root)
        convert.assert_called_once_with(
            model,
            extracted_root / "spine_converted",
            target_version="3.8.75",
            output_format="json",
            converter_path="converter.exe",
            remove_curve=False,
            create_project=False,
            editor_path=None,
        )

    def test_disabled_or_texture_only_lpk_does_not_invoke_converter(self) -> None:
        source = self.root / "source.lpk"
        source.write_bytes(b"lpk")
        extracted_root = self.root / "extracted"
        options = SpineConversionOptions(enabled=True)

        with patch.object(batch, "detect_source_type", return_value=ExtractSourceType.LPK), \
                patch.object(batch, "extract_lpk", side_effect=lambda *_args: self._successful_lpk(extracted_root, source)), \
                patch.object(batch, "discover_spine_conversion_sources") as discover, \
                patch.object(batch, "convert_spine") as convert:
            result = batch.run_extraction_batch(
                [source],
                self.root / "batch-output",
                ExtractMode.TEXTURES,
                spine_conversion=options,
            )

        self.assertTrue(result.items[0].success)
        discover.assert_not_called()
        convert.assert_not_called()

    def test_disabled_full_lpk_preserves_extraction_without_conversion(self) -> None:
        source = self.root / "source.lpk"
        source.write_bytes(b"lpk")
        extracted_root = self.root / "extracted"
        options = SpineConversionOptions(enabled=False, target_version="3.8.75")

        with patch.object(batch, "detect_source_type", return_value=ExtractSourceType.LPK), \
                patch.object(batch, "extract_lpk", side_effect=lambda *_args: self._successful_lpk(extracted_root, source)), \
                patch.object(batch, "discover_spine_conversion_sources") as discover, \
                patch.object(batch, "convert_spine") as convert:
            result = batch.run_extraction_batch(
                [source],
                self.root / "batch-output",
                ExtractMode.FULL,
                spine_conversion=options,
            )

        self.assertTrue(result.items[0].success)
        self.assertEqual(result.items[0].children, [])
        discover.assert_not_called()
        convert.assert_not_called()

    def test_conversion_failure_marks_parent_and_child_failed_and_logs_error(self) -> None:
        source = self.root / "source.lpk"
        source.write_bytes(b"lpk")
        extracted_root = self.root / "extracted"
        model = extracted_root / "model0.json"
        logs: list[tuple[str, str]] = []
        options = SpineConversionOptions(enabled=True)

        with patch.object(batch, "detect_source_type", return_value=ExtractSourceType.LPK), \
                patch.object(batch, "extract_lpk", side_effect=lambda *_args: self._successful_lpk(extracted_root, source)), \
                patch.object(batch, "discover_spine_conversion_sources", return_value=[model]), \
                patch.object(batch, "convert_spine", side_effect=SpineConversionError("converter rejected input")):
            result = batch.run_extraction_batch(
                [source],
                self.root / "batch-output",
                ExtractMode.FULL,
                log=lambda level, message: logs.append((level, message)),
                spine_conversion=options,
            )

        item = result.items[0]
        self.assertTrue(result.has_failures)
        self.assertFalse(item.success)
        self.assertIn("automatic Spine conversion", item.error or "")
        self.assertEqual(len(item.children), 1)
        self.assertFalse(item.children[0].success)
        self.assertIn("converter rejected input", item.children[0].error or "")
        self.assertTrue(any(level == "ERROR" and "converter rejected input" in message for level, message in logs))

    def test_wpk_conversion_uses_nested_created_dirs_and_ignores_shared_output_history(self) -> None:
        source = self.root / "source.wpk"
        source.write_bytes(b"wpk")
        output = self.root / "wpk-output"
        current = output / "current-model"
        current.mkdir(parents=True)
        current_model = current / "model0.json"
        current_model.write_text("{}", encoding="utf-8")
        old = output / "old-model"
        old.mkdir(parents=True)
        old_model = old / "old-model.json"
        old_model.write_text("{}", encoding="utf-8")
        nested = ExtractItemResult(
            source=self.root / "nested.lpk",
            source_type=ExtractSourceType.LPK,
            success=True,
            output_dir=current,
            extracted_dirs=[current],
        )
        parent = ExtractItemResult(
            source=source,
            source_type=ExtractSourceType.WPK,
            success=True,
            output_dir=output,
            children=[nested],
        )
        converted_dir = output / "spine_converted" / "current"
        converted = SpineConversionResult(
            output_dir=converted_dir,
            skeleton_path=converted_dir / "model.json",
            report_path=converted_dir / "report.json",
        )

        with patch.object(batch, "detect_source_type", return_value=ExtractSourceType.WPK), \
                patch.object(batch, "extract_wpk", return_value=parent), \
                patch.object(batch, "discover_spine_conversion_sources", side_effect=lambda root: [current_model] if root == current.resolve() else [old_model]), \
                patch.object(batch, "convert_spine", return_value=converted) as convert:
            result = batch.run_extraction_batch(
                [source],
                self.root / "batch-output",
                ExtractMode.FULL,
                spine_conversion=SpineConversionOptions(enabled=True),
            )

        self.assertTrue(result.items[0].success)
        convert.assert_called_once_with(
            current_model,
            output / "spine_converted",
            target_version="3.8.75",
            output_format="json",
            converter_path=None,
            remove_curve=False,
            create_project=False,
            editor_path=None,
        )

    def test_unity_item_never_enters_spine_conversion(self) -> None:
        source = self.root / "bundle"
        source.mkdir()
        output = self.root / "unity-output"
        item = ExtractItemResult(
            source=source,
            source_type=ExtractSourceType.UNITY,
            success=True,
            output_dir=output,
            message="fake Unity extraction",
        )
        options = SpineConversionOptions(enabled=True)

        with patch.object(batch, "detect_source_type", return_value=ExtractSourceType.UNITY), \
                patch.object(batch, "extract_unity", return_value=item), \
                patch.object(batch, "discover_spine_conversion_sources") as discover, \
                patch.object(batch, "convert_spine") as convert:
            result = batch.run_extraction_batch(
                [source],
                self.root / "batch-output",
                ExtractMode.FULL,
                spine_conversion=options,
            )

        self.assertTrue(result.items[0].success)
        discover.assert_not_called()
        convert.assert_not_called()

    def test_unity_output_with_spine_is_converted_under_its_own_root(self) -> None:
        source = self.root / "bundle"
        source.mkdir()
        output = self.root / "unity-output"
        output.mkdir()
        model = output / "model.json"
        item = ExtractItemResult(
            source=source,
            source_type=ExtractSourceType.UNITY,
            success=True,
            output_dir=output,
            extracted_dirs=[output],
            message="fake Unity Spine extraction",
        )
        converted_dir = output / "spine_converted" / "model"
        converted = SpineConversionResult(
            output_dir=converted_dir,
            skeleton_path=converted_dir / "skeleton.json",
            report_path=converted_dir / "report.json",
        )
        with patch.object(batch, "detect_source_type", return_value=ExtractSourceType.UNITY), \
                patch.object(batch, "extract_unity", return_value=item), \
                patch.object(batch, "discover_spine_conversion_sources", return_value=[model]) as discover, \
                patch.object(batch, "convert_spine", return_value=converted) as convert:
            result = batch.run_extraction_batch(
                [source],
                self.root / "batch-output",
                ExtractMode.FULL,
                spine_conversion=SpineConversionOptions(enabled=True),
            )

        self.assertTrue(result.items[0].success)
        discover.assert_called_once_with(output.resolve())
        convert.assert_called_once_with(
            model,
            output / "spine_converted",
            target_version="3.8.75",
            output_format="json",
            converter_path=None,
            remove_curve=False,
            create_project=False,
            editor_path=None,
        )

    def test_wpk_partial_failure_still_converts_successful_child(self) -> None:
        source = self.root / "source.wpk"
        source.write_bytes(b"wpk")
        output = self.root / "wpk-output"
        output.mkdir()
        child_root = output / "good"
        child_root.mkdir()
        model = child_root / "model.json"
        successful = ExtractItemResult(
            source=self.root / "good.lpk",
            source_type=ExtractSourceType.LPK,
            success=True,
            output_dir=child_root,
            extracted_dirs=[child_root],
        )
        failed = ExtractItemResult(
            source=self.root / "bad.lpk",
            source_type=ExtractSourceType.LPK,
            success=False,
            output_dir=output / "bad",
            error="bad child",
        )
        parent = ExtractItemResult(
            source=source,
            source_type=ExtractSourceType.WPK,
            success=False,
            output_dir=output,
            children=[successful, failed],
            error="1 child failed",
        )
        converted_dir = output / "spine_converted" / "model"
        converted = SpineConversionResult(
            output_dir=converted_dir,
            skeleton_path=converted_dir / "skeleton.json",
            report_path=converted_dir / "report.json",
        )
        with patch.object(batch, "detect_source_type", return_value=ExtractSourceType.WPK), \
                patch.object(batch, "extract_wpk", return_value=parent), \
                patch.object(batch, "discover_spine_conversion_sources", return_value=[model]) as discover, \
                patch.object(batch, "convert_spine", return_value=converted) as convert:
            result = batch.run_extraction_batch(
                [source],
                self.root / "batch-output",
                ExtractMode.FULL,
                spine_conversion=SpineConversionOptions(enabled=True),
            )

        self.assertFalse(result.items[0].success)
        discover.assert_called_once_with(child_root.resolve())
        convert.assert_called_once()
        self.assertTrue(any(child.success for child in result.items[0].children))


if __name__ == "__main__":
    unittest.main()
