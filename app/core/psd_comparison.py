"""Read-only PSD round-trip comparison at texture resolution and a frozen pose."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image


def comparison_images(before, after):
    """Include alpha-only edits; RGB hidden behind alpha is still a texel edit."""
    if before.size != after.size:
        raise ValueError("Comparison textures must have the same dimensions.")
    changed = np.any(np.asarray(before.convert("RGBA")) != np.asarray(after.convert("RGBA")), axis=2)
    highlight = Image.new("RGBA", before.size, (0, 0, 0, 0))
    pixels = np.asarray(highlight).copy()
    pixels[changed] = (255, 60, 130, 230)
    return Image.fromarray(pixels), changed


def build_comparison(metadata_path, texture_outputs, output_dir, *, mesh_data=None, progress=None):
    from app.core.psd_reconstructor import (
        PsdResourceLimits, _metadata_textures, _read_json, _resolve_repack_texture_path,
        _validate_metadata_textures, _validate_texture_paths, _render_mesh_layers, _require_cv2,
        _configure_opencv, _validate_texture_digest,
    )
    from app.core.psd_selection import uv_region

    metadata_file = Path(metadata_path).resolve()
    metadata = _read_json(metadata_file)
    textures = _metadata_textures(metadata, metadata_file)
    outputs = {int(index): Path(path) for index, path in texture_outputs.items()}
    if set(outputs) != {int(item["index"]) for item in textures}:
        raise ValueError("Comparison requires a complete texture version.")
    limits = PsdResourceLimits(mesh_max_dimension=1024)
    texture_pixels = _validate_metadata_textures(textures, limits, "PSD comparison")
    _validate_texture_paths(outputs.values(), limits, "PSD comparison")
    cv2 = _require_cv2()
    _configure_opencv(cv2, limits)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    views, affected_ids, warnings = [], set(), []
    current_moc = (mesh_data or {}).get("comparison_moc_sha256")
    source_moc = (metadata.get("source_moc_summary") or {}).get("sha256")
    if current_moc and source_moc and current_moc != source_moc:
        mesh_data = None
        warnings.append("Current model differs from this PSD's source. Showing atlas comparison only.")
    before_atlases, after_atlases = {}, {}
    drawables = (mesh_data or {}).get("drawables") or metadata.get("layers", [])

    def save_view(label, before, after, changed, highlight, affected, texture_index=None):
        record = {"label": label, "changed_pixels": int(changed.sum()), "total_pixels": int(changed.size),
                  "affected_ids": sorted(affected), "texture_index": texture_index}
        for name, image in (("before", before), ("after", after), ("highlight", highlight)):
            # Keep native atlas resolution so zooming can inspect individual
            # texels, including a single erased alpha pixel on a large atlas.
            path = directory / f"view_{len(views)}_{name}.png"
            image.save(path)
            record[name] = str(path)
        views.append(record)

    for ordinal, texture in enumerate(textures):
        index = int(texture["index"])
        source = _resolve_repack_texture_path(texture, None, metadata_file)
        _validate_texture_paths([source], limits, "PSD comparison")
        with Image.open(source) as image:
            before = image.convert("RGBA")
        _validate_texture_digest(source, texture, before, metadata_file)
        with Image.open(outputs[index]) as image:
            after = image.convert("RGBA")
        if before.size != (texture["width"], texture["height"]):
            raise ValueError("Comparison baseline texture dimensions have changed.")
        highlight, changed = comparison_images(before, after)
        affected = set()
        for drawable in drawables:
            if int(drawable.get("texture_index", -1)) != index or not drawable.get("uvs"):
                continue
            left, top, mask = uv_region(cv2, drawable["uvs"], drawable["indices"], before.size)
            if mask.size and np.any(changed[top:top + mask.shape[0], left:left + mask.shape[1]] & mask):
                affected.add(str(drawable.get("id") or drawable.get("drawable_id") or drawable.get("name")))
        affected_ids.update(affected)
        save_view(texture["name"], before, after, changed, highlight, affected, index)
        if mesh_data:
            before_atlases[index], after_atlases[index] = np.asarray(before), np.asarray(after)
        if progress:
            progress(10 + int(ordinal / len(textures) * 45), texture["name"])

    if mesh_data:
        try:
            # PSD composite semantics (including blend modes) match this workbench;
            # this is a frozen CPU reference, not a claim of native-renderer parity.
            from tempfile import TemporaryDirectory
            from psd_tools import PSDImage
            from app.core.psd_reconstructor import _save_psd
            renders = []
            with TemporaryDirectory(prefix="pose-", dir=directory) as temporary:
                for side, atlases in (("before", before_atlases), ("after", after_atlases)):
                    if set(atlases) != set(range(len(atlases))):
                        raise ValueError("Pose comparison requires consecutive texture indices.")
                    layers, size, _ = _render_mesh_layers(cv2, mesh_data,
                        [atlases[i] for i in range(len(atlases))], progress, limits, texture_pixels)
                    path = Path(temporary) / f"{side}.psd"
                    _save_psd(path, size, layers, resource_limits=limits, flat_layers=True)
                    composite = PSDImage.open(path).composite(force=True)
                    if composite is None:
                        raise ValueError("The current pose has no visible content.")
                    renders.append(composite.convert("RGBA"))
                highlight, changed = comparison_images(*renders)
                save_view("pose", *renders, changed, highlight, affected_ids)
        except Exception as exc:
            warnings.append(str(exc))
    report = {"views": views, "affected_ids": sorted(affected_ids), "warnings": warnings,
              "complete_geometry": bool(mesh_data), "metadata": str(metadata_file)}
    (directory / "comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
