import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from app.core.psd_comparison import build_comparison, comparison_images
from app.core.psd_reconstructor import reconstruct_live2d_psd, repack_atlas_png_from_psd
from tests import test_selection_psd as fixture


class PsdComparisonTests(unittest.TestCase):
    def test_difference_includes_alpha_only_edits(self):
        before = Image.new("RGBA", (5, 5), (20, 40, 60, 255))
        after = before.copy()
        after.putpixel((2, 3), (20, 40, 60, 0))
        image, mask = comparison_images(before, after)
        self.assertEqual(int(mask.sum()), 1)
        self.assertGreater(image.getpixel((2, 3))[3], 0)
        with self.assertRaisesRegex(ValueError, "same dimensions"):
            comparison_images(before, Image.new("RGBA", (2, 2)))

    def test_compare_fixed_baseline_with_shared_impact_and_same_pose(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = fixture.SelectedPosePsdTests()
            model, textures, mesh = helper.make_source(root, shared=True)
            exported = reconstruct_live2d_psd(model, root / "psd", mode="atlas-components",
                                            selected_drawable_ids=["Head"], mesh_data=mesh)
            edited = helper.edit_layers(root, exported, {
                "Head": lambda pixels: pixels.__setitem__((3, 3, slice(0, 3)), (250, 0, 0)),
            })
            output = repack_atlas_png_from_psd(edited, root / "output", metadata_path=exported.metadata_path,
                                              allow_shared_uv=True)
            hashes = {path: path.read_bytes() for path in [*textures, *output.output_paths, exported.metadata_path]}
            report = build_comparison(exported.metadata_path, output.texture_outputs, root / "compare", mesh_data=mesh)
            self.assertEqual(len(report["views"]), 3)
            self.assertEqual(report["views"][0]["changed_pixels"], 1)
            self.assertEqual(report["views"][1]["changed_pixels"], 0)
            self.assertEqual(report["views"][2]["label"], "pose")
            self.assertGreater(report["views"][2]["changed_pixels"], 0)
            self.assertEqual(report["warnings"], [])
            self.assertEqual(set(report["affected_ids"]), {"Head", "UnselectedShared"})
            for path, contents in hashes.items():
                self.assertEqual(path.read_bytes(), contents)
            for view in report["views"]:
                for key in ("before", "after", "highlight"):
                    self.assertTrue(Path(view[key]).is_file())

    def test_noop_report_and_incomplete_output_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = fixture.SelectedPosePsdTests()
            model, textures, mesh = helper.make_source(root)
            exported = helper.export(root, model, mesh)
            outputs = dict(enumerate(textures))
            report = build_comparison(exported.metadata_path, outputs, root / "noop")
            self.assertEqual(report["affected_ids"], [])
            self.assertTrue(all(view["changed_pixels"] == 0 for view in report["views"]))
            with self.assertRaisesRegex(ValueError, "complete texture version"):
                build_comparison(exported.metadata_path, {0: textures[0]}, root / "invalid")

    def test_foreign_model_uses_atlas_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = fixture.SelectedPosePsdTests()
            model, textures, mesh = helper.make_source(root)
            exported = helper.export(root, model, mesh)
            mesh["comparison_moc_sha256"] = "foreign"
            report = build_comparison(exported.metadata_path, dict(enumerate(textures)), root / "compare", mesh_data=mesh)
            self.assertEqual(len(report["views"]), 2)
            self.assertFalse(report["complete_geometry"])
            self.assertTrue(report["warnings"])


if __name__ == "__main__":
    unittest.main()
