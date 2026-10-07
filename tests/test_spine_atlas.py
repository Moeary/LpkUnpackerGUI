from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from app.core.spine_atlas import (
    AtlasEditLayer,
    SpineAtlasBoundsError,
    SpineAtlasPathError,
    SpineAtlasParseError,
    extract_spine_atlas,
    parse_atlas_text,
    writeback_spine_atlas,
)


def _save_atlas(root: Path, image: Image.Image, text: str) -> tuple[Path, Path]:
    page = root / "page.png"
    atlas = root / "main.atlas"
    image.save(page)
    atlas.write_text(text, encoding="utf-8")
    return atlas, page


class SpineAtlasTests(unittest.TestCase):
  def _tmp_path(self) -> Path:
    path = Path(tempfile.mkdtemp(prefix="spine-atlas-", dir=Path.cwd()))
    self.addCleanup(shutil.rmtree, path, ignore_errors=True)
    return path

  def test_parse_old_and_new_syntax_and_stable_duplicate_ids(self) -> None:
    old = """page.png
size: 32, 32
format: RGBA8888
filter: Linear,Linear
repeat: none
hero
  rotate: false
  xy: 1, 2
  size: 3, 4
  orig: 5, 6
  offset: 1, 0
  index: -1
hero
  rotate: false
  xy: 6, 2
  size: 3, 4
  orig: 5, 6
  offset: 1, 0
  index: -1
"""
    parsed = parse_atlas_text(old)
    self.assertEqual(parsed.pages[0].size, (32, 32))
    self.assertEqual([item.region_id for item in parsed.regions], [
        "p00_hero_i-1",
        "p00_hero_i-1_r2",
    ])
    self.assertEqual(parsed.regions[0].original_size, (5, 6))
    self.assertEqual(parsed.regions[0].offset_top, 2)

    new = """page.png
size: 32, 32
format: RGBA8888
pma: true
hero
bounds: 1, 2, 4, 3
offsets: 1, 0, 5, 6
rotate: 90
index: 7

page2.png
size: 8, 8
format: RGBA8888
small
bounds: 0, 0, 2, 2
"""
    parsed_new = parse_atlas_text(new)
    self.assertEqual(len(parsed_new.pages), 2)
    self.assertTrue(parsed_new.pages[0].pma)
    self.assertEqual(parsed_new.regions[0].rotation, 90)
    self.assertEqual(parsed_new.regions[0].original_size, (5, 6))
    self.assertEqual(parsed_new.regions[0].logical_size, (4, 3))
    self.assertEqual(parsed_new.regions[0].packed_size, (3, 4))
    self.assertEqual(parsed_new.regions[1].page_index, 1)


  def test_rotation_trim_and_no_edit_round_trip_are_exact(self) -> None:
    tmp_path = self._tmp_path()
    page_image = Image.new("RGBA", (16, 12), (0, 0, 0, 0))
    for y in range(page_image.height):
        for x in range(page_image.width):
            page_image.putpixel((x, y), (x * 7 % 256, y * 11 % 256, 91, 255))
    logical_turn = Image.new("RGBA", (2, 3))
    for y in range(logical_turn.height):
        for x in range(logical_turn.width):
            logical_turn.putpixel((x, y), (10 + x, 40 + y, 200, 255))
    page_image.paste(logical_turn.rotate(90, expand=True), (8, 3))

    # A trimmed unrotated region: 3x2 content inside a 5x4 original image.
    # A rotated region stores a 2x3 original image as a 3x2 page rectangle.
    atlas_text = """page.png
size: 16, 12
format: RGBA8888
filter: Nearest,Nearest
repeat: none
plain
  rotate: false
  xy: 1, 2
  size: 3, 2
  orig: 5, 4
  offset: 1, 0
  index: -1
turned
  rotate: 90
  xy: 8, 3
  size: 2, 3
  orig: 2, 3
  offset: 0, 0
  index: -1
"""
    atlas, source_page = _save_atlas(tmp_path, page_image, atlas_text)
    exported = extract_spine_atlas(atlas, tmp_path / "export")
    plain = Image.open(exported.region_paths["p00_plain_i-1"])
    turned = Image.open(exported.region_paths["p00_turned_i-1"])
    self.assertEqual(plain.size, (5, 4))
    self.assertEqual(turned.size, (2, 3))
    self.assertEqual(turned.tobytes(), logical_turn.tobytes())
    self.assertEqual(plain.getpixel((0, 0))[3], 0)
    self.assertEqual(plain.getpixel((1, 2)), page_image.getpixel((1, 2)))

    written = writeback_spine_atlas(exported.metadata_path, {}, tmp_path / "written")
    output = Image.open(written.page_paths[0])
    self.assertEqual(output.size, page_image.size)
    self.assertEqual(output.tobytes(), page_image.tobytes())
    # Metadata retains the relative page path and exact trim/rotation facts.
    metadata = json.loads(exported.metadata_path.read_text(encoding="utf-8"))
    self.assertEqual(metadata["pages"][0]["relative_path"], "page.png")
    turned_metadata = next(item for item in metadata["regions"] if item["name"] == "turned")
    self.assertEqual(turned_metadata["rotate"], 90)
    self.assertEqual(turned_metadata["offset_top"], 0)


  def test_pma_and_overlay_erase_preserve_unedited_pixels(self) -> None:
    tmp_path = self._tmp_path()
    straight = Image.new("RGBA", (10, 8), (0, 0, 0, 0))
    straight.putpixel((0, 0), (210, 110, 50, 128))
    straight.putpixel((1, 0), (30, 90, 190, 255))
    # Make the source page in the representation declared by pma:true.
    pma = Image.new("RGBA", straight.size)
    for point, (red, green, blue, alpha) in enumerate(straight.get_flattened_data()):
        pma.putpixel(
            (point % straight.width, point // straight.width),
            ((red * alpha + 127) // 255, (green * alpha + 127) // 255, (blue * alpha + 127) // 255, alpha),
        )
    atlas_text = """page.png
size: 10, 8
format: RGBA8888
pma: true
part
bounds: 0, 0, 2, 1
offsets: 0, 0, 2, 1
rotate: false
"""
    atlas, source_page = _save_atlas(tmp_path, pma, atlas_text)
    exported = extract_spine_atlas(atlas, tmp_path / "export")
    part_id = "p00_part_i-1"
    part = Image.open(exported.region_paths[part_id]).convert("RGBA")
    self.assertEqual(part.getpixel((0, 0))[3], 128)

    overlay = Image.new("RGBA", (1, 1), (255, 0, 0, 128))
    erase = Image.new("RGBA", (1, 1), (0, 0, 0, 255))
    result = writeback_spine_atlas(
        exported.metadata_path,
        [
            AtlasEditLayer(part_id, overlay, mode="overlay", left=0, top=0),
            AtlasEditLayer(part_id, erase, mode="erase", left=1, top=0),
        ],
        tmp_path / "written",
    )
    output = Image.open(result.page_paths[0]).convert("RGBA")
    self.assertEqual(result.changed_regions, [part_id])
    self.assertEqual(output.getpixel((1, 0)), (0, 0, 0, 0))
    # The changed pixel is written in PMA form, while a later untouched pixel
    # remains byte-for-byte equal to the source page.
    self.assertNotEqual(output.getpixel((0, 0)), pma.getpixel((0, 0)))
    self.assertEqual(output.getpixel((2, 0)), pma.getpixel((2, 0)))


  def test_paths_and_edit_bounds_are_rejected(self) -> None:
    tmp_path = self._tmp_path()
    image = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
    escaping = """../outside.png
size: 4, 4
format: RGBA8888
region
bounds: 0, 0, 1, 1
"""
    atlas, _ = _save_atlas(tmp_path, image, escaping)
    with self.assertRaisesRegex(SpineAtlasPathError, "relative|traversal|escapes"):
        extract_spine_atlas(atlas, tmp_path / "out")

    out_of_page = """page.png
size: 4, 4
format: RGBA8888
region
bounds: 3, 3, 2, 2
"""
    atlas.write_text(out_of_page, encoding="utf-8")
    atlas_page = tmp_path / "page.png"
    image.save(atlas_page)
    with self.assertRaisesRegex(SpineAtlasBoundsError, "exceeds page"):
        extract_spine_atlas(atlas, tmp_path / "out2")

    valid = """page.png
size: 4, 4
format: RGBA8888
region
bounds: 0, 0, 2, 2
"""
    atlas.write_text(valid, encoding="utf-8")
    exported = extract_spine_atlas(atlas, tmp_path / "out3")
    edit = Image.new("RGBA", (3, 1), (255, 0, 0, 255))
    with self.assertRaisesRegex(SpineAtlasBoundsError, "exceeds its original container"):
        writeback_spine_atlas(
            exported.metadata_path,
            [AtlasEditLayer("p00_region_i-1", edit, mode="overlay")],
            tmp_path / "out4",
        )

  def test_psd_export_and_round_trip_when_dependency_is_available(self) -> None:
    try:
        import psd_tools  # noqa: F401
    except ImportError:
        self.skipTest("psd-tools is optional in the lightweight test environment")
    tmp_path = self._tmp_path()
    source = Image.new("RGBA", (8, 8), (20, 30, 40, 255))
    atlas, page = _save_atlas(
        tmp_path,
        source,
        """page.png
size: 8, 8
format: RGBA8888
part
bounds: 1, 2, 2, 2
""",
    )
    exported = extract_spine_atlas(atlas, tmp_path / "export", write_psd=True)
    self.assertIsNotNone(exported.psd_path)
    metadata = json.loads(exported.metadata_path.read_text(encoding="utf-8"))
    self.assertIn(exported.atlas.regions[0].region_id, metadata["psd_baseline"])
    written = writeback_spine_atlas(exported.metadata_path, exported.psd_path, tmp_path / "written")
    self.assertEqual(written.changed_regions, [])
    self.assertEqual(Image.open(written.page_paths[0]).convert("RGBA").tobytes(), source.tobytes())

    baseline_entry = metadata["psd_baseline"][exported.atlas.regions[0].region_id]
    baseline_file = exported.metadata_path.parent / baseline_entry["path"]
    baseline_file.unlink()
    with self.assertRaisesRegex(SpineAtlasPathError, "baseline.*does not exist"):
      writeback_spine_atlas(exported.metadata_path, exported.psd_path, tmp_path / "missing-baseline")

    tampered = extract_spine_atlas(atlas, tmp_path / "tampered-export", write_psd=True)
    tampered_meta = json.loads(tampered.metadata_path.read_text(encoding="utf-8"))
    tampered_entry = tampered_meta["psd_baseline"][tampered.atlas.regions[0].region_id]
    tampered_file = tampered.metadata_path.parent / tampered_entry["path"]
    Image.new("RGBA", tuple(tampered_entry["size"]), (1, 2, 3, 255)).save(tampered_file)
    with self.assertRaisesRegex(SpineAtlasParseError, "SHA256"):
      writeback_spine_atlas(tampered.metadata_path, tampered.psd_path, tmp_path / "tampered-baseline")

  def test_psd_region_group_overlay_rename_and_visibility(self) -> None:
    try:
      from psd_tools import PSDImage
      from psd_tools.api.layers import PixelLayer
      from psd_tools.constants import Compression
    except ImportError:
      self.skipTest("psd-tools is optional in the lightweight test environment")
    tmp_path = self._tmp_path()
    source = Image.new("RGBA", (6, 6), (0, 0, 0, 0))
    source.putpixel((0, 0), (7, 8, 9, 0))
    source.putpixel((1, 1), (10, 20, 30, 128))
    source.putpixel((2, 1), (60, 70, 80, 64))
    atlas, _ = _save_atlas(
      tmp_path,
      source,
      """page.png
size: 6, 6
format: RGBA8888
part
bounds: 1, 1, 4, 3
""",
    )
    exported = extract_spine_atlas(atlas, tmp_path / "export", write_psd=True)
    region_id = exported.atlas.regions[0].region_id
    psd = PSDImage.open(str(exported.psd_path))
    group = next(node for node in psd.descendants() if node.name == region_id and hasattr(node, "descendants"))
    self.assertNotEqual(group.layer_id, -1)
    group.name = "renamed artist group"
    overlay = Image.new("RGBA", (4, 3), (0, 0, 0, 0))
    overlay.putpixel((0, 0), (200, 100, 50, 255))
    PixelLayer.frompil(
      overlay,
      group,
      name=f"{region_id}__overlay",
      top=group.top,
      left=group.left,
      compression=Compression.RLE,
    )
    changed_psd = tmp_path / "changed.psd"
    psd.save(changed_psd)
    written = writeback_spine_atlas(exported.metadata_path, changed_psd, tmp_path / "changed")
    self.assertEqual(written.changed_regions, [region_id])
    output = Image.open(written.page_paths[0]).convert("RGBA")
    changed_pixel = output.getpixel((1, 1))
    self.assertEqual(changed_pixel[:3], (200, 100, 50))
    self.assertGreater(changed_pixel[3], source.getpixel((1, 1))[3])
    for y in range(source.height):
      for x in range(source.width):
        if (x, y) != (1, 1):
          self.assertEqual(output.getpixel((x, y)), source.getpixel((x, y)))

    hidden = PSDImage.open(str(exported.psd_path))
    hidden_group = next(node for node in hidden.descendants() if node.name == region_id and hasattr(node, "descendants"))
    hidden_group.visible = False
    hidden_psd = tmp_path / "hidden.psd"
    hidden.save(hidden_psd)
    untouched = writeback_spine_atlas(exported.metadata_path, hidden_psd, tmp_path / "hidden")
    self.assertEqual(untouched.changed_regions, [])
    self.assertEqual(Image.open(untouched.page_paths[0]).convert("RGBA").tobytes(), source.tobytes())

  def test_psd_group_paint_outside_region_fails_loudly(self) -> None:
    try:
      from psd_tools import PSDImage
      from psd_tools.api.layers import PixelLayer
      from psd_tools.constants import Compression
    except ImportError:
      self.skipTest("psd-tools is optional in the lightweight test environment")
    tmp_path = self._tmp_path()
    source = Image.new("RGBA", (6, 6), (0, 0, 0, 0))
    atlas, _ = _save_atlas(
      tmp_path,
      source,
      """page.png
size: 6, 6
format: RGBA8888
part
bounds: 1, 1, 3, 3
""",
    )
    exported = extract_spine_atlas(atlas, tmp_path / "export", write_psd=True)
    region_id = exported.atlas.regions[0].region_id
    psd = PSDImage.open(str(exported.psd_path))
    group = next(node for node in psd.descendants() if node.name == region_id and hasattr(node, "descendants"))
    outside = Image.new("RGBA", (4, 3), (0, 0, 0, 0))
    outside.putpixel((3, 1), (255, 0, 0, 255))
    PixelLayer.frompil(
      outside,
      group,
      name=f"{region_id}__overlay",
      top=group.top,
      left=group.left,
      compression=Compression.RLE,
    )
    changed_psd = tmp_path / "outside.psd"
    psd.save(changed_psd)
    with self.assertRaisesRegex(SpineAtlasBoundsError, "outside.*re-pack"):
      writeback_spine_atlas(exported.metadata_path, changed_psd, tmp_path / "outside-output")
