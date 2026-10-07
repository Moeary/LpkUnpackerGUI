"""Selected atlas pieces with reversible, pixel-exact PSD layout coordinates."""
from __future__ import annotations

import copy
import math

import numpy as np
from PIL import Image

from app.core.psd_selection import PsdSelectionError, selection_uv_constraints, uv_region


def build_selected_atlas(cv2, mesh_data, drawable_ids, textures, *, packed=True, limits=None):
    from app.core.psd_reconstructor import (
        PsdLayer, PsdResourceLimits, _normalize_drawable, _safe_layer_name, _validate_layer_budget,
    )

    if isinstance(drawable_ids, (str, bytes)):
        raise PsdSelectionError("Selected drawable IDs must be a list.")
    ids = list(dict.fromkeys(str(value) for value in drawable_ids))
    if not ids:
        raise PsdSelectionError("Select at least one ArtMesh before exporting.")
    limits = limits or PsdResourceLimits()
    texture_pixels = sum(image.shape[0] * image.shape[1] for image in textures)
    _validate_layer_budget(len(ids), 0, texture_pixels, limits, "Selected atlas export")
    drawables = [_normalize_drawable(item) for item in mesh_data.get("drawables", []) if isinstance(item, dict)]
    drawables = [item for item in drawables if item is not None]
    by_id = {item["id"]: item for item in drawables}
    if len(by_id) != len(drawables):
        raise PsdSelectionError("Duplicate ArtMesh IDs are ambiguous.")
    missing = [identifier for identifier in ids if identifier not in by_id]
    if missing:
        raise PsdSelectionError("Selected ArtMesh IDs are unavailable: " + ", ".join(missing))
    sizes = [(image.shape[1], image.shape[0]) for image in textures]
    protected, shared = selection_uv_constraints(cv2, drawables, ids, sizes)
    layers, metadata, total_pixels = [], [], 0
    for index, identifier in enumerate(ids):
        drawable = by_id[identifier]
        texture_index = int(drawable["texture_index"])
        x, y, mask = uv_region(cv2, drawable["uvs"], drawable["indices"], sizes[texture_index])
        if not mask.size or not mask.any():
            raise PsdSelectionError("Selected ArtMesh has no usable UV area: " + identifier)
        h, w = mask.shape
        total_pixels += h * w
        _validate_layer_budget(len(ids), total_pixels, texture_pixels, limits, "Selected atlas export")
        crop = textures[texture_index][y:y + h, x:x + w].copy()
        crop[~mask] = 0
        name = _safe_layer_name(f"{index:03d}_{identifier}")
        layers.append(PsdLayer(name, Image.fromarray(crop), x, y))
        metadata.append({"kind": "atlas-component", "name": name, "drawable_id": identifier,
                         "texture_index": texture_index, "left": x, "top": y, "bbox": [x, y, w, h],
                         "atlas_origin": [x, y], "atlas_uv_bbox": [x, y, w, h],
                         "uvs": copy.deepcopy(drawable["uvs"]), "indices": list(drawable["indices"])})
    if packed:
        # Shelf packing keeps each piece at native resolution, without rotation.
        padding = 8
        width = max(max(layer.image.width for layer in layers) + padding * 2,
                    math.ceil(math.sqrt(sum((layer.image.width + padding) * (layer.image.height + padding)
                                            for layer in layers))) + padding)
        x, y, row_height, used_width = padding, padding, 0, 0
        for layer, info in zip(layers, metadata):
            w, h = layer.image.size
            if x + w + padding > width and x > padding:
                x, y, row_height = padding, y + row_height + padding, 0
            layer.left, layer.top = x, y
            info.update(left=x, top=y, bbox=[x, y, w, h])
            used_width = max(used_width, x + w + padding)
            x += w + padding
            row_height = max(row_height, h)
        size = (used_width, y + row_height + padding)
    else:
        size = (max(w for w, _ in sizes), max(h for _, h in sizes))
    selection = {"version": 1, "policy": "selected-atlas-pieces", "coordinate_space": "atlas-pixels",
                 "layout": "packed" if packed else "original", "selected_ids": ids,
                 "requested_ids": ids, "exported_ids": ids, "protected_drawables": protected,
                 "shared_regions": shared, "shared_uv_policy": "reject-edits-affecting-unselected",
                 "geometry_policy": "keep-layer-positions-and-canvas-size"}
    return layers, size, metadata, selection


def apply_atlas_delta(cv2, edited, baseline, canvas, info):
    x, y = map(int, info["atlas_origin"])
    height, width = edited.shape[:2]
    if x < 0 or y < 0 or x + width > canvas.shape[1] or y + height > canvas.shape[0]:
        raise PsdSelectionError("Selected atlas origin is outside its source texture.")
    left, top, mask = uv_region(cv2, info["uvs"], info["indices"], (canvas.shape[1], canvas.shape[0]))
    if (left, top) != (x, y) or mask.shape != (height, width):
        raise PsdSelectionError("Selected atlas mapping no longer matches its UV geometry.")
    changed = (edited != baseline) & mask[:, :, None]
    result = canvas.copy()
    region = result[y:y + height, x:x + width]
    region[changed] = edited[changed]
    return result, mask.astype(np.float32)
