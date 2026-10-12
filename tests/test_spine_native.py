from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.core.spine_native import find_native_library
from app.core.spine_native import SpineNativeModel
from app.core.spine_preview import load_spine_asset


class SpineNativeDiscoveryTests(unittest.TestCase):
    def test_discovery_requires_matching_manifest_family(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = root / "spine_bridge.dll"
            bridge.write_bytes(b"test placeholder")
            (root / "spine_native.json").write_text(
                json.dumps({"runtimeFamily": "4.0"}), encoding="utf-8"
            )
            self.assertIsNone(find_native_library(root, "3.8"))
            self.assertEqual(find_native_library(root, "4.0"), bridge)

    def test_common_versioned_install_layout_is_discovered(self):
        # This mirrors the downloader's versioned layout without writing into
        # the repository's runtime tree.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "3.8.75"
            root.mkdir(parents=True)
            bridge = root / "spine_bridge.dll"
            bridge.write_bytes(b"test placeholder")
            (root / "spine_native.json").write_text(
                json.dumps({"runtimeFamily": "3.8", "version": "3.8.75"}),
                encoding="utf-8",
            )
            self.assertEqual(find_native_library(root, "3.8"), bridge)


class SpineNativeRuntimeTests(unittest.TestCase):
    """Small ABI/runtime regressions using the pinned local sample assets."""

    CASES = (
        (
            "4.0.37",
            Path("runtime/output/spine/贝尔法斯特.改 魔改/model0.json"),
            "4.0",
        ),
        (
            "3.8.75",
            Path("runtime/validation/native_converter_acceptance/skeleton_0_to_3.8.75_json/skeleton_0.json"),
            "3.8",
        ),
    )

    def _open(self, source: Path, family: str):
        source = (Path(__file__).resolve().parents[1] / source).resolve()
        runtime = Path(__file__).resolve().parents[1] / "runtime" / "tools" / "spine_native"
        if not source.is_file():
            self.skipTest(f"sample is not installed: {source}")
        library = find_native_library(runtime, family)
        if library is None:
            self.skipTest(f"native bridge is not installed: {family}")
        asset = load_spine_asset(source)
        return SpineNativeModel(library, asset.skeleton_path, asset.atlas_paths[0], asset.skeleton_format)

    def test_time_stays_in_range_for_loop_and_non_loop(self):
        for _label, source, family in self.CASES:
            model = self._open(source, family)
            try:
                duration = model.duration
                self.assertGreater(duration, 0.0)
                model.set_loop(True)
                model.set_time(duration * 3.0 + 0.25)
                self.assertGreaterEqual(model.time, 0.0)
                self.assertLess(model.time, duration)
                model.set_loop(False)
                model.set_time(duration + 5.0)
                self.assertAlmostEqual(model.time, duration, places=3)
                model.update(duration)
                self.assertAlmostEqual(model.time, duration, places=3)
                model.set_time(0.0)
                model.update(duration * 2.0)
                self.assertAlmostEqual(model.time, duration, places=3)
                model.set_time(float("nan"))
                self.assertAlmostEqual(model.time, 0.0, places=3)
                model.update(float("nan"))
                self.assertAlmostEqual(model.time, 0.0, places=3)
                model.set_time(-1.0)
                self.assertAlmostEqual(model.time, 0.0, places=3)
            finally:
                model.close()

    def test_render_into_reuses_native_buffers(self):
        for _label, source, family in self.CASES:
            model = self._open(source, family)
            try:
                first = model.render_into()
                model.update(1.0 / 60.0)
                second = model.render_into()
                self.assertIs(first[0], second[0])
                self.assertIs(first[2], second[2])
                self.assertIs(first[4], second[4])
                self.assertGreater(first[1], 0)
                self.assertGreater(first[5], 0)
            finally:
                model.close()

    def test_bone_overrides_pose_until_cleared_and_setup_pose_has_no_track(self):
        for _label, source, family in self.CASES:
            model = self._open(source, family)
            try:
                if not model.supports_bones:
                    self.skipTest("installed bridge predates bone mode (ABI 2)")
                bones = model.bones()
                self.assertTrue(bones)
                self.assertEqual(bones[0].parent, -1)
                for ik in model.ik_constraints():
                    self.assertTrue(bones[ik.target].is_ik_target)
                    self.assertTrue(all(bones[index].is_ik for index in ik.bones))
                bone = next(item for item in bones if item.parent >= 0 and item.length > 0)
                model.set_paused(True)
                model.set_time(0.0)
                animated = model.bones()[bone.index].rotation
                self.assertTrue(model.set_bone_override(bone.index, bone.x, bone.y, animated + 40.0, 1.0, 1.0))
                self.assertAlmostEqual(model.bones()[bone.index].rotation, animated + 40.0, places=3)
                model.set_time(model.duration / 2.0)
                self.assertAlmostEqual(model.bones()[bone.index].rotation, animated + 40.0, places=3)
                model.set_time(0.0)
                model.clear_bone_overrides()
                self.assertAlmostEqual(model.bones()[bone.index].rotation, animated, places=3)
                self.assertFalse(model.set_bone_override(bone.index, float("nan"), 0, 0, 1, 1))
                self.assertTrue(model.show_setup_pose())
                self.assertEqual(model.time, 0.0)
                self.assertGreater(model.render_into()[1], 0)
            finally:
                model.close()

    def test_hidden_slots_are_skipped_until_shown_again(self):
        for _label, source, family in self.CASES:
            model = self._open(source, family)
            try:
                if not model.supports_slot_visibility:
                    self.skipTest("installed bridge predates slot hiding (ABI 3)")
                model.set_paused(True)
                full = model.render_into()[1]
                self.assertFalse(model.set_slot_hidden("no such slot", True))
                skeleton = Path(load_spine_asset((Path(__file__).resolve().parents[1] / source).resolve()).skeleton_path)
                if skeleton.suffix.lower() != ".json":
                    continue  # slot names are read from JSON skeletons only
                names = [slot["name"] for slot in json.loads(skeleton.read_text(encoding="utf-8"))["slots"]]
                for name in names:
                    self.assertTrue(model.set_slot_hidden(name, True))
                self.assertEqual(model.render_into()[1], 0)
                self.assertTrue(model.set_slot_hidden(names[0], False))
                self.assertTrue(model.clear_hidden_slots())
                self.assertEqual(model.render_into()[1], full)
            finally:
                model.close()


if __name__ == "__main__":
    unittest.main()
