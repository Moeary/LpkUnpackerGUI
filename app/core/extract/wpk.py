from __future__ import annotations

from pathlib import Path

from app.core.extract.lpk import extract_lpk
from app.core.extract.models import ExtractItemResult, ExtractMode, ExtractSourceType
from app.core.extract.package_classifier import output_subdir_for_lpk
from app.core.image_extractor import ImageExtractor
from app.core.wpk_handler import WPKHandler


def extract_wpk(
    wpk_path: str | Path,
    output_dir: str | Path,
    mode: ExtractMode,
    config_files: list[str | Path] | None = None,
) -> ExtractItemResult:
    source = Path(wpk_path)
    target = Path(output_dir)

    try:
        if mode == ExtractMode.TEXTURES:
            return extract_wpk_textures(source, target)
        if mode == ExtractMode.FULL:
            return extract_wpk_full(source, target, config_files)
        return ExtractItemResult(
            source=source,
            source_type=ExtractSourceType.WPK,
            success=False,
            output_dir=target,
            error=f"WPK does not support mode: {mode.value}",
        )
    except Exception as exc:
        return ExtractItemResult(
            source=source,
            source_type=ExtractSourceType.WPK,
            success=False,
            output_dir=target,
            error=str(exc),
        )


def extract_wpk_full(
    wpk_path: Path,
    output_dir: Path,
    config_files: list[str | Path] | None = None,
) -> ExtractItemResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    temp_dir, lpk_files, internal_configs = WPKHandler.extract_wpk(str(wpk_path), str(output_dir))
    if not temp_dir:
        return ExtractItemResult(
            source=wpk_path,
            source_type=ExtractSourceType.WPK,
            success=False,
            output_dir=output_dir,
            error="Failed to extract WPK archive",
        )

    children: list[ExtractItemResult] = []
    try:
        if not lpk_files:
            return ExtractItemResult(
                source=wpk_path,
                source_type=ExtractSourceType.WPK,
                success=False,
                output_dir=output_dir,
                error="No LPK files found in WPK",
            )

        combined_configs = [*internal_configs, *(config_files or [])]
        for lpk_file in lpk_files:
            target = output_dir / output_subdir_for_lpk(lpk_file, combined_configs)
            children.append(extract_lpk(lpk_file, target, ExtractMode.FULL, combined_configs))
    finally:
        WPKHandler.cleanup_temp_dir(temp_dir)

    success_count = sum(1 for item in children if item.success)
    failure_count = len(children) - success_count
    return ExtractItemResult(
        source=wpk_path,
        source_type=ExtractSourceType.WPK,
        success=failure_count == 0 and success_count > 0,
        output_dir=output_dir,
        exported_count=sum(item.exported_count for item in children),
        skipped_count=sum(item.skipped_count for item in children),
        children=children,
        message=f"WPK extracted: {success_count} LPK succeeded, {failure_count} failed",
        error=None if failure_count == 0 else f"{failure_count} nested LPK extraction(s) failed",
    )


def extract_wpk_textures(wpk_path: Path, output_dir: Path) -> ExtractItemResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    extractor = ImageExtractor(str(wpk_path))
    extracted, skipped = extractor.extract_images(str(output_dir))
    return ExtractItemResult(
        source=wpk_path,
        source_type=ExtractSourceType.WPK,
        success=True,
        output_dir=output_dir,
        exported_count=extracted,
        skipped_count=skipped,
        message=f"Extracted {extracted} texture(s) from {wpk_path.name}",
    )
