from __future__ import annotations

import tempfile
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from app.core.extract import ExtractMode, ExtractSourceType, detect_source_type, run_extraction_batch
from app.core.model import (
    Live2DPackage,
    Live2DPackageError,
    prepare_model_json_for_preview,
    resolve_live2d_package,
)
from app.core.spine_preview import (
    SpineAssetNotFoundError,
    SpinePreviewAsset,
    SpinePreviewPlan,
    load_spine_asset,
    make_spine_preview_plan,
    prepare_spine_preview_import as _prepare_spine_preview_import,
)


@dataclass
class PreviewImportResult:
    package: Live2DPackage
    preview_model_json: Path
    temp_dir: Path | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class SpinePreviewImportResult:
    asset: SpinePreviewAsset
    plan: SpinePreviewPlan
    temp_dir: Path | None = None


def prepare_package_preview_import(source, temp_root, runtime_root=None, *, should_continue=None):
    """Extract an LPK/WPK once, then select a model from the same workspace.

    Preview extraction deliberately leaves export conversion disabled. Both
    result types transfer ownership of the temporary directory to the caller.
    """
    root = Path(temp_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="package_preview_", dir=root))
    try:
        result = run_extraction_batch(
            [Path(source).resolve()], temp_dir, ExtractMode.FULL,
            should_continue=should_continue,
        )
        if should_continue and not should_continue():
            raise Live2DPackageError("Preview import cancelled")
        if result.has_failures:
            error = result.failed_items[0].error if result.failed_items else None
            raise Live2DPackageError(error or "Package preview extraction failed")
        try:
            asset = load_spine_asset(temp_dir)
        except SpineAssetNotFoundError:
            package = resolve_live2d_package(temp_dir)
            preview_json = prepare_model_json_for_preview(package.model_json)
            return PreviewImportResult(package, preview_json, temp_dir)
        return SpinePreviewImportResult(asset, make_spine_preview_plan(asset, runtime_root), temp_dir)
    except Exception:
        # Only our newly created disposable workspace is removed on failure.
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def prepare_preview_import(
    source: str | Path,
    temp_root: str | Path,
    log=None,
) -> PreviewImportResult:
    source_path = Path(source).resolve()

    direct = _try_direct_package(source_path)
    if direct:
        preview_json = prepare_model_json_for_preview(direct.model_json)
        return PreviewImportResult(package=direct, preview_model_json=preview_json)

    source_type = detect_source_type(source_path)
    if source_type not in {
        ExtractSourceType.LPK,
        ExtractSourceType.WPK,
        ExtractSourceType.UNITY,
        ExtractSourceType.FOLDER,
    }:
        raise Live2DPackageError(f"Unsupported preview source: {source_path}")

    temp_root_path = Path(temp_root).resolve()
    temp_root_path.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="lpk_preview_model_", dir=temp_root_path))

    try:
        result = run_extraction_batch(
            [source_path],
            temp_dir,
            ExtractMode.FULL,
            log=log,
        )
        if result.has_failures:
            first_error = result.failed_items[0].error if result.failed_items else "unknown error"
            raise Live2DPackageError(first_error or "preview import failed")

        package = resolve_live2d_package(temp_dir)
        preview_json = prepare_model_json_for_preview(package.model_json)
        return PreviewImportResult(
            package=package,
            preview_model_json=preview_json,
            temp_dir=temp_dir,
        )
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _try_direct_package(source_path: Path) -> Live2DPackage | None:
    try:
        return resolve_live2d_package(source_path)
    except Exception:
        return None


def prepare_spine_preview_import(
    source: str | Path,
    temp_root: str | Path,
    runtime_root: str | Path | None = None,
    log=None,
) -> SpinePreviewImportResult:
    result = _prepare_spine_preview_import(source, temp_root, runtime_root, log=log)
    return SpinePreviewImportResult(
        asset=result.asset,
        plan=result.plan,
        temp_dir=result.temp_dir,
    )
