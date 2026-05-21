import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import numpy as np
from PIL import Image


ProgressCallback = Callable[[int, str], None]


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


def reconstruct_live2d_psd(
    source: str | Path,
    output_dir: str | Path,
    progress: Optional[ProgressCallback] = None,
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

    if source_info.mesh_data:
        layers, size = _render_mesh_layers(cv2, source_info.mesh_data, textures, progress)
        mode = "mesh"
    else:
        warnings.append(
            "No drawable mesh metadata was found. Exported texture atlas layers only."
        )
        layers, size = _build_texture_atlas_layers(source_info.textures, textures)
        mode = "texture-atlas"

    model_name = _clean_model_name(source_info.model_json)
    psd_path = output_path / f"{model_name}_reconstructed.psd"
    _save_psd(psd_path, size, layers)

    _emit(progress, 100, f"PSD written: {psd_path}")
    return ReconstructionResult(
        psd_path=psd_path,
        layer_count=len(layers),
        mode=mode,
        warnings=warnings,
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
) -> tuple[list[tuple[str, Image.Image]], tuple[int, int]]:
    drawables = mesh_data.get("drawables")
    if not isinstance(drawables, list) or not drawables:
        raise PsdReconstructionError("Drawable mesh metadata is empty or invalid.")

    drawable_infos = [_normalize_drawable(item) for item in drawables if isinstance(item, dict)]
    drawable_infos = [item for item in drawable_infos if item]
    if not drawable_infos:
        raise PsdReconstructionError("No usable drawable mesh entries were found.")

    size, offset = _resolve_canvas(mesh_data, drawable_infos)
    canvas_w, canvas_h = size
    ordered = sorted(
        drawable_infos,
        key=lambda item: (item.get("render_order", item.get("draw_order", 0)), item["id"]),
    )

    layers: list[tuple[str, Image.Image]] = []
    total = max(1, len(ordered))
    for index, drawable in enumerate(ordered):
        texture_index = int(drawable.get("texture_index", 0))
        if texture_index < 0 or texture_index >= len(textures):
            continue

        texture = textures[texture_index]
        vertices = np.asarray(drawable["vertices"], dtype=np.float32) + offset
        uvs = _uv_to_texture_points(np.asarray(drawable["uvs"], dtype=np.float32), texture)
        indices = drawable["indices"]
        layer = np.zeros((canvas_h, canvas_w, 4), dtype=np.uint8)

        for tri in _iter_triangles(indices):
            if max(tri) >= len(vertices) or max(tri) >= len(uvs):
                continue
            _warp_triangle(cv2, texture, layer, uvs[list(tri)], vertices[list(tri)])

        opacity = float(drawable.get("opacity", 1.0))
        if opacity < 1.0:
            layer[:, :, 3] = np.clip(layer[:, :, 3].astype(np.float32) * opacity, 0, 255).astype(
                np.uint8
            )

        image = Image.fromarray(layer, "RGBA")
        layers.append((_safe_layer_name(drawable["id"]), image))
        _emit(progress, 20 + int((index + 1) / total * 70), f"Rendered {drawable['id']}")

    if not layers:
        raise PsdReconstructionError("No drawable layers were rendered.")
    return layers, size


def _build_texture_atlas_layers(
    paths: list[Path],
    textures: list[np.ndarray],
) -> tuple[list[tuple[str, Image.Image]], tuple[int, int]]:
    width = max(texture.shape[1] for texture in textures)
    height = max(texture.shape[0] for texture in textures)
    layers: list[tuple[str, Image.Image]] = []

    for path, texture in zip(paths, textures):
        layer = np.zeros((height, width, 4), dtype=np.uint8)
        h, w = texture.shape[:2]
        layer[:h, :w] = texture
        layers.append((_safe_layer_name(path.stem), Image.fromarray(layer, "RGBA")))

    return layers, (width, height)


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
        "render_order": int(item.get("render_order", item.get("draw_order", 0))),
        "draw_order": int(item.get("draw_order", item.get("render_order", 0))),
    }


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
        flags=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0, 0),
    )

    mask = np.zeros((dy1 - dy0, dx1 - dx0), dtype=np.uint8)
    cv2.fillConvexPoly(mask, dst_shift.astype(np.int32), 255, lineType=cv2.LINE_AA)
    _alpha_blend(dst[dy0:dy1, dx0:dx1], warped, mask)


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


def _uv_to_texture_points(uvs: np.ndarray, texture: np.ndarray) -> np.ndarray:
    points = uvs.astype(np.float32).copy()
    if np.nanmax(np.abs(points)) <= 2.0:
        points[:, 0] *= float(texture.shape[1])
        points[:, 1] *= float(texture.shape[0])
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


def _save_psd(path: Path, size: tuple[int, int], layers: list[tuple[str, Image.Image]]) -> None:
    from psd_tools import PSDImage
    from psd_tools.api.layers import PixelLayer

    psd = PSDImage.new("RGBA", size)
    for name, image in layers:
        psd.append(PixelLayer.frompil(image, psd, name=name))
    psd.save(str(path))


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
