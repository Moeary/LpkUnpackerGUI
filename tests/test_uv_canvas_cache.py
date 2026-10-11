from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QColor, QImage, QPixmap
    from PySide6.QtWidgets import QApplication

    from app.gui.ArtMeshInspector import ArtMeshEntry, _MeshCanvas
except ImportError:  # pragma: no cover - the lightweight system env omits Qt.
    QApplication = None


@unittest.skipIf(QApplication is None, "PySide6 is optional in the lightweight test environment")
class UvCanvasCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        entries = [ArtMeshEntry(name=f"m{index}", drawable_id=f"m{index}", texture_index=0,
                                uvs=[(.1 + index * .2, .1), (.2 + index * .2, .1), (.15 + index * .2, .3)],
                                indices=[0, 1, 2]) for index in range(4)]
        image = QImage(256, 256, QImage.Format.Format_ARGB32)
        image.fill(QColor(90, 110, 140))
        self.canvas = _MeshCanvas("atlas")
        self.canvas.resize(300, 300)
        self.canvas.set_scene(entries, (256, 256), 1, pixmap=QPixmap.fromImage(image), selected_indices={1})
        self.addCleanup(self.canvas.close)
        self.entries = entries

    def test_pan_reuses_the_base_layer_and_selection_changes_rebuild_it(self):
        canvas = self.canvas
        canvas.grab()
        first = canvas._base_cache[1]
        canvas.pan_offset += QPointF(12, -7)
        canvas.grab()
        self.assertIs(canvas._base_cache[1], first)  # panning only moves the cached layer
        canvas.set_scene(self.entries, (256, 256), 2, pixmap=canvas.pixmap, selected_indices={2})
        canvas.grab()
        self.assertIsNot(canvas._base_cache[1], first)

    def test_zoom_gesture_stretches_the_cache_then_settles_to_a_new_scale(self):
        canvas = self.canvas
        canvas.grab()
        first = canvas._base_cache
        canvas._zoom_at(1.25, QPointF(150, 150))
        self.assertIsNotNone(canvas._stretched_base())
        canvas.grab()
        self.assertIs(canvas._base_cache, first)  # no rebuild while zooming
        canvas._interaction_settled()
        canvas.grab()
        self.assertNotEqual(canvas._base_cache[0][0], first[0][0])
        self.assertFalse(canvas._interacting)

    def test_selected_mesh_is_drawn_on_top_of_the_texture(self):
        canvas = self.canvas
        image = canvas.grab().toImage()
        (a, b, _c), = self.entries[1].atlas_triangles((256, 256))
        midpoint = canvas.source_to_view(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))  # on the first edge
        color = image.pixelColor(round(midpoint.x()), round(midpoint.y()))
        self.assertGreater(color.red(), 200)  # the yellow selection outline


if __name__ == "__main__":
    unittest.main()
