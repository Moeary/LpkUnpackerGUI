import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from app.core.psd_reconstructor import (
    PsdLayer, PsdReconstructionError, _save_psd, reconstruct_selected_live2d_psd,
    repack_atlas_png_from_psd,
)


class SelectedPosePsdTests(unittest.TestCase):
    def make_source(self, root, *, masked=False, shared=False):
        root = Path(root)
        textures = []
        for index in range(2):
            pixels = np.zeros((32, 32, 4), dtype=np.uint8)
            pixels[:] = (40 + index * 30, 80, 120, 255)
            if index == 1:
                pixels[12:22, 12:22, 3] = 128
            path = root / f"atlas_{index}.png"
            Image.fromarray(pixels).save(path, compress_level=1)
            textures.append(path)
        model = root / "model.json"
        model.write_text(json.dumps({"Version": 3, "FileReferences": {
            "Moc": "model.moc3", "Textures": [path.name for path in textures],
        }}), encoding="utf-8")
        (root / "model.moc3").write_bytes(b"fixture")

        def drawable(name, position, uv, texture=0, **extra):
            x, y = position
            u, v = uv
            return {"id": name, "texture_index": texture,
                    "vertices": [[x, y], [x + 8, y], [x + 8, y + 8], [x, y + 8]],
                    "uvs": [[u / 32, 1 - v / 32], [(u + 8) / 32, 1 - v / 32],
                            [(u + 8) / 32, 1 - (v + 8) / 32], [u / 32, 1 - (v + 8) / 32]],
                    "indices": [0, 1, 2, 0, 2, 3], "opacity": 1.0, "visible": True,
                    "render_order": 1, **extra}

        drawables = [drawable("Head", (48, 32), (4, 4), masks=[4] if masked else [],
                              opacity=0.5 if masked else 1),
                     drawable("EarLeft", (36, 32), (2, 2), texture=1),
                     drawable("EarRight", (60, 32), (20, 2), texture=1),
                     drawable("HairTop", (48, 20), (20, 20), render_order=2),
                     drawable("MaskDependency", (48, 32), (12, 12), texture=1, visible=False),
                     drawable("UnselectedBody", (80, 70), (20, 4))]
        if shared:
            drawables.append(drawable("UnselectedShared", (90, 32), (4, 4)))
        mesh = {"coordinate_space": "canvas-pixels-y-down", "canvas": {"width": 128, "height": 128},
                "drawables": drawables}
        return model, textures, mesh

    def export(self, root, model, mesh, ids=None, **kwargs):
        return reconstruct_selected_live2d_psd(
            model, root / "export", ids or ["Head", "EarLeft", "EarRight", "HairTop"],
            mesh_data=mesh, resource_limits={"mesh_max_dimension": 0}, **kwargs,
        )

    def metadata(self, result):
        return json.loads(result.metadata_path.read_text(encoding="utf-8"))

    def edit_layers(self, root, result, changes, *, canvas_size=None):
        metadata = self.metadata(result)
        layers = []
        for info in metadata["layers"]:
            image = np.asarray(Image.open(result.metadata_path.parent / info["baseline_path"]).convert("RGBA")).copy()
            operation = changes.get(info["drawable_id"])
            if operation:
                operation(image)
            layers.append(PsdLayer(info["name"], Image.fromarray(image), info["left"], info["top"]))
        path = root / "edited.psd"
        _save_psd(path, canvas_size or (metadata["canvas"]["width"], metadata["canvas"]["height"]), layers)
        return path

    def test_whole_parts_keep_visual_pose_layout_and_mask_source_indices(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, _, mesh = self.make_source(root)
            original = copy.deepcopy(mesh)
            result = self.export(root, model, mesh, selection_region=[50, 34, 1, 1])
            metadata = self.metadata(result)
            layers = {item["drawable_id"]: item for item in metadata["layers"]}
            self.assertEqual(set(layers), {"Head", "EarLeft", "EarRight", "HairTop"})
            self.assertLess(layers["HairTop"]["top"], layers["Head"]["top"])
            self.assertLess(layers["EarLeft"]["left"], layers["Head"]["left"])
            self.assertGreater(layers["EarRight"]["left"], layers["Head"]["left"])
            self.assertGreaterEqual(layers["Head"]["bbox"][2], 8)
            self.assertLess(metadata["canvas"]["width"], 128)
            self.assertEqual(metadata["selection"]["policy"], "whole-visible-drawables")
            self.assertEqual(mesh, original)

    def test_no_edit_preserves_png_file_bytes_both_atlases_and_source_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, textures, mesh = self.make_source(root)
            sources = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in [model, *textures]}
            result = self.export(root, model, mesh)
            packed = repack_atlas_png_from_psd(result.psd_path, root / "noop")
            for index, source in enumerate(textures):
                self.assertEqual(packed.texture_outputs[index].read_bytes(), source.read_bytes())
                np.testing.assert_array_equal(np.asarray(Image.open(packed.texture_outputs[index])), np.asarray(Image.open(source)))
            self.assertEqual(sources, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in sources})

    def test_rgb_edit_only_changes_selected_uv_and_preserves_other_atlas(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, textures, mesh = self.make_source(root)
            result = self.export(root, model, mesh, ["Head"])
            edited = self.edit_layers(root, result, {"Head": lambda image: image.__setitem__((3, 3, slice(0, 3)), (220, 30, 40))})
            packed = repack_atlas_png_from_psd(edited, root / "packed", metadata_path=result.metadata_path)
            original = np.asarray(Image.open(textures[0]))
            pixels = np.asarray(Image.open(packed.texture_outputs[0]))
            changed = np.any(original != pixels, axis=2)
            self.assertEqual(int(changed.sum()), 1)
            self.assertTrue(changed[7, 7])
            np.testing.assert_array_equal(pixels[:, :, 3], original[:, :, 3])
            self.assertEqual(packed.texture_outputs[1].read_bytes(), textures[1].read_bytes())

    def test_mask_dependency_not_exported_and_opacity_not_baked_into_atlas_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, textures, mesh = self.make_source(root, masked=True)
            result = self.export(root, model, mesh, ["Head"])
            metadata = self.metadata(result)
            self.assertEqual([item["drawable_id"] for item in metadata["layers"]], ["Head"])
            baseline = Image.open(result.metadata_path.parent / metadata["layers"][0]["baseline_path"])
            self.assertLessEqual(baseline.getpixel((3, 3))[3], 64)
            edited = self.edit_layers(root, result, {"Head": lambda image: image.__setitem__((3, 3, slice(0, 3)), (230, 60, 20))})
            packed = repack_atlas_png_from_psd(edited, root / "packed", metadata_path=result.metadata_path)
            original = np.asarray(Image.open(textures[0]))
            pixels = np.asarray(Image.open(packed.texture_outputs[0]))
            np.testing.assert_array_equal(pixels[:, :, 3], original[:, :, 3])
            self.assertGreater(int(np.any(original != pixels, axis=2).sum()), 0)

    def test_preview_hidden_mask_keeps_intrinsic_clipping_for_selected_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, textures, mesh = self.make_source(root, masked=True)
            before = {path.name: path.read_bytes() for path in textures}
            normal = self.export(root / "normal", model, mesh, ["Head"])
            hidden = copy.deepcopy(mesh)
            mask = hidden["drawables"][4]
            mask.update(source_pose_opacity=mask["opacity"], preview_opacity_multiplier=0,
                        opacity=0, visible=False)
            original_hidden = copy.deepcopy(hidden)
            result = self.export(root / "hidden", model, hidden, ["Head"])
            normal_metadata, metadata = self.metadata(normal), self.metadata(result)
            self.assertEqual([item["drawable_id"] for item in metadata["layers"]], ["Head"])
            self.assertEqual(metadata["layers"][0]["baseline_rgba_sha256"],
                             normal_metadata["layers"][0]["baseline_rgba_sha256"])
            self.assertEqual(hidden, original_hidden)
            packed = repack_atlas_png_from_psd(result.psd_path, root / "packed", metadata_path=result.metadata_path)
            for index, path in enumerate(textures):
                self.assertEqual(path.read_bytes(), before[path.name])
                self.assertEqual(packed.texture_outputs[index].read_bytes(), before[path.name])

    def test_fully_masked_selection_is_reported_without_losing_requested_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, textures, mesh = self.make_source(root, masked=True)
            mask_pixels = np.asarray(Image.open(textures[1])).copy()
            mask_pixels[12:22, 12:22, 3] = 0
            Image.fromarray(mask_pixels).save(textures[1])
            result = self.export(root, model, mesh, ["Head", "EarLeft"])
            metadata = self.metadata(result)
            self.assertEqual([item["drawable_id"] for item in metadata["layers"]], ["EarLeft"])
            selection = metadata["selection"]
            self.assertEqual(selection["requested_ids"], ["Head", "EarLeft"])
            self.assertEqual(selection["selected_ids"], ["Head", "EarLeft"])
            self.assertEqual(selection["exported_ids"], ["EarLeft"])
            self.assertEqual(selection["omitted_ids"], ["Head"])
            self.assertEqual(result.report["selection"], selection)
            omission = [warning for warning in result.warnings if "omitted from PSD" in warning]
            self.assertEqual(len(omission), 1)
            self.assertIn("Head", omission[0])
            self.assertIn("current pose after clipping", omission[0])
            self.assertNotIn("EarLeft", omission[0])
            saved_report = json.loads(result.report_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_report["selection"]["omitted_ids"], ["Head"])
            packed = repack_atlas_png_from_psd(result.psd_path, root / "noop")
            for index, source in enumerate(textures):
                self.assertEqual(packed.texture_outputs[index].read_bytes(), source.read_bytes())

    def test_all_selected_drawables_masked_fail_with_current_pose_and_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, textures, mesh = self.make_source(root, masked=True)
            mask_pixels = np.asarray(Image.open(textures[1])).copy()
            mask_pixels[12:22, 12:22, 3] = 0
            Image.fromarray(mask_pixels).save(textures[1])
            with self.assertRaisesRegex(PsdReconstructionError, "current pose.*clipping/transparency.*Head"):
                self.export(root, model, mesh, ["Head"])
            self.assertFalse(list((root / "export").glob("*.psd")))
            self.assertFalse(list((root / "export").glob("*.lpkpsd.json")))

    def test_alpha_erase_changes_only_selected_texel_in_masked_pose(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, textures, mesh = self.make_source(root, masked=True)
            result = self.export(root, model, mesh, ["Head"])
            edited = self.edit_layers(root, result, {"Head": lambda image: image.__setitem__((3, 3, 3), 0)})
            packed = repack_atlas_png_from_psd(edited, root / "packed", metadata_path=result.metadata_path)
            original = np.asarray(Image.open(textures[0]))
            pixels = np.asarray(Image.open(packed.texture_outputs[0]))
            self.assertLessEqual(int(pixels[7, 7, 3]), 4)
            self.assertEqual(int(np.any(original != pixels, axis=2).sum()), 1)

    def test_shared_uv_with_unselected_is_reported_and_edit_is_atomic_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, textures, mesh = self.make_source(root, shared=True)
            result = self.export(root, model, mesh, ["Head"])
            self.assertEqual(result.shared_regions[0]["unselected_id"], "UnselectedShared")
            noop = repack_atlas_png_from_psd(result.psd_path, root / "noop")
            self.assertEqual(noop.texture_outputs[0].read_bytes(), textures[0].read_bytes())
            edited = self.edit_layers(root, result, {"Head": lambda image: image.__setitem__((3, 3, slice(0, 3)), (240, 0, 0))})
            with self.assertRaisesRegex(PsdReconstructionError, "UnselectedShared"):
                repack_atlas_png_from_psd(edited, root / "rejected", metadata_path=result.metadata_path)
            self.assertFalse(list((root / "rejected").glob("*.png")))
            report = json.loads((root / "rejected" / "repack_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["affected_unselected_ids"], ["UnselectedShared"])
            self.assertEqual(report["status"], "rejected")

    def test_conflicting_edits_of_selected_shared_uv_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, _, mesh = self.make_source(root, shared=True)
            result = self.export(root, model, mesh, ["Head", "UnselectedShared"])
            edited = self.edit_layers(root, result, {
                "Head": lambda image: image.__setitem__((3, 3, slice(0, 3)), (240, 0, 0)),
                "UnselectedShared": lambda image: image.__setitem__((3, 3, slice(0, 3)), (0, 240, 0)),
            })
            with self.assertRaisesRegex(PsdReconstructionError, "conflicting edits"):
                repack_atlas_png_from_psd(edited, root / "rejected", metadata_path=result.metadata_path, allow_shared_uv=True)
            self.assertFalse(list((root / "rejected").glob("*.png")))

    def test_explicit_shared_uv_consent_updates_shared_texels_and_reports_actual_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, textures, mesh = self.make_source(root, shared=True)
            partial = copy.deepcopy(mesh["drawables"][-1])
            partial["id"] = "PotentialSharedButNotEdited"
            partial["uvs"] = [[4 / 32, 1 - 4 / 32], [5 / 32, 1 - 4 / 32], [5 / 32, 1 - 5 / 32], [4 / 32, 1 - 5 / 32]]
            mesh["drawables"].append(partial)
            result = self.export(root, model, mesh, ["Head"])
            self.assertEqual(len(result.shared_regions), 2)
            edited = self.edit_layers(root, result, {"Head": lambda image: image.__setitem__((3, 3, slice(0, 3)), (240, 0, 0))})
            packed = repack_atlas_png_from_psd(edited, root / "allowed", metadata_path=result.metadata_path, allow_shared_uv=True)
            self.assertEqual(packed.report["affected_unselected_ids"], ["UnselectedShared"])
            self.assertTrue(packed.report["allow_shared_uv"])
            before = np.asarray(Image.open(textures[0]))
            after = np.asarray(Image.open(packed.texture_outputs[0]))
            changed = np.any(before != after, axis=2)
            self.assertEqual(int(changed.sum()), 1)
            self.assertTrue(changed[7, 7])
            np.testing.assert_array_equal(before[~changed], after[~changed])
            self.assertEqual(packed.texture_outputs[1].read_bytes(), textures[1].read_bytes())
            self.assertTrue(packed.warnings)

    def test_shared_uv_consent_is_rejected_for_original_export_modes(self):
        from app.core.psd_reconstructor import reconstruct_live2d_psd
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, _, mesh = self.make_source(root)
            original = reconstruct_live2d_psd(model, root / "original", mesh_data=mesh)
            with self.assertRaisesRegex(PsdReconstructionError, "only for a selected pose PSD"):
                repack_atlas_png_from_psd(original.psd_path, root / "rejected", allow_shared_uv=True)

    def test_both_selected_atlases_can_be_edited_and_paint_child_is_bound(self):
        from psd_tools import PSDImage
        from psd_tools.api.layers import PixelLayer
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, textures, mesh = self.make_source(root)
            result = self.export(root, model, mesh, ["Head", "EarLeft"])
            psd = PSDImage.open(result.psd_path)
            metadata = self.metadata(result)
            for info in metadata["layers"]:
                unit = next(layer for layer in psd.descendants() if layer.layer_id == info["unit_group_id"])
                paint = next(child for child in unit if child.name == "Paint")
                PixelLayer.frompil(Image.new("RGBA", (1, 1), (250, 10, 40, 255)), parent=paint,
                                   name="Painted detail", left=info["left"] + 3, top=info["top"] + 3)
            edited = root / "painted.psd"
            psd.save(edited)
            packed = repack_atlas_png_from_psd(edited, root / "packed", metadata_path=result.metadata_path)
            for index, texture in enumerate(textures):
                before = np.asarray(Image.open(texture))
                after = np.asarray(Image.open(packed.texture_outputs[index]))
                self.assertEqual(int(np.any(before != after, axis=2).sum()), 1)
                np.testing.assert_array_equal(before[:, :, 3], after[:, :, 3])

    def test_layer_move_and_canvas_resize_fail_instead_of_wrong_mapping(self):
        from psd_tools import PSDImage

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, _, mesh = self.make_source(root)
            result = self.export(root, model, mesh, ["Head"])
            resized = self.edit_layers(root, result, {}, canvas_size=(64, 64))
            with self.assertRaisesRegex(PsdReconstructionError, "canvas size changed"):
                repack_atlas_png_from_psd(resized, root / "resized", metadata_path=result.metadata_path)
            psd = PSDImage.open(result.psd_path)
            pixel = next(layer for layer in psd.descendants() if not layer.is_group())
            pixel.offset = (pixel.left + 1, pixel.top)
            moved = root / "moved.psd"
            psd.save(moved)
            with self.assertRaisesRegex(PsdReconstructionError, "moved or resized"):
                repack_atlas_png_from_psd(moved, root / "moved", metadata_path=result.metadata_path)

    def test_missing_id_invalid_region_and_changed_visibility_metadata_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, _, mesh = self.make_source(root)
            with self.assertRaisesRegex(PsdReconstructionError, "unavailable"):
                self.export(root, model, mesh, ["NotARealID"])
            with self.assertRaisesRegex(PsdReconstructionError, "positive size"):
                self.export(root, model, mesh, ["Head"], selection_region=[0, 0, -1, 2])
            result = self.export(root, model, mesh, ["Head"])
            metadata = self.metadata(result)
            metadata["layers"][0]["visibility_factor"]["sha256"] = "0" * 64
            result.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            with self.assertRaisesRegex(PsdReconstructionError, "digest changed"):
                repack_atlas_png_from_psd(result.psd_path, root / "invalid")


if __name__ == "__main__":
    unittest.main()
