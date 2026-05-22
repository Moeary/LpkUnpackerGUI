import json
import math
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import numpy as np
from PIL import Image


ProgressCallback = Callable[[int, str], None]
METADATA_FORMAT = "LpkUnpacker.Live2DAtlasPSD"
METADATA_VERSION = 1


class PsdReconstructionError(RuntimeError):
    pass


class MissingDependencyError(PsdReconstructionError):
    pass


@dataclass
class Live2DSource:
    root_dir: Path
    model_json: Path
    moc3: Optional[Path]
    textures: list[Path]
    mesh_data: Optional[dict[str, Any]]


@dataclass
class ReconstructionResult:
    psd_path: Path
    layer_count: int
    mode: str
    warnings: list[str]
    metadata_path: Optional[Path] = None
    output_paths: list[Path] = field(default_factory=list)

    @property
    def primary_path(self) -> Path:
        if self.output_paths:
            return self.output_paths[0]
        return self.psd_path


@dataclass
class PsdLayer:
    name: str
    image: Image.Image
    left: int = 0
    top: int = 0


def reconstruct_live2d_psd(
    source: str | Path,
    output_dir: str | Path,
    progress: Optional[ProgressCallback] = None,
    mode: str = "mesh",
) -> ReconstructionResult:
    cv2 = _require_cv2()
    _require_psd_tools()

    source_info = resolve_live2d_source(Path(source))
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    _emit(progress, 5, "Live2D source resolved")

    textures = [_load_rgba_texture(cv2, path) for path in source_info.textures]
    if not textures:
        raise PsdReconstructionError("No texture atlas PNG files were found.")

    _emit(progress, 20, f"Loaded {len(textures)} texture atlas file(s)")

    if mode == "mesh" and not source_info.mesh_data:
        try:
            from app.core.cubism_core import CubismCoreError, export_drawables_sidecar

            _emit(progress, 22, "No mesh sidecar found. Trying Cubism Core drawable export.")
            sidecar_path = export_drawables_sidecar(
                source_info.model_json,
                output_path,
                progress=lambda value, message: _emit(progress, min(70, 22 + value // 3), message),
            )
            source_info.mesh_data = _read_json(sidecar_path)
            warnings.append(f"Drawable mesh metadata was exported with Cubism Core: {sidecar_path}")
        except CubismCoreError as exc:
            warnings.append(f"Cubism Core drawable export unavailable: {exc}")

    if mode == "mesh" and source_info.mesh_data:
        layers, size, layer_metadata = _render_mesh_layers(
            cv2, source_info.mesh_data, textures, progress
        )
        mode = "mesh"
    else:
        if mode == "mesh":
            warnings.append(
                "No drawable mesh metadata was found. Exported editable atlas layers instead."
            )
        layers, size, layer_metadata = _build_texture_component_layers(
            cv2, source_info.textures, textures, progress
        )
        mode = "atlas-components"

    model_name = _clean_model_name(source_info.model_json)
    suffix = "mesh_pose" if mode == "mesh" else "editable_atlas"
    psd_path = output_path / f"{model_name}_{suffix}.psd"
    metadata_path = output_path / f"{model_name}_{suffix}.lpkpsd.json"
    _emit(progress, 95, f"Writing PSD with {len(layers)} layer(s)")
    _save_psd(psd_path, size, layers, raw_layers=(mode == "mesh"))
    _write_json(
        metadata_path,
        _build_export_metadata(source_info, textures, size, mode, layer_metadata),
    )

    _emit(progress, 100, f"PSD written: {psd_path}")
    return ReconstructionResult(
        psd_path=psd_path,
        layer_count=len(layers),
        mode=mode,
        warnings=warnings,
        metadata_path=metadata_path,
    )


def repack_atlas_png_from_psd(
    psd_path: str | Path,
    output_dir: str | Path,
    metadata_path: str | Path | None = None,
    progress: Optional[ProgressCallback] = None,
) -> ReconstructionResult:
    _require_psd_tools()
    from psd_tools import PSDImage

    psd_file = Path(psd_path).resolve()
    if not psd_file.is_file():
        raise PsdReconstructionError(f"PSD file does not exist: {psd_file}")

    metadata_file = Path(metadata_path).resolve() if metadata_path else _default_metadata_path(psd_file)
    metadata = _read_json(metadata_file)
    if metadata.get("format") != METADATA_FORMAT:
        raise PsdReconstructionError(f"Unsupported PSD metadata file: {metadata_file}")
    if metadata.get("mode") != "atlas-components":
        raise PsdReconstructionError(
            "Only editable atlas PSD metadata can be repacked into atlas PNG files."
        )

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    textures = _metadata_textures(metadata)
    layers_metadata = _metadata_layers(metadata)

    psd = PSDImage.open(str(psd_file))
    psd_layers = _index_psd_layers(psd)
    canvases = {
        texture["index"]: Image.new("RGBA", (texture["width"], texture["height"]), (0, 0, 0, 0))
        for texture in textures
    }

    warnings: list[str] = []
    _emit(progress, 10, f"Loaded PSD: {psd_file}")
    total = max(1, len(layers_metadata))

    for index, layer_info in enumerate(layers_metadata):
        texture_index = int(layer_info["texture_index"])
        canvas = canvases.get(texture_index)
        if canvas is None:
            continue

        layer = psd_layers.get(str(layer_info["name"]))
        if layer is None:
            warnings.append(f"Layer missing in PSD: {layer_info['name']}")
            continue

        image = layer.composite(force=True)
        if image is None:
            warnings.append(f"Layer has no pixel content: {layer_info['name']}")
            continue

        left = int(getattr(layer, "left", layer_info.get("left", 0)))
        top = int(getattr(layer, "top", layer_info.get("top", 0)))
        _alpha_composite_at(canvas, image.convert("RGBA"), left, top)
        _emit(progress, 10 + int((index + 1) / total * 75), f"Packed {layer_info['name']}")

    outputs: list[Path] = []
    for texture in textures:
        canvas = canvases[texture["index"]]
        output_file = output_path / texture["name"]
        canvas.save(output_file)
        outputs.append(output_file)

    _emit(progress, 100, f"Atlas PNG written: {output_path}")
    return ReconstructionResult(
        psd_path=outputs[0] if outputs else output_path,
        layer_count=len(layers_metadata),
        mode="atlas-repack",
        warnings=warnings,
        metadata_path=metadata_file,
        output_paths=outputs,
    )


def resolve_live2d_source(source: Path) -> Live2DSource:
    source = source.resolve()
    if not source.exists():
        raise PsdReconstructionError(f"Input path does not exist: {source}")

    if source.is_dir():
        model_json = _find_model_json(source)
    elif source.suffix.lower() == ".json":
        model_json = source
    elif source.suffix.lower() == ".moc3":
        model_json = _find_model_json_for_moc(source)
    else:
        raise PsdReconstructionError("Please select a model3.json file, .moc3 file, or folder.")

    data = _read_json(model_json)
    file_refs = data.get("FileReferences", {})
    root_dir = model_json.parent

    moc3 = None
    moc_value = file_refs.get("Moc")
    if isinstance(moc_value, str) and moc_value:
        moc3 = (root_dir / moc_value).resolve()

    textures = []
    for item in file_refs.get("Textures", []):
        if isinstance(item, str) and item:
            texture_path = (root_dir / item).resolve()
            if texture_path.is_file():
                textures.append(texture_path)

    mesh_data = _find_mesh_sidecar(root_dir, model_json, moc3)
    return Live2DSource(root_dir, model_json.resolve(), moc3, textures, mesh_data)


def _find_model_json(folder: Path) -> Path:
    candidates = []
    for path in folder.rglob("*.json"):
        try:
            data = _read_json(path)
        except PsdReconstructionError:
            continue
        refs = data.get("FileReferences")
        if isinstance(refs, dict) and refs.get("Moc") and refs.get("Textures"):
            score = 0 if path.name.endswith(".model3.json") else 1
            candidates.append((score, len(path.parts), path))

    if not candidates:
        raise PsdReconstructionError("No Live2D model3.json file was found in the folder.")

    return sorted(candidates)[0][2]


def _find_model_json_for_moc(moc3: Path) -> Path:
    moc_name = moc3.name.lower()
    candidates = []
    for path in moc3.parent.rglob("*.json"):
        try:
            data = _read_json(path)
        except PsdReconstructionError:
            continue

        moc_value = data.get("FileReferences", {}).get("Moc")
        if isinstance(moc_value, str) and Path(moc_value).name.lower() == moc_name:
            score = 0 if path.name.endswith(".model3.json") else 1
            candidates.append((score, len(path.parts), path))

    if not candidates:
        raise PsdReconstructionError(
            "A .moc3 file needs a sibling model3.json that references it."
        )

    return sorted(candidates)[0][2]


def _find_mesh_sidecar(
    root_dir: Path,
    model_json: Path,
    moc3: Optional[Path],
) -> Optional[dict[str, Any]]:
    names = [
        f"{model_json.stem}.drawables.json",
        "drawables.json",
        "mesh.json",
        "meshes.json",
    ]
    if moc3:
        names.extend([f"{moc3.stem}.drawables.json", f"{moc3.stem}.mesh.json"])

    for name in names:
        path = root_dir / name
        if path.is_file():
            data = _read_json(path)
            if isinstance(data, list):
                return {"drawables": data}
            if isinstance(data, dict):
                return data
    return None


def _render_mesh_layers(
    cv2,
    mesh_data: dict[str, Any],
    textures: list[np.ndarray],
    progress: Optional[ProgressCallback],
) -> tuple[list[PsdLayer], tuple[int, int], list[dict[str, Any]]]:
    drawables = mesh_data.get("drawables")
    if not isinstance(drawables, list) or not drawables:
        raise PsdReconstructionError("Drawable mesh metadata is empty or invalid.")

    drawable_infos = []
    for source_index, item in enumerate(drawables):
        if not isinstance(item, dict):
            continue
        normalized = _normalize_drawable(item)
        if normalized:
            normalized["source_index"] = source_index
            drawable_infos.append(normalized)
    if not drawable_infos:
        raise PsdReconstructionError("No usable drawable mesh entries were found.")

    size, offset = _resolve_canvas(mesh_data, drawable_infos)
    canvas_w, canvas_h = size
    drawable_by_index = {int(item["source_index"]): item for item in drawable_infos}
    ordered = sorted(
        drawable_infos,
        key=lambda item: (item.get("render_order", item.get("draw_order", 0)), item["id"]),
    )

    layers: list[PsdLayer] = []
    layer_metadata: list[dict[str, Any]] = []
    total = max(1, len(ordered))
    for index, drawable in enumerate(ordered):
        if not drawable.get("visible", True):
            continue

        texture_index = int(drawable.get("texture_index", 0))
        if texture_index < 0 or texture_index >= len(textures):
            continue

        opacity = float(drawable.get("opacity", 1.0))
        if opacity <= 0.001:
            continue

        texture = textures[texture_index]
        vertices = np.asarray(drawable["vertices"], dtype=np.float32) + offset
        uvs = _uv_to_texture_points(np.asarray(drawable["uvs"], dtype=np.float32), texture)
        if not _uv_source_has_alpha(texture, uvs):
            continue

        indices = drawable["indices"]
        bounds = _vertex_bounds(vertices, canvas_w, canvas_h)
        if bounds is None:
            continue
        layer_left, layer_top, layer_width, layer_height = bounds
        layer = np.zeros((layer_height, layer_width, 4), dtype=np.uint8)
        local_vertices = vertices - np.asarray([layer_left, layer_top], dtype=np.float32)

        _paint_triangles(cv2, texture, layer, uvs, local_vertices, indices)

        if opacity < 1.0:
            layer[:, :, 3] = np.clip(layer[:, :, 3].astype(np.float32) * opacity, 0, 255).astype(
                np.uint8
            )
        _apply_drawable_masks(
            cv2,
            layer,
            drawable,
            drawable_by_index,
            textures,
            offset,
            np.asarray([layer_left, layer_top], dtype=np.float32),
        )

        bbox = _alpha_bbox(layer)
        if bbox is None:
            continue
        crop_left, crop_top, width, height = bbox
        left = layer_left + crop_left
        top = layer_top + crop_top

        layer_name = _safe_layer_name(drawable["id"])
        image = Image.fromarray(
            layer[crop_top : crop_top + height, crop_left : crop_left + width],
            "RGBA",
        )
        layers.append(PsdLayer(layer_name, image, left, top))
        layer_metadata.append(
            {
                "kind": "drawable-mesh",
                "name": layer_name,
                "drawable_id": drawable["id"],
                "texture_index": texture_index,
                "left": left,
                "top": top,
                "bbox": [left, top, width, height],
                "vertices": drawable["vertices"],
                "uvs": drawable["uvs"],
                "indices": drawable["indices"],
                "opacity": drawable.get("opacity", 1.0),
                "render_order": drawable.get("render_order", 0),
                "draw_order": drawable.get("draw_order", 0),
            }
        )
        _emit(progress, 20 + int((index + 1) / total * 70), f"Rendered {drawable['id']}")

    if not layers:
        raise PsdReconstructionError("No drawable layers were rendered.")
    return layers, size, layer_metadata


def _build_texture_component_layers(
    cv2,
    paths: list[Path],
    textures: list[np.ndarray],
    progress: Optional[ProgressCallback] = None,
) -> tuple[list[PsdLayer], tuple[int, int], list[dict[str, Any]]]:
    width = max(texture.shape[1] for texture in textures)
    height = max(texture.shape[0] for texture in textures)
    layers: list[PsdLayer] = []
    layer_metadata: list[dict[str, Any]] = []

    for texture_index, (path, texture) in enumerate(zip(paths, textures)):
        alpha = texture[:, :, 3]
        mask = (alpha > 0).astype(np.uint8)
        component_count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        pieces = []

        for label_index in range(1, component_count):
            x, y, w, h, area = [int(value) for value in stats[label_index]]
            if area < 16:
                continue
            pieces.append((y, x, w, h, area, label_index))

        if not pieces and np.any(alpha > 0):
            pieces.append((0, 0, texture.shape[1], texture.shape[0], int(np.count_nonzero(alpha)), 0))

        pieces.sort(key=lambda item: (item[0], item[1], -item[4]))
        for piece_index, (y, x, w, h, area, label_index) in enumerate(pieces, start=1):
            crop = texture[y : y + h, x : x + w].copy()
            if label_index:
                component_mask = labels[y : y + h, x : x + w] == label_index
                crop[:, :, 3] = np.where(component_mask, crop[:, :, 3], 0).astype(np.uint8)

            layer_name = _safe_layer_name(f"tex{texture_index:02d}_piece_{piece_index:03d}")
            layers.append(PsdLayer(layer_name, Image.fromarray(crop, "RGBA"), x, y))
            layer_metadata.append(
                {
                    "kind": "atlas-component",
                    "name": layer_name,
                    "texture_index": texture_index,
                    "texture_name": path.name,
                    "left": x,
                    "top": y,
                    "bbox": [x, y, w, h],
                    "alpha_area": area,
                }
            )

    if not layers:
        layers, size, layer_metadata = _build_texture_atlas_layers(paths, textures)
        return layers, size, layer_metadata

    _emit(progress, 85, f"Split texture atlases into {len(layers)} editable layer(s)")
    return layers, (width, height), layer_metadata


def _build_texture_atlas_layers(
    paths: list[Path],
    textures: list[np.ndarray],
) -> tuple[list[PsdLayer], tuple[int, int], list[dict[str, Any]]]:
    width = max(texture.shape[1] for texture in textures)
    height = max(texture.shape[0] for texture in textures)
    layers: list[PsdLayer] = []
    layer_metadata: list[dict[str, Any]] = []

    for texture_index, (path, texture) in enumerate(zip(paths, textures)):
        layer = np.zeros((height, width, 4), dtype=np.uint8)
        h, w = texture.shape[:2]
        layer[:h, :w] = texture
        layer_name = _safe_layer_name(path.stem)
        layers.append(PsdLayer(layer_name, Image.fromarray(layer, "RGBA")))
        layer_metadata.append(
            {
                "kind": "texture-atlas",
                "name": layer_name,
                "texture_index": texture_index,
                "texture_name": path.name,
                "left": 0,
                "top": 0,
                "bbox": [0, 0, w, h],
                "alpha_area": int(np.count_nonzero(texture[:, :, 3])),
            }
        )

    return layers, (width, height), layer_metadata


def _normalize_drawable(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    vertices = _as_points(item.get("vertices") or item.get("vertex_positions"))
    uvs = _as_points(item.get("uvs") or item.get("vertex_uvs"))
    indices = _as_int_list(item.get("indices") or item.get("triangle_indices"))
    if vertices is None or uvs is None or not indices:
        return None

    return {
        "id": str(item.get("id") or item.get("name") or f"drawable_{len(vertices)}"),
        "texture_index": int(item.get("texture_index", item.get("texture", 0))),
        "vertices": vertices,
        "uvs": uvs,
        "indices": indices,
        "opacity": float(item.get("opacity", 1.0)),
        "visible": _drawable_visible(item),
        "masks": _as_int_list(item.get("masks") or []),
        "inverted_mask": bool(item.get("inverted_mask", False)),
        "dynamic_flags": int(item.get("dynamic_flags", 1)),
        "constant_flags": int(item.get("constant_flags", 0)),
        "render_order": int(item.get("render_order", item.get("draw_order", 0))),
        "draw_order": int(item.get("draw_order", item.get("render_order", 0))),
    }


def _drawable_visible(item: dict[str, Any]) -> bool:
    if "visible" in item:
        return bool(item["visible"])
    if "dynamic_flags" in item:
        return bool(int(item["dynamic_flags"]) & 1)
    return True


def _resolve_canvas(
    mesh_data: dict[str, Any],
    drawables: list[dict[str, Any]],
) -> tuple[tuple[int, int], np.ndarray]:
    canvas = mesh_data.get("canvas") if isinstance(mesh_data.get("canvas"), dict) else {}
    width = canvas.get("width") or mesh_data.get("canvas_width") or mesh_data.get("width")
    height = canvas.get("height") or mesh_data.get("canvas_height") or mesh_data.get("height")

    all_vertices = np.concatenate(
        [np.asarray(item["vertices"], dtype=np.float32) for item in drawables], axis=0
    )
    min_xy = np.nanmin(all_vertices, axis=0)
    max_xy = np.nanmax(all_vertices, axis=0)

    if width and height:
        offset = np.asarray(
            [
                float(canvas.get("offset_x", mesh_data.get("offset_x", 0))),
                float(canvas.get("offset_y", mesh_data.get("offset_y", 0))),
            ],
            dtype=np.float32,
        )
        return (int(math.ceil(float(width))), int(math.ceil(float(height)))), offset

    padding = float(mesh_data.get("padding", 16))
    output_w = int(math.ceil(max_xy[0] - min_xy[0] + padding * 2))
    output_h = int(math.ceil(max_xy[1] - min_xy[1] + padding * 2))
    offset = np.asarray([-min_xy[0] + padding, -min_xy[1] + padding], dtype=np.float32)
    return (max(1, output_w), max(1, output_h)), offset


def _warp_triangle(cv2, src: np.ndarray, dst: np.ndarray, src_tri: np.ndarray, dst_tri: np.ndarray) -> None:
    src_rect = cv2.boundingRect(src_tri.astype(np.float32))
    dst_rect = cv2.boundingRect(dst_tri.astype(np.float32))
    sx, sy, sw, sh = src_rect
    dx, dy, dw, dh = dst_rect

    if sw <= 0 or sh <= 0 or dw <= 0 or dh <= 0:
        return

    sx0, sy0 = max(0, sx), max(0, sy)
    sx1, sy1 = min(src.shape[1], sx + sw), min(src.shape[0], sy + sh)
    dx0, dy0 = max(0, dx), max(0, dy)
    dx1, dy1 = min(dst.shape[1], dx + dw), min(dst.shape[0], dy + dh)

    if sx1 <= sx0 or sy1 <= sy0 or dx1 <= dx0 or dy1 <= dy0:
        return

    src_crop = src[sy0:sy1, sx0:sx1]
    src_shift = src_tri - np.asarray([sx0, sy0], dtype=np.float32)
    dst_shift = dst_tri - np.asarray([dx0, dy0], dtype=np.float32)
    matrix = cv2.getAffineTransform(src_shift.astype(np.float32), dst_shift.astype(np.float32))

    warped = cv2.warpAffine(
        src_crop,
        matrix,
        (dx1 - dx0, dy1 - dy0),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0, 0),
    )

    mask = np.zeros((dy1 - dy0, dx1 - dx0), dtype=np.uint8)
    cv2.fillConvexPoly(mask, dst_shift.astype(np.int32), 255, lineType=cv2.LINE_AA)
    _alpha_blend(dst[dy0:dy1, dx0:dx1], warped, mask)


def _paint_triangles(
    cv2,
    texture: np.ndarray,
    layer: np.ndarray,
    uvs: np.ndarray,
    vertices: np.ndarray,
    indices: list[int],
) -> None:
    for tri in _iter_triangles(indices):
        if max(tri) >= len(vertices) or max(tri) >= len(uvs):
            continue
        _warp_triangle(cv2, texture, layer, uvs[list(tri)], vertices[list(tri)])


def _apply_drawable_masks(
    cv2,
    layer: np.ndarray,
    drawable: dict[str, Any],
    drawable_by_index: dict[int, dict[str, Any]],
    textures: list[np.ndarray],
    offset: np.ndarray,
    origin: np.ndarray,
) -> None:
    mask_indices = drawable.get("masks") or []
    if not mask_indices:
        return

    mask_alpha = np.zeros(layer.shape[:2], dtype=np.uint8)
    for mask_index in mask_indices:
        mask_drawable = drawable_by_index.get(int(mask_index))
        if not mask_drawable:
            continue
        texture_index = int(mask_drawable.get("texture_index", 0))
        if texture_index < 0 or texture_index >= len(textures):
            continue

        texture = textures[texture_index]
        vertices = np.asarray(mask_drawable["vertices"], dtype=np.float32) + offset
        uvs = _uv_to_texture_points(np.asarray(mask_drawable["uvs"], dtype=np.float32), texture)
        if not _uv_source_has_alpha(texture, uvs):
            continue

        mask_layer = np.zeros_like(layer)
        _paint_triangles(
            cv2,
            texture,
            mask_layer,
            uvs,
            vertices - origin,
            mask_drawable["indices"],
        )
        opacity = float(mask_drawable.get("opacity", 1.0))
        if opacity < 1.0:
            mask_layer[:, :, 3] = np.clip(
                mask_layer[:, :, 3].astype(np.float32) * opacity,
                0,
                255,
            ).astype(np.uint8)
        mask_alpha = np.maximum(mask_alpha, mask_layer[:, :, 3])

    if not np.any(mask_alpha):
        layer[:, :, 3] = 0
        return

    factor = mask_alpha.astype(np.float32) / 255.0
    if drawable.get("inverted_mask"):
        factor = 1.0 - factor
    layer[:, :, 3] = np.clip(layer[:, :, 3].astype(np.float32) * factor, 0, 255).astype(
        np.uint8
    )


def _alpha_blend(dst_roi: np.ndarray, src_rgba: np.ndarray, mask: np.ndarray) -> None:
    src_alpha = (src_rgba[:, :, 3:4].astype(np.float32) / 255.0) * (
        mask[:, :, None].astype(np.float32) / 255.0
    )
    dst_alpha = dst_roi[:, :, 3:4].astype(np.float32) / 255.0
    out_alpha = src_alpha + dst_alpha * (1.0 - src_alpha)

    src_rgb = src_rgba[:, :, :3].astype(np.float32)
    dst_rgb = dst_roi[:, :, :3].astype(np.float32)
    numerator = src_rgb * src_alpha + dst_rgb * dst_alpha * (1.0 - src_alpha)
    out_rgb = np.divide(
        numerator,
        np.maximum(out_alpha, 1e-6),
        out=np.zeros_like(numerator),
        where=out_alpha > 0,
    )

    dst_roi[:, :, :3] = np.clip(out_rgb, 0, 255).astype(np.uint8)
    dst_roi[:, :, 3:4] = np.clip(out_alpha * 255.0, 0, 255).astype(np.uint8)


def _vertex_bounds(
    vertices: np.ndarray,
    canvas_w: int,
    canvas_h: int,
    padding: int = 2,
) -> Optional[tuple[int, int, int, int]]:
    min_xy = np.floor(np.nanmin(vertices, axis=0)).astype(int) - padding
    max_xy = np.ceil(np.nanmax(vertices, axis=0)).astype(int) + padding
    left = max(0, int(min_xy[0]))
    top = max(0, int(min_xy[1]))
    right = min(canvas_w, int(max_xy[0]))
    bottom = min(canvas_h, int(max_xy[1]))
    if right <= left or bottom <= top:
        return None
    return left, top, right - left, bottom - top


def _uv_source_has_alpha(texture: np.ndarray, points: np.ndarray) -> bool:
    min_xy = np.floor(np.nanmin(points, axis=0)).astype(int) - 1
    max_xy = np.ceil(np.nanmax(points, axis=0)).astype(int) + 1
    left = max(0, int(min_xy[0]))
    top = max(0, int(min_xy[1]))
    right = min(texture.shape[1], int(max_xy[0]))
    bottom = min(texture.shape[0], int(max_xy[1]))
    if right <= left or bottom <= top:
        return False
    return bool(np.any(texture[top:bottom, left:right, 3] > 0))


def _alpha_bbox(image: np.ndarray) -> Optional[tuple[int, int, int, int]]:
    alpha = image[:, :, 3]
    coords = np.argwhere(alpha > 0)
    if coords.size == 0:
        return None
    top, left = coords.min(axis=0)
    bottom, right = coords.max(axis=0)
    return int(left), int(top), int(right - left + 1), int(bottom - top + 1)


def _uv_to_texture_points(uvs: np.ndarray, texture: np.ndarray) -> np.ndarray:
    points = uvs.astype(np.float32).copy()
    if np.nanmax(np.abs(points)) <= 2.0:
        points[:, 0] *= float(texture.shape[1])
        points[:, 1] = (1.0 - points[:, 1]) * float(texture.shape[0])
    return points


def _iter_triangles(indices: list[int]) -> Iterable[tuple[int, int, int]]:
    for i in range(0, len(indices) - 2, 3):
        yield indices[i], indices[i + 1], indices[i + 2]


def _as_points(value: Any) -> Optional[list[list[float]]]:
    if not isinstance(value, list) or not value:
        return None
    if all(isinstance(item, (int, float)) for item in value):
        if len(value) % 2 != 0:
            return None
        return [[float(value[i]), float(value[i + 1])] for i in range(0, len(value), 2)]

    points = []
    for item in value:
        if isinstance(item, dict):
            points.append([float(item.get("x", 0)), float(item.get("y", 0))])
        elif isinstance(item, list | tuple) and len(item) >= 2:
            points.append([float(item[0]), float(item[1])])
        else:
            return None
    return points


def _as_int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    return [int(item) for item in value if isinstance(item, (int, float))]


def _load_rgba_texture(cv2, path: Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise PsdReconstructionError(f"Failed to read texture: {path}")
    if image.ndim == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2RGBA)
    elif image.shape[2] == 3:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGBA)
    elif image.shape[2] == 4:
        image = cv2.cvtColor(image, cv2.COLOR_BGRA2RGBA)
    else:
        raise PsdReconstructionError(f"Unsupported texture format: {path}")
    return image


def _save_psd(
    path: Path,
    size: tuple[int, int],
    layers: list[PsdLayer],
    raw_layers: bool = False,
) -> None:
    if raw_layers:
        _save_fast_rgba_psd(path, size, layers)
        return

    from psd_tools import PSDImage
    from psd_tools.api.layers import PixelLayer
    from psd_tools.constants import Compression

    psd = PSDImage.new("RGBA", size)
    compression = Compression.RAW if raw_layers else Compression.RLE
    for layer in layers:
        psd.append(
            PixelLayer.frompil(
                layer.image,
                psd,
                name=layer.name,
                top=layer.top,
                left=layer.left,
                compression=compression,
            )
        )
    psd.save(str(path))


def _save_fast_rgba_psd(path: Path, size: tuple[int, int], layers: list[PsdLayer]) -> None:
    width, height = size
    if width <= 0 or height <= 0:
        raise PsdReconstructionError(f"Invalid PSD canvas size: {size}")

    visible_layers = [layer for layer in layers if layer.image.width and layer.image.height]
    if len(visible_layers) > 32767:
        raise PsdReconstructionError("PSD layer count exceeds the PSD format limit.")

    records = []
    channel_data_lengths = []
    for layer in reversed(visible_layers):
        layer_width, layer_height = layer.image.size
        top = int(layer.top)
        left = int(layer.left)
        bottom = top + layer_height
        right = left + layer_width
        channel_length = 2 + layer_width * layer_height
        channel_data_lengths.append((layer, channel_length))
        record = [
            struct.pack(">iiii", top, left, bottom, right),
            struct.pack(">H", 4),
        ]
        for channel_id in (0, 1, 2, -1):
            record.append(struct.pack(">hI", channel_id, channel_length))
        record.extend(
            [
                b"8BIM",
                b"norm",
                struct.pack(">BBBB", 255, 0, 8, 0),
            ]
        )
        extra = b"".join(
            [
                struct.pack(">I", 0),
                struct.pack(">I", 0),
                _pack_pascal_string(layer.name),
            ]
        )
        record.extend([struct.pack(">I", len(extra)), extra])
        records.append(b"".join(record))

    layer_records = b"".join(records)
    layer_pixels_length = sum(channel_length * 4 for _, channel_length in channel_data_lengths)
    layer_info_data_length = 2 + len(layer_records) + layer_pixels_length
    layer_info_padding = layer_info_data_length % 2
    layer_info_section_length = layer_info_data_length + layer_info_padding
    layer_and_mask_length = 4 + layer_info_section_length + 4

    with path.open("wb") as f:
        f.write(b"8BPS")
        f.write(struct.pack(">H", 1))
        f.write(b"\0" * 6)
        f.write(struct.pack(">HIIHH", 4, height, width, 8, 3))
        f.write(struct.pack(">I", 0))
        f.write(struct.pack(">I", 0))
        f.write(struct.pack(">I", layer_and_mask_length))
        f.write(struct.pack(">I", layer_info_section_length))
        f.write(struct.pack(">h", len(visible_layers)))
        f.write(layer_records)

        for layer, _ in channel_data_lengths:
            rgba = np.asarray(layer.image.convert("RGBA"))
            for channel in (0, 1, 2, 3):
                f.write(struct.pack(">H", 0))
                f.write(np.ascontiguousarray(rgba[:, :, channel]).tobytes())

        if layer_info_padding:
            f.write(b"\0")
        f.write(struct.pack(">I", 0))

        flat = Image.new("RGBA", size, (0, 0, 0, 0))
        for layer in visible_layers:
            _alpha_composite_at(flat, layer.image.convert("RGBA"), layer.left, layer.top)
        flat_rgba = np.asarray(flat)
        f.write(struct.pack(">H", 0))
        for channel in (0, 1, 2, 3):
            f.write(np.ascontiguousarray(flat_rgba[:, :, channel]).tobytes())


def _pack_pascal_string(name: str) -> bytes:
    raw = name.encode("macroman", errors="replace")[:255]
    data = bytes([len(raw)]) + raw
    padding = (-len(data)) % 4
    return data + (b"\0" * padding)


def _build_export_metadata(
    source: Live2DSource,
    textures: list[np.ndarray],
    canvas_size: tuple[int, int],
    mode: str,
    layers: list[dict[str, Any]],
) -> dict[str, Any]:
    texture_entries = []
    for index, (path, texture) in enumerate(zip(source.textures, textures)):
        h, w = texture.shape[:2]
        try:
            relative_path = path.resolve().relative_to(source.root_dir).as_posix()
        except ValueError:
            relative_path = path.name
        texture_entries.append(
            {
                "index": index,
                "name": path.name,
                "relative_path": relative_path,
                "width": int(w),
                "height": int(h),
            }
        )

    return {
        "format": METADATA_FORMAT,
        "version": METADATA_VERSION,
        "mode": mode,
        "source_model": str(source.model_json),
        "canvas": {"width": int(canvas_size[0]), "height": int(canvas_size[1])},
        "textures": texture_entries,
        "layers": layers,
    }


def _write_json(path: Path, data: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _default_metadata_path(psd_path: Path) -> Path:
    metadata_path = psd_path.with_suffix(".lpkpsd.json")
    if metadata_path.is_file():
        return metadata_path
    raise PsdReconstructionError(
        f"Metadata file not found: {metadata_path}. Repack needs the .lpkpsd.json exported with the PSD."
    )


def _metadata_textures(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    textures = metadata.get("textures")
    if not isinstance(textures, list) or not textures:
        raise PsdReconstructionError("PSD metadata does not contain texture entries.")

    normalized = []
    for item in textures:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "index": int(item["index"]),
                "name": _safe_output_name(str(item["name"])),
                "width": int(item["width"]),
                "height": int(item["height"]),
            }
        )
    if not normalized:
        raise PsdReconstructionError("PSD metadata texture entries are invalid.")
    return normalized


def _metadata_layers(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    layers = metadata.get("layers")
    if not isinstance(layers, list) or not layers:
        raise PsdReconstructionError("PSD metadata does not contain layer entries.")

    normalized = []
    for item in layers:
        if not isinstance(item, dict) or item.get("kind") not in {"atlas-component", "texture-atlas"}:
            continue
        if "name" not in item or "texture_index" not in item:
            continue
        normalized.append(item)
    if not normalized:
        raise PsdReconstructionError("PSD metadata does not contain repackable atlas layers.")
    return normalized


def _index_psd_layers(psd) -> dict[str, Any]:
    layers: dict[str, Any] = {}
    for layer in psd.descendants():
        if not getattr(layer, "is_group", lambda: False)():
            layers.setdefault(str(layer.name), layer)
    return layers


def _alpha_composite_at(canvas: Image.Image, image: Image.Image, left: int, top: int) -> None:
    canvas_w, canvas_h = canvas.size
    src_w, src_h = image.size
    dst_left = max(0, left)
    dst_top = max(0, top)
    dst_right = min(canvas_w, left + src_w)
    dst_bottom = min(canvas_h, top + src_h)
    if dst_right <= dst_left or dst_bottom <= dst_top:
        return

    src_left = dst_left - left
    src_top = dst_top - top
    src_right = src_left + (dst_right - dst_left)
    src_bottom = src_top + (dst_bottom - dst_top)
    canvas.alpha_composite(
        image.crop((src_left, src_top, src_right, src_bottom)),
        dest=(dst_left, dst_top),
    )


def _safe_output_name(name: str) -> str:
    safe = Path(name).name
    return safe or "texture.png"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as exc:
        raise PsdReconstructionError(f"Failed to read JSON {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise PsdReconstructionError(f"JSON root must be an object: {path}")
    return data


def _require_cv2():
    try:
        import cv2
    except Exception as exc:
        raise MissingDependencyError(
            "OpenCV is required for PSD reconstruction. Run `pixi install` first."
        ) from exc
    return cv2


def _require_psd_tools() -> None:
    try:
        import psd_tools  # noqa: F401
    except Exception as exc:
        raise MissingDependencyError(
            "psd-tools is required for PSD output. Run `pixi install` first."
        ) from exc


def _clean_model_name(model_json: Path) -> str:
    name = model_json.name
    if name.endswith(".model3.json"):
        return name[: -len(".model3.json")]
    return model_json.stem


def _safe_layer_name(name: str) -> str:
    clean = re.sub(r"[\\/:*?\"<>|]+", "_", str(name)).strip()
    return clean[:255] or "Layer"


def _emit(progress: Optional[ProgressCallback], value: int, message: str) -> None:
    if progress:
        progress(value, message)
