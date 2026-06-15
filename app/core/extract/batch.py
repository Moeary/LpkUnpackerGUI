from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable

from app.core.extract.detector import detect_source_type
from app.core.extract.lpk import extract_lpk
from app.core.extract.models import (
    ExtractBatchResult,
    ExtractItemResult,
    ExtractMode,
    ExtractSourceType,
)
from app.core.extract.package_classifier import output_subdir_for_lpk
from app.core.extract.unity import extract_unity
from app.core.extract.wpk import extract_wpk

ProgressCallback = Callable[[int, int], None]
LogCallback = Callable[[str, str], None]
ContinueCallback = Callable[[], bool]


def run_extraction_batch(
    sources: Iterable[str | Path],
    output_dir: str | Path,
    mode: ExtractMode | str,
    config_files: list[str | Path] | None = None,
    progress: ProgressCallback | None = None,
    log: LogCallback | None = None,
    should_continue: ContinueCallback | None = None,
    texture_output_subdir: bool = False,
) -> ExtractBatchResult:
    extract_mode = mode if isinstance(mode, ExtractMode) else ExtractMode(mode)
    source_paths = [Path(path) for path in sources]
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    result = ExtractBatchResult(output_dir=output_path)
    total = len(source_paths)
    _log(log, "INFO", "=== Analyzing Sources ===")
    _log(log, "INFO", f"Total sources: {total}")
    _log(log, "INFO", f"Extraction mode: {extract_mode.value}")
    _log(log, "INFO", "=========================")

    for index, source in enumerate(source_paths):
        if should_continue and not should_continue():
            _log(log, "WARNING", "Extraction stopped before all sources were processed")
            break

        source_type = detect_source_type(source)
        _log(log, "INFO", f"Processing {index + 1}/{total}: {source.name}")
        _log(log, "INFO", f"Detected source type: {source_type.value}")

        item = _extract_one(
            source,
            source_type,
            output_path,
            extract_mode,
            config_files=config_files,
            log=log,
            texture_output_subdir=texture_output_subdir,
        )
        result.items.append(item)
        _log_item(log, item)

        if progress:
            progress(index + 1, total)

    _log(
        log,
        "INFO",
        (
            "Extraction summary: "
            f"{result.success_count}/{result.total_count} succeeded, "
            f"{result.failure_count} failed, "
            f"{result.exported_count} exported, "
            f"{result.skipped_count} skipped"
        ),
    )
    return result


def _extract_one(
    source: Path,
    source_type: ExtractSourceType,
    output_dir: Path,
    mode: ExtractMode,
    config_files: list[str | Path] | None,
    log: LogCallback | None,
    texture_output_subdir: bool,
) -> ExtractItemResult:
    try:
        if source_type == ExtractSourceType.LPK:
            return extract_lpk(
                source,
                _target_dir(
                    output_dir,
                    source,
                    source_type,
                    mode,
                    texture_output_subdir,
                    config_files,
                ),
                mode,
                config_files,
            )
        if source_type == ExtractSourceType.WPK:
            return extract_wpk(
                source,
                _target_dir(
                    output_dir,
                    source,
                    source_type,
                    mode,
                    texture_output_subdir,
                    config_files,
                ),
                mode,
                config_files,
            )
        if source_type in {ExtractSourceType.UNITY, ExtractSourceType.FOLDER}:
            unity_mode = ExtractMode.LIVE2D if mode == ExtractMode.FULL else mode
            return extract_unity(
                source,
                _target_dir(
                    output_dir,
                    source,
                    source_type,
                    unity_mode,
                    texture_output_subdir,
                    config_files,
                ),
                unity_mode,
                log=log,
            )

        return ExtractItemResult(
            source=source,
            source_type=source_type,
            success=False,
            output_dir=output_dir,
            error=f"Unsupported source type: {source}",
        )
    except Exception as exc:
        return ExtractItemResult(
            source=source,
            source_type=source_type,
            success=False,
            output_dir=output_dir,
            error=str(exc),
        )


def _target_dir(
    output_dir: Path,
    source: Path,
    source_type: ExtractSourceType,
    mode: ExtractMode,
    texture_output_subdir: bool,
    config_files: list[str | Path] | None = None,
) -> Path:
    if mode == ExtractMode.TEXTURES:
        texture_root = output_dir / "textures"
        if not texture_output_subdir:
            return texture_root

        if source_type in {ExtractSourceType.UNITY, ExtractSourceType.FOLDER}:
            return texture_root / source.name
        return texture_root

    if mode in {ExtractMode.FULL, ExtractMode.LIVE2D}:
        if source_type == ExtractSourceType.LPK:
            return output_dir / output_subdir_for_lpk(source, config_files)
        if source_type == ExtractSourceType.WPK:
            return output_dir
        return output_dir / "live2d"

    if source_type in {ExtractSourceType.UNITY, ExtractSourceType.FOLDER}:
        return output_dir / "unity"

    return output_dir


def _log_item(log: LogCallback | None, item: ExtractItemResult) -> None:
    if item.success:
        _log(log, "INFO", item.message or f"Completed: {item.source.name}")
    else:
        _log(log, "ERROR", f"Failed: {item.source.name}: {item.error or item.message}")

    for child in item.children:
        _log_item(log, child)


def _log(log: LogCallback | None, level: str, message: str) -> None:
    if log:
        log(level, message)
