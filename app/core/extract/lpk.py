from __future__ import annotations

from pathlib import Path

from app.core.extract.detector import find_config_for_lpk
from app.core.extract.models import ExtractItemResult, ExtractMode, ExtractSourceType
from app.core.image_extractor import ImageExtractor
from app.core.lpk_loader import LpkLoader


def extract_lpk(
    lpk_path: str | Path,
    output_dir: str | Path,
    mode: ExtractMode,
    config_files: list[str | Path] | None = None,
) -> ExtractItemResult:
    source = Path(lpk_path)
    target = Path(output_dir)
    config_path = find_config_for_lpk(source, config_files)

    try:
        if mode == ExtractMode.TEXTURES:
            return extract_lpk_textures(source, target, config_path)
        if mode == ExtractMode.FULL:
            return extract_lpk_full(source, target, config_path)
        return ExtractItemResult(
            source=source,
            source_type=ExtractSourceType.LPK,
            success=False,
            error=f"LPK does not support mode: {mode.value}",
        )
    except SystemExit as exc:
        return ExtractItemResult(
            source=source,
            source_type=ExtractSourceType.LPK,
            success=False,
            output_dir=target,
            error=f"LPK extractor exited unexpectedly: {exc}",
        )
    except Exception as exc:
        return ExtractItemResult(
            source=source,
            source_type=ExtractSourceType.LPK,
            success=False,
            output_dir=target,
            error=str(exc),
        )


def extract_lpk_full(
    lpk_path: Path,
    output_dir: Path,
    config_path: Path | None = None,
) -> ExtractItemResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    loader = LpkLoader(str(lpk_path), str(config_path) if config_path else None)
    loader.extract(str(output_dir))
    return ExtractItemResult(
        source=lpk_path,
        source_type=ExtractSourceType.LPK,
        success=True,
        output_dir=output_dir,
        exported_count=1,
        message=f"Extracted LPK: {lpk_path.name}",
    )


def extract_lpk_textures(
    lpk_path: Path,
    output_dir: Path,
    config_path: Path | None = None,
) -> ExtractItemResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    extractor = ImageExtractor(str(lpk_path), str(config_path) if config_path else None)
    extracted, skipped = extractor.extract_images(str(output_dir))
    return ExtractItemResult(
        source=lpk_path,
        source_type=ExtractSourceType.LPK,
        success=True,
        output_dir=output_dir,
        exported_count=extracted,
        skipped_count=skipped,
        message=f"Extracted {extracted} texture(s) from {lpk_path.name}",
    )
