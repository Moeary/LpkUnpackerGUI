import base64
import hashlib
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
METADATA_VERSION = 2

# The names are deliberately fixed.  Photoshop users can still add semantic
# sub-groups below these folders, while the importer has one stable anchor when
# a layer is renamed or duplicated.
PSD_GROUP_ORIGINAL = "Original"
PSD_GROUP_PAINT = "Paint"
PSD_GROUP_AI_EDIT = "AI_Edit"
PSD_BINDING_GROUPS = (PSD_GROUP_ORIGINAL, PSD_GROUP_PAINT, PSD_GROUP_AI_EDIT)


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
    # Regions changed by this operation.  The entries are intentionally plain
    # dictionaries so the GUI can display them and callers can serialize them
    # without importing an additional model type.
    change_regions: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    shared_regions: list[dict[str, Any]] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)
    report_path: Optional[Path] = None
    # Internal exact masks used to produce conflict reports.  They are kept
    # out of the serialized report because NumPy arrays are not JSON values.
    region_masks: list[dict[str, Any]] = field(default_factory=list, repr=False)

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
    role: str = PSD_GROUP_ORIGINAL
    unit_id: Optional[str] = None
    blend_mode: str = "normal"


class _BoundPsdLayers:
    """Composite adapter for legacy flat PSD overlay layers.

    Early PSD exports did not create an edit-unit group.  Photoshop users
    commonly kept the original layer as ``name`` and added edits as
    ``name_1``, ``name_2`` and so on.  psd-tools can composite a real Group,
    but synthesising one would lose the original layer IDs and parent
    visibility.  This small adapter keeps the actual stack order and exposes
    the subset of the layer API used by the repacker.
    """

    def __init__(self, layers: list[Any]) -> None:
        self.layers = list(layers)
        boxes = [
            (
                int(getattr(layer, "left", 0)),
                int(getattr(layer, "top", 0)),
                int(getattr(layer, "left", 0)) + int(getattr(layer, "width", 0)),
                int(getattr(layer, "top", 0)) + int(getattr(layer, "height", 0)),
            )
            for layer in self.layers
        ]
        if boxes:
            left = min(box[0] for box in boxes)
            top = min(box[1] for box in boxes)
            right = max(box[2] for box in boxes)
            bottom = max(box[3] for box in boxes)
        else:
            left = top = right = bottom = 0
        self.left = left
        self.top = top
        self.width = max(0, right - left)
        self.height = max(0, bottom - top)
        self.name = str(getattr(self.layers[0], "name", "") if self.layers else "")
        self.visible = True
        self.parent = None
        self.layer_id = 0

    def is_group(self) -> bool:
        return False

    def is_visible(self) -> bool:
        return any(_layer_effectively_visible(layer) for layer in self.layers)

    def composite(self, force: bool = False, **_: Any) -> Image.Image | None:
        if self.width <= 0 or self.height <= 0:
            return None
        result = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        # psd-tools exposes the PSD stack from bottom to top.  Applying the
        # layers in that order reproduces Photoshop's normal alpha stacking.
        for layer in self.layers:
            if not _layer_effectively_visible(layer):
                continue
            image = layer.composite(force=force)
            if image is None:
                continue
            _alpha_composite_at(
                result,
                image.convert("RGBA"),
                int(getattr(layer, "left", 0)) - self.left,
                int(getattr(layer, "top", 0)) - self.top,
            )
        return result


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
    extra_memory_bytes: int = 0,
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
    estimated_bytes = (
        texture_pixels * 4 * 2
        + total_layer_pixels * 4 * 5
        + max(0, int(extra_memory_bytes))
    )
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
    unique_layers: list[Any] = []
    seen: set[int] = set()
    for layer in psd_layers.values():
        if bool(getattr(layer, "is_group", lambda: False)()):
            continue
        marker = id(layer)
        if marker in seen:
            continue
        seen.add(marker)
        unique_layers.append(layer)
    if len(unique_layers) > limits.max_layer_count:
        raise PsdReconstructionError(
            f"{operation} blocked by the safety limit: PSD contains {len(unique_layers):,} indexed layers, "
            f"exceeding {limits.max_layer_count:,}."
        )
    total_pixels = 0
    for layer in unique_layers:
        name = str(getattr(layer, "name", "?"))
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
        len(unique_layers), total_pixels, texture_pixels, limits, operation
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
    mode = str(mode or "mesh").strip().lower()
    if mode not in {"mesh", "atlas-components", "atlas-artmesh"}:
        raise PsdReconstructionError(
            f"Unsupported PSD reconstruction mode: {mode}. "
            "Use mesh, atlas-components, or atlas-artmesh."
        )
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

    if mode in {"mesh", "atlas-artmesh"} and (
        not source_info.mesh_data or (mode == "mesh" and parameter_values)
    ):
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
    elif mode == "atlas-artmesh":
        if not source_info.mesh_data:
            raise PsdReconstructionError(
                "Atlas ArtMesh export requires Cubism drawable metadata."
            )
        layers, size, layer_metadata, shared_regions = _build_atlas_artmesh_layers(
            cv2,
            source_info.mesh_data,
            source_info.textures,
            textures,
            progress,
            limits,
            texture_pixels,
        )
        mode = "atlas-artmesh"
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
    suffix = {
        "mesh": "mesh_pose",
        "atlas-components": "editable_atlas",
        "atlas-artmesh": "atlas_artmesh",
    }[mode]
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
    _save_psd(
        psd_path, size, layers, progress=progress, resource_limits=limits,
        flat_layers=(mode == "mesh"),
    )
    _emit(progress, 98, "Writing metadata")
    _refresh_pixel_references_from_psd(psd_path, layer_metadata)
    metadata = _build_export_metadata(
        source_info,
        textures,
        size,
        mode,
        layer_metadata,
        pose_name=pose_name,
        parameter_values=parameter_values,
    )
    if mode == "atlas-artmesh":
        metadata["shared_regions"] = shared_regions
    _write_layer_baselines(psd_path, metadata_path, layer_metadata)
    _write_json(metadata_path, metadata)
    export_report = {
        "mode": mode,
        "psd": str(psd_path),
        "metadata": str(metadata_path),
        "layer_count": len(layers),
        "shared_regions": shared_regions if mode == "atlas-artmesh" else [],
        "conflicts": [],
    }
    report_path = output_path / f"{model_name}_{suffix}.report.json"
    _write_json(report_path, export_report)

    _emit(progress, 100, f"PSD written: {psd_path}")
    return ReconstructionResult(
        psd_path=psd_path,
        layer_count=len(layers),
        mode=mode,
        warnings=warnings,
        metadata_path=metadata_path,
        shared_regions=shared_regions if mode == "atlas-artmesh" else [],
        report=export_report,
        report_path=report_path,
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
    cv2 = _require_cv2()
    from psd_tools import PSDImage

    psd_file = Path(psd_path).resolve()
    if not psd_file.is_file():
        raise PsdReconstructionError(f"PSD file does not exist: {psd_file}")

    metadata_file = Path(metadata_path).resolve() if metadata_path else _default_metadata_path(psd_file)
    metadata = _read_json(metadata_file)
    if metadata.get("format") != METADATA_FORMAT:
        raise PsdReconstructionError(f"Unsupported PSD metadata file: {metadata_file}")
    _validate_metadata_source_summaries(metadata)
    mode = str(metadata.get("mode") or "")
    if mode not in {"atlas-components", "atlas-artmesh", "mesh"}:
        raise PsdReconstructionError(
            "Only mesh, atlas-components, or atlas-artmesh PSD metadata can be repacked."
        )
    shared_regions = (
        [dict(item) for item in (metadata.get("shared_regions") or []) if isinstance(item, Mapping)]
        if mode == "atlas-artmesh"
        else []
    )

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    textures = _metadata_textures(metadata)
    layers_metadata = _metadata_layers(
        metadata,
        {"drawable-mesh"}
        if mode == "mesh"
        else {"atlas-component", "texture-atlas", "atlas-artmesh"},
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
            psd,
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

    canvases, warnings = _load_atlas_repack_canvases(
        textures,
        input_texture_paths,
        output_path=output_path,
        metadata_file=metadata_file,
    )
    baseline_canvases, baseline_warnings = _load_atlas_repack_canvases(
        textures,
        None,
        output_path=None,
        metadata_file=metadata_file,
    )
    warnings.extend(baseline_warnings)
    total = max(1, len(layers_metadata))
    change_regions: list[dict[str, Any]] = []
    region_masks: list[dict[str, Any]] = []

    for index, layer_info in enumerate(layers_metadata):
        texture_index = int(layer_info["texture_index"])
        canvas = canvases.get(texture_index)
        baseline_canvas = baseline_canvases.get(texture_index)
        if canvas is None or baseline_canvas is None:
            continue

        layer = _find_bound_psd_layer(psd, psd_layers, layer_info)
        if layer is None:
            warnings.append(f"Layer missing in PSD: {layer_info['name']}")
            continue
        if not _layer_effectively_visible(layer):
            warnings.append(f"Layer or parent group hidden in PSD: {layer_info['name']}")
            continue
        if bool(getattr(layer, "is_group", lambda: False)()) and not _has_visible_edit_children(layer):
            warnings.append(f"No visible Paint or AI_Edit child in PSD: {layer_info['name']}")
            continue

        image = layer.composite(force=True)
        if image is None:
            warnings.append(f"Layer has no pixel content: {layer_info['name']}")
            continue

        left = int(getattr(layer, "left", layer_info.get("left", 0)))
        top = int(getattr(layer, "top", layer_info.get("top", 0)))
        edited = np.asarray(image.convert("RGBA"), dtype=np.uint8)
        baseline_layer = _load_layer_baseline(
            metadata_file,
            layer_info,
            baseline_canvas,
            left,
            top,
            edited.shape[:2],
            baseline_origin=(int(layer_info.get("left", left)), int(layer_info.get("top", top))),
        )
        changed = _rgba_difference_mask(edited, baseline_layer)
        reference_changed = _pixel_reference_changed_mask(edited, layer_info.get("pixel_reference"))
        if reference_changed is not None and not np.any(reference_changed):
            changed.fill(False)
        if changed.shape != edited.shape[:2]:
            changed = cv2.resize(changed.astype(np.uint8), (edited.shape[1], edited.shape[0]), interpolation=cv2.INTER_NEAREST).astype(bool)
        if np.any(changed):
            before = np.asarray(canvas).copy()
            _replace_rgba_at(canvas, edited, changed, left, top)
            actual_changed = np.any(np.asarray(canvas) != before, axis=2)
            region = _mask_region(actual_changed, texture_index, layer_info.get("name"))
            if region:
                change_regions.append(region)
                region_masks.append(
                    {
                        "texture_index": texture_index,
                        "layer": str(layer_info.get("name") or ""),
                        "mask": actual_changed,
                    }
                )
        _emit(progress, 10 + int((index + 1) / total * 75), f"Packed {layer_info['name']}")

    outputs: list[Path] = []
    _emit(progress, 95, "Writing atlas PNG")
    for texture in textures:
        canvas = canvases[texture["index"]]
        output_file = output_path / _texture_output_name(texture)
        canvas.save(output_file)
        outputs.append(output_file)

    _emit(progress, 100, f"Atlas PNG written: {output_path}")
    conflicts = _detect_mask_conflicts(region_masks)
    report = {
        "mode": "atlas-repack",
        "metadata": str(metadata_file),
        "output_paths": [str(path) for path in outputs],
        "change_regions": change_regions,
        "conflicts": conflicts,
        "shared_regions": shared_regions,
    }
    report_path = output_path / "repack_report.json"
    _write_json(report_path, report)
    return ReconstructionResult(
        psd_path=outputs[0] if outputs else output_path,
        layer_count=len(layers_metadata),
        mode="atlas-repack",
        warnings=warnings,
        metadata_path=metadata_file,
        output_paths=outputs,
        texture_outputs={int(texture["index"]): output for texture, output in zip(textures, outputs)},
        change_regions=change_regions,
        conflicts=conflicts,
        shared_regions=shared_regions,
        report=report,
        report_path=report_path,
        region_masks=region_masks,
    )


def _repack_mesh_psd_layers(
    psd,
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
    canvases, warnings = _load_mesh_repack_canvases(
        textures,
        input_texture_paths,
        output_path=output_path,
        metadata_file=metadata_file,
    )
    original_canvases, baseline_warnings = _load_mesh_repack_canvases(
        textures,
        None,
        output_path=None,
        metadata_file=metadata_file,
    )
    warnings.extend(baseline_warnings)
    total = max(1, len(layers_metadata))
    change_regions: list[dict[str, Any]] = []
    region_masks: list[dict[str, Any]] = []

    for index, layer_info in enumerate(layers_metadata):
        texture_index = int(layer_info["texture_index"])
        canvas = canvases.get(texture_index)
        original_canvas = original_canvases.get(texture_index)
        if canvas is None or original_canvas is None:
            continue

        layer = _find_bound_psd_layer(psd, psd_layers, layer_info)
        if layer is None:
            warnings.append(f"Layer missing in PSD: {layer_info['name']}")
            continue
        if not _layer_effectively_visible(layer):
            warnings.append(f"Layer or parent group hidden in PSD: {layer_info['name']}")
            continue
        if bool(getattr(layer, "is_group", lambda: False)()) and not _has_visible_edit_children(layer):
            warnings.append(f"No visible Paint or AI_Edit child in PSD: {layer_info['name']}")
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

        if bool(getattr(layer, "is_group", lambda: False)()):
            group_left = int(getattr(layer, "left", layer_info.get("left", 0)))
            group_top = int(getattr(layer, "top", layer_info.get("top", 0)))
            left = int(layer_info.get("left", group_left))
            top = int(layer_info.get("top", group_top))
            bbox = layer_info.get("bbox")
            expected_width = int(bbox[2]) if isinstance(bbox, (list, tuple)) and len(bbox) >= 4 else image.width
            expected_height = int(bbox[3]) if isinstance(bbox, (list, tuple)) and len(bbox) >= 4 else image.height
            source = _place_layer_image(
                np.asarray(image.convert("RGBA"), dtype=np.uint8),
                (expected_height, expected_width),
                group_left - left,
                group_top - top,
            )
        else:
            left = int(getattr(layer, "left", layer_info.get("left", 0)))
            top = int(getattr(layer, "top", layer_info.get("top", 0)))
            source = np.asarray(image.convert("RGBA"))
        changed_tiles = _pixel_reference_changed_mask(
            source,
            layer_info.get("pixel_reference"),
        )
        if changed_tiles is not None and not np.any(changed_tiles):
            _emit(progress, 10 + int((index + 1) / total * 80), f"Packed {layer_info['name']}")
            continue
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
        baseline_layer = _load_layer_baseline(
            metadata_file,
            layer_info,
            Image.fromarray(original_layer, "RGBA"),
            left,
            top,
            source.shape[:2],
            # New exports store an immutable crop at the metadata origin.  A
            # legacy mesh export has no crop and its fallback is already the
            # freshly rendered local layer at the current PSD position.
            baseline_origin=(
                (int(layer_info.get("left", left)), int(layer_info.get("top", top)))
                if layer_info.get("baseline_path")
                else (left, top)
            ),
        )
        if baseline_layer.shape == source.shape and not np.array_equal(
            baseline_layer, original_layer
        ):
            # The exported PSD decode is the authoritative baseline.  This
            # avoids treating premultiplied/PNG round-trip bytes as edits when
            # a mesh was rendered from the source atlas.
            original_layer = baseline_layer
        edit_mask = _changed_pixel_mask(cv2, source, original_layer)
        if changed_tiles is not None:
            edit_mask = np.where(changed_tiles, edit_mask, 0).astype(np.uint8)
        if not np.any(edit_mask):
            _emit(progress, 10 + int((index + 1) / total * 80), f"Packed {layer_info['name']}")
            continue

        before_canvas = canvas.copy()
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

        actual_changed = np.any(canvas != before_canvas, axis=2)
        region = _mask_region(actual_changed, texture_index, layer_info.get("name"))
        if region:
            change_regions.append(region)
            region_masks.append(
                {
                    "texture_index": texture_index,
                    "layer": str(layer_info.get("name") or ""),
                    "mask": actual_changed,
                }
            )

        _emit(progress, 10 + int((index + 1) / total * 80), f"Packed {layer_info['name']}")

    outputs: list[Path] = []
    _emit(progress, 95, "Writing atlas PNG")
    for texture in textures:
        canvas = canvases[texture["index"]]
        output_file = output_path / _texture_output_name(texture)
        Image.fromarray(canvas, "RGBA").save(output_file)
        outputs.append(output_file)

    _emit(progress, 100, f"Atlas PNG written: {output_path}")
    conflicts = _detect_mask_conflicts(region_masks)
    report = {
        "mode": "mesh-repack",
        "metadata": str(metadata_file),
        "output_paths": [str(path) for path in outputs],
        "change_regions": change_regions,
        "conflicts": conflicts,
    }
    report_path = output_path / "repack_report.json"
    _write_json(report_path, report)
    return ReconstructionResult(
        psd_path=outputs[0] if outputs else output_path,
        layer_count=len(layers_metadata),
        mode="mesh-repack",
        warnings=warnings,
        metadata_path=metadata_file,
        output_paths=outputs,
        texture_outputs={int(texture["index"]): output for texture, output in zip(textures, outputs)},
        change_regions=change_regions,
        conflicts=conflicts,
        report=report,
        report_path=report_path,
        region_masks=region_masks,
    )


def _load_mesh_repack_canvases(
    textures: list[dict[str, Any]],
    input_texture_paths: Mapping[int, str | Path] | None = None,
    *,
    output_path: Path | None = None,
    metadata_file: Path | None = None,
) -> tuple[dict[int, np.ndarray], list[str]]:
    canvases: dict[int, np.ndarray] = {}
    warnings: list[str] = []
    for texture in textures:
        width = int(texture["width"])
        height = int(texture["height"])
        source_path = _resolve_repack_texture_path(texture, input_texture_paths)
        _ensure_repack_source(source_path, texture, output_path, metadata_file)
        with Image.open(source_path) as source_image:
            image = source_image.convert("RGBA")
        if image.size != (width, height):
            raise PsdReconstructionError(
                f"Texture size changed for {source_path}: expected {width}x{height}, "
                f"got {image.width}x{image.height}. Repack aborted to preserve the fixed baseline."
            )
        _validate_texture_digest(source_path, texture, image)
        canvases[int(texture["index"])] = np.asarray(image).copy()
    return canvases, warnings


def _load_atlas_repack_canvases(
    textures: list[dict[str, Any]],
    input_texture_paths: Mapping[int, str | Path] | None = None,
    *,
    output_path: Path | None = None,
    metadata_file: Path | None = None,
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
        _ensure_repack_source(source_path, texture, output_path, metadata_file)
        with Image.open(source_path) as source_image:
            image = source_image.convert("RGBA")
        if image.size != (width, height):
            raise PsdReconstructionError(
                f"Texture size changed for {source_path}: expected {width}x{height}, "
                f"got {image.width}x{image.height}. Repack aborted to preserve the fixed baseline."
            )
        _validate_texture_digest(source_path, texture, image)
        canvases[int(texture["index"])] = image
    return canvases, warnings


def _validate_texture_digest(
    source_path: Path,
    texture: Mapping[str, Any],
    image: Image.Image,
) -> None:
    expected = str(texture.get("rgba_sha256") or "")
    fixed_source = str(texture.get("source_path") or "")
    if not expected or not fixed_source:
        return
    # In a multi-PSD pass source_path may be the previous output.  Only the
    # path recorded in metadata is the immutable baseline whose digest must
    # still match.
    if source_path.resolve() != Path(fixed_source).resolve():
        return
    actual = _rgba_sha256(image)
    if actual != expected:
        raise PsdReconstructionError(
            f"Original texture digest changed for {texture.get('relative_path') or texture.get('name') or source_path.name}: "
            f"{source_path}. Repack requires the fixed original baseline."
        )


def _resolve_repack_texture_path(
    texture: Mapping[str, Any],
    input_texture_paths: Mapping[int, str | Path] | None,
) -> Path:
    texture_index = int(texture["index"])
    candidate = (input_texture_paths or {}).get(texture_index)
    value = candidate or texture.get("source_path") or ""
    if not value:
        raise PsdReconstructionError(
            f"No source texture path is available for texture index {texture_index} "
            f"({texture.get('relative_path') or texture.get('name') or '?'})"
        )
    return Path(str(value)).resolve()


def _ensure_repack_source(
    source_path: Path,
    texture: Mapping[str, Any],
    output_path: Path | None,
    metadata_file: Path | None,
) -> None:
    if not source_path.is_file():
        relative = texture.get("relative_path") or texture.get("name") or "?"
        raise PsdReconstructionError(
            f"Original texture is missing for {relative}: {source_path}. "
            "Repack requires the fixed original baseline."
        )
    if output_path is None:
        return
    target = (output_path / _texture_output_name(texture)).resolve()
    fixed_source_value = str(texture.get("source_path") or "")
    fixed_source = Path(fixed_source_value).resolve() if fixed_source_value else None
    # A multi-PSD pass may intentionally feed the preceding pass's output
    # back into the next pass.  That is safe when the input differs from the
    # immutable source path.  A direct repack that resolves to the source
    # atlas itself remains an error even when an explicit input mapping was
    # supplied.
    if target == source_path.resolve() and (fixed_source is None or fixed_source == source_path.resolve()):
        raise PsdReconstructionError(
            f"Repack output would overwrite the original baseline texture: {source_path}. "
            "Choose a separate output directory."
        )


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

    signatures: list[dict[str, Any]] = []
    for psd_file in ordered_paths:
        metadata_file = _default_metadata_path(psd_file)
        metadata = _read_json(metadata_file)
        if metadata.get("format") != METADATA_FORMAT:
            raise PsdReconstructionError(
                f"Unsupported PSD metadata file for multi-PSD repack: {metadata_file}"
            )
        signatures.append(_metadata_model_signature(metadata))
    baseline_signature = signatures[0]
    consistency_errors: list[str] = []
    for index, signature in enumerate(signatures[1:], start=2):
        differences = _compare_model_signatures(baseline_signature, signature)
        if differences:
            consistency_errors.append(f"PSD #{index}: " + "; ".join(differences))
    if consistency_errors:
        raise PsdReconstructionError(
            "Multi-PSD model mismatch; all PSDs must use the same model and texture atlas layout. "
            + " | ".join(consistency_errors)
        )

    latest: ReconstructionResult | None = None
    texture_paths: dict[int, Path] | None = None
    total = len(ordered_paths)
    warnings: list[str] = []
    all_regions: list[dict[str, Any]] = []
    all_conflicts: list[dict[str, Any]] = []
    all_shared_regions: list[dict[str, Any]] = []
    all_region_masks: list[dict[str, Any]] = []
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
        for shared in result.shared_regions:
            enriched_shared = dict(shared)
            enriched_shared["psd"] = str(psd_file)
            all_shared_regions.append(enriched_shared)
        for region in result.change_regions:
            enriched = dict(region)
            enriched["psd"] = str(psd_file)
            all_regions.append(enriched)
        for region in result.region_masks:
            enriched_mask = dict(region)
            enriched_mask["psd"] = str(psd_file)
            all_region_masks.append(enriched_mask)
        for conflict in result.conflicts:
            enriched = dict(conflict)
            enriched["psd"] = str(psd_file)
            all_conflicts.append(enriched)
        texture_paths = result.texture_outputs
        latest = result

    if latest is None:
        raise PsdReconstructionError("No PSD was available for multi-PSD repack.")
    _emit(progress, 100, f"Atlas PNG written: {Path(output_dir)}")
    for index, first in enumerate(all_region_masks):
        first_mask = first.get("mask")
        if not isinstance(first_mask, np.ndarray):
            continue
        for second in all_region_masks[index + 1 :]:
            if first.get("psd") == second.get("psd"):
                continue
            if int(first.get("texture_index", -1)) != int(second.get("texture_index", -2)):
                continue
            second_mask = second.get("mask")
            if not isinstance(second_mask, np.ndarray) or first_mask.shape != second_mask.shape:
                continue
            overlap_mask = np.asarray(first_mask, dtype=bool) & np.asarray(second_mask, dtype=bool)
            region = _mask_region(
                overlap_mask,
                int(first.get("texture_index", -1)),
                f"{first.get('layer', '')}|{second.get('layer', '')}",
            )
            if region is not None:
                all_conflicts.append(
                    {
                        "kind": "multi-psd-overlap",
                        "potential": False,
                        "texture_index": int(first.get("texture_index", -1)),
                        "layers": [first.get("layer", ""), second.get("layer", "")],
                        "psds": [first.get("psd", ""), second.get("psd", "")],
                        "bbox": region["bbox"],
                        "pixel_count": region["pixel_count"],
                    }
                )
    report = {
        "mode": "multi-repack",
        "psds": [str(path) for path in ordered_paths],
        "model_signature": baseline_signature,
        "output_paths": [str(path) for path in latest.output_paths],
        "change_regions": all_regions,
        "conflicts": all_conflicts,
        "shared_regions": all_shared_regions,
    }
    report_path = Path(output_dir) / "multi_repack_report.json"
    _write_json(report_path, report)
    return ReconstructionResult(
        psd_path=latest.psd_path,
        layer_count=sum(1 for _ in ordered_paths),
        mode="multi-repack",
        warnings=warnings,
        metadata_path=latest.metadata_path,
        output_paths=latest.output_paths,
        texture_outputs=latest.texture_outputs,
        change_regions=all_regions,
        conflicts=all_conflicts,
        shared_regions=all_shared_regions,
        report=report,
        report_path=report_path,
        region_masks=all_region_masks,
    )


def _metadata_model_signature(metadata: Mapping[str, Any]) -> dict[str, Any]:
    textures = metadata.get("textures") or []
    normalized_textures: list[dict[str, Any]] = []
    for item in textures:
        if not isinstance(item, Mapping):
            continue
        normalized_textures.append(
            {
                "index": int(item.get("index", len(normalized_textures))),
                "relative_path": str(item.get("relative_path") or item.get("name") or "").replace("\\", "/"),
                "name": str(item.get("name") or ""),
                "width": int(item.get("width", 0)),
                "height": int(item.get("height", 0)),
                "rgba_sha256": str(item.get("rgba_sha256") or ""),
            }
        )
    canvas = metadata.get("canvas") if isinstance(metadata.get("canvas"), Mapping) else {}
    source_model = str(metadata.get("source_model") or "")
    model_summary = metadata.get("source_model_summary")
    moc_summary = metadata.get("source_moc_summary")
    return {
        "model": Path(source_model).name.casefold() if source_model else "",
        "model_sha256": str(model_summary.get("sha256") or "") if isinstance(model_summary, Mapping) else "",
        "moc_sha256": str(moc_summary.get("sha256") or "") if isinstance(moc_summary, Mapping) else "",
        "canvas": [int(canvas.get("width", 0)), int(canvas.get("height", 0))],
        "textures": normalized_textures,
    }


def _compare_model_signatures(first: Mapping[str, Any], second: Mapping[str, Any]) -> list[str]:
    differences: list[str] = []
    if first.get("model") and second.get("model") and first.get("model") != second.get("model"):
        differences.append(f"model {first.get('model')} != {second.get('model')}")
    if first.get("model_sha256") and second.get("model_sha256") and first.get("model_sha256") != second.get("model_sha256"):
        differences.append("model JSON digest differs")
    if first.get("moc_sha256") and second.get("moc_sha256") and first.get("moc_sha256") != second.get("moc_sha256"):
        differences.append("MOC3 digest differs")
    # Canvas dimensions describe the projection of an individual export.  A
    # mesh/pose PSD may legitimately use a different projection canvas while
    # still belonging to the same model and texture layout, so they are not a
    # model-consistency key for multi-PSD repack.
    first_textures = list(first.get("textures") or [])
    second_textures = list(second.get("textures") or [])
    if first_textures != second_textures:
        differences.append("texture atlas paths, order, or dimensions differ")
    return differences


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
    _paint_triangles(cv2, texture, layer, texture_points, local_vertices, indices)
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

    # Compare all four channels.  In particular, alpha=0 is a deliberate
    # erasure and must reach the atlas instead of being filtered as an
    # ``alpha-only`` difference.  Exported layer baselines are decoded from
    # the PSD itself, so exact equality is stable across a no-op round trip.
    changed = np.any(edited.astype(np.uint8) != reference.astype(np.uint8), axis=2)
    return (changed.astype(np.uint8) * 255)


def _build_pixel_reference(image: Image.Image, tile_size: int = 32) -> dict[str, Any]:
    rgba = np.ascontiguousarray(np.asarray(image.convert("RGBA"), dtype=np.uint8))
    height, width = rgba.shape[:2]
    tile_size = max(8, int(tile_size))
    hashes = bytearray()
    for top in range(0, height, tile_size):
        for left in range(0, width, tile_size):
            tile = np.ascontiguousarray(
                rgba[top : top + tile_size, left : left + tile_size]
            )
            hashes.extend(hashlib.blake2b(tile.tobytes(), digest_size=8).digest())
    return {
        "version": 1,
        "width": width,
        "height": height,
        "tile_size": tile_size,
        "digest": base64.b64encode(
            hashlib.blake2b(rgba.tobytes(), digest_size=16).digest()
        ).decode("ascii"),
        "tile_hashes": base64.b64encode(bytes(hashes)).decode("ascii"),
    }


def _rgba_sha256(value: Image.Image | np.ndarray) -> str:
    """Return a stable digest of decoded, tightly packed RGBA bytes."""
    if isinstance(value, Image.Image):
        rgba = np.ascontiguousarray(np.asarray(value.convert("RGBA"), dtype=np.uint8))
    else:
        rgba = np.ascontiguousarray(np.asarray(value, dtype=np.uint8))
    if rgba.ndim != 3 or rgba.shape[2] != 4:
        raise PsdReconstructionError("RGBA digest requires an RGBA image.")
    return hashlib.sha256(rgba.tobytes()).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise PsdReconstructionError(f"Failed to read source summary file {path}: {exc}") from exc
    return digest.hexdigest()


def _file_summary(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    path = Path(path).resolve()
    if not path.is_file():
        return None
    return {
        "path": str(path),
        "name": path.name,
        "size": int(path.stat().st_size),
        "sha256": _file_sha256(path),
    }


def _validate_metadata_source_summaries(metadata: Mapping[str, Any]) -> None:
    """Reject a model/moc replacement when newer metadata has a summary."""
    for key, label in (
        ("source_model_summary", "model JSON"),
        ("source_moc_summary", "MOC3"),
    ):
        summary = metadata.get(key)
        if not isinstance(summary, Mapping):
            continue
        path_value = str(summary.get("path") or "")
        if not path_value:
            continue
        path = Path(path_value).resolve()
        if not path.is_file():
            raise PsdReconstructionError(
                f"Source {label} is missing: {path}. Re-export the PSD from the fixed model baseline."
            )
        expected_size = int(summary.get("size", -1))
        expected_digest = str(summary.get("sha256") or "")
        actual_size = int(path.stat().st_size)
        if expected_size >= 0 and actual_size != expected_size:
            raise PsdReconstructionError(
                f"Source {label} size changed for {path}: expected {expected_size}, got {actual_size}."
            )
        if expected_digest and _file_sha256(path) != expected_digest:
            raise PsdReconstructionError(
                f"Source {label} digest changed for {path}. Re-export the PSD from the fixed model baseline."
            )


def _refresh_pixel_references_from_psd(
    psd_path: Path,
    layers_metadata: list[dict[str, Any]],
) -> None:
    from psd_tools import PSDImage

    indexed = _index_psd_layers(PSDImage.open(psd_path))
    for layer_info in layers_metadata:
        layer = _find_bound_psd_layer_from_index(indexed, layer_info)
        if layer is None:
            continue
        image = layer.composite(force=True)
        if image is not None:
            layer_info["pixel_reference"] = _build_pixel_reference(image)
        layer_info["layer_id"] = int(getattr(layer, "layer_id", 0) or 0)
        layer_info["binding_path"] = _layer_binding_path(layer)
        layer_info["psd_group"] = _top_level_group_name(layer)


def _pixel_reference_changed_mask(
    rgba: np.ndarray,
    reference: Any,
) -> np.ndarray | None:
    if not isinstance(reference, Mapping):
        return None
    image = np.ascontiguousarray(np.asarray(rgba, dtype=np.uint8))
    height, width = image.shape[:2]
    if (
        int(reference.get("width", -1)) != width
        or int(reference.get("height", -1)) != height
    ):
        return None
    try:
        expected_digest = base64.b64decode(str(reference.get("digest", "")), validate=True)
        actual_digest = hashlib.blake2b(image.tobytes(), digest_size=16).digest()
        if actual_digest == expected_digest:
            return np.zeros((height, width), dtype=bool)

        tile_size = max(8, int(reference.get("tile_size", 32)))
        expected = base64.b64decode(
            str(reference.get("tile_hashes", "")),
            validate=True,
        )
    except (TypeError, ValueError):
        return None

    tiles_x = math.ceil(width / tile_size)
    tiles_y = math.ceil(height / tile_size)
    if len(expected) != tiles_x * tiles_y * 8:
        return None

    changed = np.zeros((height, width), dtype=bool)
    offset = 0
    for top in range(0, height, tile_size):
        for left in range(0, width, tile_size):
            tile = np.ascontiguousarray(
                image[top : top + tile_size, left : left + tile_size]
            )
            digest = hashlib.blake2b(tile.tobytes(), digest_size=8).digest()
            if digest != expected[offset : offset + 8]:
                changed[
                    top : top + tile_size,
                    left : left + tile_size,
                ] = True
            offset += 8
    return changed


def _rgba_difference_mask(edited: np.ndarray, baseline: np.ndarray) -> np.ndarray:
    """Return an exact per-pixel RGBA change mask.

    The old repacker compared only visible RGB values.  That made a Photoshop
    eraser ineffective because an alpha-only change was discarded.  Keeping
    all four channels here also means transparent RGB edits are handled
    consistently and untouched pixels are copied byte-for-byte.
    """
    edited = np.asarray(edited, dtype=np.uint8)
    baseline = np.asarray(baseline, dtype=np.uint8)
    if edited.shape != baseline.shape:
        return np.ones(edited.shape[:2], dtype=bool)
    return np.any(edited != baseline, axis=2)


def _replace_rgba_at(
    canvas: Image.Image | np.ndarray,
    edited: np.ndarray,
    changed: np.ndarray,
    left: int,
    top: int,
) -> None:
    """Replace only changed RGBA pixels in a canvas, including erasures."""
    pil_canvas = canvas if isinstance(canvas, Image.Image) else None
    target = np.asarray(canvas).copy() if pil_canvas is not None else canvas
    if target.ndim != 3 or target.shape[2] != 4:
        raise PsdReconstructionError("RGBA replacement requires an RGBA canvas.")
    edited = np.asarray(edited, dtype=np.uint8)
    changed = np.asarray(changed, dtype=bool)
    if edited.ndim != 3 or edited.shape[2] != 4 or changed.shape != edited.shape[:2]:
        raise PsdReconstructionError("RGBA replacement layer dimensions are invalid.")

    canvas_h, canvas_w = target.shape[:2]
    src_h, src_w = edited.shape[:2]
    dst_left = max(0, int(left))
    dst_top = max(0, int(top))
    dst_right = min(canvas_w, int(left) + src_w)
    dst_bottom = min(canvas_h, int(top) + src_h)
    if dst_right <= dst_left or dst_bottom <= dst_top:
        return
    src_left = dst_left - int(left)
    src_top = dst_top - int(top)
    src_right = src_left + dst_right - dst_left
    src_bottom = src_top + dst_bottom - dst_top
    mask = changed[src_top:src_bottom, src_left:src_right]
    roi = target[dst_top:dst_bottom, dst_left:dst_right]
    roi[mask] = edited[src_top:src_bottom, src_left:src_right][mask]
    if pil_canvas is not None:
        pil_canvas.paste(Image.fromarray(target, "RGBA"))


def _or_mask_at(
    target: np.ndarray,
    source: np.ndarray,
    left: int,
    top: int,
) -> None:
    canvas_h, canvas_w = target.shape[:2]
    src_h, src_w = source.shape[:2]
    dst_left = max(0, int(left))
    dst_top = max(0, int(top))
    dst_right = min(canvas_w, int(left) + src_w)
    dst_bottom = min(canvas_h, int(top) + src_h)
    if dst_right <= dst_left or dst_bottom <= dst_top:
        return
    src_left = dst_left - int(left)
    src_top = dst_top - int(top)
    target[dst_top:dst_bottom, dst_left:dst_right] |= source[
        src_top : src_top + dst_bottom - dst_top,
        src_left : src_left + dst_right - dst_left,
    ]


def _mask_region(
    mask: np.ndarray,
    texture_index: int,
    layer_name: Any,
) -> dict[str, Any] | None:
    coords = np.argwhere(np.asarray(mask, dtype=bool))
    if coords.size == 0:
        return None
    top, left = coords.min(axis=0)
    bottom, right = coords.max(axis=0)
    return {
        "texture_index": int(texture_index),
        "layer": str(layer_name or ""),
        "bbox": [int(left), int(top), int(right - left + 1), int(bottom - top + 1)],
        "pixel_count": int(coords.shape[0]),
    }


def _detect_region_conflicts(regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for index, first in enumerate(regions):
        for second in regions[index + 1 :]:
            if int(first.get("texture_index", -1)) != int(second.get("texture_index", -2)):
                continue
            overlap = _bbox_intersection(first.get("bbox"), second.get("bbox"))
            if overlap is None:
                continue
            conflicts.append(
                {
                    "texture_index": int(first.get("texture_index", -1)),
                    "layers": [str(first.get("layer") or ""), str(second.get("layer") or "")],
                    "bbox": list(overlap),
                    # Change regions currently carry bboxes for a compact
                    # report.  Their bbox intersection is only a candidate;
                    # do not present it as a proven pixel collision.
                    "kind": "potential-overlap",
                    "potential": True,
                }
            )
    return conflicts


def _detect_mask_conflicts(regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Report only proven pixel collisions between changed atlas masks."""
    conflicts: list[dict[str, Any]] = []
    for index, first in enumerate(regions):
        first_mask = first.get("mask")
        if not isinstance(first_mask, np.ndarray):
            continue
        for second in regions[index + 1 :]:
            if int(first.get("texture_index", -1)) != int(second.get("texture_index", -2)):
                continue
            second_mask = second.get("mask")
            if not isinstance(second_mask, np.ndarray) or first_mask.shape != second_mask.shape:
                continue
            overlap = np.asarray(first_mask, dtype=bool) & np.asarray(second_mask, dtype=bool)
            region = _mask_region(
                overlap,
                int(first.get("texture_index", -1)),
                f"{first.get('layer', '')}|{second.get('layer', '')}",
            )
            if region:
                conflicts.append(
                    {
                        "texture_index": int(first.get("texture_index", -1)),
                        "layers": [str(first.get("layer") or ""), str(second.get("layer") or "")],
                        "bbox": region["bbox"],
                        "pixel_count": region["pixel_count"],
                        "kind": "overlap",
                        "potential": False,
                    }
                )
    return conflicts


def _bbox_intersection(first: Any, second: Any) -> tuple[int, int, int, int] | None:
    if not isinstance(first, (list, tuple)) or len(first) < 4:
        return None
    if not isinstance(second, (list, tuple)) or len(second) < 4:
        return None
    left = max(int(first[0]), int(second[0]))
    top = max(int(first[1]), int(second[1]))
    right = min(int(first[0]) + int(first[2]), int(second[0]) + int(second[2]))
    bottom = min(int(first[1]) + int(first[3]), int(second[1]) + int(second[3]))
    if right <= left or bottom <= top:
        return None
    return left, top, right - left, bottom - top


def _load_layer_baseline(
    metadata_file: Path,
    layer_info: Mapping[str, Any],
    fallback_canvas: Image.Image | np.ndarray,
    left: int,
    top: int,
    shape: tuple[int, int],
    baseline_origin: tuple[int, int] | None = None,
) -> np.ndarray:
    """Load the immutable exported-layer baseline.

    New exports write a small PNG per layer next to the metadata file.  This is
    deliberately independent of the evolving multi-PSD target atlas.  Legacy
    metadata falls back to the corresponding crop from the fixed source atlas,
    which is still preferable to comparing against a previous pass.
    """
    baseline_path = str(layer_info.get("baseline_path") or "")
    if baseline_path:
        candidate = (metadata_file.parent / baseline_path).resolve()
        if not candidate.is_file():
            raise PsdReconstructionError(
                f"PSD layer baseline is missing: {candidate}. Re-export the PSD before repacking."
            )
        with Image.open(candidate) as image:
            baseline = np.asarray(image.convert("RGBA"), dtype=np.uint8).copy()
        expected_digest = str(layer_info.get("baseline_rgba_sha256") or "")
        if expected_digest and _rgba_sha256(baseline) != expected_digest:
            raise PsdReconstructionError(
                f"PSD layer baseline digest changed for {layer_info.get('name', '?')}: {candidate}. "
                "Re-export the PSD."
            )
        expected_size_value = layer_info.get("baseline_size")
        if isinstance(expected_size_value, (list, tuple)) and len(expected_size_value) >= 2:
            expected_size = (int(expected_size_value[0]), int(expected_size_value[1]))
            if (baseline.shape[1], baseline.shape[0]) != expected_size:
                raise PsdReconstructionError(
                    f"PSD layer baseline size changed for {layer_info.get('name', '?')}: "
                    f"expected {expected_size[0]}x{expected_size[1]}, "
                    f"got {baseline.shape[1]}x{baseline.shape[0]}. Re-export the PSD."
                )
        expected_bbox = layer_info.get("bbox")
        if isinstance(expected_bbox, (list, tuple)) and len(expected_bbox) >= 4:
            expected_size = (int(expected_bbox[2]), int(expected_bbox[3]))
            if baseline.shape[:2][::-1] != expected_size:
                raise PsdReconstructionError(
                    f"PSD layer baseline size changed for {layer_info.get('name', '?')}: "
                    f"expected {expected_size[0]}x{expected_size[1]}, "
                    f"got {baseline.shape[1]}x{baseline.shape[0]}. Re-export the PSD."
                )
        return _align_baseline_image(
            baseline,
            shape,
            baseline_origin or (left, top),
            (left, top),
            layer_info,
        )

    source = np.asarray(fallback_canvas.convert("RGBA") if isinstance(fallback_canvas, Image.Image) else fallback_canvas)
    if source.ndim != 3 or source.shape[2] != 4:
        raise PsdReconstructionError("Fixed baseline canvas is not RGBA.")
    height, width = shape
    source_left, source_top = baseline_origin or (left, top)
    if source.shape[:2] == (height, width) and (source_left, source_top) == (left, top):
        return source.astype(np.uint8, copy=False)
    # Crop a source atlas for legacy atlas metadata.  Out-of-bounds regions are
    # invalid because silently padding would turn missing baseline bytes into
    # edits.
    src_left = int(source_left)
    src_top = int(source_top)
    if src_left < 0 or src_top < 0 or src_left + width > source.shape[1] or src_top + height > source.shape[0]:
        raise PsdReconstructionError(
            f"PSD layer baseline crop is outside the fixed atlas for {layer_info.get('name', '?')}."
        )
    return source[src_top : src_top + height, src_left : src_left + width].astype(np.uint8, copy=False)


def _align_baseline_image(
    baseline: np.ndarray,
    shape: tuple[int, int],
    baseline_origin: tuple[int, int],
    edited_origin: tuple[int, int],
    layer_info: Mapping[str, Any],
) -> np.ndarray:
    """Place a fixed layer baseline into an edited group's expanded bbox."""
    height, width = shape
    result = np.zeros((height, width, 4), dtype=np.uint8)
    offset_x = int(baseline_origin[0]) - int(edited_origin[0])
    offset_y = int(baseline_origin[1]) - int(edited_origin[1])
    src_h, src_w = baseline.shape[:2]
    dst_left = max(0, offset_x)
    dst_top = max(0, offset_y)
    dst_right = min(width, offset_x + src_w)
    dst_bottom = min(height, offset_y + src_h)
    if dst_right <= dst_left or dst_bottom <= dst_top:
        return result
    src_left = dst_left - offset_x
    src_top = dst_top - offset_y
    result[dst_top:dst_bottom, dst_left:dst_right] = baseline[
        src_top : src_top + dst_bottom - dst_top,
        src_left : src_left + dst_right - dst_left,
    ]
    return result


def _place_layer_image(
    image: np.ndarray,
    shape: tuple[int, int],
    offset_x: int,
    offset_y: int,
) -> np.ndarray:
    """Clip a group composite into the fixed ArtMesh layer rectangle."""
    height, width = shape
    result = np.zeros((height, width, 4), dtype=np.uint8)
    src_h, src_w = image.shape[:2]
    dst_left = max(0, int(offset_x))
    dst_top = max(0, int(offset_y))
    dst_right = min(width, int(offset_x) + src_w)
    dst_bottom = min(height, int(offset_y) + src_h)
    if dst_right <= dst_left or dst_bottom <= dst_top:
        return result
    src_left = dst_left - int(offset_x)
    src_top = dst_top - int(offset_y)
    result[dst_top:dst_bottom, dst_left:dst_right] = image[
        src_top : src_top + dst_bottom - dst_top,
        src_left : src_left + dst_right - dst_left,
    ]
    return result


def _write_layer_baselines(
    psd_path: Path,
    metadata_path: Path,
    layers_metadata: list[dict[str, Any]],
) -> None:
    """Persist decoded PSD layer bytes used as the future immutable baseline."""
    from psd_tools import PSDImage

    indexed = _index_psd_layers(PSDImage.open(str(psd_path)))
    baseline_dir = metadata_path.with_suffix("").with_name(
        metadata_path.stem.replace(".lpkpsd", "") + ".baseline"
    )
    baseline_dir.mkdir(parents=True, exist_ok=True)
    for index, layer_info in enumerate(layers_metadata):
        layer = _find_bound_psd_layer_from_index(indexed, layer_info)
        if layer is None:
            continue
        unit = _edit_unit_group(layer)
        baseline_layer = unit if unit is not None else layer
        image = baseline_layer.composite(force=True)
        if image is None:
            continue
        # Index-based filenames avoid collisions when users intentionally use
        # duplicate drawable IDs.  The layer_id/path remains the binding key.
        output = baseline_dir / f"layer_{index:04d}.png"
        image.convert("RGBA").save(output, format="PNG")
        try:
            relative = output.resolve().relative_to(metadata_path.parent.resolve()).as_posix()
        except ValueError:
            relative = output.name
        layer_info["baseline_path"] = relative
        layer_info["baseline_rgba_sha256"] = _rgba_sha256(image)
        layer_info["baseline_size"] = [int(image.width), int(image.height)]
        layer_info["layer_id"] = int(getattr(layer, "layer_id", 0) or 0)
        layer_info["pixel_layer_id"] = int(getattr(layer, "layer_id", 0) or 0)
        if unit is not None:
            layer_info["unit_group_id"] = int(getattr(unit, "layer_id", 0) or 0)
            layer_info["binding_path"] = _layer_binding_path(unit)
        else:
            layer_info["binding_path"] = _layer_binding_path(layer)
        layer_info["psd_group"] = _top_level_group_name(layer)


def _layer_binding_path(layer: Any) -> str:
    parts: list[str] = []
    current = layer
    while current is not None and getattr(current, "parent", None) is not None:
        parts.append(str(getattr(current, "name", "")))
        current = getattr(current, "parent", None)
    parts.reverse()
    return "/".join(part for part in parts if part)


def _top_level_group_name(layer: Any) -> str:
    current = layer
    parent = getattr(current, "parent", None)
    while parent is not None and getattr(parent, "parent", None) is not None:
        current = parent
        parent = getattr(current, "parent", None)
    return str(getattr(current, "name", "")) if current is not layer else ""


def _edit_unit_group(layer: Any) -> Any | None:
    """Return the nearest group that owns Original/Paint/AI_Edit anchors."""
    current = layer
    while current is not None:
        if bool(getattr(current, "is_group", lambda: False)()):
            names = {
                str(getattr(child, "name", ""))
                for child in current
                if bool(getattr(child, "is_group", lambda: False)())
            }
            if set(PSD_BINDING_GROUPS).issubset(names):
                return current
        current = getattr(current, "parent", None)
    return None


def _has_visible_edit_children(unit: Any) -> bool:
    """Whether a bound edit unit has visible Original/Paint/AI_Edit content."""
    for child in unit:
        if str(getattr(child, "name", "")) not in set(PSD_BINDING_GROUPS):
            continue
        if not _layer_effectively_visible(child):
            continue
        if bool(getattr(child, "is_group", lambda: False)()):
            if any(_layer_effectively_visible(descendant) for descendant in child.descendants()):
                return True
        elif _layer_effectively_visible(child):
            return True
    return False


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
        extra_memory_bytes=(
            source_size[0] * source_size[1] * 8
            if coordinate_scale < 1.0
            else 0
        ),
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
        # Rasterize in the original Cubism coordinate space, matching the
        # pre-limit exporter that produced clean layers.  Scaling every mesh
        # vertex before rasterization moves shared triangle edges onto slightly
        # different subpixels and exposes their anti-aliased boundaries.
        source_vertices = np.asarray(drawable["vertices"], dtype=np.float32) + offset
        vertices = source_vertices * coordinate_scale
        uvs = _uv_to_texture_points(np.asarray(drawable["uvs"], dtype=np.float32), texture)
        if not _uv_source_has_alpha(texture, uvs):
            continue

        indices = drawable["indices"]
        source_bounds = _vertex_bounds(source_vertices, source_size[0], source_size[1])
        if source_bounds is None:
            continue
        layer_left, layer_top, layer_width, layer_height = source_bounds
        layer_pixels = layer_width * layer_height
        if layer_pixels * 20 > limits.max_memory_mb * 1024 * 1024:
            raise PsdReconstructionError(
                f"PSD export blocked by the safety limit: drawable {drawable['id']} "
                f"needs too much temporary raster memory."
            )
        layer = np.zeros((layer_height, layer_width, 4), dtype=np.uint8)
        local_vertices = source_vertices - np.asarray([layer_left, layer_top], dtype=np.float32)

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
            1.0,
        )

        bbox = _alpha_bbox(layer)
        if bbox is None:
            continue
        crop_left, crop_top, width, height = bbox
        source_left = layer_left + crop_left
        source_top = layer_top + crop_top
        source_right = source_left + width
        source_bottom = source_top + height
        left = int(math.floor(source_left * coordinate_scale))
        top = int(math.floor(source_top * coordinate_scale))
        right = int(math.ceil(source_right * coordinate_scale))
        bottom = int(math.ceil(source_bottom * coordinate_scale))
        output_width = max(1, right - left)
        output_height = max(1, bottom - top)

        layer_name = _safe_layer_name(drawable["id"])
        group_name = _drawable_group_name(drawable)
        source_image = Image.fromarray(
            layer[crop_top : crop_top + height, crop_left : crop_left + width],
            "RGBA",
        )
        image = (
            _resize_rgba_premultiplied(source_image, (output_width, output_height))
            if coordinate_scale != 1.0
            else source_image
        )
        layers.append(
            PsdLayer(
                layer_name, image, left, top, group_name,
                blend_mode=str(drawable.get("blend_mode", "normal")),
            )
        )
        layer_metadata.append(
            {
                "kind": "drawable-mesh",
                "name": layer_name,
                "group": group_name,
                "drawable_id": drawable["id"],
                "texture_index": texture_index,
                "left": left,
                "top": top,
                "bbox": [left, top, output_width, output_height],
                "vertices": drawable["vertices"],
                "uvs": drawable["uvs"],
                "indices": drawable["indices"],
                "opacity": drawable.get("opacity", 1.0),
                "render_order": drawable.get("render_order", 0),
                "draw_order": drawable.get("draw_order", 0),
                "blend_mode": drawable.get("blend_mode", "normal"),
                "blend_mode_value": drawable.get("blend_mode_value", 0),
                "coordinate_scale": coordinate_scale,
            }
        )
        _emit(progress, 20 + int((index + 1) / total * 70), f"Rendered {drawable['id']}")

    if not layers:
        raise PsdReconstructionError("No drawable layers were rendered.")
    # Keep one editable layer per drawable.  Global seam-cover layers obscure
    # edits underneath them and have no ArtMesh identity for reverse mapping.
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


def _build_atlas_artmesh_layers(
    cv2,
    mesh_data: dict[str, Any],
    paths: list[Path],
    textures: list[np.ndarray],
    progress: Optional[ProgressCallback] = None,
    limits: PsdResourceLimits | None = None,
    texture_pixels: int | None = None,
) -> tuple[list[PsdLayer], tuple[int, int], list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract every Cubism ArtMesh UV footprint as an editable atlas overlay.

    This mode intentionally does not inspect visibility, dynamic flags, opacity,
    masks, or current pose.  UV/indices are geometry, so hidden, tiny and
    transparent fragments remain represented and can be painted back later.
    """
    limits = limits or PsdResourceLimits()
    drawables = mesh_data.get("drawables")
    if not isinstance(drawables, list) or not drawables:
        raise PsdReconstructionError("Drawable mesh metadata is empty or invalid.")
    normalized: list[dict[str, Any]] = []
    for source_index, item in enumerate(drawables):
        if not isinstance(item, dict):
            continue
        value = _normalize_drawable(item)
        if value is None:
            continue
        value["source_index"] = source_index
        normalized.append(value)
    if not normalized:
        raise PsdReconstructionError("No usable drawable mesh entries were found.")

    texture_pixels = texture_pixels if texture_pixels is not None else sum(
        texture.shape[0] * texture.shape[1] for texture in textures
    )
    width = max(texture.shape[1] for texture in textures)
    height = max(texture.shape[0] for texture in textures)
    _validate_canvas_size((width, height), limits, "PSD export")

    # First build coverage masks.  Besides providing exact crop bounds this
    # gives callers a useful report when several ArtMeshes share atlas texels.
    footprints: list[dict[str, Any]] = []
    for drawable in normalized:
        texture_index = int(drawable.get("texture_index", 0))
        if texture_index < 0 or texture_index >= len(textures):
            continue
        texture = textures[texture_index]
        points = _uv_to_texture_points(
            np.asarray(drawable["uvs"], dtype=np.float32), texture
        )
        mask = np.zeros(texture.shape[:2], dtype=np.uint8)
        for tri in _iter_triangles(drawable["indices"]):
            if max(tri) >= len(points):
                continue
            tri_points = points[list(tri)]
            if not np.all(np.isfinite(tri_points)):
                continue
            _fill_mesh_triangle_mask(cv2, mask, tri_points)
        bbox = _mask_bbox(mask)
        if bbox is None:
            # Preserve a valid but sub-pixel/degenerate fragment as a one-pixel
            # overlay rather than silently dropping it.
            bbox = _points_pixel_bbox(points, texture.shape[1], texture.shape[0])
            if bbox is None:
                continue
            x, y, w, h = bbox
            mask[y : y + h, x : x + w] = 255
        footprints.append(
            {
                "drawable": drawable,
                "texture_index": texture_index,
                "points": points,
                "mask": mask,
                "bbox": bbox,
            }
        )

    if not footprints:
        raise PsdReconstructionError("No valid ArtMesh UV footprints were found.")
    planned_pixels = sum(int(item["bbox"][2]) * int(item["bbox"][3]) for item in footprints)
    _validate_layer_budget(
        len(footprints), planned_pixels, texture_pixels, limits, "PSD export"
    )

    shared_regions: list[dict[str, Any]] = []
    for index, first in enumerate(footprints):
        for second in footprints[index + 1 :]:
            if first["texture_index"] != second["texture_index"]:
                continue
            # Each footprint mask has the full atlas shape because it is also
            # used below to crop the corresponding layer.  Do not perform a
            # full-atlas boolean AND for every pair: a model with many
            # drawables turns that into O(drawables² * atlas_pixels) work and
            # can retain/allocate gigabytes of temporary arrays.  The bboxes
            # are exact bounds of the non-zero masks, so only their
            # intersection can contain a shared pixel.
            first_left, first_top, first_width, first_height = first["bbox"]
            second_left, second_top, second_width, second_height = second["bbox"]
            left = max(int(first_left), int(second_left))
            top = max(int(first_top), int(second_top))
            right = min(
                int(first_left) + int(first_width),
                int(second_left) + int(second_width),
            )
            bottom = min(
                int(first_top) + int(first_height),
                int(second_top) + int(second_height),
            )
            if right <= left or bottom <= top:
                continue
            first_mask = first["mask"][top:bottom, left:right]
            second_mask = second["mask"][top:bottom, left:right]
            overlap = (first_mask > 0) & (second_mask > 0)
            region = _mask_region(
                overlap,
                int(first["texture_index"]),
                f"{first['drawable']['id']}|{second['drawable']['id']}",
            )
            if region:
                region["bbox"][0] += left
                region["bbox"][1] += top
                region["kind"] = "shared-atlas-region"
                region["drawables"] = [
                    str(first["drawable"]["id"]),
                    str(second["drawable"]["id"]),
                ]
                shared_regions.append(region)

    layers: list[PsdLayer] = []
    layer_metadata: list[dict[str, Any]] = []
    used_names: dict[str, int] = {}
    total = max(1, len(footprints))
    for index, item in enumerate(footprints):
        drawable = item["drawable"]
        texture_index = int(item["texture_index"])
        texture = textures[texture_index]
        x, y, w, h = [int(value) for value in item["bbox"]]
        crop = texture[y : y + h, x : x + w].copy()
        local_mask = item["mask"][y : y + h, x : x + w] == 0
        crop[:, :, 3] = np.where(local_mask, 0, crop[:, :, 3]).astype(np.uint8)
        base_name = _safe_layer_name(
            f"tex{texture_index:02d}_{drawable['id']}_artmesh"
        )
        occurrence = used_names.get(base_name, 0)
        used_names[base_name] = occurrence + 1
        layer_name = base_name if occurrence == 0 else f"{base_name}_{occurrence}"
        layers.append(
            PsdLayer(layer_name, Image.fromarray(crop, "RGBA"), x, y)
        )
        layer_metadata.append(
            {
                "kind": "atlas-artmesh",
                "name": layer_name,
                "drawable_id": drawable["id"],
                "source_index": int(drawable.get("source_index", index)),
                "texture_index": texture_index,
                "texture_name": paths[texture_index].name,
                "left": x,
                "top": y,
                "bbox": [x, y, w, h],
                "atlas_uv_bbox": [x, y, w, h],
                "vertices": drawable["vertices"],
                "uvs": drawable["uvs"],
                "indices": drawable["indices"],
                "opacity": drawable.get("opacity", 1.0),
                "visible": bool(drawable.get("visible", True)),
                "dynamic_flags": int(drawable.get("dynamic_flags", 1)),
                "render_order": drawable.get("render_order", 0),
                "draw_order": drawable.get("draw_order", 0),
            }
        )
        _emit(progress, 20 + int((index + 1) / total * 70), f"Extracted {drawable['id']}")

    return layers, (width, height), layer_metadata, shared_regions


def _fill_mesh_triangle_mask(cv2, mask: np.ndarray, points: np.ndarray) -> None:
    points = points.astype(np.float32)
    x0 = max(0, int(math.floor(float(np.min(points[:, 0])))))
    y0 = max(0, int(math.floor(float(np.min(points[:, 1])))))
    x1 = min(mask.shape[1], int(math.ceil(float(np.max(points[:, 0])))) + 1)
    y1 = min(mask.shape[0], int(math.ceil(float(np.max(points[:, 1])))) + 1)
    if x1 <= x0 or y1 <= y0:
        return
    shifted = np.rint(points - np.asarray([x0, y0], dtype=np.float32)).astype(np.int32)
    cv2.fillConvexPoly(mask[y0:y1, x0:x1], shifted, 255, lineType=cv2.LINE_8)


def _mask_bbox(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    coords = np.argwhere(np.asarray(mask) > 0)
    if coords.size == 0:
        return None
    top, left = coords.min(axis=0)
    bottom, right = coords.max(axis=0)
    return int(left), int(top), int(right - left + 1), int(bottom - top + 1)


def _points_pixel_bbox(
    points: np.ndarray,
    width: int,
    height: int,
) -> tuple[int, int, int, int] | None:
    finite = points[np.all(np.isfinite(points), axis=1)]
    if len(finite) == 0:
        return None
    left = max(0, min(width - 1, int(math.floor(float(np.min(finite[:, 0]))))))
    top = max(0, min(height - 1, int(math.floor(float(np.min(finite[:, 1]))))))
    right = max(left + 1, min(width, int(math.ceil(float(np.max(finite[:, 0]))) + 1)))
    bottom = max(top + 1, min(height, int(math.ceil(float(np.max(finite[:, 1]))) + 1)))
    if right <= left or bottom <= top:
        return None
    return left, top, right - left, bottom - top


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

    # Keep interpolation taps outside the triangle's own bounding box.  Without
    # this padding, pixels along a shared mesh edge sample the transparent
    # border of the temporary crop rather than the neighbouring atlas texels.
    sx0, sy0 = max(0, sx - 2), max(0, sy - 2)
    sx1, sy1 = min(src.shape[1], sx + sw + 2), min(src.shape[0], sy + sh + 2)
    dx0, dy0 = max(0, dx - 1), max(0, dy - 1)
    dx1, dy1 = min(dst.shape[1], dx + dw + 1), min(dst.shape[0], dy + dh + 1)

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

    # Internal triangle edges are not visible object edges.  Anti-aliasing each
    # triangle separately blends two half-covered pixels and leaves a bright or
    # transparent seam.  A hard, unexpanded mask assigns every shared-edge
    # pixel to at least one triangle.  The completed high-resolution drawable
    # is downsampled as one image later, which provides anti-aliasing only at
    # the actual ArtMesh silhouette.
    mask = np.zeros((dy1 - dy0, dx1 - dx0), dtype=np.uint8)
    cv2.fillConvexPoly(
        mask,
        np.rint(dst_shift).astype(np.int32),
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
    """Rasterize one ArtMesh with a single UV map.

    Sampling each triangle into a transparent temporary crop makes interpolation
    read that crop border at shared edges.  Compositing anti-aliased triangle
    masks then turns two half-covered edges into a visible seam.  Building one
    destination-to-texture map avoids both effects: every covered pixel is
    sampled once from the complete atlas.  The completed high-resolution
    drawable is resized as one image later, so only its outer silhouette is
    anti-aliased.
    """
    height, width = layer.shape[:2]
    map_x = np.full((height, width), -1.0, dtype=np.float32)
    map_y = np.full((height, width), -1.0, dtype=np.float32)
    coverage = np.zeros((height, width), dtype=bool)
    fallback_triangles: list[tuple[np.ndarray, np.ndarray]] = []

    for tri in _iter_triangles(indices):
        if max(tri) >= len(vertices) or max(tri) >= len(uvs):
            continue
        dst_tri = np.asarray(vertices[list(tri)], dtype=np.float32)
        src_tri = np.asarray(uvs[list(tri)], dtype=np.float32)
        if not np.all(np.isfinite(dst_tri)) or not np.all(np.isfinite(src_tri)):
            continue
        edge_a = dst_tri[1] - dst_tri[0]
        edge_b = dst_tri[2] - dst_tri[0]
        determinant = float(edge_a[0] * edge_b[1] - edge_a[1] * edge_b[0])
        if abs(determinant) < 1e-5:
            fallback_triangles.append((src_tri, dst_tri))
            continue

        x0 = max(0, int(math.floor(float(np.min(dst_tri[:, 0])))))
        y0 = max(0, int(math.floor(float(np.min(dst_tri[:, 1])))))
        x1 = min(width, int(math.ceil(float(np.max(dst_tri[:, 0])))) + 1)
        y1 = min(height, int(math.ceil(float(np.max(dst_tri[:, 1])))) + 1)
        if x1 <= x0 or y1 <= y0:
            continue

        shifted = dst_tri - np.asarray([x0, y0], dtype=np.float32)
        hard_mask = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
        points = np.rint(shifted).astype(np.int32)
        cv2.fillConvexPoly(hard_mask, points, 255, lineType=cv2.LINE_8)
        selected = hard_mask > 0
        if not np.any(selected):
            continue

        inverse = cv2.getAffineTransform(dst_tri, src_tri)
        rows, columns = np.ogrid[y0:y1, x0:x1]
        source_x = (
            inverse[0, 0] * columns
            + inverse[0, 1] * rows
            + inverse[0, 2]
        ).astype(np.float32)
        source_y = (
            inverse[1, 0] * columns
            + inverse[1, 1] * rows
            + inverse[1, 2]
        ).astype(np.float32)

        roi_x = map_x[y0:y1, x0:x1]
        roi_y = map_y[y0:y1, x0:x1]
        roi_coverage = coverage[y0:y1, x0:x1]
        roi_x[selected] = source_x[selected]
        roi_y[selected] = source_y[selected]
        roi_coverage[selected] = True

    if np.any(coverage):
        sampled = cv2.remap(
            texture,
            map_x,
            map_y,
            interpolation=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(0, 0, 0, 0),
        )
        layer[coverage] = sampled[coverage]
    for src_tri, dst_tri in fallback_triangles:
        _warp_triangle(cv2, texture, layer, src_tri, dst_tri)


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
        # An empty ordinary mask hides the drawable.  An empty inverted mask
        # is the complement of zero coverage, so it must preserve the source
        # alpha instead of clearing the whole layer.
        if drawable.get("inverted_mask"):
            return
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


def _resize_rgba_premultiplied(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Resize RGBA without pulling transparent black into visible edge pixels."""
    if image.size == size:
        return image
    rgba = np.asarray(image.convert("RGBA"), dtype=np.float32)
    alpha = rgba[:, :, 3:4] / 255.0
    premultiplied = np.concatenate((rgba[:, :, :3] * alpha, rgba[:, :, 3:4]), axis=2)
    resized = np.asarray(
        Image.fromarray(np.clip(premultiplied, 0, 255).astype(np.uint8), "RGBA").resize(
            size,
            Image.Resampling.LANCZOS,
        ),
        dtype=np.float32,
    )
    out_alpha = resized[:, :, 3:4]
    rgb = np.divide(
        resized[:, :, :3] * 255.0,
        np.maximum(out_alpha, 1.0),
        out=np.zeros_like(resized[:, :, :3]),
        where=out_alpha > 0,
    )
    output = np.concatenate((np.clip(rgb, 0, 255), np.clip(out_alpha, 0, 255)), axis=2)
    return Image.fromarray(output.astype(np.uint8), "RGBA")


def _build_seam_protection_layers(
    clean_composite: Image.Image,
    size: tuple[int, int],
    layers: list[PsdLayer],
) -> tuple[PsdLayer | None, PsdLayer | None]:
    """Build small non-repack layers that restore composite-after-resize output."""
    resized_stack = Image.new("RGBA", size, (0, 0, 0, 0))
    for layer in layers:
        resized_stack.alpha_composite(
            layer.image,
            dest=(int(layer.left), int(layer.top)),
        )

    target = np.asarray(clean_composite.convert("RGBA"), dtype=np.float32) / 255.0
    front = np.asarray(resized_stack, dtype=np.float32) / 255.0
    target_alpha = target[:, :, 3:4]
    front_alpha = front[:, :, 3:4]
    remaining = 1.0 - front_alpha

    under_alpha = np.divide(
        target_alpha - front_alpha,
        np.maximum(remaining, 1e-6),
        out=np.zeros_like(target_alpha),
        where=remaining > 1e-6,
    )
    under_alpha = np.clip(under_alpha, 0.0, 1.0)

    target_premultiplied = target[:, :, :3] * target_alpha
    front_premultiplied = front[:, :, :3] * front_alpha
    under_premultiplied = np.divide(
        target_premultiplied - front_premultiplied,
        np.maximum(remaining, 1e-6),
        out=np.zeros_like(target_premultiplied),
        where=remaining > 1e-6,
    )
    under_rgb = np.divide(
        under_premultiplied,
        np.maximum(under_alpha, 1e-6),
        out=np.zeros_like(under_premultiplied),
        where=under_alpha > 1e-6,
    )

    under_correction = np.concatenate(
        (np.clip(under_rgb, 0.0, 1.0), under_alpha),
        axis=2,
    )
    under_correction[under_alpha[:, :, 0] < (1.0 / 255.0)] = 0.0
    underlay = _crop_protection_layer(
        under_correction,
        "__Seam protection underlay - do not edit__",
    )

    # Where both composites are already opaque, an underlay cannot alter RGB.
    # Find the least-opaque source-over colour that transforms the separately
    # resized stack back to the clean composite.  This keeps the correction as
    # transparent as possible instead of covering editable artwork wholesale.
    front_rgb = front[:, :, :3]
    target_rgb = target[:, :, :3]
    delta = target_rgb - front_rgb
    brighter = np.divide(
        np.maximum(delta, 0.0),
        np.maximum(1.0 - front_rgb, 1e-6),
    )
    darker = np.divide(
        np.maximum(-delta, 0.0),
        np.maximum(front_rgb, 1e-6),
    )
    overlay_alpha = np.max(np.maximum(brighter, darker), axis=2, keepdims=True)
    opaque = (target_alpha > 0.995) & (front_alpha > 0.995)
    visible_delta = np.max(np.abs(delta), axis=2, keepdims=True) > (2.0 / 255.0)
    overlay_alpha = np.where(opaque & visible_delta, overlay_alpha, 0.0)
    overlay_alpha = np.clip(overlay_alpha, 0.0, 1.0)
    overlay_rgb = np.divide(
        target_rgb - front_rgb * (1.0 - overlay_alpha),
        np.maximum(overlay_alpha, 1e-6),
        out=np.zeros_like(target_rgb),
        where=overlay_alpha > 1e-6,
    )
    overlay_correction = np.concatenate(
        (np.clip(overlay_rgb, 0.0, 1.0), overlay_alpha),
        axis=2,
    )
    overlay = _crop_protection_layer(
        overlay_correction,
        "__Seam protection overlay - hide for edge edits__",
    )
    return underlay, overlay


def _crop_protection_layer(
    correction: np.ndarray,
    name: str,
) -> PsdLayer | None:
    rgba = np.clip(correction * 255.0, 0, 255).astype(np.uint8)
    bbox = _alpha_bbox(rgba)
    if bbox is None:
        return None
    left, top, width, height = bbox
    image = Image.fromarray(rgba[top : top + height, left : left + width], "RGBA")
    return PsdLayer(name, image, left, top)


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
    *,
    flat_layers: bool = False,
) -> None:
    from psd_tools import PSDImage
    from psd_tools.api.layers import PixelLayer
    from psd_tools.constants import BlendMode, Compression

    limits = resource_limits or PsdResourceLimits()
    _validate_canvas_size(size, limits, "PSD export")
    _validate_layer_budget(
        len(layers), _layers_pixel_count(layers), 0, limits, "PSD export"
    )
    psd = PSDImage.new("RGBA", size)
    compression = Compression.RAW
    from psd_tools.api.layers import Group
    from psd_tools.constants import Tag

    semantic_groups: dict[str, Any] = {}
    unit_groups: dict[str, Any] = {}

    def make_group(parent: Any, name: str) -> Any:
        group = Group.new(parent=parent, name=name, open_folder=True)
        parent.append(group)
        return group

    def set_layer_id(layer: Any, value: int) -> None:
        # psd-tools creates layer IDs as -1.  Assigning the standard tagged
        # block gives us a real persistent PSD layer ID that survives save,
        # rename, and reopening in Photoshop.
        try:
            layer.tagged_blocks.set_data(Tag.LAYER_ID, int(value))
        except Exception:
            pass

    def set_blend_mode(pixel: Any, mode: str) -> None:
        # Cubism's compatibility multiply uses the same RGB blend as PSD
        # Multiply over the opaque artwork below it.  Treating a shadow mesh
        # as Normal replaces that artwork with the shadow texture instead.
        # Retain the blend on the actual ArtMesh layer so it stays editable
        # and keeps its normal texture-space reverse mapping.
        modes = {
            "normal": BlendMode.NORMAL,
            "add-compatible": BlendMode.LINEAR_DODGE,
            "multiply-compatible": BlendMode.MULTIPLY,
            "add": BlendMode.LINEAR_DODGE,
            "darken": BlendMode.DARKEN,
            "multiply": BlendMode.MULTIPLY,
            "color-burn": BlendMode.COLOR_BURN,
            "linear-burn": BlendMode.LINEAR_BURN,
            "lighten": BlendMode.LIGHTEN,
            "screen": BlendMode.SCREEN,
            "color-dodge": BlendMode.COLOR_DODGE,
            "overlay": BlendMode.OVERLAY,
            "soft-light": BlendMode.SOFT_LIGHT,
            "hard-light": BlendMode.HARD_LIGHT,
            "linear-light": BlendMode.LINEAR_LIGHT,
            "hue": BlendMode.HUE,
            "color": BlendMode.COLOR,
        }
        pixel.blend_mode = modes.get(str(mode).lower(), BlendMode.NORMAL)

    # Every edit unit receives its own Original/Paint/AI_Edit anchors.  A
    # semantic group, when present, is only an outer organization layer and is
    # never used as the binding identity.
    for index, layer in enumerate(layers):
        if flat_layers:
            # Input order is Cubism's back-to-front render order.  Grouping
            # nonadjacent drawables by part changes that order and occlusion.
            pixel = PixelLayer.frompil(
                layer.image, psd, name=layer.name, top=layer.top,
                left=layer.left, compression=compression,
            )
            set_layer_id(pixel, 100000 + index * 10 + 5)
            set_blend_mode(pixel, layer.blend_mode)
            continue
        unit_name = str(layer.unit_id or layer.name)
        semantic_name = str(layer.group or "").strip()
        if semantic_name and semantic_name not in semantic_groups:
            semantic_groups[semantic_name] = make_group(psd, semantic_name)
        outer = semantic_groups.get(semantic_name, psd)
        # Duplicate names are legal in PSD, but deterministic suffixes make
        # fallback binding unambiguous when a layer ID is unavailable.
        unit_key = f"{unit_name}#{index}"
        unit = make_group(outer, unit_name)
        unit_groups[unit_key] = unit
        set_layer_id(unit, 100000 + index * 10 + 1)
        anchors: dict[str, Any] = {}
        for offset, role in enumerate(PSD_BINDING_GROUPS, start=2):
            anchor = make_group(unit, role)
            set_layer_id(anchor, 100000 + index * 10 + offset)
            anchors[role] = anchor
            anchors[PSD_GROUP_ORIGINAL].visible = True
        role = str(layer.role or PSD_GROUP_PAINT)
        if role not in anchors:
            role = PSD_GROUP_PAINT
        pixel = PixelLayer.frompil(
            layer.image,
            anchors[role],
            name=layer.name,
            top=layer.top,
            left=layer.left,
            compression=compression,
        )
        set_layer_id(pixel, 100000 + index * 10 + 5)
        set_blend_mode(pixel, layer.blend_mode)

    total = max(1, len(layers))
    step = max(1, total // 40)
    for index, layer in enumerate(layers, start=1):
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
                "rgba_sha256": _rgba_sha256(texture),
            }
        )

    metadata = {
        "format": METADATA_FORMAT,
        "version": METADATA_VERSION,
        "mode": mode,
        "source_model": str(source.model_json),
        "source_root": str(source.root_dir),
        "source_model_summary": _file_summary(source.model_json),
        "source_moc_summary": _file_summary(source.moc3),
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
    source_root_value = metadata.get("source_root")
    if isinstance(source_root_value, str) and source_root_value:
        source_root = Path(source_root_value).resolve()
    source_model = metadata.get("source_model")
    if source_root is None and isinstance(source_model, str) and source_model:
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
                "rgba_sha256": str(item.get("rgba_sha256") or ""),
            }
        )
    if not normalized:
        raise PsdReconstructionError("PSD metadata texture entries are invalid.")
    name_counts: dict[str, int] = {}
    for item in normalized:
        name_counts[item["name"].casefold()] = name_counts.get(item["name"].casefold(), 0) + 1
    for item in normalized:
        if name_counts[item["name"].casefold()] > 1:
            item["output_name"] = f"texture_{int(item['index']):02d}_{item['name']}"
        else:
            item["output_name"] = item["name"]
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
        is_group = bool(getattr(layer, "is_group", lambda: False)())
        name = str(layer.name)
        if not is_group:
            layers.setdefault(name, layer)
        layer_id = int(getattr(layer, "layer_id", 0) or 0)
        if layer_id:
            layers.setdefault(f"@id:{layer_id}", layer)
        binding_path = _layer_binding_path(layer)
        if binding_path:
            layers.setdefault(f"@path:{binding_path}", layer)
    return layers


def _find_bound_psd_layer(psd, indexed: Mapping[str, Any], layer_info: Mapping[str, Any]) -> Any | None:
    """Resolve a PSD layer by durable identity, then strict name fallback."""
    # A name-only metadata record belongs to the legacy flat format.  The
    # index maps that name to the first pixel layer, but the strict overlay
    # convention below may contain several layers which must be composited as
    # one actual PSD stack.  Durable IDs/paths are safe to resolve directly.
    durable = bool(
        int(layer_info.get("unit_group_id", 0) or 0)
        or int(layer_info.get("layer_id", 0) or 0)
        or str(layer_info.get("binding_path") or "")
    )
    if durable:
        layer = _find_bound_psd_layer_from_index(indexed, layer_info)
        if layer is not None:
            return layer

    expected_name = str(layer_info.get("name") or "")
    if not expected_name:
        return None
    # Photoshop overlay conventions use exactly `originalName_123`.  Collect
    # the exact original and every numeric overlay in PSD stack order.  Do not
    # accept arbitrary prefixes/suffixes, which can silently bind a different
    # artist layer with a similar name.
    pattern = re.compile(rf"^{re.escape(expected_name)}_([0-9]+)$")
    candidates = [
        candidate
        for candidate in psd.descendants()
        if not getattr(candidate, "is_group", lambda: False)()
        and (
            str(getattr(candidate, "name", "")) == expected_name
            or pattern.fullmatch(str(getattr(candidate, "name", "")))
        )
    ]

    if not candidates:
        return None
    # Legacy exports may not carry our persistent group ID.  If the exact
    # layer or a strict numeric overlay belongs to an edit unit, bind the
    # unit so all visible Original/Paint/AI_Edit children are composited.
    for candidate in candidates:
        unit = _edit_unit_group(candidate)
        if unit is not None:
            return unit
    if len(candidates) > 1:
        return _BoundPsdLayers(candidates)
    role = str(layer_info.get("psd_group") or PSD_GROUP_PAINT)
    role_candidates = [item for item in candidates if _top_level_group_name(item) == role]
    return (role_candidates or candidates)[-1]


def _find_bound_psd_layer_from_index(indexed: Mapping[str, Any], layer_info: Mapping[str, Any]) -> Any | None:
    unit_group_id = int(layer_info.get("unit_group_id", 0) or 0)
    if unit_group_id:
        layer = indexed.get(f"@id:{unit_group_id}")
        if layer is not None and bool(getattr(layer, "is_group", lambda: False)()):
            return layer
    layer_id = int(layer_info.get("layer_id", 0) or 0)
    if layer_id:
        layer = indexed.get(f"@id:{layer_id}")
        if layer is not None:
            return layer
    binding_path = str(layer_info.get("binding_path") or "")
    if binding_path:
        layer = indexed.get(f"@path:{binding_path}")
        if layer is not None:
            return layer
    name = str(layer_info.get("name") or "")
    return indexed.get(name)


def _layer_effectively_visible(layer: Any) -> bool:
    checker = getattr(layer, "is_visible", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            pass
    current = layer
    while current is not None:
        if not bool(getattr(current, "visible", True)):
            return False
        current = getattr(current, "parent", None)
    return True


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


def _texture_output_name(texture: Mapping[str, Any]) -> str:
    return _safe_output_name(
        str(texture.get("output_name") or texture.get("name") or "texture.png")
    )


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
