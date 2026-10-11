import copy
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from app.core.psd_reconstructor import PsdReconstructionError, reconstruct_live2d_psd, repack_atlas_png_from_psd
from tests import test_selection_psd as fixture


class SelectedAtlasTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = fixture.SelectedPosePsdTests()
        self.model, self.textures, self.mesh = self.fixture.make_source(self.root, shared=True)

    def export(self, ids=None, layout="packed"):
        return reconstruct_live2d_psd(self.model, self.root / "export", mode="atlas-components",
                                     selected_drawable_ids=ids or ["EarLeft", "HairTop"],
                                     mesh_data=self.mesh, atlas_layout=layout)

    def test_packed_layers_keep_mapping_across_atlases_and_noop_is_byte_exact(self):
        original = copy.deepcopy(self.mesh)
        result = self.export()
        metadata = self.fixture.metadata(result)
        self.assertEqual(result.mode, "atlas-components")
        self.assertEqual(metadata["selection"]["layout"], "packed")
        self.assertEqual({info["drawable_id"] for info in metadata["layers"]}, {"EarLeft", "HairTop"})
        self.assertTrue(any([info["left"], info["top"]] != info["atlas_origin"] for info in metadata["layers"]))
        packed = repack_atlas_png_from_psd(result.psd_path, self.root / "noop")
        self.assertEqual(packed.mode, "atlas-repack")
        self.assertEqual(self.mesh, original)
        for index, texture in enumerate(self.textures):
            self.assertEqual(packed.texture_outputs[index].read_bytes(), texture.read_bytes())

    def test_color_and_alpha_write_back_to_source_coordinates_only(self):
        result = self.export()
        edited = self.fixture.edit_layers(self.root, result, {
            "EarLeft": lambda pixels: pixels.__setitem__((3, 3, slice(0, 3)), (240, 20, 20)),
            "HairTop": lambda pixels: pixels.__setitem__((2, 2, 3), 0),
        })
        output = repack_atlas_png_from_psd(edited, self.root / "edited", metadata_path=result.metadata_path)
        for index, texture in enumerate(self.textures):
            with Image.open(texture) as source, Image.open(output.texture_outputs[index]) as target:
                before, after = np.asarray(source), np.asarray(target)
                self.assertEqual(int(np.any(before != after, axis=2).sum()), 1)
                if index == 0:
                    self.assertEqual(int(after[22, 22, 3]), 0)
                else:
                    self.assertEqual(after[5, 5, :3].tolist(), [240, 20, 20])

    def test_hidden_parts_export_and_original_layout_preserves_uv_positions(self):
        result = self.export(["MaskDependency"], "original")
        info = self.fixture.metadata(result)["layers"][0]
        self.assertEqual([info["left"], info["top"]], info["atlas_origin"])
        self.assertEqual(info["drawable_id"], "MaskDependency")

    def test_shared_uv_requires_consent_and_conflicts_reject_without_pngs(self):
        result = self.export(["Head"])
        self.assertEqual(result.shared_regions[0]["unselected_id"], "UnselectedShared")
        edited = self.fixture.edit_layers(self.root, result, {
            "Head": lambda pixels: pixels.__setitem__((3, 3, slice(0, 3)), (200, 0, 0)),
        })
        with self.assertRaisesRegex(PsdReconstructionError, "UnselectedShared"):
            repack_atlas_png_from_psd(edited, self.root / "rejected", metadata_path=result.metadata_path)
        self.assertFalse(list((self.root / "rejected").glob("*.png")))
        accepted = repack_atlas_png_from_psd(edited, self.root / "allowed", metadata_path=result.metadata_path,
                                             allow_shared_uv=True)
        self.assertEqual(accepted.report["affected_unselected_ids"], ["UnselectedShared"])
        both = self.export(["Head", "UnselectedShared"])
        edited = self.fixture.edit_layers(self.root, both, {
            "Head": lambda pixels: pixels.__setitem__((3, 3, slice(0, 3)), (200, 0, 0)),
            "UnselectedShared": lambda pixels: pixels.__setitem__((3, 3, slice(0, 3)), (0, 200, 0)),
        })
        with self.assertRaisesRegex(PsdReconstructionError, "conflicting edits"):
            repack_atlas_png_from_psd(edited, self.root / "conflict", metadata_path=both.metadata_path)
        self.assertFalse(list((self.root / "conflict").glob("*.png")))

    def edit_merged(self, result, changes, *, hide=False):
        """Edit the single merged layer in place, as an artist would."""
        from psd_tools import PSDImage
        from psd_tools.api.layers import PixelLayer
        from psd_tools.constants import Tag
        metadata = self.fixture.metadata(result)
        psd = PSDImage.open(result.psd_path)
        by_name = {info["merged_layer"]["name"]: info for info in metadata["layers"]}
        for name, info in by_name.items():
            merged = info["merged_layer"]
            original = next(layer for layer in psd.descendants()
                            if not layer.is_group() and layer.name == name)
            pixels = np.asarray(original.topil().convert("RGBA")).copy()
            for piece in metadata["layers"]:
                if piece["merged_layer"]["name"] == name and piece["drawable_id"] in changes:
                    y, x = piece["top"] - merged["top"], piece["left"] - merged["left"]
                    changes[piece["drawable_id"]](pixels[y:y + piece["bbox"][3], x:x + piece["bbox"][2]])
            replacement = PixelLayer.frompil(Image.fromarray(pixels), original.parent, name=name,
                                             left=original.left, top=original.top)
            replacement.tagged_blocks.set_data(Tag.LAYER_ID, int(original.layer_id))
            original.parent.remove(original)
            if hide:  # hide the edit unit (the visible top-level group)
                replacement.parent.parent.visible = False
        edited = self.root / "merged-edit.psd"
        psd.save(edited)
        return edited

    def test_single_layer_merges_pieces_and_writes_back_like_per_artmesh_layers(self):
        from psd_tools import PSDImage
        result = reconstruct_live2d_psd(self.model, self.root / "single", mode="atlas-components",
                                        selected_drawable_ids=["EarLeft", "HairTop"], mesh_data=self.mesh,
                                        atlas_single_layer=True)
        metadata = self.fixture.metadata(result)
        self.assertEqual(metadata["selection"]["layer_mode"], "single")
        self.assertEqual({info["drawable_id"] for info in metadata["layers"]}, {"EarLeft", "HairTop"})
        self.assertEqual({info["merged_layer"]["name"] for info in metadata["layers"]}, {"selected_atlas"})
        pixel_layers = [layer.name for layer in PSDImage.open(result.psd_path).descendants() if not layer.is_group()]
        self.assertEqual(pixel_layers, ["selected_atlas"])
        noop = repack_atlas_png_from_psd(result.psd_path, self.root / "single-noop")
        for index, texture in enumerate(self.textures):
            self.assertEqual(noop.texture_outputs[index].read_bytes(), texture.read_bytes())

        edited = self.edit_merged(result, {
            "EarLeft": lambda pixels: pixels.__setitem__((3, 3, slice(0, 3)), (240, 20, 20)),
            "HairTop": lambda pixels: pixels.__setitem__((2, 2, 3), 0),  # erase one texel
        })
        output = repack_atlas_png_from_psd(edited, self.root / "single-edited", metadata_path=result.metadata_path)
        self.assertEqual({item["layer"] for item in output.region_masks}, {"EarLeft", "HairTop"})
        for index, texture in enumerate(self.textures):
            with Image.open(texture) as source, Image.open(output.texture_outputs[index]) as target:
                before, after = np.asarray(source), np.asarray(target)
                self.assertEqual(int(np.any(before != after, axis=2).sum()), 1)
                if index == 0:
                    self.assertEqual(int(after[22, 22, 3]), 0)
                else:
                    self.assertEqual(after[5, 5, :3].tolist(), [240, 20, 20])

    def test_single_layer_original_layout_uses_one_layer_per_texture_and_hidden_layer_is_ignored(self):
        result = reconstruct_live2d_psd(self.model, self.root / "single-original", mode="atlas-components",
                                        selected_drawable_ids=["EarLeft", "HairTop"], mesh_data=self.mesh,
                                        atlas_layout="original", atlas_single_layer=True)
        layers = self.fixture.metadata(result)["layers"]
        self.assertEqual(len({info["merged_layer"]["name"] for info in layers}), 2)  # pieces on two textures
        self.assertTrue(all([info["left"], info["top"]] == info["atlas_origin"] for info in layers))
        edited = self.edit_merged(result, {"EarLeft": lambda pixels: pixels.__setitem__((3, 3, 3), 0)}, hide=True)
        output = repack_atlas_png_from_psd(edited, self.root / "hidden", metadata_path=result.metadata_path)
        for index, texture in enumerate(self.textures):
            self.assertEqual(output.texture_outputs[index].read_bytes(), texture.read_bytes())
        self.assertTrue(any("hidden" in warning for warning in output.warnings))

    def test_resized_canvas_rejected(self):
        result = self.export()
        edited = self.fixture.edit_layers(self.root, result, {}, canvas_size=(128, 128))
        with self.assertRaisesRegex(PsdReconstructionError, "canvas size changed"):
            repack_atlas_png_from_psd(edited, self.root / "resized", metadata_path=result.metadata_path)

    def test_edit_original_psd_pixel_preserves_other_pixels_and_alpha(self):
        from psd_tools import PSDImage
        from psd_tools.api.layers import PixelLayer
        from psd_tools.constants import Tag
        pixels = np.random.default_rng(7).integers(1, 255, (32, 32, 4), dtype=np.uint8)
        Image.fromarray(pixels).save(self.textures[0])
        result = self.export(["Head"])
        metadata = self.fixture.metadata(result)
        info = metadata["layers"][0]
        psd = PSDImage.open(result.psd_path)
        original = next(layer for layer in psd.descendants() if layer.layer_id == info["pixel_layer_id"])
        image = original.topil().convert("RGBA")
        rgba = image.getpixel((3, 3))
        image.putpixel((3, 3), (255 - rgba[0], rgba[1], rgba[2], rgba[3]))
        replacement = PixelLayer.frompil(image, original.parent, name=original.name,
                                         left=original.left, top=original.top)
        replacement.tagged_blocks.set_data(Tag.LAYER_ID, info["pixel_layer_id"])
        original.parent.remove(original)
        edited = self.root / "original-edit.psd"
        psd.save(edited)
        packed = repack_atlas_png_from_psd(edited, self.root / "original-edit", metadata_path=result.metadata_path,
                                          allow_shared_uv=True)
        with Image.open(packed.texture_outputs[0]) as after_image:
            after = np.asarray(after_image)
            self.assertEqual(int(np.any(pixels != after, axis=2).sum()), 1)
            np.testing.assert_array_equal(pixels[:, :, 1:], after[:, :, 1:])


if __name__ == "__main__":
    unittest.main()
