import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from app.core.psd_reconstructor import reconstruct_live2d_psd, repack_atlas_png_from_psd
from scripts.validate_artmesh_acceptance import (
    _assert_edit_output,
    _edit_psd,
    _read_json,
)
from tests.test_psd_reconstructor import PsdReconstructorTests


class ArtMeshAcceptanceTests(unittest.TestCase):
    def test_psd_color_alpha_erase_and_new_overlay_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, original = PsdReconstructorTests()._make_source(root, second_drawable=False)
            export = reconstruct_live2d_psd(model, root / "export", mode="atlas-artmesh")
            self.assertIsNotNone(export.metadata_path)
            metadata_path = Path(export.metadata_path)
            metadata = _read_json(metadata_path)
            edited_psd = root / "edited" / "artmesh.psd"
            edited_psd.parent.mkdir()
            edit = _edit_psd(export.psd_path, edited_psd, metadata, metadata_path)
            repacked = repack_atlas_png_from_psd(
                edited_psd,
                root / "edited" / "repack",
                metadata_path=metadata_path,
            )
            output_path = repacked.texture_outputs.get(0) or repacked.output_paths[0]
            with Image.open(output_path) as image:
                output = np.asarray(image.convert("RGBA"), dtype=np.uint8).copy()
            source = np.asarray(original, dtype=np.uint8)
            assertion = _assert_edit_output(source, output, edit)
            self.assertEqual(assertion["changed_pixels"], 3)
            self.assertEqual(assertion["unaffected_pixels"], source.shape[0] * source.shape[1] - 3)
            self.assertEqual(output[edit["atlas_pixels"]["erase"][1], edit["atlas_pixels"]["erase"][0], 3], 0)
            self.assertEqual(edit["overlay_layer"], "acceptance_overlay_new_layer")


if __name__ == "__main__":
    unittest.main()
