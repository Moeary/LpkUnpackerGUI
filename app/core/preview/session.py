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


@dataclass
class PreviewImportResult:
    package: Live2DPackage
    preview_model_json: Path
    temp_dir: Path | None = None
    warnings: list[str] = field(default_factory=list)


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
