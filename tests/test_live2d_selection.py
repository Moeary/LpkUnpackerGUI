from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from app.gui.live2d_selection import ModelCoordinates, PreviewTransform, SelectionScene


class Live2DSelectionTests(unittest.TestCase):
    def test_sdk_mvp_and_preview_transform_are_both_inverted_at_all_aspects(self):
        canvas = {"origin_x": 830, "origin_y": 500, "pixels_per_unit": 400}
        matrix = np.eye(4)
        matrix[0, 0], matrix[1, 1] = .25, .65
        matrix[0, 3], matrix[1, 3] = .15, -.3
        for width, height, dpr in ((500, 500, 1), (900, 300, 1.75), (300, 800, 1.75)):
            transform = PreviewTransform(width, height, 400 * dpr, 600 * dpr, 1.7, .13, -.2, 23)
            coordinates = ModelCoordinates(transform, matrix.flatten(order="F"), canvas)
            point = (950, 710)
            window_point = coordinates.canvas_to_window(point)
            np.testing.assert_allclose(coordinates.window_to_canvas(window_point), point, atol=1e-8)
            # The native SDK anisotropic scale and translation make the old
            # FBO-normalized shortcut incorrect even when it is reversible.
            old_clip = transform.window_to_clip(window_point)
            self.assertNotAlmostEqual(old_clip[0] * 400 + canvas["origin_x"], point[0])

    def test_rotated_rectangle_preserves_polygon_instead_of_selecting_its_empty_bbox_corners(self):
        coordinates = ModelCoordinates(PreviewTransform(400, 400, 400, 400, rotation=45),
                                       np.eye(4).flatten(order="F"),
                                       {"origin_x": 200, "origin_y": 200, "pixels_per_unit": 200})
        region = coordinates.window_rect((150, 150), (250, 250))
        self.assertEqual(region["coordinate_space"], "canvas-pixels-y-down")
        self.assertGreater(region["width"], 100)
        snapshot = {"drawables": [
            {"id": "inside", "vertices": [[190, 190], [210, 190], [200, 210]], "indices": [0, 1, 2]},
            {"id": "bbox-only", "vertices": [[132, 132], [140, 132], [132, 140]], "indices": [0, 1, 2]},
        ]}
        self.assertEqual(SelectionScene(snapshot).hit_region(region), ["inside"])

    def test_overlap_alpha_masks_hidden_parts_and_replacement_invalidate_texture_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "atlas.png"
            Image.new("RGBA", (16, 16), (255, 0, 0, 0)).save(path)
            triangle = {"vertices": [[0, 0], [16, 0], [0, 16]], "uvs": [[0, 1], [1, 1], [0, 0]],
                        "indices": [0, 1, 2], "opacity": 1, "texture_index": 0}
            snapshot = {"parts": [{"id": "hidden", "opacity": 0}], "drawables": [
                dict(triangle, id="back", texture_index=1, render_order=1),
                dict(triangle, id="transparent-front", render_order=8),
                dict(triangle, id="hidden-part", texture_index=1, render_order=9, parent_part_id="hidden"),
                dict(triangle, id="degenerate", texture_index=1, render_order=10, vertices=[[0, 0]] * 3),
            ]}
            cache = {}
            scene = SelectionScene(snapshot, [path], alpha_cache=cache)
            self.assertEqual(scene.hit_point((4, 4)), ["back"])
            self.assertEqual(scene.hit_region({"x": 0, "y": 0, "width": 10, "height": 10}), ["back"])
            Image.new("RGBA", (16, 16), (255, 0, 0, 255)).save(path)
            self.assertEqual(scene.hit_point((4, 4)), ["transparent-front", "back"])
            self.assertEqual(len(cache), 1)
            snapshot["drawables"][1]["masks"] = [2]
            snapshot["drawables"][2]["vertices"] = [[10, 10], [16, 10], [10, 16]]
            self.assertEqual(scene.hit_point((4, 4)), ["back"])
            snapshot["drawables"][1]["inverted_mask"] = True
            self.assertEqual(scene.hit_point((4, 4)), ["transparent-front", "back"])

    def test_region_respects_normal_and_inverted_masks_including_partial_visibility(self):
        with tempfile.TemporaryDirectory() as directory:
            texture = Path(directory) / "texture.png"
            Image.new("RGBA", (64, 64), (240, 170, 110, 255)).save(texture)
            triangle = {"vertices": [[0, 0], [64, 0], [0, 64]], "uvs": [[0, 1], [1, 1], [0, 0]],
                        "indices": [0, 1, 2], "texture_index": 0}
            snapshot = {"drawables": [dict(triangle, id="clipped", masks=[1]),
                                      dict(triangle, id="mask", opacity=0)]}
            scene = SelectionScene(snapshot, [texture])
            region = {"x": 4, "y": 4, "width": 20, "height": 20}
            self.assertIn("clipped", scene.hit_region(region))
            snapshot["drawables"][0]["inverted_mask"] = True
            self.assertNotIn("clipped", scene.hit_region(region))
            snapshot["drawables"][1]["vertices"] = [[12, 12], [32, 12], [12, 32]]
            self.assertIn("clipped", scene.hit_region(region))
            snapshot["drawables"][0]["inverted_mask"] = False
            self.assertIn("clipped", scene.hit_region(region))
            snapshot["drawables"][1]["vertices"] = [[40, 40], [64, 40], [40, 64]]
            self.assertNotIn("clipped", scene.hit_region(region))


if __name__ == "__main__":
    unittest.main()
