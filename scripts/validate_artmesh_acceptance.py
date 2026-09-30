"""Run a real Live2D ArtMesh UV-atlas PSD acceptance test.

The command is intentionally separate from the no-edit round-trip validator.
It exports one real model in ``atlas-artmesh`` mode, performs an exact no-op
repack, then edits the PSD itself (one color edit, one alpha erase, and one
new Paint overlay layer) before repacking again.  Every artifact is written to
a new output directory; source files and an optional original LPK are only
hashed and read.

Example::

    pixi run python scripts/validate_artmesh_acceptance.py \
        --model runtime/validation/native_live2d/.../model0.json \
        --lpk E:/SteamLibrary/steamapps/common/Live2DViewerEX/shared/workshop/2754242023/2754242023.lpk \
        --cubism-core E:/SteamLibrary/steamapps/common/Live2DViewerEX/bin/exstudio/exstudio_Data/Plugins/x86_64/Live2DCubismCore.dll \
        --output runtime/validation/artmesh_acceptance

The command uses the source atlas at its original resolution.  It does not
export a pose or infer parts from connected components: ArtMesh UV triangles
are the coverage source, including hidden/tiny drawable metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.psd_reconstructor import (  # noqa: E402
    PsdReconstructionError,
    reconstruct_live2d_psd,
    repack_atlas_png_from_psd,
    resolve_live2d_source,
)


TINY_BBOX_AREA = 2048
REPORT_FORMAT = "LpkUnpacker.ArtMeshAcceptance"
REPORT_VERSION = 1


class AcceptanceError(RuntimeError):
    """Raised when the acceptance setup or an exact assertion fails."""


def _safe_component(value: str) -> str:
    component = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value)).strip("._")
    return component or "input"


def _ensure_fresh_output(output: Path, inputs: Iterable[Path]) -> Path:
    output = output.expanduser().resolve()
    for source in inputs:
        source = source.expanduser().resolve()
        source_root = source if source.is_dir() else source.parent
        if output == source_root or source_root in output.parents or output in source_root.parents:
            raise AcceptanceError(
                f"Output directory must be independent of source root {source_root}: {output}"
            )
    if output.exists() and any(output.iterdir()):
        raise AcceptanceError(f"Output directory must be new or empty; refusing to reuse {output}")
    output.mkdir(parents=True, exist_ok=True)
    return output


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_hashes(paths: Iterable[Path]) -> dict[str, str]:
    return {str(path.resolve()): _sha256_file(path) for path in paths if path.is_file()}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise AcceptanceError(f"Failed to read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AcceptanceError(f"JSON root must be an object: {path}")
    return value


def _compare_rgba(source: Path, output: Path) -> tuple[int, np.ndarray, np.ndarray]:
    if not source.is_file():
        raise AcceptanceError(f"Source image does not exist: {source}")
    if not output.is_file():
        raise AcceptanceError(f"Output image does not exist: {output}")
    with Image.open(source) as source_image:
        source_rgba = np.asarray(source_image.convert("RGBA"), dtype=np.uint8).copy()
    with Image.open(output) as output_image:
        output_rgba = np.asarray(output_image.convert("RGBA"), dtype=np.uint8).copy()
    if source_rgba.shape != output_rgba.shape:
        raise AcceptanceError(
            f"RGBA size mismatch for {source} -> {output}: "
            f"{source_rgba.shape} != {output_rgba.shape}"
        )
    changed = np.any(source_rgba != output_rgba, axis=2)
    return int(changed.sum()), source_rgba, output_rgba


def _find_sidecar(export_dir: Path, model: Path) -> Path | None:
    candidates = [
        export_dir / f"{model.stem}.drawables.json",
        export_dir / "drawables.json",
    ]
    return next((path for path in candidates if path.is_file()), None)


def _is_group(layer: Any) -> bool:
    checker = getattr(layer, "is_group", None)
    return bool(checker()) if callable(checker) else False


def _find_layer_by_id(psd: Any, layer_id: int, *, group: bool | None = None) -> Any | None:
    for layer in psd.descendants():
        if int(getattr(layer, "layer_id", 0) or 0) != int(layer_id):
            continue
        if group is None or _is_group(layer) == group:
            return layer
    return None


def _find_named_child(parent: Any, name: str, *, group: bool | None = None) -> Any | None:
    for child in parent:
        if str(getattr(child, "name", "")) != name:
            continue
        if group is None or _is_group(child) == group:
            return child
    return None


def _select_edit_target(metadata: Mapping[str, Any], metadata_path: Path) -> tuple[dict[str, Any], np.ndarray]:
    candidates: list[tuple[int, dict[str, Any], np.ndarray]] = []
    for item in metadata.get("layers", []):
        if not isinstance(item, dict):
            continue
        baseline_name = str(item.get("baseline_path") or "")
        if not baseline_name:
            continue
        baseline_path = (metadata_path.parent / baseline_name).resolve()
        if not baseline_path.is_file():
            continue
        with Image.open(baseline_path) as image:
            baseline = np.asarray(image.convert("RGBA"), dtype=np.uint8).copy()
        alpha_count = int(np.count_nonzero(baseline[:, :, 3]))
        if alpha_count >= 3:
            candidates.append((alpha_count, item, baseline))
    if not candidates:
        raise AcceptanceError("No ArtMesh layer has at least three opaque pixels for the edit probe.")
    _, item, baseline = max(candidates, key=lambda value: value[0])
    return item, baseline


def _edit_psd(
    source_psd: Path,
    edited_psd: Path,
    metadata: Mapping[str, Any],
    metadata_path: Path,
) -> dict[str, Any]:
    from psd_tools import PSDImage
    from psd_tools.api.layers import PixelLayer
    from psd_tools.constants import Compression, Tag

    target_info, baseline = _select_edit_target(metadata, metadata_path)
    unit_group_id = int(target_info.get("unit_group_id", 0) or 0)
    if not unit_group_id:
        raise AcceptanceError(f"ArtMesh metadata has no durable unit group ID: {target_info.get('name')}")

    psd = PSDImage.open(str(source_psd))
    unit = _find_layer_by_id(psd, unit_group_id, group=True)
    if unit is None:
        raise AcceptanceError(f"PSD unit group not found: {unit_group_id}")
    original_group = _find_named_child(unit, "Original", group=True)
    paint_group = _find_named_child(unit, "Paint", group=True)
    if original_group is None or paint_group is None:
        raise AcceptanceError(f"PSD ArtMesh edit anchors are missing for {target_info.get('name')}")

    layer_name = str(target_info.get("name") or "")
    original_pixel = _find_named_child(original_group, layer_name, group=False)
    if original_pixel is None:
        raise AcceptanceError(f"PSD Original pixel layer not found: {layer_name}")
    original_image = original_pixel.topil()
    if original_image is None:
        raise AcceptanceError(f"PSD Original layer has no pixels: {layer_name}")
    edited = np.asarray(original_image.convert("RGBA"), dtype=np.uint8).copy()
    if edited.shape != baseline.shape:
        raise AcceptanceError(
            f"PSD Original layer size differs from immutable baseline for {layer_name}: "
            f"{edited.shape} != {baseline.shape}"
        )

    coords = np.argwhere(edited[:, :, 3] > 0)
    if len(coords) < 3:
        raise AcceptanceError(f"PSD Original layer became too transparent for edit probe: {layer_name}")
    color_y, color_x = [int(value) for value in coords[len(coords) // 5]]
    erase_y, erase_x = [int(value) for value in coords[(len(coords) * 3) // 5]]
    overlay_y, overlay_x = [int(value) for value in coords[(len(coords) * 4) // 5]]
    if len({(color_y, color_x), (erase_y, erase_x), (overlay_y, overlay_x)}) != 3:
        raise AcceptanceError("Edit probe selected duplicate ArtMesh pixels")

    original_color = edited[color_y, color_x].tolist()
    original_erase = edited[erase_y, erase_x].tolist()
    color_value = [255, 0, 255, int(original_color[3])]
    if color_value == original_color:
        color_value = [0, 255, 255, int(original_color[3])]
    overlay_value = [0, 255, 255, 255]
    edited[color_y, color_x] = np.asarray(color_value, dtype=np.uint8)
    # Photoshop stores the RGB channels of a fully transparent pixel even
    # when an eraser sets its alpha to zero.  The acceptance assertion checks
    # the alpha transition and records those preserved RGB bytes explicitly.
    edited[erase_y, erase_x] = np.asarray(
        [int(original_erase[0]), int(original_erase[1]), int(original_erase[2]), 0],
        dtype=np.uint8,
    )

    original_top = int(getattr(original_pixel, "top", target_info.get("top", 0)))
    original_left = int(getattr(original_pixel, "left", target_info.get("left", 0)))
    original_group.remove(original_pixel)
    replacement = PixelLayer.frompil(
        Image.fromarray(edited, "RGBA"),
        original_group,
        name=layer_name,
        top=original_top,
        left=original_left,
        compression=Compression.RAW,
    )
    try:
        replacement.tagged_blocks.set_data(Tag.LAYER_ID, int(target_info.get("pixel_layer_id", 0) or 0))
    except Exception:
        pass

    # A one-pixel opaque Paint child exercises the real PSD stack and keeps
    # the output probe small even when the selected ArtMesh has a large bbox.
    overlay_image = Image.new("RGBA", (1, 1), tuple(overlay_value))
    overlay_left = original_left + overlay_x
    overlay_top = original_top + overlay_y
    PixelLayer.frompil(
        overlay_image,
        paint_group,
        name="acceptance_overlay_new_layer",
        top=overlay_top,
        left=overlay_left,
        compression=Compression.RAW,
    )
    psd.save(str(edited_psd))

    return {
        "layer": layer_name,
        "texture_index": int(target_info.get("texture_index", 0)),
        "atlas_origin": [original_left, original_top],
        "local_pixels": {
            "color": [color_x, color_y],
            "erase": [erase_x, erase_y],
            "overlay": [overlay_x, overlay_y],
        },
        "atlas_pixels": {
            "color": [original_left + color_x, original_top + color_y],
            "erase": [original_left + erase_x, original_top + erase_y],
            "overlay": [overlay_left, overlay_top],
        },
        "before": {
            "color": original_color,
            "erase": original_erase,
        },
        "after": {
            "color": color_value,
            "erase": [int(original_erase[0]), int(original_erase[1]), int(original_erase[2]), 0],
            "overlay": overlay_value,
        },
        "overlay_layer": "acceptance_overlay_new_layer",
    }


def _assert_edit_output(
    source_image: np.ndarray,
    output_image: np.ndarray,
    edit: Mapping[str, Any],
) -> dict[str, Any]:
    if source_image.shape != output_image.shape:
        raise AcceptanceError(f"Edited atlas shape changed: {source_image.shape} != {output_image.shape}")
    changed = np.any(source_image != output_image, axis=2)
    height, width = changed.shape
    expected: dict[tuple[int, int], list[int]] = {}
    for name, value in dict(edit.get("atlas_pixels") or {}).items():
        point = [int(item) for item in value]
        x, y = point
        if x < 0 or y < 0 or x >= width or y >= height:
            raise AcceptanceError(f"Edit probe point is outside atlas: {name}={point}")
        expected[(x, y)] = [int(item) for item in dict(edit["after"])[name]]

    changed_points = {(int(x), int(y)) for y, x in np.argwhere(changed)}
    if changed_points != set(expected):
        missing = sorted(set(expected) - changed_points)
        unexpected = sorted(changed_points - set(expected))
        raise AcceptanceError(
            "Edited repack changed an unexpected pixel set: "
            f"missing={missing}, unexpected_count={len(unexpected)}, unexpected_head={unexpected[:10]}"
        )
    actual_values: dict[str, list[int]] = {}
    for name, value in dict(edit.get("atlas_pixels") or {}).items():
        point = (int(value[0]), int(value[1]))
        x, y = point
        actual = output_image[y, x].tolist()
        expected_value = expected[point]
        if name == "erase":
            # A fully transparent PSD pixel has unspecified RGB bytes after
            # compositing.  Alpha must be zero; record the decoder's RGB so
            # the report remains useful without treating transparent color as
            # a visible edit failure.
            expected_alpha = int(expected_value[3])
            if int(actual[3]) != expected_alpha:
                raise AcceptanceError(f"Edited alpha mismatch at {point}: {actual[3]} != {expected_alpha}")
        elif actual != expected_value:
            raise AcceptanceError(f"Edited pixel mismatch at {point}: {actual} != {expected_value}")
        actual_values[name] = actual
    return {
        "changed_pixels": int(changed.sum()),
        "expected_pixels": len(expected),
        "unaffected_pixels": int(changed.size - changed.sum()),
        "exact_changed_points": sorted([list(point) for point in changed_points]),
        "actual_changed_rgba": actual_values,
    }


def _metrics(metadata: Mapping[str, Any], sidecar: Mapping[str, Any] | None) -> dict[str, Any]:
    layers = [item for item in metadata.get("layers", []) if isinstance(item, dict)]
    textures = [item for item in metadata.get("textures", []) if isinstance(item, dict)]
    atlas_pixels = sum(int(item.get("width", 0)) * int(item.get("height", 0)) for item in textures)
    hidden = [
        item
        for item in layers
        if not bool(item.get("visible", True)) or not (int(item.get("dynamic_flags", 1)) & 1)
    ]
    tiny = [
        item
        for item in layers
        if isinstance(item.get("bbox"), (list, tuple))
        and len(item["bbox"]) >= 4
        and int(item["bbox"][2]) * int(item["bbox"][3]) <= TINY_BBOX_AREA
    ]
    shared = [item for item in metadata.get("shared_regions", []) if isinstance(item, dict)]
    shared_pixels = sum(int(item.get("pixel_count", 0)) for item in shared)
    raw_drawables = None
    if sidecar is not None and isinstance(sidecar.get("drawables"), list):
        raw_drawables = len(sidecar["drawables"])
    return {
        "raw_drawables": raw_drawables,
        "exported_artmesh_layers": len(layers),
        "skipped_drawables": (raw_drawables - len(layers)) if raw_drawables is not None else None,
        "hidden_drawables": len(hidden),
        "hidden_drawable_ids": [str(item.get("drawable_id") or item.get("name") or "") for item in hidden],
        "tiny_bbox_area_threshold": TINY_BBOX_AREA,
        "tiny_drawables": len(tiny),
        "tiny_drawable_ids": [str(item.get("drawable_id") or item.get("name") or "") for item in tiny],
        "texture_count": len(textures),
        "texture_sizes": [
            {"index": int(item.get("index", 0)), "width": int(item.get("width", 0)), "height": int(item.get("height", 0))}
            for item in textures
        ],
        "atlas_pixels": atlas_pixels,
        "shared_uv_regions": len(shared),
        "shared_uv_pairwise_pixels": shared_pixels,
        "shared_uv_pairwise_coverage_ratio": (shared_pixels / atlas_pixels) if atlas_pixels else 0.0,
        "shared_uv_regions_detail": shared,
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the real Live2D ArtMesh UV-atlas PSD acceptance test.")
    parser.add_argument("--model", required=True, type=Path, help="Live2D model3 JSON or an equivalent model JSON.")
    parser.add_argument("--lpk", type=Path, help="Optional original LPK; read and hash it to prove it was not modified.")
    parser.add_argument("--cubism-core", type=Path, help="Live2DCubismCore.dll used to export drawable metadata.")
    parser.add_argument(
        "--reuse-export",
        type=Path,
        help="Reuse an existing atlas-artmesh export directory and run only the no-edit/edit repacks.",
    )
    parser.add_argument("--output", required=True, type=Path, help="New independent output directory.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    started = time.perf_counter()
    report: dict[str, Any] = {
        "format": REPORT_FORMAT,
        "version": REPORT_VERSION,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "passed": False,
        "errors": [],
        "warnings": [],
    }
    previous_core = os.environ.get("LPK_CUBISM_CORE_DLL")
    try:
        model = args.model.expanduser().resolve()
        lpk = args.lpk.expanduser().resolve() if args.lpk else None
        core = args.cubism_core.expanduser().resolve() if args.cubism_core else None
        inputs = [model] + ([lpk] if lpk else [])
        if core:
            inputs.append(core)
        reuse_export = args.reuse_export.expanduser().resolve() if args.reuse_export else None
        for path in inputs:
            if not path.is_file():
                raise AcceptanceError(f"Input file does not exist: {path}")
        if reuse_export and not reuse_export.is_dir():
            raise AcceptanceError(f"Reuse export directory does not exist: {reuse_export}")
        output = _ensure_fresh_output(
            args.output,
            inputs + ([reuse_export] if reuse_export else []),
        )
        source = resolve_live2d_source(model)
        source_files = [source.model_json]
        if source.moc3:
            source_files.append(source.moc3)
        source_files.extend(source.textures)
        input_hashes_before = _file_hashes(source_files + ([lpk] if lpk else []))
        report["inputs"] = {
            "model": str(source.model_json),
            "moc3": str(source.moc3) if source.moc3 else None,
            "textures": [str(path) for path in source.textures],
            "lpk": str(lpk) if lpk else None,
            "sha256_before": input_hashes_before,
        }

        if core:
            os.environ["LPK_CUBISM_CORE_DLL"] = str(core)
        export_warnings: list[str] = []
        export_report_path: Path | None = None
        if reuse_export:
            if not reuse_export.is_dir():
                raise AcceptanceError(f"Reuse export directory does not exist: {reuse_export}")
            candidates = sorted(reuse_export.glob("*_atlas_artmesh.psd"))
            metadata_candidates = sorted(reuse_export.glob("*_atlas_artmesh.lpkpsd.json"))
            if len(candidates) != 1 or len(metadata_candidates) != 1:
                raise AcceptanceError(
                    "--reuse-export needs exactly one *_atlas_artmesh.psd and one "
                    "*_atlas_artmesh.lpkpsd.json in the directory"
                )
            export_psd = candidates[0]
            export_metadata = metadata_candidates[0]
        else:
            export_dir = output / "export"
            result = reconstruct_live2d_psd(model, export_dir, mode="atlas-artmesh")
            if result.mode != "atlas-artmesh":
                raise AcceptanceError(f"Exporter returned unexpected mode: {result.mode}")
            if result.metadata_path is None:
                raise AcceptanceError("Atlas ArtMesh export did not produce metadata")
            export_psd = result.psd_path
            export_metadata = result.metadata_path
            export_warnings = list(result.warnings)
            export_report_path = result.report_path
        metadata = _read_json(export_metadata)
        sidecar_path = _find_sidecar(export_psd.parent, source.model_json)
        sidecar = _read_json(sidecar_path) if sidecar_path else None
        metrics = _metrics(metadata, sidecar)
        if metrics["skipped_drawables"] not in (None, 0):
            raise AcceptanceError(f"Atlas ArtMesh export skipped drawable entries: {metrics['skipped_drawables']}")

        source_texture = source.textures[0]
        no_edit_dir = output / "no_edit"
        noop = repack_atlas_png_from_psd(export_psd, no_edit_dir, metadata_path=export_metadata)
        noop_output = noop.texture_outputs.get(0) or noop.output_paths[0]
        noop_changed, source_rgba, noop_rgba = _compare_rgba(source_texture, Path(noop_output))
        if noop_changed:
            raise AcceptanceError(f"No-edit atlas repack changed {noop_changed} RGBA pixels")

        edited_psd = output / "edited" / "artmesh-edited.psd"
        edited_psd.parent.mkdir(parents=True, exist_ok=True)
        edit = _edit_psd(export_psd, edited_psd, metadata, export_metadata)
        edited_dir = output / "edited" / "repack"
        edited_result = repack_atlas_png_from_psd(
            edited_psd,
            edited_dir,
            metadata_path=export_metadata,
        )
        edited_output = edited_result.texture_outputs.get(0) or edited_result.output_paths[0]
        with Image.open(edited_output) as image:
            edited_rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8).copy()
        edit_assertion = _assert_edit_output(source_rgba, edited_rgba, edit)

        input_hashes_after = _file_hashes(source_files + ([lpk] if lpk else []))
        if input_hashes_before != input_hashes_after:
            raise AcceptanceError("A source model, texture, MOC3, or LPK hash changed during validation")

        report.update(
            {
                "output_directory": str(output),
                "reused_export": bool(reuse_export),
                "export": {
                    "psd": str(export_psd),
                    "metadata": str(export_metadata),
                    "report": str(export_report_path) if export_report_path else None,
                    "warnings": export_warnings,
                },
                "metrics": metrics,
                "no_edit": {
                    "output": str(noop_output),
                    "changed_pixels": noop_changed,
                    "rgba_shape": list(noop_rgba.shape),
                    "status": "passed",
                },
                "edited": {
                    "psd": str(edited_psd),
                    "output": str(edited_output),
                    "edit": edit,
                    "repack_report": str(edited_result.report_path) if edited_result.report_path else None,
                    "assertion": edit_assertion,
                    "change_regions": edited_result.change_regions,
                    "conflicts": edited_result.conflicts,
                    "status": "passed",
                },
                "inputs": {
                    **report["inputs"],
                    "sha256_after": input_hashes_after,
                },
                "passed": True,
            }
        )
    except Exception as exc:
        report["errors"] = [f"{type(exc).__name__}: {exc}"]
    finally:
        if previous_core is None:
            os.environ.pop("LPK_CUBISM_CORE_DLL", None)
        else:
            os.environ["LPK_CUBISM_CORE_DLL"] = previous_core
        report["duration_seconds"] = round(time.perf_counter() - started, 3)

    output_path = Path(report.get("output_directory") or args.output.expanduser().resolve())
    output_path.mkdir(parents=True, exist_ok=True)
    report_path = output_path / "artmesh_acceptance_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(report_path)
    for error in report["errors"]:
        print(f"FAILED: {error}", file=sys.stderr)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
