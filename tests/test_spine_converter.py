import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.core.spine_converter import (
    SpineConverterNotFoundError,
    SpineConversionProcessError,
    SpineSourceAmbiguousError,
    SpineSourceError,
    SpineUnsupportedError,
    convert_spine,
    discover_converter,
)


class SpineConverterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="spine-converter-test-", dir=Path.cwd()))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.converter = self.root / "lpk_spine_converter.dll"
        self.converter.write_bytes(b"test converter placeholder")

    def _skeleton_json(self, path: Path, version: str = "4.0.37") -> None:
        path.write_text(
            json.dumps(
                {
                    "skeleton": {"hash": "fixture", "spine": version},
                    "bones": [{"name": "root"}],
                    "slots": [],
                    "skins": [],
                    "animations": {},
                }
            ),
            encoding="utf-8",
        )

    def _atlas(self, directory: Path, *, rotate: int = 0, scale: float | None = None) -> tuple[Path, Path]:
        page = directory / "page.png"
        straight = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        straight.putpixel((1, 1), (200, 100, 50, 128))
        # Store the page as PMA so that the output check catches accidental
        # byte-copying after the atlas has declared pma:true.
        pma = Image.new("RGBA", straight.size)
        for y in range(straight.height):
            for x in range(straight.width):
                red, green, blue, alpha = straight.getpixel((x, y))
                pma.putpixel(
                    (x, y),
                    (
                        (red * alpha + 127) // 255,
                        (green * alpha + 127) // 255,
                        (blue * alpha + 127) // 255,
                        alpha,
                    ),
                )
        pma.save(page)
        atlas = directory / "main.atlas"
        atlas_lines = ["page.png", "size:8,8", "filter:Linear,Linear", "pma:true"]
        if scale is not None:
            atlas_lines.append(f"scale:{scale}")
        atlas_lines.extend(["part", "bounds:1,1,2,2", "offsets:0,0,2,2", f"rotate:{rotate}", "index:-1", ""])
        atlas.write_text("\n".join(atlas_lines), encoding="utf-8")
        return atlas, page

    def _fake_converter(self, target: str = "3.8.99", *, binary: bool = False):
        def run(converter, source, output, requested_target, remove_curve):
            self.assertEqual(Path(converter), self.converter.resolve())
            self.assertTrue(Path(source).suffix.lower() in {".json", ".skel"})
            self.assertEqual(requested_target, target)
            self.assertIsInstance(remove_curve, bool)
            output = Path(output)
            output.parent.mkdir(parents=True, exist_ok=True)
            if binary:
                output.write_bytes(f"fixture {target}\0".encode("ascii"))
            else:
                self._skeleton_json(output, target)
            return [str(converter), "spine_converter_convert"], "", "", 0

        return run

    def test_direct_skel_copies_unique_atlas_and_writes_report(self) -> None:
        skeleton = self.root / "skeleton.skel"
        skeleton.write_bytes(b"fixture 4.0.37\0")
        atlas, page = self._atlas(self.root)
        output_root = self.root / "outputs"

        with patch("app.core.spine_converter._run_native_converter", side_effect=self._fake_converter()):
            result = convert_spine(
                skeleton,
                output_root,
                target_version="3.8.99",
                converter_path=self.converter,
                remove_curve=True,
            )

        self.assertNotEqual(result.output_dir, output_root.resolve())
        self.assertTrue(result.skeleton_path.is_file())
        self.assertTrue((result.output_dir / atlas.name).is_file())
        output_page = result.output_dir / page.name
        self.assertTrue(output_page.is_file())
        self.assertTrue(skeleton.read_bytes().startswith(b"fixture"))
        atlas_text = (result.output_dir / atlas.name).read_text(encoding="utf-8")
        self.assertIn("xy: 1, 1", atlas_text)
        self.assertIn("filter: Linear, Linear", atlas_text)
        self.assertNotIn("pma:", atlas_text)
        converted_pixel = Image.open(output_page).convert("RGBA").getpixel((1, 1))
        self.assertEqual(converted_pixel[3], 128)
        self.assertLessEqual(max(abs(converted_pixel[index] - (200, 100, 50)[index]) for index in range(3)), 1)
        report = json.loads(result.report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["target_version"], "3.8.99")
        self.assertEqual(report["returncode"], 0)
        self.assertEqual(report["converter_type"], "in-process native DLL")
        self.assertEqual(report["converter_abi_version"], "1")
        self.assertTrue(any("pma:true" in warning for warning in result.warnings))

    def test_model0_reference_and_explicit_texture_do_not_duplicate_pma_page(self) -> None:
        skeleton = self.root / "skeleton_0.skel"
        skeleton.write_bytes(b"fixture 4.0.37\0")
        atlas, page = self._atlas(self.root)
        model = self.root / "model0.json"
        model.write_text(
            json.dumps(
                {
                    "type": 9,
                    "skeleton": skeleton.name,
                    "atlases": [{"atlas": atlas.name, "textures": [page.name]}],
                }
            ),
            encoding="utf-8",
        )
        with patch("app.core.spine_converter._run_native_converter", side_effect=self._fake_converter()):
            result = convert_spine(model, self.root / "out", target_version="3.8.99", converter_path=self.converter)
        self.assertTrue((result.output_dir / atlas.name).is_file())
        self.assertTrue((result.output_dir / page.name).is_file())
        self.assertEqual(len(list(result.output_dir.glob("page*.png"))), 1)
        self.assertNotIn("model0.json", {path.name for path in result.output_dir.iterdir()})

    def test_directory_requires_one_skeleton(self) -> None:
        self._skeleton_json(self.root / "one.json")
        self._skeleton_json(self.root / "two.json")
        with self.assertRaises(SpineSourceAmbiguousError):
            convert_spine(self.root, self.root / "out", converter_path=self.converter)

    def test_direct_skeleton_rejects_multiple_unmapped_atlases(self) -> None:
        skeleton = self.root / "skeleton.skel"
        skeleton.write_bytes(b"fixture 4.0.37\0")
        self._atlas(self.root)
        other = self.root / "other.atlas"
        other.write_text("other.png\nsize:1,1\nformat:RGBA8888\n", encoding="utf-8")
        with self.assertRaises(SpineSourceAmbiguousError):
            convert_spine(skeleton, self.root / "out", converter_path=self.converter)

    def test_rotation_other_than_zero_or_ninety_is_explicitly_unsupported(self) -> None:
        skeleton = self.root / "skeleton.skel"
        skeleton.write_bytes(b"fixture 4.0.37\0")
        self._atlas(self.root, rotate=180)
        with patch("app.core.spine_converter._run_native_converter", side_effect=self._fake_converter()):
            with self.assertRaises(SpineUnsupportedError):
                convert_spine(skeleton, self.root / "out", target_version="3.8.99", converter_path=self.converter)

    def test_converter_failure_writes_failure_report(self) -> None:
        skeleton = self.root / "skeleton.skel"
        skeleton.write_bytes(b"fixture 4.0.37\0")

        def failed(*_args, **_kwargs):
            return [], "", "bad skeleton", 9

        with patch("app.core.spine_converter._run_native_converter", side_effect=failed):
            with self.assertRaises(SpineConversionProcessError):
                convert_spine(skeleton, self.root / "out", target_version="3.8.99", converter_path=self.converter)
        reports = list((self.root / "out").rglob("spine_conversion_report.json"))
        self.assertEqual(len(reports), 1)
        self.assertIn("bad skeleton", json.loads(reports[0].read_text(encoding="utf-8"))["error"])

    def test_discover_explicit_path_is_authoritative(self) -> None:
        self.assertEqual(discover_converter(self.converter), self.converter.resolve())
        self.assertIsNone(discover_converter(self.root / "missing.dll"))

    def test_output_format_skel_validates_binary_version(self) -> None:
        source = self.root / "skeleton.json"
        self._skeleton_json(source, "3.8.99")
        with patch(
            "app.core.spine_converter._run_native_converter",
            side_effect=self._fake_converter("3.8.99", binary=True),
        ):
            result = convert_spine(
                source,
                self.root / "out",
                target_version="3.8.99",
                output_format="skel",
                converter_path=self.converter,
            )
        self.assertEqual(result.skeleton_path.suffix, ".skel")
        self.assertTrue(result.skeleton_path.read_bytes().startswith(b"fixture 3.8.99"))

    def test_repeated_conversion_uses_a_new_child_and_preserves_previous_output(self) -> None:
        source = self.root / "skeleton.skel"
        original = b"fixture 4.0.37\0"
        source.write_bytes(original)
        output_root = self.root / "out"
        with patch("app.core.spine_converter._run_native_converter", side_effect=self._fake_converter()):
            first = convert_spine(source, output_root, target_version="3.8.99", converter_path=self.converter)
            first_bytes = first.skeleton_path.read_bytes()
            second = convert_spine(source, output_root, target_version="3.8.99", converter_path=self.converter)
        self.assertNotEqual(first.output_dir, second.output_dir)
        self.assertEqual(first.skeleton_path.read_bytes(), first_bytes)
        self.assertEqual(source.read_bytes(), original)

    def test_invalid_version_missing_dll_and_native_failure_are_explicit(self) -> None:
        source = self.root / "skeleton.skel"
        source.write_bytes(b"fixture 4.0.37\0")
        with self.assertRaises(SpineSourceError):
            convert_spine(source, self.root / "bad-version", target_version="3.8", converter_path=self.converter)
        with self.assertRaises(SpineConverterNotFoundError):
            convert_spine(source, self.root / "missing-dll", converter_path=self.root / "missing.dll")

        def native_failed(*_args, **_kwargs):
            return [], "", "native error", 9

        with patch("app.core.spine_converter._run_native_converter", side_effect=native_failed):
            with self.assertRaises(SpineConversionProcessError):
                convert_spine(source, self.root / "native-failed", target_version="3.8.99", converter_path=self.converter)

    def test_scale_resizes_page_and_rejects_zero_sized_region(self) -> None:
        source = self.root / "skeleton.skel"
        source.write_bytes(b"fixture 4.0.37\0")
        self._atlas(self.root, scale=2)
        with patch("app.core.spine_converter._run_native_converter", side_effect=self._fake_converter()):
            result = convert_spine(source, self.root / "scaled", target_version="3.8.99", converter_path=self.converter)
        with Image.open(result.output_dir / "page.png") as scaled_page:
            self.assertEqual(scaled_page.size, (4, 4))
        atlas_text = (result.output_dir / "main.atlas").read_text(encoding="utf-8")
        self.assertIn("size: 4, 4", atlas_text)
        self.assertIn("size: 1, 1", atlas_text)

        tiny_root = self.root / "tiny"
        tiny_root.mkdir()
        self._atlas(tiny_root, scale=100)
        tiny_skeleton = tiny_root / "skeleton2.skel"
        tiny_skeleton.write_bytes(b"fixture 4.0.37\0")
        with patch("app.core.spine_converter._run_native_converter", side_effect=self._fake_converter()):
            with self.assertRaises(SpineUnsupportedError):
                convert_spine(tiny_skeleton, self.root / "tiny-out", target_version="3.8.99", converter_path=self.converter)


if __name__ == "__main__":
    unittest.main()
