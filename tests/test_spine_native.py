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


if __name__ == "__main__":
    unittest.main()
