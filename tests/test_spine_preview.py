import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core.preview.sources import is_spine_preview_source
from app.core.spine_preview import (
    SpineRuntimeUnavailableError,
    SpineVersionMismatchError,
    _atlas_page_names,
    discover_spine_runtime,
    load_spine_asset,
    make_spine_preview_plan,
    read_spine_version,
)


class SpinePreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="spine-preview-test-"))
        self.asset_dir = self.temp / "asset"
        self.asset_dir.mkdir()
        (self.asset_dir / "page.png").write_bytes(b"png-placeholder")
        (self.asset_dir / "page2.png").write_bytes(b"png-placeholder")
        (self.asset_dir / "sample.atlas").write_text(
            "page.png\n"
            "  size: 64,64\n"
            "  format: RGBA8888\n"
            "region-a\n"
            "  xy: 0,0\n"
            "  size: 8,8\n"
            "\n"
            "page2.png\n"
            "  size: 32,32\n"
            "region-b\n"
            "  bounds: 1,2,3,4\n",
            encoding="utf-8",
        )
        (self.asset_dir / "skeleton.json").write_text(
            json.dumps(
                {
                    "skeleton": {"spine": "3.8.75", "width": 10, "height": 12},
                    "bones": [{"name": "root"}],
                    "slots": [{"name": "slot", "bone": "root", "attachment": "region-a"}],
                    "skins": {"default": {"slot": {"region-a": {"type": "region", "path": "region-a"}}}},
                    "animations": {"idle": {}, "walk": {}},
                }
            ),
            encoding="utf-8",
        )
        (self.asset_dir / "model0.json").write_text(
            json.dumps(
                {
                    "skeleton": "skeleton.json",
                    "atlases": [{"atlas": "sample.atlas", "textures": ["page.png", "page2.png"]}],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def _native_root(self, *versions: str) -> Path:
        root = self.temp / "spine_native"
        root.mkdir(exist_ok=True)
        for version in versions:
            family = ".".join(version.split(".")[:2])
            folder = root / version
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "spine_bridge.dll").write_bytes(b"native bridge placeholder")
            (folder / "spine_native.json").write_text(
                json.dumps(
                    {
                        "package_id": "spine_native",
                        "version": version,
                        "runtimeFamily": family,
                        "library": "spine_bridge.dll",
                    }
                ),
                encoding="utf-8",
            )
        return root

    def test_asset_version_and_metadata_are_resolved(self):
        asset = load_spine_asset(self.asset_dir)
        self.assertEqual(asset.spine_version, "3.8.75")
        self.assertEqual(asset.family, "3.8")
        self.assertEqual([path.name for path in asset.atlas_paths], ["sample.atlas"])
        self.assertEqual({path.name for path in asset.texture_paths}, {"page.png", "page2.png"})
        self.assertEqual(asset.skin_names, ("default",))
        self.assertEqual(asset.animation_names, ("idle", "walk"))
        self.assertIn("region-a", asset.attachment_names)
        self.assertTrue(is_spine_preview_source(self.asset_dir))
        self.assertTrue(is_spine_preview_source(self.asset_dir / "model0.json"))

    def test_spine_text_suffixes_are_accepted(self):
        self.assertTrue(is_spine_preview_source(self.asset_dir / "sample.atlas"))
        atlas_txt = self.asset_dir / "sample.atlas.txt"
        atlas_txt.write_text((self.asset_dir / "sample.atlas").read_text(encoding="utf-8"), encoding="utf-8")
        self.assertTrue(is_spine_preview_source(atlas_txt))

    def test_atlas_page_names_do_not_include_region_names(self):
        self.assertEqual(_atlas_page_names(self.asset_dir / "sample.atlas"), ["page.png", "page2.png"])

    def test_native_plan_uses_bridge_and_keeps_metadata(self):
        root = self._native_root("3.8.75")
        plan = make_spine_preview_plan(load_spine_asset(self.asset_dir), root)
        self.assertEqual(plan.mode, "native")
        self.assertTrue(plan.can_animate)
        self.assertIsNotNone(plan.runtime)
        self.assertEqual(plan.runtime.family, "3.8")
        self.assertEqual(plan.runtime.version, "3.8.75")
        self.assertEqual(plan.runtime.library_path.name, "spine_bridge.dll")
        self.assertEqual(plan.runtime.api_style, "native")
        self.assertEqual(plan.asset.animation_names, ("idle", "walk"))
        self.assertEqual(plan.asset.skin_names, ("default",))

    def test_native_runtime_discovery_uses_exact_version_child(self):
        root = self._native_root("3.8.75", "3.8.99", "4.0")
        found = discover_spine_runtime(root, requested_family="3.8", requested_version="3.8.75")
        self.assertEqual(found.root_dir, (root / "3.8.75").resolve())
        self.assertEqual(found.library_path, (root / "3.8.75" / "spine_bridge.dll").resolve())
        self.assertEqual(found.source_manifest, (root / "3.8.75" / "spine_native.json").resolve())

        four = discover_spine_runtime(root, requested_family="4.0", requested_version="4.0.37")
        self.assertEqual(four.root_dir, (root / "4.0").resolve())
        self.assertEqual(four.family, "4.0")

    def test_native_runtime_rejects_wrong_exact_bridge(self):
        root = self._native_root("3.8.99")
        with self.assertRaises(SpineVersionMismatchError) as context:
            discover_spine_runtime(root, requested_family="3.8", requested_version="3.8.75")
        self.assertIn("3.8.75", str(context.exception))
        self.assertIn("3.8.99", str(context.exception))

    def test_native_runtime_rejects_reverse_exact_bridge(self):
        root = self._native_root("3.8.75")
        with self.assertRaises(SpineVersionMismatchError):
            discover_spine_runtime(root, requested_family="3.8", requested_version="3.8.99")

    def test_explicit_runtime_root_does_not_fallback_to_other_family(self):
        root = self._native_root("3.8.75", "4.0")
        with self.assertRaises(SpineRuntimeUnavailableError):
            discover_spine_runtime(root / "4.0", requested_family="3.8", requested_version="3.8.75")

    def test_missing_native_runtime_is_explicit_atlas_fallback(self):
        missing = self.temp / "missing-native"
        missing.mkdir()
        plan = make_spine_preview_plan(load_spine_asset(self.asset_dir), missing)
        self.assertEqual(plan.mode, "atlas")
        self.assertFalse(plan.can_animate)
        self.assertIn("native bridge", plan.reason)
        self.assertTrue(any("runtime/tools/spine_native" in warning for warning in plan.warnings))

    def test_default_discovery_reports_missing_native_install(self):
        with mock.patch("app.core.spine_preview.find_native_library", return_value=None):
            with self.assertRaises(SpineRuntimeUnavailableError) as context:
                discover_spine_runtime(None, requested_family="3.8", requested_version="3.8.75")
        self.assertIn("native Spine bridge", str(context.exception))

    def test_atlas_only_asset_is_never_claimed_as_animated(self):
        plan = make_spine_preview_plan(load_spine_asset(self.asset_dir / "sample.atlas"), self._native_root("3.8.75"))
        self.assertEqual(plan.mode, "atlas")
        self.assertFalse(plan.can_animate)
        self.assertIn("skeleton", plan.reason)

    def test_spine_21_is_downgraded(self):
        path = self.asset_dir / "old.json"
        path.write_text(
            json.dumps({"skeleton": {"spine": "2.1.27"}, "bones": [{"name": "root"}]}),
            encoding="utf-8",
        )
        self.assertEqual(read_spine_version(path), "2.1.27")
        plan = make_spine_preview_plan(load_spine_asset(path), self._native_root("3.8.75"))
        self.assertEqual(plan.mode, "atlas")
        self.assertIn("2.1", plan.reason)

    def test_unsupported_family_keeps_runtime_error_for_conversion_choice(self):
        path = self.asset_dir / "unsupported.json"
        path.write_text(
            json.dumps(
                {
                    "skeleton": {"spine": "4.1.24"},
                    "bones": [{"name": "root"}],
                    "slots": [],
                    "skins": {},
                    "animations": {},
                }
            ),
            encoding="utf-8",
        )
        plan = make_spine_preview_plan(load_spine_asset(path), self.temp / "missing-native")
        self.assertEqual(plan.mode, "atlas")
        self.assertTrue(plan.runtime_missing)
        self.assertIn("3.8.75", plan.runtime_error or "")


if __name__ == "__main__":
    unittest.main()
