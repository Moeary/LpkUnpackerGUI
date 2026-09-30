import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from app.core.psd_reconstructor import (
    PsdLayer,
    PsdReconstructionError,
    reconstruct_live2d_psd,
    repack_atlas_png_from_psd,
    repack_multiple_psds,
)


class PsdReconstructorTests(unittest.TestCase):
    def _make_source(self, root: Path, *, second_drawable: bool = True) -> tuple[Path, np.ndarray]:
        texture = np.zeros((16, 16, 4), dtype=np.uint8)
        texture[2:12, 2:12] = (20, 40, 80, 255)
        texture[14, 14] = (90, 100, 110, 255)
        (root / "nested").mkdir(parents=True)
        texture_path = root / "nested" / "atlas.png"
        Image.fromarray(texture, "RGBA").save(texture_path)
        model_path = root / "demo.model3.json"
        model_path.write_text(
            json.dumps(
                {
                    "FileReferences": {
                        "Moc": "demo.moc3",
                        "Textures": ["nested/atlas.png"],
                    }
                }
            ),
            encoding="utf-8",
        )
        (root / "demo.moc3").write_bytes(b"placeholder")
        drawables = [
            {
                "id": "Visible",
                "texture_index": 0,
                "vertices": [[0, 0], [16, 0], [16, 16]],
                "uvs": [[0, 1], [1, 1], [1, 0]],
                "indices": [0, 1, 2],
                "visible": True,
                "opacity": 1.0,
            }
        ]
        if second_drawable:
            drawables.append(
                {
                    "id": "HiddenTiny",
                    "texture_index": 0,
                    "vertices": [[3, 3], [3.2, 3], [3, 3.2]],
                    "uvs": [[0.2, 0.8], [0.21, 0.8], [0.2, 0.79]],
                    "indices": [0, 1, 2],
                    "visible": False,
                    "dynamic_flags": 0,
                    "opacity": 0.0,
                }
            )
        (root / "demo.drawables.json").write_text(
            json.dumps({"canvas": {"width": 16, "height": 16}, "drawables": drawables}),
            encoding="utf-8",
        )
        return model_path, texture

    def test_atlas_artmesh_keeps_hidden_tiny_drawables_and_reports_shared_regions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, _ = self._make_source(root)
            result = reconstruct_live2d_psd(model, root / "export", mode="atlas-artmesh")
            metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(result.mode, "atlas-artmesh")
            self.assertEqual(len(metadata["layers"]), 2)
            self.assertEqual(len(result.shared_regions), 1)
            # The overlap is away from the atlas origin.  This guards the
            # bbox-sliced shared-region calculation against returning a local
            # crop coordinate instead of the atlas coordinate.
            self.assertEqual(result.shared_regions[0]["bbox"][:2], [3, 3])
            self.assertEqual(metadata["textures"][0]["relative_path"], "nested/atlas.png")
            self.assertTrue((root / "export" / "demo_atlas_artmesh.baseline").is_dir())

    def test_noop_is_byte_exact_and_rgba_erase_reaches_atlas(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, original = self._make_source(root, second_drawable=False)
            result = reconstruct_live2d_psd(model, root / "export", mode="atlas-artmesh")
            metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
            self.assertRegex(metadata["textures"][0]["rgba_sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(metadata["source_moc_summary"]["sha256"])
            self.assertTrue(result.report_path.is_file())
            noop = repack_atlas_png_from_psd(result.psd_path, root / "noop")
            np.testing.assert_array_equal(np.asarray(Image.open(noop.output_paths[0])), original)

            layer_info = metadata["layers"][0]
            baseline = np.asarray(
                Image.open(result.metadata_path.parent / layer_info["baseline_path"]).convert("RGBA")
            ).copy()
            baseline[:, :, 3] = 0
            # Recreating the same deterministic edit-unit IDs simulates a
            # Photoshop eraser edit while retaining the exported metadata.
            from app.core import psd_reconstructor as reconstructor

            edited_psd = root / "edited.psd"
            reconstructor._save_psd(
                edited_psd,
                (16, 16),
                [PsdLayer(layer_info["name"], Image.fromarray(baseline), layer_info["left"], layer_info["top"])],
            )
            erased = repack_atlas_png_from_psd(
                edited_psd,
                root / "erased",
                metadata_path=result.metadata_path,
            )
            output = np.asarray(Image.open(erased.output_paths[0]))
            self.assertEqual(int(output[2, 2, 3]), 0)

    def test_legacy_numeric_overlays_follow_the_flat_psd_stack(self):
        """Old flat PSDs must combine name/name_1/name_2 in stack order."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, _ = self._make_source(root, second_drawable=False)
            result = reconstruct_live2d_psd(model, root / "export", mode="atlas-artmesh")
            metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
            layer_info = metadata["layers"][0]
            baseline = np.asarray(
                Image.open(result.metadata_path.parent / layer_info["baseline_path"]).convert("RGBA")
            ).copy()

            first_edit = np.zeros_like(baseline)
            first_edit[4, 4] = (220, 20, 20, 255)
            second_edit = np.zeros_like(baseline)
            second_edit[4, 4] = (20, 40, 230, 255)

            from psd_tools import PSDImage
            from psd_tools.api.layers import PixelLayer

            flat_psd = root / "flat.psd"
            psd = PSDImage.new("RGBA", (16, 16))
            psd.append(
                PixelLayer.frompil(
                    Image.fromarray(baseline, "RGBA"), psd, name=layer_info["name"]
                )
            )
            psd.append(
                PixelLayer.frompil(
                    Image.fromarray(first_edit, "RGBA"),
                    psd,
                    name=f"{layer_info['name']}_1",
                )
            )
            psd.append(
                PixelLayer.frompil(
                    Image.fromarray(second_edit, "RGBA"),
                    psd,
                    name=f"{layer_info['name']}_2",
                )
            )
            psd.save(str(flat_psd))

            # Simulate metadata produced by the old flat exporter: no durable
            # IDs or binding path, so strict numeric overlay matching is used.
            for key in ("layer_id", "unit_group_id", "pixel_layer_id", "binding_path", "psd_group"):
                layer_info.pop(key, None)
            result.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

            repacked = repack_atlas_png_from_psd(
                flat_psd,
                root / "repacked",
                metadata_path=result.metadata_path,
            )
            output = np.asarray(Image.open(repacked.output_paths[0]).convert("RGBA"))
            # The last flat overlay is the top of the actual PSD stack.
            self.assertEqual(output[4, 4].tolist(), [20, 40, 230, 255])

    def test_missing_or_changed_baseline_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, _ = self._make_source(root, second_drawable=False)
            result = reconstruct_live2d_psd(model, root / "export", mode="atlas-artmesh")
            (root / "nested" / "atlas.png").unlink()
            with self.assertRaises(PsdReconstructionError):
                repack_atlas_png_from_psd(result.psd_path, root / "out")

            # Writing next to the source atlas must never destroy the fixed
            # baseline used by future PSD passes.
            model, _ = self._make_source(root / "again", second_drawable=False)
            same_source = reconstruct_live2d_psd(model, root / "again" / "export", mode="atlas-artmesh")
            with self.assertRaises(PsdReconstructionError):
                repack_atlas_png_from_psd(same_source.psd_path, root / "again" / "nested")

            # Replacing a texture at the same path must not silently become a
            # new fixed baseline.
            model3, _ = self._make_source(root / "changed", second_drawable=False)
            changed = reconstruct_live2d_psd(model3, root / "changed" / "export", mode="atlas-artmesh")
            changed_texture = root / "changed" / "nested" / "atlas.png"
            replacement = np.zeros((16, 16, 4), dtype=np.uint8)
            Image.fromarray(replacement, "RGBA").save(changed_texture)
            with self.assertRaisesRegex(PsdReconstructionError, "digest changed"):
                repack_atlas_png_from_psd(changed.psd_path, root / "changed" / "out")

            model4, _ = self._make_source(root / "moc_changed", second_drawable=False)
            moc_result = reconstruct_live2d_psd(
                model4,
                root / "moc_changed" / "export",
                mode="atlas-artmesh",
            )
            (root / "moc_changed" / "demo.moc3").write_bytes(b"replaced")
            with self.assertRaisesRegex(PsdReconstructionError, r"MOC3 (size|digest) changed"):
                repack_atlas_png_from_psd(moc_result.psd_path, root / "moc_changed" / "out")

    def test_multi_psd_rejects_different_relative_texture_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, _ = self._make_source(root, second_drawable=False)
            first = reconstruct_live2d_psd(model, root / "one", mode="atlas-artmesh")
            model2, _ = self._make_source(root / "other", second_drawable=False)
            second = reconstruct_live2d_psd(model2, root / "two", mode="atlas-artmesh")
            metadata = json.loads(second.metadata_path.read_text(encoding="utf-8"))
            metadata["textures"][0]["relative_path"] = "different/atlas.png"
            second.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            with self.assertRaisesRegex(PsdReconstructionError, "model mismatch"):
                repack_multiple_psds([first.psd_path, second.psd_path], root / "multi")

    def test_multi_psd_accepts_same_model_with_different_projection_canvas(self):
        """Pose exports may use different projection canvases for one model."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, _ = self._make_source(root / "source", second_drawable=False)
            first = reconstruct_live2d_psd(model, root / "one", mode="atlas-artmesh")
            second = reconstruct_live2d_psd(model, root / "two", mode="atlas-artmesh")
            metadata = json.loads(second.metadata_path.read_text(encoding="utf-8"))
            metadata["canvas"] = {"width": 80, "height": 60}
            second.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

            merged = repack_multiple_psds(
                [first.psd_path, second.psd_path],
                root / "multi",
            )
            self.assertTrue(merged.report_path.is_file())
            self.assertEqual(merged.report["model_signature"]["canvas"], [16, 16])


if __name__ == "__main__":
    unittest.main()
