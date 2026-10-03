"""Read-only audit of atlas rectangles and editor mesh image coverage."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from PIL import Image, ImageDraw
from app.core.spine_atlas import parse_atlas


def inspect(root: Path):
    atlas = parse_atlas(next(root.glob("*.atlas")))
    overlaps = []
    for i, a in enumerate(atlas.regions):
        for b in atlas.regions[i + 1:]:
            if a.page_index != b.page_index:
                continue
            width = min(a.x + a.packed_width, b.x + b.packed_width) - max(a.x, b.x)
            height = min(a.y + a.packed_height, b.y + b.packed_height) - max(a.y, b.y)
            if width > 0 and height > 0:
                overlaps.append([a.name, b.name, width * height])
    project = root / "editor_project"
    data = json.loads((project / "skeleton_0.json").read_text(encoding="utf-8"))
    records = []
    for skin in data["skins"]:
        for slot, attachments in skin.get("attachments", {}).items():
            for name, attachment in attachments.items():
                if attachment.get("type") != "mesh":
                    continue
                image_path = project / data["skeleton"]["images"] / (attachment.get("path", name) + ".png")
                with Image.open(image_path) as image:
                    alpha = np.asarray(image.convert("RGBA"))[:, :, 3]
                    mask = Image.new("L", image.size)
                    draw = ImageDraw.Draw(mask)
                    uvs = attachment["uvs"]
                    vertices = [(uvs[i] * image.width, uvs[i+1] * image.height) for i in range(0, len(uvs), 2)]
                    indices = attachment["triangles"]
                    for i in range(0, len(indices), 3):
                        draw.polygon([vertices[j] for j in indices[i:i+3]], fill=255)
                    opaque = alpha > 8
                    outside = opaque & (np.asarray(mask) == 0)
                    records.append(dict(slot=slot, attachment=name, image=str(image_path),
                                        size=list(image.size), nontransparent_pixels=int(opaque.sum()),
                                        outside_mesh_pixels=int(outside.sum())))
    return dict(pages=len(atlas.pages), regions=len(atlas.regions),
                overlapping_rectangle_pairs=len(overlaps), overlaps=overlaps,
                mesh_images=records,
                note="Outside-mesh pixels include padding and neighboring atlas content; this is a diagnostic, not a safe erase mask.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = inspect(args.source.resolve())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in {"overlaps", "mesh_images"}}, ensure_ascii=False))
    for item in sorted(report["mesh_images"], key=lambda r: r["outside_mesh_pixels"], reverse=True)[:5]:
        print(item["attachment"], item["size"], item["outside_mesh_pixels"])
