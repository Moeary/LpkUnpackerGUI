from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from PIL import Image

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from app.gui.ArtMeshInspector import ArtMeshInspector
except ImportError:  # pragma: no cover - Qt is optional in lightweight envs.
    QApplication = None
    ArtMeshInspector = None


@unittest.skipIf(QApplication is None, "PySide6 is optional in the lightweight test environment")
class ArtMeshInspectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_pose_and_atlas_hits_select_topmost_drawable_and_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nested").mkdir()
            Image.new("RGBA", (32, 32), (40, 80, 120, 255)).save(root / "nested" / "atlas.png")
            metadata_path = root / "demo_atlas_artmesh.lpkpsd.json"
            metadata_path.write_text(
                json.dumps(
                    {
                        "format": "LpkUnpacker.Live2DAtlasPSD",
                        "version": 2,
                        "mode": "atlas-artmesh",
                        "source_root": str(root),
                        "canvas": {"width": 100, "height": 100},
                        "textures": [
                            {
                                "index": 0,
                                "name": "atlas.png",
                                "relative_path": "nested/atlas.png",
                                "width": 32,
                                "height": 32,
                            }
                        ],
                        "layers": [
                            {
                                "kind": "atlas-artmesh",
                                "name": "A_artmesh",
                                "drawable_id": "A",
                                "texture_index": 0,
                                "vertices": [[10, 10], [90, 10], [90, 90]],
                                "uvs": [[0, 1], [1, 1], [1, 0]],
                                "indices": [0, 1, 2],
                                "render_order": 1,
                            },
                            {
                                "kind": "atlas-artmesh",
                                "name": "B_artmesh",
                                "drawable_id": "B",
                                "texture_index": 0,
                                "vertices": [[10, 10], [90, 10], [90, 90]],
                                "uvs": [[0, 1], [1, 1], [1, 0]],
                                "indices": [0, 1, 2],
                                "render_order": 2,
                            },
                        ],
                        "shared_regions": [
                            {
                                "drawables": ["A", "B"],
                                "texture_index": 0,
                                "bbox": [0, 0, 32, 32],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            inspector = ArtMeshInspector(metadata_path)
            self.assertEqual(len(inspector.entries), 2)
            self.assertEqual(inspector.pose_size, (100, 100))
            self.assertEqual(inspector.hit_pose(50, 50), 1)
            self.assertEqual(inspector.hit_atlas(0, 24, 16), 1)
            inspector.select_entry("A")
            self.assertEqual(inspector.current_entry().drawable_id, "A")
            self.assertEqual(len(inspector.shared_regions), 1)
            inspector.refresh()
            self.assertEqual(inspector.current_entry().drawable_id, "A")
            inspector.close()

    def test_sidecar_canvas_and_geometry_fill_missing_pose_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGBA", (16, 8), (20, 40, 60, 255)).save(root / "atlas.png")
            (root / "demo.drawables.json").write_text(
                json.dumps(
                    {
                        "canvas": {"width": 200, "height": 100},
                        "drawables": [
                            {
                                "id": "A",
                                "texture_index": 0,
                                "vertices": [[10, 10], [100, 10], [100, 80]],
                                "uvs": [[0, 1], [1, 1], [1, 0]],
                                "indices": [0, 1, 2],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            metadata_path = root / "demo.lpkpsd.json"
            metadata_path.write_text(
                json.dumps(
                    {
                        "format": "LpkUnpacker.Live2DAtlasPSD",
                        "mode": "atlas-artmesh",
                        "source_model": str(root / "demo.json"),
                        "source_root": str(root),
                        "textures": [
                            {
                                "index": 0,
                                "name": "atlas.png",
                                "relative_path": "atlas.png",
                                "width": 16,
                                "height": 8,
                            }
                        ],
                        "layers": [
                            {
                                "kind": "atlas-artmesh",
                                "name": "A_artmesh",
                                "drawable_id": "A",
                                "texture_index": 0,
                                "indices": [0, 1, 2],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            inspector = ArtMeshInspector(metadata_path)
            self.assertEqual(inspector.sidecar_canvas_size, (200, 100))
            self.assertEqual(inspector.pose_size, (200, 100))
            self.assertEqual(inspector.hit_pose(40, 30), 0)
            inspector.close()


if __name__ == "__main__":
    unittest.main()
