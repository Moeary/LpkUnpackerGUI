from __future__ import annotations

import unittest

import numpy as np

from app.core.spine_pick import PickRegion, pick_region, regions_by_page


def quad(x0, y0, x1, y1, u0, v0, u1, v1, alpha=1.0):
    """Two triangles covering a skeleton rectangle with the given UV rectangle."""
    corners = [(x0, y0, u0, v0), (x1, y0, u1, v0), (x1, y1, u1, v1), (x0, y1, u0, v1)]
    order = (0, 1, 2, 0, 2, 3)
    return [list(corners[index]) + [1.0, 1.0, 1.0, alpha] for index in order]


class SpinePickTests(unittest.TestCase):
    def setUp(self):
        # One 100x100 page holding two regions side by side.  Spine-cpp flips
        # V, so the top pixel rows (y 0..49) are v 1.0..0.5.
        self.regions = regions_by_page([
            PickRegion("left", "left", 0, 0, 0, 50, 50),
            PickRegion("right", "right", 0, 50, 0, 50, 50),
        ])
        self.sizes = {0: (100, 100)}
        lower = quad(0, 0, 10, 10, 0.0, 0.5, 0.5, 1.0)      # draws "left"
        upper = quad(5, 5, 15, 15, 0.5, 0.5, 1.0, 1.0)      # draws "right", on top
        self.vertices = np.asarray(lower + upper, dtype=np.float32)
        self.batches = [(0, 6, 0), (6, 6, 0)]

    def pick(self, point, **kwargs):
        return pick_region(self.vertices, self.batches, point, self.sizes, self.regions, **kwargs)

    def test_topmost_part_wins_where_parts_overlap(self):
        self.assertEqual(self.pick((7, 7)).region_id, "right")
        self.assertEqual(self.pick((2, 2)).region_id, "left")
        self.assertEqual(self.pick((14, 14)).region_id, "right")

    def test_point_outside_every_part_picks_nothing(self):
        self.assertIsNone(self.pick((40, 40)))

    def test_transparent_texel_falls_through_to_the_part_below(self):
        transparent_right = lambda page, x, y: 0 if x >= 50 else 255
        self.assertEqual(self.pick((7, 7), alpha_at=transparent_right).region_id, "left")
        self.assertIsNone(self.pick((14, 14), alpha_at=transparent_right))

    def test_v_is_flipped_into_page_rows(self):
        result = self.pick((2, 8))   # near the top of "left": v close to 1.0
        self.assertEqual(result.region_id, "left")
        self.assertLess(result.pixel[1], 25)

    def test_hidden_and_degenerate_triangles_are_ignored(self):
        hidden = np.asarray(quad(0, 0, 10, 10, 0.0, 0.5, 0.5, 1.0) + quad(0, 0, 10, 10, 0.5, 0.5, 1.0, 1.0, alpha=0.0),
                            dtype=np.float32)
        self.assertEqual(pick_region(hidden, [(0, 6, 0), (6, 6, 0)], (5, 5), self.sizes, self.regions).region_id, "left")
        flat = np.asarray([[0, 0, .1, .9, 1, 1, 1, 1]] * 3, dtype=np.float32)
        self.assertIsNone(pick_region(flat, [(0, 3, 0)], (0, 0), self.sizes, self.regions))


if __name__ == "__main__":
    unittest.main()
