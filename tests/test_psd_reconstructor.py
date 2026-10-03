import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from app.core.psd_reconstructor import (
    PsdLayer,
    PsdReconstructionError,
    _apply_drawable_masks,
    reconstruct_live2d_psd,
    repack_atlas_png_from_psd,
    repack_multiple_psds,
)


class PsdReconstructorTests(unittest.TestCase):
    def test_pose_psd_preserves_multiply_shadow_and_exact_repack(self):
        from psd_tools import PSDImage
        from psd_tools.constants import BlendMode

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, original = self._make_source(root, second_drawable=False)
            sidecar = root / "demo.drawables.json"
            mesh = json.loads(sidecar.read_text(encoding="utf-8"))
            mesh["drawables"][0]["blend_mode"] = "multiply-compatible"
            mesh["drawables"][0]["blend_mode_value"] = 2
            sidecar.write_text(json.dumps(mesh), encoding="utf-8")
            result = reconstruct_live2d_psd(model, root / "export", mode="mesh")
            psd = PSDImage.open(result.psd_path)
            self.assertEqual(psd[0].blend_mode, BlendMode.MULTIPLY)
            metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["layers"][0]["blend_mode"], "multiply-compatible")
            repacked = repack_atlas_png_from_psd(result.psd_path, root / "repacked")
            np.testing.assert_array_equal(np.asarray(Image.open(repacked.output_paths[0])), original)

    def test_multiply_artmesh_darkens_background_in_saved_composite(self):
        from psd_tools import PSDImage
        from app.core.psd_reconstructor import _save_psd

        layers = [
            PsdLayer("Face", Image.new("RGBA", (4, 4), (200, 160, 120, 255))),
            PsdLayer(
                "Shadow", Image.new("RGBA", (4, 4), (128, 128, 128, 255)),
                blend_mode="multiply-compatible",
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "multiply.psd"
            _save_psd(path, (4, 4), layers, flat_layers=True)
            pixel = PSDImage.open(path).composite(force=True).getpixel((2, 2))
            np.testing.assert_allclose(pixel, (100, 80, 60, 255), atol=1)

    def test_pose_psd_keeps_interleaved_render_order_without_groups(self):
        from psd_tools import PSDImage
        from app.core.psd_reconstructor import _save_psd

        # Head/body/head drawables must remain interleaved, even when their
        # semantic part names repeat.  Verify the saved PSD's actual composite.
        layers = [
            PsdLayer("Back", Image.new("RGBA", (4, 4), "red"), group="Head"),
            PsdLayer("Middle", Image.new("RGBA", (4, 4), "blue"), group="Body"),
            PsdLayer("Front", Image.new("RGBA", (4, 4), "lime"), group="Head"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pose.psd"
            _save_psd(path, (4, 4), layers, flat_layers=True)
            psd = PSDImage.open(path)
            self.assertEqual([layer.name for layer in psd], ["Back", "Middle", "Front"])
            self.assertTrue(all(not layer.is_group() for layer in psd))
            self.assertEqual(psd.composite(force=True).getpixel((2, 2)), (0, 255, 0, 255))
            self.assertEqual(len({layer.layer_id for layer in psd}), 3)

    def _apply_test_mask(self, *, inverted: bool, texture_alpha: int) -> np.ndarray:
        """Apply one small mask and return the resulting alpha channel."""
        import cv2

        layer = np.zeros((4, 4, 4), dtype=np.uint8)
        layer[:, :, 3] = 200
        texture = np.zeros((4, 4, 4), dtype=np.uint8)
        texture[:, :, 3] = texture_alpha
        mask = {
            "texture_index": 0,
            "vertices": [[0, 0], [4, 0], [0, 4]],
            "uvs": [[0, 0], [1, 0], [0, 1]],
            "indices": [0, 1, 2],
            "opacity": 1.0,
        }
        drawable = {"masks": [1], "inverted_mask": inverted}
        _apply_drawable_masks(
            cv2,
            layer,
            drawable,
            {1: mask},
            [texture],
            np.zeros(2, dtype=np.float32),
            np.zeros(2, dtype=np.float32),
        )
        return layer[:, :, 3]

    def test_empty_mask_clears_normal_drawable_but_preserves_inverted_drawable(self):
        normal = self._apply_test_mask(inverted=False, texture_alpha=0)
        inverted = self._apply_test_mask(inverted=True, texture_alpha=0)
        self.assertTrue(np.all(normal == 0))
        self.assertTrue(np.all(inverted == 200))

    def test_scaled_pose_exports_only_bound_drawables(self):
        from psd_tools import PSDImage

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, original = self._make_source(root, second_drawable=False)
            result = reconstruct_live2d_psd(
                model, root / "scaled", mode="mesh",
                resource_limits={"mesh_max_dimension": 8},
            )
            psd = PSDImage.open(result.psd_path)
            metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
            self.assertLessEqual(max(psd.size), 8)
            self.assertEqual([layer.name for layer in psd], [item["name"] for item in metadata["layers"]])
            self.assertTrue(all(not layer.is_group() for layer in psd))
            repacked = repack_atlas_png_from_psd(result.psd_path, root / "repacked")
            np.testing.assert_array_equal(np.asarray(Image.open(repacked.output_paths[0])), original)

    def test_partial_inverted_mask_uses_one_minus_mask_alpha(self):
        alpha = self._apply_test_mask(inverted=True, texture_alpha=128)
        # The triangle contains this interior pixel and leaves the opposite
        # corner outside; a 128/255 mask should therefore halve only the
        # covered alpha.
        self.assertEqual(int(alpha[1, 0]), int(200 * (1.0 - 128.0 / 255.0)))
        self.assertEqual(int(alpha[3, 3]), 200)

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

    def test_mesh_one_pixel_edit_does_not_reprocess_offset_baseline_region(self):
        """A one-pixel mesh edit must stay local after baseline alignment."""
        from psd_tools import PSDImage
        from psd_tools.api.layers import PixelLayer
        from psd_tools.constants import Compression

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, original = self._make_source(root, second_drawable=False)
            result = reconstruct_live2d_psd(model, root / "export", mode="mesh")
            metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
            target = metadata["layers"][0]
            # Keep the fixture's drawable away from the canvas origin so this
            # regression exercises the local-crop/global-baseline alignment.
            self.assertNotEqual((int(target["left"]), int(target["top"])), (0, 0))

            psd = PSDImage.open(str(result.psd_path))
            original_pixel = next(layer for layer in psd if layer.layer_id == target["layer_id"])
            baseline = np.asarray(original_pixel.topil().convert("RGBA"), dtype=np.uint8)
            coords = np.argwhere(baseline[:, :, 3] > 0)
            self.assertGreaterEqual(len(coords), 3)
            local_y, local_x = [int(value) for value in coords[len(coords) // 2]]

            edited = original_pixel.topil().convert("RGBA")
            edited.putpixel((local_x, local_y), (255, 0, 255, 255))
            from psd_tools.constants import Tag
            replacement = PixelLayer.frompil(
                edited, psd, name=original_pixel.name,
                top=original_pixel.top, left=original_pixel.left,
                compression=Compression.RAW,
            )
            replacement.tagged_blocks.set_data(Tag.LAYER_ID, target["layer_id"])
            psd.remove(original_pixel)
            edited_psd = root / "mesh-edited.psd"
            psd.save(str(edited_psd))
            repacked = repack_atlas_png_from_psd(
                edited_psd,
                root / "repacked",
                metadata_path=result.metadata_path,
            )
            output = np.asarray(Image.open(repacked.output_paths[0]).convert("RGBA"))
            source_rgba = np.asarray(original, dtype=np.uint8)
            changed = np.any(source_rgba != output, axis=2)
            points = np.argwhere(changed)
            self.assertGreater(int(changed.sum()), 0)
            self.assertLessEqual(int(changed.sum()), 64)
            self.assertLessEqual(
                int((points[:, 1].max() - points[:, 1].min() + 1) * (points[:, 0].max() - points[:, 0].min() + 1)),
                64,
            )

    def test_mesh_moved_flat_layer_without_pixel_edits_preserves_texture(self):
        """Moving a flat layer alone must not invent texture pixel edits."""
        from psd_tools import PSDImage

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, original = self._make_source(root, second_drawable=False)
            result = reconstruct_live2d_psd(model, root / "export", mode="mesh")
            metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
            target = metadata["layers"][0]

            psd = PSDImage.open(str(result.psd_path))
            pixel = next(layer for layer in psd if layer.layer_id == target["layer_id"])
            pixel.offset = (int(pixel.left) + 2, int(pixel.top) + 3)
            moved_psd = root / "mesh-moved.psd"
            psd.save(str(moved_psd))

            # Simulate an older export whose metadata has no immutable layer
            # PNG.  The mesh fallback is a local render and must use the
            # moved PSD origin directly instead of cropping at the old origin.
            legacy_metadata = json.loads(json.dumps(metadata))
            for layer_info in legacy_metadata["layers"]:
                for key in ("baseline_path", "baseline_rgba_sha256", "baseline_size"):
                    layer_info.pop(key, None)
            legacy_metadata_path = root / "legacy.lpkpsd.json"
            legacy_metadata_path.write_text(
                json.dumps(legacy_metadata),
                encoding="utf-8",
            )

            repacked = repack_atlas_png_from_psd(
                moved_psd,
                root / "repacked",
                metadata_path=legacy_metadata_path,
            )
            output = np.asarray(Image.open(repacked.output_paths[0]).convert("RGBA"))
            changed = np.any(np.asarray(original) != output, axis=2)
            self.assertEqual(int(changed.sum()), 0)
            self.assertFalse(repacked.warnings)

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
