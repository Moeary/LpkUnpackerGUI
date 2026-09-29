"""Validate no-edit PNG/PSD round-trips for Live2D and Spine inputs.

The command deliberately writes every artifact below a fresh output directory
and never uses a source directory as a repack destination.  It is intended for
acceptance runs against real unpacked samples, while the unit tests exercise it
with small generated fixtures.

Examples::

    pixi run python scripts/validate_edit_roundtrip.py \
        --live2d path/to/model.model3.json \
        --atlas path/to/atlas.atlas \
        --output runtime/acceptance/edit-roundtrip

    pixi run python scripts/validate_edit_roundtrip.py \
        --live2d path/to/model.model3.json \
        --cubism-core path/to/Live2DCubismCore.dll \
        --output runtime/acceptance/live2d-roundtrip
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.psd_reconstructor import (  # noqa: E402
    reconstruct_live2d_psd,
    repack_atlas_png_from_psd,
    resolve_live2d_source,
)
from app.core.spine_atlas import (  # noqa: E402
    extract_spine_atlas,
    writeback_spine_atlas,
)


class ValidationError(RuntimeError):
    """Raised when the validation setup or an image comparison is invalid."""


def _safe_component(value: str) -> str:
    component = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value)).strip("._")
    return component or "input"


def _ensure_fresh_output(output: Path, inputs: Iterable[Path]) -> Path:
    output = output.expanduser().resolve()
    for source in inputs:
        source = source.expanduser().resolve()
        source_root = source if source.is_dir() else source.parent
        if (
            output == source_root
            or source_root in output.parents
            or output in source_root.parents
        ):
            raise ValidationError(
                f"Output directory must be independent of source root {source_root}: {output}"
            )
    if output.exists() and any(output.iterdir()):
        raise ValidationError(
            f"Output directory must be new or empty; refusing to reuse {output}"
        )
    output.mkdir(parents=True, exist_ok=True)
    return output


def _fresh_case_dir(parent: Path, name: str) -> Path:
    case_dir = parent / _safe_component(name)
    if case_dir.exists():
        if any(case_dir.iterdir()):
            raise ValidationError(f"Validation case output is not empty: {case_dir}")
    else:
        case_dir.mkdir(parents=True)
    return case_dir


def _compare_rgba(source: Path, output: Path) -> int:
    if not source.is_file():
        raise ValidationError(f"Source image does not exist: {source}")
    if not output.is_file():
        raise ValidationError(f"Round-trip image does not exist: {output}")
    with Image.open(source) as source_image:
        source_rgba = np.asarray(source_image.convert("RGBA"), dtype=np.uint8)
    with Image.open(output) as output_image:
        output_rgba = np.asarray(output_image.convert("RGBA"), dtype=np.uint8)
    if source_rgba.shape != output_rgba.shape:
        raise ValidationError(
            f"RGBA size mismatch for {source} -> {output}: "
            f"{source_rgba.shape} != {output_rgba.shape}"
        )
    return int(np.any(source_rgba != output_rgba, axis=2).sum())


def _base_record(
    *,
    family: str,
    mode: str,
    source: Path,
    output: Path,
    started: float,
) -> dict[str, Any]:
    return {
        "family": family,
        "mode": mode,
        "source_path": str(source.resolve()),
        "output_directory": str(output.resolve()),
        "part_count": 0,
        "texture_count": 0,
        "source_paths": [],
        "output_paths": [],
        "duration_seconds": 0.0,
        "changed_pixels": 0,
        "warnings": [],
        "status": "failed",
    }


def _run_live2d_case(
    model: Path,
    mode: str,
    output: Path,
) -> dict[str, Any]:
    started = time.perf_counter()
    record = _base_record(
        family="live2d",
        mode=mode,
        source=model,
        output=output,
        started=started,
    )
    try:
        source = resolve_live2d_source(model)
        result = reconstruct_live2d_psd(model, output, mode=mode)
        if result.mode != mode:
            raise ValidationError(
                f"Requested Live2D mode {mode!r}, exporter produced {result.mode!r}"
            )
        repack_dir = output / "roundtrip"
        repacked = repack_atlas_png_from_psd(
            result.psd_path,
            repack_dir,
            metadata_path=result.metadata_path,
        )
        output_by_index = {
            int(index): Path(path)
            for index, path in repacked.texture_outputs.items()
        }
        if not output_by_index:
            output_by_index = {
                index: Path(path)
                for index, path in enumerate(repacked.output_paths)
            }
        changed_pixels = 0
        output_paths: list[str] = []
        source_paths: list[str] = []
        for index, source_path in enumerate(source.textures):
            target = output_by_index.get(index)
            if target is None:
                raise ValidationError(f"Missing round-trip texture index {index}")
            changed_pixels += _compare_rgba(source_path, target)
            source_paths.append(str(source_path.resolve()))
            output_paths.append(str(target.resolve()))
        record.update(
            {
                "part_count": int(result.layer_count),
                "texture_count": len(source.textures),
                "source_paths": source_paths,
                "output_paths": output_paths,
                "changed_pixels": changed_pixels,
                "warnings": list(result.warnings) + list(repacked.warnings),
                "psd_path": str(result.psd_path.resolve()),
                "metadata_path": str(result.metadata_path.resolve())
                if result.metadata_path
                else None,
                "status": "passed" if changed_pixels == 0 else "failed",
            }
        )
        if changed_pixels:
            raise ValidationError(
                f"Live2D {mode} round-trip changed {changed_pixels} pixels"
            )
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        record["duration_seconds"] = round(time.perf_counter() - started, 3)
    return record


def _run_spine_case(
    atlas: Path,
    output: Path,
    *,
    with_psd: bool,
) -> dict[str, Any]:
    mode = "psd" if with_psd else "png"
    started = time.perf_counter()
    record = _base_record(
        family="spine",
        mode=mode,
        source=atlas,
        output=output,
        started=started,
    )
    try:
        exported = extract_spine_atlas(atlas, output, write_psd=with_psd)
        if with_psd and exported.psd_path is None:
            raise ValidationError("Spine PSD export did not produce a PSD path")
        roundtrip_dir = output / "roundtrip"
        written = writeback_spine_atlas(
            exported.metadata_path,
            exported.psd_path if with_psd else None,
            roundtrip_dir,
        )
        source_paths: list[str] = []
        output_paths: list[str] = []
        changed_pixels = 0
        for page in exported.atlas.pages:
            if exported.atlas.atlas_path is None:
                raise ValidationError("Spine export lost its atlas source path")
            source_page = (exported.atlas.atlas_path.parent / page.name).resolve()
            output_page = written.page_paths.get(page.index)
            if output_page is None:
                raise ValidationError(f"Missing round-trip page index {page.index}")
            changed_pixels += _compare_rgba(source_page, output_page)
            source_paths.append(str(source_page))
            output_paths.append(str(Path(output_page).resolve()))
        record.update(
            {
                "part_count": len(exported.atlas.regions),
                "texture_count": len(exported.atlas.pages),
                "source_paths": source_paths,
                "output_paths": output_paths,
                "changed_pixels": changed_pixels,
                "warnings": list(exported.warnings) + list(written.warnings),
                "metadata_path": str(exported.metadata_path.resolve()),
                "psd_path": str(exported.psd_path.resolve())
                if exported.psd_path
                else None,
                "status": "passed" if changed_pixels == 0 else "failed",
            }
        )
        if changed_pixels:
            raise ValidationError(
                f"Spine {mode} round-trip changed {changed_pixels} pixels"
            )
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        record["duration_seconds"] = round(time.perf_counter() - started, 3)
    return record


def _write_report(output: Path, report: dict[str, Any]) -> Path:
    report_path = output / "validation_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate no-edit RGBA round-trips for Live2D and Spine assets."
    )
    parser.add_argument(
        "--live2d",
        type=Path,
        help="Live2D model3 JSON to validate in atlas-artmesh and mesh modes.",
    )
    parser.add_argument(
        "--atlas",
        action="append",
        type=Path,
        default=[],
        help="Spine .atlas path; repeat this option for multiple atlases.",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="New independent directory for PSDs, round-trip pages, and JSON report.",
    )
    parser.add_argument(
        "--cubism-core",
        type=Path,
        help="Optional Live2DCubismCore.dll used by mesh export when no sidecar exists.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.live2d is None and not args.atlas:
        print("At least one of --live2d or --atlas is required.", file=sys.stderr)
        return 2

    inputs = [path for path in ([args.live2d] if args.live2d else [])] + list(args.atlas)
    try:
        for source in inputs:
            if not source.expanduser().is_file():
                raise ValidationError(f"Input file does not exist: {source}")
        if args.cubism_core is not None:
            if not args.cubism_core.expanduser().is_file():
                raise ValidationError(f"Cubism Core DLL does not exist: {args.cubism_core}")
        output = _ensure_fresh_output(args.output, inputs)
    except Exception as exc:
        print(f"validation setup failed: {exc}", file=sys.stderr)
        return 1

    previous_core = os.environ.get("LPK_CUBISM_CORE_DLL")
    if args.cubism_core is not None:
        os.environ["LPK_CUBISM_CORE_DLL"] = str(args.cubism_core.expanduser().resolve())

    started = time.perf_counter()
    cases: list[dict[str, Any]] = []
    try:
        if args.live2d is not None:
            model = args.live2d.expanduser().resolve()
            live_root = _fresh_case_dir(output, "live2d")
            for mode in ("atlas-artmesh", "mesh"):
                cases.append(
                    _run_live2d_case(
                        model,
                        mode,
                        _fresh_case_dir(live_root, mode),
                    )
                )
        for index, atlas in enumerate(args.atlas):
            atlas = atlas.expanduser().resolve()
            spine_root = _fresh_case_dir(
                output,
                f"spine_{index:02d}_{_safe_component(atlas.stem)}",
            )
            for mode, with_psd in (("png", False), ("psd", True)):
                cases.append(
                    _run_spine_case(
                        atlas,
                        _fresh_case_dir(spine_root, mode),
                        with_psd=with_psd,
                    )
                )
    finally:
        if previous_core is None:
            os.environ.pop("LPK_CUBISM_CORE_DLL", None)
        else:
            os.environ["LPK_CUBISM_CORE_DLL"] = previous_core

    errors = [case.get("error") for case in cases if case.get("status") != "passed"]
    report = {
        "format": "LpkUnpacker.EditRoundtripValidation",
        "version": 1,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.perf_counter() - started, 3),
        "output_directory": str(output),
        "cases": cases,
        "warnings": [warning for case in cases for warning in case.get("warnings", [])],
        "errors": errors,
        "passed": not errors and bool(cases),
    }
    report_path = _write_report(output, report)
    print(report_path)
    if errors:
        for error in errors:
            print(f"FAILED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
