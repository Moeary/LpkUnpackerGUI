from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from scripts.validate_edit_roundtrip import main


class ValidateEditRoundtripTests(unittest.TestCase):
    def test_small_live2d_and_spine_fixture(self) -> None:
        try:
            import cv2  # noqa: F401
            import psd_tools  # noqa: F401
        except ImportError:
            self.skipTest("Live2D validation dependencies are optional")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "source"
            source_root.mkdir()
            texture = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
            texture.putpixel((2, 2), (20, 40, 80, 255))
            texture.save(source_root / "atlas.png")
            model = source_root / "demo.model3.json"
            model.write_text(
                json.dumps(
                    {
                        "FileReferences": {
                            "Moc": "demo.moc3",
                            "Textures": ["atlas.png"],
                        }
                    }
                ),
                encoding="utf-8",
            )
            (source_root / "demo.moc3").write_bytes(b"placeholder")
            (source_root / "demo.drawables.json").write_text(
                json.dumps(
                    {
                        "canvas": {"width": 16, "height": 16},
                        "drawables": [
                            {
                                "id": "Drawable",
                                "texture_index": 0,
                                "vertices": [[0, 0], [16, 0], [16, 16]],
                                "uvs": [[0, 1], [1, 1], [1, 0]],
                                "indices": [0, 1, 2],
                                "visible": True,
                                "opacity": 1.0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            spine_page = Image.new("RGBA", (4, 4), (30, 20, 10, 255))
            spine_page.save(source_root / "page.png")
            atlas = source_root / "demo.atlas"
            atlas.write_text(
                "page.png\n"
                "size: 4, 4\n"
                "format: RGBA8888\n"
                "part\n"
                "bounds: 0, 0, 2, 2\n",
                encoding="utf-8",
            )

            output = root / "validation"
            self.assertEqual(
                main(
                    [
                        "--live2d",
                        str(model),
                        "--atlas",
                        str(atlas),
                        "--output",
                        str(output),
                    ]
                ),
                0,
            )
            report = json.loads((output / "validation_report.json").read_text(encoding="utf-8"))
            self.assertTrue(report["passed"])
            self.assertEqual(len(report["cases"]), 4)
            self.assertTrue(all(case["status"] == "passed" for case in report["cases"]))
            self.assertTrue(all(case["changed_pixels"] == 0 for case in report["cases"]))


if __name__ == "__main__":
    unittest.main()
