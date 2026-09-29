import json
import tempfile
import unittest
from pathlib import Path

from app.core.preview.sources import is_spine_preview_source
from app.core.spine_preview import (
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
                    "skeleton": {"spine": "3.8.99", "width": 10, "height": 12},
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
        import shutil

        shutil.rmtree(self.temp, ignore_errors=True)

    def test_asset_version_and_metadata_are_resolved(self):
        asset = load_spine_asset(self.asset_dir)
        self.assertEqual(asset.spine_version, "3.8.99")
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
        self.assertEqual(
            _atlas_page_names(self.asset_dir / "sample.atlas"),
            ["page.png", "page2.png"],
        )

    def test_web_pose_export_uses_draw_order_and_restores_slots(self):
        html = (Path(__file__).parents[1] / "assets" / "spine" / "preview.html").read_text(encoding="utf-8")
        self.assertIn("const drawOrder = Array.from(skeleton.drawOrder || []);", html)
        self.assertIn("maximum is \" + MAX_POSE_LAYERS", html)
        self.assertIn("Pose export blocked: unsupported asset features", html)
        self.assertIn("try {\n        // Spine's drawOrder", html)
        self.assertIn("} finally {\n        // WebGL capture or canvas encoding", html)
        self.assertIn("slot.setAttachment(saved[index])", html)
        self.assertIn("if (!manifest.runtime.combined) await loadScript(manifest.runtime.core);", html)
        self.assertIn("assetManager.hasErrors()", html)

    def test_without_user_runtime_plan_is_explicit_atlas_fallback(self):
        plan = make_spine_preview_plan(load_spine_asset(self.asset_dir))
        self.assertEqual(plan.mode, "atlas")
        self.assertIn("runtime", plan.reason)

    def test_runtime_manifest_must_match_skeleton_family(self):
        runtime = self.temp / "runtime"
        runtime.mkdir()
        (runtime / "spine-core.js").write_text("// user supplied core", encoding="utf-8")
        (runtime / "spine-webgl.js").write_text("// user supplied webgl", encoding="utf-8")
        (runtime / "spine_runtime.json").write_text(
            json.dumps({"version": "3.8.99", "core": "spine-core.js", "webgl": "spine-webgl.js"}),
            encoding="utf-8",
        )
        found = discover_spine_runtime(runtime, requested_family="3.8")
        self.assertEqual(found.family, "3.8")
        plan = make_spine_preview_plan(load_spine_asset(self.asset_dir), runtime)
        self.assertEqual(plan.mode, "runtime")
        self.assertEqual(plan.runtime, found)

        (runtime / "spine_runtime.json").write_text(
            json.dumps({"version": "4.2.40", "core": "spine-core.js", "webgl": "spine-webgl.js"}),
            encoding="utf-8",
        )
        with self.assertRaises(SpineVersionMismatchError):
            discover_spine_runtime(runtime, requested_family="3.8")

    def test_combined_webgl_manifest_does_not_schedule_core_twice(self):
        runtime = self.temp / "combined-runtime"
        runtime.mkdir()
        (runtime / "spine-core.js").write_text("// official core artifact", encoding="utf-8")
        (runtime / "spine-webgl.js").write_text("// official self-contained webgl artifact", encoding="utf-8")
        (runtime / "spine_runtime.json").write_text(
            json.dumps(
                {
                    "version": "3.8",
                    "core": "spine-core.js",
                    "webgl": "spine-webgl.js",
                    "combined": True,
                }
            ),
            encoding="utf-8",
        )
        found = discover_spine_runtime(runtime, requested_family="3.8")
        self.assertTrue(found.combined_webgl)
        self.assertEqual(found.scripts, (found.webgl_script,))
        self.assertEqual(found.core_script.name, "spine-core.js")

    def test_spine_21_binary_or_json_is_downgraded(self):
        path = self.asset_dir / "old.json"
        path.write_text(
            json.dumps({"skeleton": {"spine": "2.1.27"}, "bones": [{"name": "root"}]}),
            encoding="utf-8",
        )
        self.assertEqual(read_spine_version(path), "2.1.27")
        plan = make_spine_preview_plan(load_spine_asset(path))
        self.assertEqual(plan.mode, "atlas")
        self.assertIn("2.1", plan.reason)


if __name__ == "__main__":
    unittest.main()
