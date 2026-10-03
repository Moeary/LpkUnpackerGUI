"""Pose-space PSD selection and conservative texture-space write constraints.

The complete drawable list remains available for clipping masks.  A selection
only controls which editable units are exported; it never changes MOC or UVs.
"""

import base64
import copy
import hashlib
import math
import zlib
from typing import Any, Mapping, Sequence

import numpy as np


class PsdSelectionError(ValueError):
    pass


def normalize_region(region: Any) -> dict[str, Any] | None:
    if region is None:
        return None
    if isinstance(region, Mapping):
        values = [region.get(key) for key in ("x", "y", "width", "height")]
    elif isinstance(region, (list, tuple)) and len(region) == 4:
        values = list(region)
    else:
        raise PsdSelectionError("Selection region must contain x, y, width and height.")
    try:
        values = [float(value) for value in values]
    except (TypeError, ValueError) as exc:
        raise PsdSelectionError("Selection region coordinates must be finite numbers.") from exc
    if not all(math.isfinite(value) for value in values) or min(values[2:]) <= 0:
        raise PsdSelectionError("Selection region must have a finite, positive size.")
    result = dict(zip(("x", "y", "width", "height"), values))
    if isinstance(region, Mapping) and region.get("polygon") is not None:
        points = np.asarray(region["polygon"], dtype=np.float64)
        if points.ndim != 2 or points.shape[1] != 2 or len(points) < 3 or not np.all(np.isfinite(points)):
            raise PsdSelectionError("Selection polygon must contain finite coordinate pairs.")
        result["polygon"] = points.tolist()
    return result


def prepare_selection(
    mesh_data: Mapping[str, Any],
    drawables: list[dict[str, Any]],
    drawable_ids: Sequence[str],
    source_size: tuple[int, int],
    source_offset: np.ndarray,
    region: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if isinstance(drawable_ids, (str, bytes)):
        raise PsdSelectionError("Selected drawable IDs must be a list, not a string.")
    ids = list(dict.fromkeys(str(value) for value in drawable_ids))
    if not ids or any(not value for value in ids):
        raise PsdSelectionError("Select at least one visible ArtMesh before exporting.")
    available: dict[str, dict[str, Any]] = {}
    for drawable in drawables:
        identifier = drawable["id"]
        if identifier in available:
            raise PsdSelectionError(f"Duplicate drawable ID is ambiguous: {identifier}")
        available[identifier] = drawable
    missing = [value for value in ids if value not in available]
    if missing:
        raise PsdSelectionError("Selected ArtMesh IDs are unavailable: " + ", ".join(missing))
    visible = [available[value] for value in ids if available[value].get("visible", True)
               and float(available[value].get("opacity", 1)) > 0.001]
    if not visible:
        raise PsdSelectionError("The selected ArtMeshes have no visible pose content.")
    points = np.concatenate([np.asarray(item["vertices"], dtype=np.float64) for item in visible])
    if points.ndim != 2 or points.shape[1] != 2 or not np.all(np.isfinite(points)):
        raise PsdSelectionError("Selected ArtMesh vertices must be finite coordinate pairs.")
    points += source_offset
    origin = np.floor(np.min(points, axis=0)).astype(int) - 4
    end = np.ceil(np.max(points, axis=0)).astype(int) + 5
    size = end - origin
    if np.any(size <= 0):
        raise PsdSelectionError("The selected pose bounds are empty.")
    copied = copy.deepcopy(dict(mesh_data))
    copied["canvas"] = {
        **dict(mesh_data.get("canvas") or {}), "width": int(size[0]), "height": int(size[1]),
        "offset_x": float(source_offset[0] - origin[0]),
        "offset_y": float(source_offset[1] - origin[1]),
    }
    selection = {
        "version": 1, "policy": "whole-visible-drawables",
        "coordinate_space": "canvas-pixels-y-down", "requested_ids": ids,
        "selected_ids": [item["id"] for item in visible],
        "region": normalize_region(region), "origin": origin.tolist(),
        "source_canvas": {"width": int(source_size[0]), "height": int(source_size[1])},
        "shared_uv_policy": "reject-edits-affecting-unselected",
        "geometry_policy": "keep-layer-positions-and-canvas-size",
    }
    if isinstance(mesh_data.get("selection_pose"), Mapping):
        selection["pose"] = copy.deepcopy(dict(mesh_data["selection_pose"]))
    return copied, selection


def texture_points(uvs: Any, size: tuple[int, int]) -> np.ndarray:
    points = np.asarray(uvs, dtype=np.float32).copy()
    if points.ndim != 2 or points.shape[1] != 2 or not np.all(np.isfinite(points)):
        raise PsdSelectionError("ArtMesh UV coordinates must be finite pairs.")
    if len(points) and np.max(np.abs(points)) <= 2:
        points[:, 0] *= size[0]
        points[:, 1] = (1 - points[:, 1]) * size[1]
    return points


def uv_region(cv2: Any, uvs: Any, indices: Any, size: tuple[int, int]) -> tuple[int, int, np.ndarray]:
    points = texture_points(uvs, size)
    if not len(points):
        return 0, 0, np.zeros((0, 0), dtype=bool)
    left, top = np.maximum(0, np.floor(np.min(points, axis=0)).astype(int))
    right, bottom = np.minimum(size, np.ceil(np.max(points, axis=0)).astype(int) + 1)
    if right <= left or bottom <= top:
        return 0, 0, np.zeros((0, 0), dtype=bool)
    mask = np.zeros((bottom - top, right - left), dtype=np.uint8)
    shifted = points - np.asarray([left, top], dtype=np.float32)
    values = [int(value) for value in indices]
    for index in range(0, len(values) - 2, 3):
        triangle = values[index:index + 3]
        if min(triangle) < 0 or max(triangle) >= len(points):
            raise PsdSelectionError("ArtMesh triangle index is outside its vertex array.")
        vertices = shifted[triangle]
        if abs(float(np.linalg.det(np.stack((vertices[1] - vertices[0], vertices[2] - vertices[0]))))) < 1e-6:
            continue
        cv2.fillConvexPoly(mask, np.rint(vertices).astype(np.int32), 255, lineType=cv2.LINE_8)
    return int(left), int(top), mask > 0


def selection_uv_constraints(
    cv2: Any, drawables: list[dict[str, Any]], selected_ids: Sequence[str],
    texture_sizes: list[tuple[int, int]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected = set(selected_ids)
    coverage: dict[int, np.ndarray] = {}
    for item in drawables:
        if item["id"] not in selected:
            continue
        texture_index = int(item.get("texture_index", -1))
        if not 0 <= texture_index < len(texture_sizes):
            raise PsdSelectionError(f"Selected ArtMesh has an invalid texture index: {item['id']}")
        size = texture_sizes[texture_index]
        mask = coverage.setdefault(texture_index, np.zeros((size[1], size[0]), dtype=bool))
        left, top, local = uv_region(cv2, item["uvs"], item["indices"], size)
        mask[top:top + local.shape[0], left:left + local.shape[1]] |= local
    protected: list[dict[str, Any]] = []
    shared: list[dict[str, Any]] = []
    for item in drawables:
        texture_index = int(item.get("texture_index", -1))
        if item["id"] in selected or texture_index not in coverage:
            continue
        left, top, local = uv_region(cv2, item["uvs"], item["indices"], texture_sizes[texture_index])
        overlap = local & coverage[texture_index][top:top + local.shape[0], left:left + local.shape[1]]
        ys, xs = np.nonzero(overlap)
        if not len(xs):
            continue
        protected.append({"id": item["id"], "texture_index": texture_index,
                          "uvs": item["uvs"], "indices": item["indices"]})
        shared.append({"texture_index": texture_index, "unselected_id": item["id"],
                       "selected_ids": list(selected_ids), "pixels": int(len(xs)),
                       "bbox": [int(left + xs.min()), int(top + ys.min()),
                                int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)]})
    return protected, shared


def encode_visibility(factor: np.ndarray) -> dict[str, Any]:
    values = np.rint(np.clip(factor, 0, 1) * 65535).astype("<u2")
    payload = values.tobytes()
    return {"width": int(values.shape[1]), "height": int(values.shape[0]),
            "encoding": "u16-zlib-base64", "sha256": hashlib.sha256(payload).hexdigest(),
            "data": base64.b64encode(zlib.compress(payload)).decode("ascii")}


def decode_visibility(value: Mapping[str, Any], shape: tuple[int, int]) -> np.ndarray:
    if value.get("encoding") != "u16-zlib-base64" or (value.get("height"), value.get("width")) != shape:
        raise PsdSelectionError("Selection visibility metadata does not match the original layer size.")
    length = shape[0] * shape[1] * 2
    try:
        compressed = base64.b64decode(value["data"], validate=True)
        decoder = zlib.decompressobj()
        payload = decoder.decompress(compressed, length + 1)
    except (ValueError, KeyError, zlib.error) as exc:
        raise PsdSelectionError("Selection visibility metadata is invalid.") from exc
    if len(payload) != length or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise PsdSelectionError("Selection visibility metadata has an invalid data size.")
    if hashlib.sha256(payload).hexdigest() != value.get("sha256"):
        raise PsdSelectionError("Selection visibility metadata digest changed.")
    return np.frombuffer(payload, dtype="<u2").reshape(shape).astype(np.float32) / 65535


def apply_pose_delta(
    cv2: Any, edited: np.ndarray, baseline: np.ndarray, factor: np.ndarray,
    canvas: np.ndarray, vertices: np.ndarray, uvs: Any, indices: Any,
    baseline_canvas: np.ndarray | None = None,
) -> np.ndarray:
    """Project only edited channel deltas through the frozen pose triangles.

    RGB-only edits retain the exact original atlas alpha.  Alpha edits are
    divided by the recorded clipping/opacity factor; invisible pose texels
    cannot provide an inverse mapping and remain untouched.
    """
    delta = edited.astype(np.float32) - baseline.astype(np.float32)
    visible = factor > 1 / 65535
    delta[:, :, 3] = np.divide(delta[:, :, 3], factor, out=np.zeros_like(factor), where=visible)
    changed = np.any(edited != baseline, axis=2) & visible
    delta[~changed] = 0
    result = canvas.copy()
    size = (canvas.shape[1], canvas.shape[0])
    texture_uv = texture_points(uvs, size)
    values = [int(value) for value in indices]
    for index in range(0, len(values) - 2, 3):
        triangle = values[index:index + 3]
        source_tri = vertices[triangle].astype(np.float32)
        target_tri = texture_uv[triangle].astype(np.float32)
        if abs(float(np.linalg.det(np.stack((source_tri[1] - source_tri[0], source_tri[2] - source_tri[0]))))) < 1e-6:
            continue
        left = max(0, int(math.floor(float(np.min(target_tri[:, 0])))))
        top = max(0, int(math.floor(float(np.min(target_tri[:, 1])))))
        right = min(size[0], int(math.ceil(float(np.max(target_tri[:, 0])))) + 1)
        bottom = min(size[1], int(math.ceil(float(np.max(target_tri[:, 1])))) + 1)
        if right <= left or bottom <= top:
            continue
        shifted = target_tri - np.asarray([left, top], dtype=np.float32)
        matrix = cv2.getAffineTransform(source_tri, shifted)
        warped = cv2.warpAffine(delta, matrix, (right - left, bottom - top), flags=cv2.INTER_LINEAR,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        warped_mask = cv2.warpAffine(changed.astype(np.uint8), matrix, (right - left, bottom - top),
                                     flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        tri_mask = np.zeros(warped_mask.shape, dtype=np.uint8)
        cv2.fillConvexPoly(tri_mask, np.rint(shifted).astype(np.int32), 1, lineType=cv2.LINE_8)
        write = (warped_mask > 0) & (tri_mask > 0)
        # Triangles on a shared edge use the immutable input canvas, so a
        # boundary delta is never applied twice.
        original = (baseline_canvas if baseline_canvas is not None else canvas)[top:bottom, left:right].astype(np.float32)
        candidate = np.rint(np.clip(original + warped, 0, 255)).astype(np.uint8)
        result[top:bottom, left:right][write] = candidate[write]
    return result


def affected_unselected(cv2: Any, changed: np.ndarray, protected: list[dict[str, Any]], texture_index: int) -> list[str]:
    affected = []
    for item in protected:
        if int(item["texture_index"]) != texture_index:
            continue
        left, top, mask = uv_region(cv2, item["uvs"], item["indices"], (changed.shape[1], changed.shape[0]))
        if np.any(changed[top:top + mask.shape[0], left:left + mask.shape[1]] & mask):
            affected.append(str(item["id"]))
    return affected
