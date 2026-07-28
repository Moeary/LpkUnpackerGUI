import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

import numpy as np
from PIL import Image


ProgressCallback = Callable[[int, str], None]
METADATA_FORMAT = "LpkUnpacker.Live2DAtlasPSD"
METADATA_VERSION = 1


class PsdReconstructionError(RuntimeError):
    pass


class MissingDependencyError(PsdReconstructionError):
    pass


@dataclass(frozen=True)
class PsdResourceLimits:
    """Conservative per-job limits used before allocating image buffers.

    PSD export can retain the source textures, rendered layers and psd-tools
    channel data at the same time.  These limits intentionally leave a large
    margin for the rest of the desktop instead of trying to consume all RAM.
    """

    max_cpu_threads: int = 2
    max_memory_mb: int = 8192
    max_texture_pixels: int = 64 * 1024 * 1024
    max_canvas_pixels: int = 48 * 1024 * 1024
    max_total_layer_pixels: int = 192 * 1024 * 1024
    max_layer_count: int = 512
    mesh_max_dimension: int = 2048

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None = None) -> "PsdResourceLimits":
        values = values or {}
        defaults = cls()

        def read(name: str, default: int, minimum: int) -> int:
            try:
                return max(minimum, int(values.get(name, default)))
            except (TypeError, ValueError):
                return default

        return cls(
            max_cpu_threads=read("max_cpu_threads", defaults.max_cpu_threads, 1),
            max_memory_mb=read("max_memory_mb", defaults.max_memory_mb, 512),
            max_texture_pixels=read("max_texture_pixels", defaults.max_texture_pixels, 1),
            max_canvas_pixels=read("max_canvas_pixels", defaults.max_canvas_pixels, 1),
            max_total_layer_pixels=read(
                "max_total_layer_pixels", defaults.max_total_layer_pixels, 1
            ),
            max_layer_count=read("max_layer_count", defaults.max_layer_count, 1),
            mesh_max_dimension=read("mesh_max_dimension", defaults.mesh_max_dimension, 0),
        )


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
    # Maps the source texture index to the latest written PNG.  Multi-PSD
    # repack uses this as the next PSD's baseline, so untouched pixels survive
    # every pass instead of being recreated from a transparent canvas.
    texture_outputs: dict[int, Path] = field(default_factory=dict)

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
    group: Optional[str] = None


def _resolve_resource_limits(
    resource_limits: PsdResourceLimits | Mapping[str, Any] | None,
) -> PsdResourceLimits:
    if isinstance(resource_limits, PsdResourceLimits):
        return resource_limits
    return PsdResourceLimits.from_mapping(resource_limits)


def _configure_opencv(cv2, limits: PsdResourceLimits) -> None:
    """Keep OpenCV from taking every logical CPU during a PSD job."""
    try:
        cv2.setNumThreads(limits.max_cpu_threads)
    except Exception:
        # Thread limiting is a safety improvement, not a requirement for export.
        pass


def _scaled_mesh_canvas(
    source_size: tuple[int, int], limits: PsdResourceLimits
) -> tuple[tuple[int, int], float]:
    """Scale a display-space mesh PSD while preserving its aspect ratio."""
    width, height = (int(source_size[0]), int(source_size[1]))
    _pixel_count(width, height, "Mesh canvas")
    maximum = int(limits.mesh_max_dimension)
    if maximum <= 0 or max(width, height) <= maximum:
        return (width, height), 1.0
    scale = maximum / float(max(width, height))
    return (max(1, int(round(width * scale))), max(1, int(round(height * scale)))), scale


def _metadata_coordinate_scale(layer_info: Mapping[str, Any]) -> float:
    try:
        scale = float(layer_info.get("coordinate_scale", 1.0))
    except (TypeError, ValueError):
        return 1.0
    return scale if math.isfinite(scale) and scale > 0 else 1.0


def _pixel_count(width: int, height: int, label: str) -> int:
    if width <= 0 or height <= 0:
        raise PsdReconstructionError(f"{label} has an invalid size: {width}x{height}.")
    return width * height


def _format_mib(byte_count: int) -> str:
    return f"{byte_count / (1024 * 1024):.0f} MiB"


def _validate_canvas_size(
    size: tuple[int, int], limits: PsdResourceLimits, operation: str
) -> int:
    width, height = (int(size[0]), int(size[1]))
    pixels = _pixel_count(width, height, f"{operation} canvas")
    if pixels > limits.max_canvas_pixels:
        raise PsdReconstructionError(
            f"{operation} blocked by the safety limit: canvas {width}x{height} "
            f"({pixels:,} pixels) exceeds the configured maximum "
            f"({limits.max_canvas_pixels:,} pixels)."
        )
    return pixels


def _validate_texture_paths(
    paths: list[Path], limits: PsdResourceLimits, operation: str
) -> int:
    total_pixels = 0
    for path in paths:
        try:
            with Image.open(path) as image:
                pixels = _pixel_count(int(image.width), int(image.height), f"Texture {path.name}")
        except PsdReconstructionError:
            raise
        except Exception as exc:
            raise PsdReconstructionError(f"Failed to inspect texture {path}: {exc}") from exc
        total_pixels += pixels
        if total_pixels > limits.max_texture_pixels:
            raise PsdReconstructionError(
                f"{operation} blocked by the safety limit: texture atlases total "
                f"{total_pixels:,} pixels, exceeding {limits.max_texture_pixels:,} pixels."
            )
    return total_pixels


def _validate_metadata_textures(
    textures: list[dict[str, Any]], limits: PsdResourceLimits, operation: str
) -> int:
    total_pixels = 0
    for texture in textures:
        width = int(texture["width"])
        height = int(texture["height"])
        _validate_canvas_size((width, height), limits, operation)
        total_pixels += _pixel_count(width, height, f"Texture {texture.get('name', '?')}")
        if total_pixels > limits.max_texture_pixels:
            raise PsdReconstructionError(
                f"{operation} blocked by the safety limit: target atlases total "
                f"{total_pixels:,} pixels, exceeding {limits.max_texture_pixels:,} pixels."
            )
    return total_pixels


def _layers_pixel_count(layers: Iterable[PsdLayer]) -> int:
    return sum(_pixel_count(layer.image.width, layer.image.height, f"Layer {layer.name}") for layer in layers)


def _metadata_layers_pixel_count(layers: Iterable[dict[str, Any]]) -> int:
    total_pixels = 0
    for layer in layers:
        bbox = layer.get("bbox")
        if not isinstance(bbox, list | tuple) or len(bbox) < 4:
            raise PsdReconstructionError(f"Layer metadata is missing a valid bounding box: {layer.get('name', '?')}")
        total_pixels += _pixel_count(int(bbox[2]), int(bbox[3]), f"Layer {layer.get('name', '?')}")
    return total_pixels


def _validate_layer_budget(
    layer_count: int,
    total_layer_pixels: int,
    texture_pixels: int,
    limits: PsdResourceLimits,
    operation: str,
) -> None:
    if layer_count > limits.max_layer_count:
        raise PsdReconstructionError(
            f"{operation} blocked by the safety limit: {layer_count:,} layers exceed the configured "
            f"maximum of {limits.max_layer_count:,}."
        )
    if total_layer_pixels > limits.max_total_layer_pixels:
        raise PsdReconstructionError(
            f"{operation} blocked by the safety limit: layers contain {total_layer_pixels:,} pixels, "
            f"exceeding {limits.max_total_layer_pixels:,} pixels."
        )

    # Export keeps source RGBA textures and several copies of each layer while
    # psd-tools builds channel data.  The multiplier is deliberately cautious.
    estimated_bytes = texture_pixels * 4 * 2 + total_layer_pixels * 4 * 5
    limit_bytes = limits.max_memory_mb * 1024 * 1024
    if estimated_bytes > limit_bytes:
        raise PsdReconstructionError(
            f"{operation} blocked by the safety limit: estimated peak memory "
            f"{_format_mib(estimated_bytes)} exceeds the configured budget "
            f"{limits.max_memory_mb:,} MiB."
        )


def _validate_metadata_layer_budget(
    layers: list[dict[str, Any]],
    texture_pixels: int,
    limits: PsdResourceLimits,
    operation: str,
) -> None:
    _validate_layer_budget(
        len(layers), _metadata_layers_pixel_count(layers), texture_pixels, limits, operation
    )


def _validate_psd_layers(
    psd_layers: Mapping[str, Any],
    texture_pixels: int,
    limits: PsdResourceLimits,
    operation: str,
) -> None:
    if len(psd_layers) > limits.max_layer_count:
        raise PsdReconstructionError(
            f"{operation} blocked by the safety limit: PSD contains {len(psd_layers):,} indexed layers, "
            f"exceeding {limits.max_layer_count:,}."
        )
    total_pixels = 0
    for name, layer in psd_layers.items():
        width = int(getattr(layer, "width", 0) or 0)
        height = int(getattr(layer, "height", 0) or 0)
        if width <= 0 or height <= 0:
            continue
        pixels = _pixel_count(width, height, f"PSD layer {name}")
        if pixels > limits.max_total_layer_pixels:
            raise PsdReconstructionError(
                f"{operation} blocked by the safety limit: PSD layer {name} contains "
                f"{pixels:,} pixels, exceeding the total layer budget."
            )
        total_pixels += pixels
    _validate_layer_budget(
        len(psd_layers), total_pixels, texture_pixels, limits, operation
    )


def reconstruct_live2d_psd(
    source: str | Path,
    output_dir: str | Path,
    progress: Optional[ProgressCallback] = None,
    mode: str = "mesh",
    parameter_values: dict[str, float] | None = None,
    pose_name: str | None = None,
    output_name: str | None = None,
    resource_limits: PsdResourceLimits | Mapping[str, Any] | None = None,
) -> ReconstructionResult:
    limits = _resolve_resource_limits(resource_limits)
    cv2 = _require_cv2()
    _configure_opencv(cv2, limits)
    _require_psd_tools()

    source_info = resolve_live2d_source(Path(source))
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    _emit(progress, 5, "Live2D source resolved")

    texture_pixels = _validate_texture_paths(source_info.textures, limits, "PSD export")
    textures = [_load_rgba_texture(cv2, path) for path in source_info.textures]
    if not textures:
        raise PsdReconstructionError("No texture atlas PNG files were found.")

    _emit(progress, 20, f"Loaded {len(textures)} texture atlas file(s)")

    if mode == "mesh" and (parameter_values or not source_info.mesh_data):
        try:
            from app.core.cubism_core import CubismCoreError, export_drawables_sidecar

            _emit(progress, 22, "No mesh sidecar found. Trying Cubism Core drawable export.")
            sidecar_path = export_drawables_sidecar(
                source_info.model_json,
                output_path,
                progress=lambda value, message: _emit(progress, min(70, 22 + value // 3), message),
                parameter_values=parameter_values,
                pose_name=pose_name,
            )
            source_info.mesh_data = _read_json(sidecar_path)
            warnings.append(f"Drawable mesh metadata was exported with Cubism Core: {sidecar_path}")
        except CubismCoreError as exc:
            warnings.append(f"Cubism Core drawable export unavailable: {exc}")

    if mode == "mesh" and source_info.mesh_data:
        layers, size, layer_metadata = _render_mesh_layers(
            cv2, source_info.mesh_data, textures, progress, limits, texture_pixels
        )
        mode = "mesh"
    else:
        if mode == "mesh":
            warnings.append(
                "No drawable mesh metadata was found. Exported editable atlas layers instead."
            )
        layers, size, layer_metadata = _build_texture_component_layers(
            cv2, source_info.textures, textures, progress, limits, texture_pixels
        )
        mode = "atlas-components"

    model_name = _safe_output_name(output_name or _clean_model_name(source_info.model_json))
    suffix = "mesh_pose" if mode == "mesh" else "editable_atlas"
    psd_path = output_path / f"{model_name}_{suffix}.psd"
    metadata_path = output_path / f"{model_name}_{suffix}.lpkpsd.json"
    _emit(progress, 90, f"Preparing PSD with {len(layers)} layer(s)")
    _validate_canvas_size(size, limits, "PSD export")
    _validate_layer_budget(
        len(layers),
        _layers_pixel_count(layers),
        texture_pixels,
        limits,
        "PSD export",
    )
    _save_psd(psd_path, size, layers, progress=progress, resource_limits=limits)
    _emit(progress, 98, "Writing metadata")
    _write_json(
        metadata_path,
        _build_export_metadata(
            source_info,
            textures,
            size,
            mode,
            layer_metadata,
            pose_name=pose_name,
            parameter_values=parameter_values,
        ),
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
    resource_limits: PsdResourceLimits | Mapping[str, Any] | None = None,
    input_texture_paths: Mapping[int, str | Path] | None = None,
) -> ReconstructionResult:
    limits = _resolve_resource_limits(resource_limits)
    _require_psd_tools()
    from psd_tools import PSDImage

    psd_file = Path(psd_path).resolve()
    if not psd_file.is_file():
        raise PsdReconstructionError(f"PSD file does not exist: {psd_file}")

    metadata_file = Path(metadata_path).resolve() if metadata_path else _default_metadata_path(psd_file)
    metadata = _read_json(metadata_file)
    if metadata.get("format") != METADATA_FORMAT:
        raise PsdReconstructionError(f"Unsupported PSD metadata file: {metadata_file}")
    mode = str(metadata.get("mode") or "")
    if mode not in {"atlas-components", "mesh"}:
        raise PsdReconstructionError(
            "Only mesh or editable atlas PSD metadata can be repacked into atlas PNG files."
        )

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    textures = _metadata_textures(metadata)
    layers_metadata = _metadata_layers(
        metadata,
        {"drawable-mesh"} if mode == "mesh" else {"atlas-component", "texture-atlas"},
    )
    texture_pixels = _validate_metadata_textures(textures, limits, "PSD repack")
    _validate_metadata_layer_budget(layers_metadata, texture_pixels, limits, "PSD repack")

    _emit(progress, 5, f"Reading PSD: {psd_file}")
    psd = PSDImage.open(str(psd_file))
    _validate_canvas_size((int(psd.width), int(psd.height)), limits, "PSD repack")
    psd_layers = _index_psd_layers(psd)
    _validate_psd_layers(psd_layers, texture_pixels, limits, "PSD repack")
    _emit(progress, 10, f"Loaded PSD: {psd_file}")

    if mode == "mesh":
        return _repack_mesh_psd_layers(
            psd_layers,
            textures,
            layers_metadata,
            output_path,
            progress,
            metadata_file,
            limits,
            texture_pixels,
            input_texture_paths,
        )

    canvases, warnings = _load_atlas_repack_canvases(textures, input_texture_paths)
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
    _emit(progress, 95, "Writing atlas PNG")
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
        texture_outputs={int(texture["index"]): output for texture, output in zip(textures, outputs)},
    )


def _repack_mesh_psd_layers(
    psd_layers: dict[str, Any],
    textures: list[dict[str, Any]],
    layers_metadata: list[dict[str, Any]],
    output_path: Path,
    progress: Optional[ProgressCallback],
    metadata_file: Path,
    limits: PsdResourceLimits,
    texture_pixels: int,
    input_texture_paths: Mapping[int, str | Path] | None = None,
) -> ReconstructionResult:
    cv2 = _require_cv2()
    _configure_opencv(cv2, limits)
    _validate_layer_budget(
        len(layers_metadata),
        _metadata_layers_pixel_count(layers_metadata),
        texture_pixels,
        limits,
        "PSD mesh repack",
    )
    canvases, warnings = _load_mesh_repack_canvases(textures, input_texture_paths)
    original_canvases = {index: canvas.copy() for index, canvas in canvases.items()}
    total = max(1, len(layers_metadata))

    for index, layer_info in enumerate(layers_metadata):
        texture_index = int(layer_info["texture_index"])
        canvas = canvases.get(texture_index)
        original_canvas = original_canvases.get(texture_index)
        if canvas is None or original_canvas is None:
            continue

        layer = psd_layers.get(str(layer_info["name"]))
        if layer is None:
            warnings.append(f"Layer missing in PSD: {layer_info['name']}")
            continue

        image = layer.composite(force=True)
        if image is None:
            warnings.append(f"Layer has no pixel content: {layer_info['name']}")
            continue

        try:
            coordinate_scale = _metadata_coordinate_scale(layer_info)
            vertices = np.asarray(layer_info["vertices"], dtype=np.float32) * coordinate_scale
            uvs = np.asarray(layer_info["uvs"], dtype=np.float32)
            indices = _as_int_list(layer_info["indices"])
        except Exception:
            warnings.append(f"Layer metadata is invalid: {layer_info['name']}")
            continue

        if len(vertices) != len(uvs) or not indices:
            warnings.append(f"Layer metadata has mismatched mesh data: {layer_info['name']}")
            continue

        left = int(getattr(layer, "left", layer_info.get("left", 0)))
        top = int(getattr(layer, "top", layer_info.get("top", 0)))
        source = np.asarray(image.convert("RGBA"))
        local_vertices = vertices - np.asarray([left, top], dtype=np.float32)
        texture_points = _uv_to_texture_points(uvs, canvas)
        original_layer = _render_repack_reference_layer(
            cv2,
            original_canvas,
            source.shape,
            local_vertices,
            texture_points,
            indices,
            float(layer_info.get("opacity", 1.0)),
        )
        edit_mask = _changed_pixel_mask(cv2, source, original_layer)
        if not np.any(edit_mask):
            _emit(progress, 10 + int((index + 1) / total * 80), f"Packed {layer_info['name']}")
            continue

        for tri in _iter_triangles(indices):
            if max(tri) >= len(local_vertices) or max(tri) >= len(texture_points):
                continue
            _replace_triangle_masked(
                cv2,
                source,
                edit_mask,
                canvas,
                local_vertices[list(tri)],
                texture_points[list(tri)],
            )

        _emit(progress, 10 + int((index + 1) / total * 80), f"Packed {layer_info['name']}")

    outputs: list[Path] = []
    _emit(progress, 95, "Writing atlas PNG")
    for texture in textures:
        canvas = canvases[texture["index"]]
        output_file = output_path / texture["name"]
        Image.fromarray(canvas, "RGBA").save(output_file)
        outputs.append(output_file)

    _emit(progress, 100, f"Atlas PNG written: {output_path}")
    return ReconstructionResult(
        psd_path=outputs[0] if outputs else output_path,
        layer_count=len(layers_metadata),
        mode="mesh-repack",
        warnings=warnings,
        metadata_path=metadata_file,
        output_paths=outputs,
        texture_outputs={int(texture["index"]): output for texture, output in zip(textures, outputs)},
    )


def _load_mesh_repack_canvases(
    textures: list[dict[str, Any]],
    input_texture_paths: Mapping[int, str | Path] | None = None,
) -> tuple[dict[int, np.ndarray], list[str]]:
    canvases: dict[int, np.ndarray] = {}
    warnings: list[str] = []
    for texture in textures:
        width = int(texture["width"])
        height = int(texture["height"])
        source_path = _resolve_repack_texture_path(texture, input_texture_paths)
        if source_path.is_file():
            with Image.open(source_path) as source_image:
                image = source_image.convert("RGBA")
            if image.size != (width, height):
                warnings.append(
                    f"Texture size changed, resizing original atlas for repack: {source_path}"
                )
                image = image.resize((width, height), Image.Resampling.LANCZOS)
        else:
            warnings.append(
                f"Original texture missing; unchanged hidden pixels cannot be preserved: {source_path}"
            )
            image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        canvases[int(texture["index"])] = np.asarray(image).copy()
    return canvases, warnings


def _load_atlas_repack_canvases(
    textures: list[dict[str, Any]],
    input_texture_paths: Mapping[int, str | Path] | None = None,
) -> tuple[dict[int, Image.Image], list[str]]:
    """Load the original atlas as the base for component PSD repack too.

    Component exports do not necessarily cover every pixel in an atlas.  A
    transparent new canvas made an untouched region look like an edit on the
    next pass, which is especially dangerous for alpha-heavy hair textures.
    """
    canvases: dict[int, Image.Image] = {}
    warnings: list[str] = []
    for texture in textures:
        width = int(texture["width"])
        height = int(texture["height"])
        source_path = _resolve_repack_texture_path(texture, input_texture_paths)
        if source_path.is_file():
            with Image.open(source_path) as source_image:
                image = source_image.convert("RGBA")
            if image.size != (width, height):
                warnings.append(
                    f"Texture size changed, resizing original atlas for repack: {source_path}"
                )
                image = image.resize((width, height), Image.Resampling.LANCZOS)
        else:
            warnings.append(
                f"Original texture missing; unchanged hidden pixels cannot be preserved: {source_path}"
            )
            image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        canvases[int(texture["index"])] = image
    return canvases, warnings


def _resolve_repack_texture_path(
    texture: Mapping[str, Any],
    input_texture_paths: Mapping[int, str | Path] | None,
) -> Path:
    texture_index = int(texture["index"])
    candidate = (input_texture_paths or {}).get(texture_index)
    return Path(str(candidate or texture.get("source_path") or ""))


def repack_multiple_psds(
    psd_paths: Iterable[str | Path],
    output_dir: str | Path,
    progress: Optional[ProgressCallback] = None,
    resource_limits: PsdResourceLimits | Mapping[str, Any] | None = None,
) -> ReconstructionResult:
    """Repack PSDs from low to high priority into one atlas set.

    The user-facing list is ordered high to low, so it is processed in reverse:
    later (higher priority) PSDs receive the preceding output as their base.
    """
    ordered_paths = [Path(path).resolve() for path in psd_paths]
    if len(ordered_paths) < 2:
        raise PsdReconstructionError("Multi-PSD repack requires at least two PSD files.")

    latest: ReconstructionResult | None = None
    texture_paths: dict[int, Path] | None = None
    total = len(ordered_paths)
    warnings: list[str] = []
    for step, psd_file in enumerate(reversed(ordered_paths)):
        start = int(step / total * 95)
        span = max(1, int(95 / total))
        result = repack_atlas_png_from_psd(
            psd_file,
            output_dir,
            progress=lambda value, message, start=start, span=span, step=step: _emit(
                progress,
                min(95, start + int(value / 100 * span)),
                f"[{step + 1}/{total}] {message}",
            ),
            resource_limits=resource_limits,
            input_texture_paths=texture_paths,
        )
        warnings.extend(result.warnings)
        texture_paths = result.texture_outputs
        latest = result

    if latest is None:
        raise PsdReconstructionError("No PSD was available for multi-PSD repack.")
    _emit(progress, 100, f"Atlas PNG written: {Path(output_dir)}")
    return ReconstructionResult(
        psd_path=latest.psd_path,
        layer_count=sum(1 for _ in ordered_paths),
        mode="multi-repack",
        warnings=warnings,
        metadata_path=latest.metadata_path,
        output_paths=latest.output_paths,
        texture_outputs=latest.texture_outputs,
    )


def _render_repack_reference_layer(
    cv2,
    texture: np.ndarray,
    shape: tuple[int, int, int],
    local_vertices: np.ndarray,
    texture_points: np.ndarray,
    indices: list[int],
    opacity: float,
) -> np.ndarray:
    layer = np.zeros(shape, dtype=np.uint8)
    for tri in _iter_triangles(indices):
        if max(tri) >= len(local_vertices) or max(tri) >= len(texture_points):
            continue
        _warp_triangle(
            cv2,
            texture,
            layer,
            texture_points[list(tri)],
            local_vertices[list(tri)],
        )
    if opacity < 1.0:
        layer[:, :, 3] = np.clip(layer[:, :, 3].astype(np.float32) * opacity, 0, 255).astype(
            np.uint8
        )
    return layer


def _changed_pixel_mask(cv2, edited: np.ndarray, reference: np.ndarray) -> np.ndarray:
    if edited.shape != reference.shape:
        reference = cv2.resize(
            reference,
            (edited.shape[1], edited.shape[0]),
            interpolation=cv2.INTER_LINEAR,
        )

    delta = np.abs(edited.astype(np.int16) - reference.astype(np.int16))
    rgb_delta = np.max(delta[:, :, :3], axis=2)
    # Mesh exports apply Live2D drawable masks.  Those masks make parts of an
    # otherwise opaque reconstructed reference transparent, even when the
    # artist did not touch the PSD.  Treating alpha-only differences as edits
    # therefore erased hair and other masked drawables on writeback.  Only a
    # visible RGB change in pixels that are visible in both images is safe to
    # project back onto the original atlas.
    mutually_visible = (edited[:, :, 3] > 8) & (reference[:, :, 3] > 8)
    changed = (rgb_delta > 12) & mutually_visible
    mask = (changed.astype(np.uint8) * 255)
    if not np.any(mask):
        return mask

    kernel = np.ones((3, 3), dtype=np.uint8)
    return cv2.dilate(mask, kernel, iterations=1)


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
    limits: PsdResourceLimits,
    texture_pixels: int,
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

    source_size, offset = _resolve_canvas(mesh_data, drawable_infos)
    size, coordinate_scale = _scaled_mesh_canvas(source_size, limits)
    canvas_w, canvas_h = size
    _validate_canvas_size(size, limits, "PSD export")

    # Validate all potential output bounds before creating any drawable-sized
    # RGBA buffer.  A malformed sidecar can otherwise request a huge canvas.
    planned_layer_count = 0
    planned_layer_pixels = 0
    for drawable in drawable_infos:
        texture_index = int(drawable.get("texture_index", 0))
        if (
            not drawable.get("visible", True)
            or float(drawable.get("opacity", 1.0)) <= 0.001
            or texture_index < 0
            or texture_index >= len(textures)
        ):
            continue
        vertices = (np.asarray(drawable["vertices"], dtype=np.float32) + offset) * coordinate_scale
        bounds = _vertex_bounds(vertices, canvas_w, canvas_h)
        if bounds is None:
            continue
        planned_layer_count += 1
        planned_layer_pixels += bounds[2] * bounds[3]
    _validate_layer_budget(
        planned_layer_count,
        planned_layer_pixels,
        texture_pixels,
        limits,
        "PSD export",
    )

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
        vertices = (np.asarray(drawable["vertices"], dtype=np.float32) + offset) * coordinate_scale
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
            coordinate_scale,
        )

        bbox = _alpha_bbox(layer)
        if bbox is None:
            continue
        crop_left, crop_top, width, height = bbox
        left = layer_left + crop_left
        top = layer_top + crop_top

        layer_name = _safe_layer_name(drawable["id"])
        group_name = _drawable_group_name(drawable)
        image = Image.fromarray(
            layer[crop_top : crop_top + height, crop_left : crop_left + width],
            "RGBA",
        )
        layers.append(PsdLayer(layer_name, image, left, top, group_name))
        layer_metadata.append(
            {
                "kind": "drawable-mesh",
                "name": layer_name,
                "group": group_name,
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
                "coordinate_scale": coordinate_scale,
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
    limits: PsdResourceLimits | None = None,
    texture_pixels: int | None = None,
) -> tuple[list[PsdLayer], tuple[int, int], list[dict[str, Any]]]:
    limits = limits or PsdResourceLimits()
    texture_pixels = texture_pixels if texture_pixels is not None else sum(
        texture.shape[0] * texture.shape[1] for texture in textures
    )
    width = max(texture.shape[1] for texture in textures)
    height = max(texture.shape[0] for texture in textures)
    _validate_canvas_size((width, height), limits, "PSD export")
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
        planned_pixels = sum(piece[2] * piece[3] for piece in pieces)
        _validate_layer_budget(
            len(layers) + len(pieces),
            _layers_pixel_count(layers) + planned_pixels,
            texture_pixels,
            limits,
            "PSD export",
        )
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
        layers, size, layer_metadata = _build_texture_atlas_layers(
            paths, textures, limits, texture_pixels
        )
        return layers, size, layer_metadata

    _emit(progress, 85, f"Split texture atlases into {len(layers)} editable layer(s)")
    return layers, (width, height), layer_metadata


def _build_texture_atlas_layers(
    paths: list[Path],
    textures: list[np.ndarray],
    limits: PsdResourceLimits | None = None,
    texture_pixels: int | None = None,
) -> tuple[list[PsdLayer], tuple[int, int], list[dict[str, Any]]]:
    limits = limits or PsdResourceLimits()
    texture_pixels = texture_pixels if texture_pixels is not None else sum(
        texture.shape[0] * texture.shape[1] for texture in textures
    )
    width = max(texture.shape[1] for texture in textures)
    height = max(texture.shape[0] for texture in textures)
    _validate_canvas_size((width, height), limits, "PSD export")
    _validate_layer_budget(
        len(textures),
        len(textures) * width * height,
        texture_pixels,
        limits,
        "PSD export",
    )
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
        "parent_part_index": int(item.get("parent_part_index", -1)),
        "parent_part_id": item.get("parent_part_id"),
        "blend_mode": str(item.get("blend_mode", "normal")),
        "blend_mode_value": int(item.get("blend_mode_value", 0)),
        "render_order": int(item.get("render_order", item.get("draw_order", 0))),
        "draw_order": int(item.get("draw_order", item.get("render_order", 0))),
    }


def _drawable_visible(item: dict[str, Any]) -> bool:
    if "visible" in item:
        return bool(item["visible"])
    if "dynamic_flags" in item:
        return bool(int(item["dynamic_flags"]) & 1)
    return True


def _drawable_group_name(drawable: dict[str, Any]) -> str:
    group = _semantic_group_name(drawable)
    if group:
        return group

    part_id = drawable.get("parent_part_id")
    if part_id:
        return _part_group_name(str(part_id))
    return "Character / Unassigned"


def _semantic_group_name(drawable: dict[str, Any]) -> Optional[str]:
    text = " ".join(
        str(value)
        for value in (
            drawable.get("id", ""),
            drawable.get("parent_part_id", ""),
            drawable.get("blend_mode", ""),
        )
    ).lower()
    if re.search(r"\b(touch|hit|drag)\b|touch|hitarea", text):
        return "Hit Areas"
    if re.search(r"sky|bg|back|background|haikei|ground|floor|sand|beach", text):
        return "Background"
    if re.search(
        r"effect|fx|water|shui|soda|splash|ice|snow|smoke|fire|spark|light|glow|"
        r"particle|kirakira|bubble|wave|star|shine",
        text,
    ):
        return "Effects"

    blend_mode = str(drawable.get("blend_mode", "normal")).lower()
    opacity = float(drawable.get("opacity", 1.0))
    if blend_mode not in {"normal", "0"} or opacity < 0.95:
        return "Effects"
    return None


def _part_group_name(part_id: str) -> str:
    key = part_id.lower()
    if re.search(r"hair|kami|bang|tail|twin", key):
        return "Character / Hair"
    if re.search(r"eye|mabuta|mayu|brow|hitomi|pupil|face|head|mouth|nose", key):
        return "Character / Head"
    if re.search(r"arm|hand|ude|te|finger|yubi", key):
        return "Character / Arms"
    if re.search(r"leg|foot|ashi|knee", key):
        return "Character / Legs"
    if re.search(r"cloth|dress|skirt|shirt|body|mune|torso", key):
        return "Character / Body & Clothes"
    if re.search(r"accessory|acc|ribbon|hat|weapon|gun|sword|dao", key):
        return "Character / Accessories"
    clean = _safe_layer_name(part_id)
    return f"Character / {clean}"


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

    # Adjacent Cubism triangles often meet on fractional coordinates.  A
    # one-pixel overlap with a hard mask avoids transparent/black hairline
    # seams created by anti-aliased masks and zero-alpha border interpolation.
    center = np.mean(dst_shift, axis=0)
    expanded = center + (dst_shift - center) * 1.01
    mask = np.zeros((dy1 - dy0, dx1 - dx0), dtype=np.uint8)
    cv2.fillConvexPoly(
        mask,
        np.rint(expanded).astype(np.int32),
        255,
        lineType=cv2.LINE_8,
    )
    _alpha_blend(dst[dy0:dy1, dx0:dx1], warped, mask)


def _replace_triangle_masked(
    cv2,
    src: np.ndarray,
    src_mask: np.ndarray,
    dst: np.ndarray,
    src_tri: np.ndarray,
    dst_tri: np.ndarray,
) -> None:
    src_rect = cv2.boundingRect(src_tri.astype(np.float32))
    dst_rect = cv2.boundingRect(dst_tri.astype(np.float32))
    sx, sy, sw, sh = src_rect
    dx, dy, dw, dh = dst_rect

    if sw <= 0 or sh <= 0 or dw <= 0 or dh <= 0:
        return

    sx0, sy0 = max(0, sx), max(0, sy)
    sx1, sy1 = min(src.shape[1], sx + sw), min(src.shape[0], sy + sh)
    dx0, dy0 = max(0, dx - 1), max(0, dy - 1)
    dx1, dy1 = min(dst.shape[1], dx + dw + 1), min(dst.shape[0], dy + dh + 1)

    if sx1 <= sx0 or sy1 <= sy0 or dx1 <= dx0 or dy1 <= dy0:
        return

    src_crop = src[sy0:sy1, sx0:sx1]
    mask_crop = src_mask[sy0:sy1, sx0:sx1]
    src_shift = src_tri - np.asarray([sx0, sy0], dtype=np.float32)
    dst_shift = dst_tri - np.asarray([dx0, dy0], dtype=np.float32)
    matrix = cv2.getAffineTransform(src_shift.astype(np.float32), dst_shift.astype(np.float32))

    target_size = (dx1 - dx0, dy1 - dy0)
    warped = cv2.warpAffine(
        src_crop,
        matrix,
        target_size,
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )
    warped_mask = cv2.warpAffine(
        mask_crop,
        matrix,
        target_size,
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )

    tri_mask = np.zeros((dy1 - dy0, dx1 - dx0), dtype=np.uint8)
    expanded = _expand_triangle(dst_shift, amount=0.75)
    cv2.fillConvexPoly(tri_mask, np.round(expanded).astype(np.int32), 255, lineType=cv2.LINE_8)
    replace = (warped_mask > 0) & (tri_mask > 0)
    if not np.any(replace):
        return

    if warped.shape[2] == 4:
        warped = warped.copy()
        warped[:, :, 3] = np.where(replace, warped[:, :, 3], 0).astype(np.uint8)
    dst_roi = dst[dy0:dy1, dx0:dx1]
    dst_roi[replace] = warped[replace]


def _expand_triangle(points: np.ndarray, amount: float = 0.5) -> np.ndarray:
    center = np.mean(points, axis=0)
    vectors = points - center
    lengths = np.linalg.norm(vectors, axis=1, keepdims=True)
    safe_lengths = np.maximum(lengths, 1e-6)
    return points + vectors / safe_lengths * float(amount)


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
    coordinate_scale: float = 1.0,
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
        vertices = (
            np.asarray(mask_drawable["vertices"], dtype=np.float32) + offset
        ) * coordinate_scale
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
    progress: Optional[ProgressCallback] = None,
    resource_limits: PsdResourceLimits | None = None,
) -> None:
    from psd_tools import PSDImage
    from psd_tools.api.layers import PixelLayer
    from psd_tools.constants import Compression

    limits = resource_limits or PsdResourceLimits()
    _validate_canvas_size(size, limits, "PSD export")
    _validate_layer_budget(
        len(layers), _layers_pixel_count(layers), 0, limits, "PSD export"
    )
    psd = PSDImage.new("RGBA", size)
    compression = Compression.RAW
    total = max(1, len(layers))
    step = max(1, total // 40)
    for index, layer in enumerate(layers, start=1):
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
        if index == total or index % step == 0:
            _emit(progress, 90 + int(index / total * 6), f"Prepared PSD layer {index}/{total}")
    _emit(progress, 97, "Saving PSD file")
    psd.save(str(path))


def _build_export_metadata(
    source: Live2DSource,
    textures: list[np.ndarray],
    canvas_size: tuple[int, int],
    mode: str,
    layers: list[dict[str, Any]],
    pose_name: str | None = None,
    parameter_values: dict[str, float] | None = None,
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

    metadata = {
        "format": METADATA_FORMAT,
        "version": METADATA_VERSION,
        "mode": mode,
        "source_model": str(source.model_json),
        "canvas": {"width": int(canvas_size[0]), "height": int(canvas_size[1])},
        "textures": texture_entries,
        "layers": layers,
    }
    if pose_name or parameter_values:
        metadata["pose"] = {
            "name": str(pose_name or ""),
            "parameters": {
                str(key): float(value)
                for key, value in dict(parameter_values or {}).items()
            },
        }
    return metadata


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

    source_root = None
    source_model = metadata.get("source_model")
    if isinstance(source_model, str) and source_model:
        source_root = Path(source_model).resolve().parent

    normalized = []
    for item in textures:
        if not isinstance(item, dict):
            continue
        relative_path = str(item.get("relative_path") or item.get("name") or "")
        source_path = ""
        if source_root and relative_path:
            source_path = str((source_root / relative_path).resolve())
        normalized.append(
            {
                "index": int(item["index"]),
                "name": _safe_output_name(str(item["name"])),
                "width": int(item["width"]),
                "height": int(item["height"]),
                "relative_path": relative_path,
                "source_path": source_path,
            }
        )
    if not normalized:
        raise PsdReconstructionError("PSD metadata texture entries are invalid.")
    return normalized


def _metadata_layers(metadata: dict[str, Any], allowed_kinds: set[str] | None = None) -> list[dict[str, Any]]:
    layers = metadata.get("layers")
    if not isinstance(layers, list) or not layers:
        raise PsdReconstructionError("PSD metadata does not contain layer entries.")

    allowed = allowed_kinds or {"atlas-component", "texture-atlas"}
    normalized = []
    for item in layers:
        if not isinstance(item, dict) or item.get("kind") not in allowed:
            continue
        if "name" not in item or "texture_index" not in item:
            continue
        normalized.append(item)
    if not normalized:
        raise PsdReconstructionError("PSD metadata does not contain repackable layers.")
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
