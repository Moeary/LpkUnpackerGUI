from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

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
from app.core.spine_converter import (
    SpineConversionOptions,
    convert_spine,
    discover_spine_conversion_sources,
)

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
    spine_conversion: SpineConversionOptions | Mapping[str, Any] | None = None,
) -> ExtractBatchResult:
    extract_mode = mode if isinstance(mode, ExtractMode) else ExtractMode(mode)
    source_paths = [Path(path) for path in sources]
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    conversion_options = _normalize_spine_conversion(spine_conversion)

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
            spine_conversion=conversion_options,
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
    spine_conversion: SpineConversionOptions,
) -> ExtractItemResult:
    try:
        # LPK/WPK have one full-package model path; LIVE2D is an extraction
        # intent used by the preview/UI layer and maps to that same package
        # path.  Keep the original mode for the conversion gate below.
        package_mode = ExtractMode.FULL if mode == ExtractMode.LIVE2D else mode
        if source_type == ExtractSourceType.LPK:
            item = extract_lpk(
                source,
                _target_dir(
                    output_dir,
                    source,
                    source_type,
                    package_mode,
                    texture_output_subdir,
                    config_files,
                ),
                package_mode,
                config_files,
            )
            return _maybe_convert_spine_item(item, mode, spine_conversion, log)
        if source_type == ExtractSourceType.WPK:
            item = extract_wpk(
                source,
                _target_dir(
                    output_dir,
                    source,
                    source_type,
                    package_mode,
                    texture_output_subdir,
                    config_files,
                ),
                package_mode,
                config_files,
            )
            return _maybe_convert_spine_item(item, mode, spine_conversion, log)
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


def _normalize_spine_conversion(
    value: SpineConversionOptions | Mapping[str, Any] | None,
) -> SpineConversionOptions:
    if isinstance(value, SpineConversionOptions):
        return value
    if isinstance(value, Mapping):
        target = str(value.get("target_version", "3.8.75") or "3.8.75").strip()
        output_format = str(value.get("output_format", "json") or "json").strip().lower()
        return SpineConversionOptions(
            enabled=bool(value.get("enabled", False)),
            target_version=target or "3.8.75",
            output_format=output_format or "json",
            converter_path=value.get("converter_path"),
            remove_curve=bool(value.get("remove_curve", False)),
            create_project=bool(value.get("create_project", False)),
            editor_path=value.get("editor_path"),
        )
    return SpineConversionOptions()


def _maybe_convert_spine_item(
    item: ExtractItemResult,
    mode: ExtractMode,
    options: SpineConversionOptions,
    log: LogCallback | None,
) -> ExtractItemResult:
    """Append opt-in Spine conversions to one completed LPK/WPK item."""

    if not options.enabled or mode not in {ExtractMode.FULL, ExtractMode.LIVE2D}:
        return item
    if item.source_type not in {ExtractSourceType.LPK, ExtractSourceType.WPK}:
        return item
    if item.source_type == ExtractSourceType.LPK and not item.success:
        return item
    if not item.output_dir or not item.output_dir.is_dir():
        return item

    extraction_roots = _spine_conversion_roots(item)
    if not extraction_roots:
        return item

    conversion_root = item.output_dir / "spine_converted"
    conversion_children: list[ExtractItemResult] = []
    for extraction_root in extraction_roots:
        try:
            sources = discover_spine_conversion_sources(extraction_root)
        except Exception as exc:
            conversion_children.append(
                ExtractItemResult(
                    source=extraction_root,
                    source_type=item.source_type,
                    success=False,
                    output_dir=conversion_root,
                    error=(
                        f"Automatic Spine source discovery failed for "
                        f"{extraction_root}: {exc}"
                    ),
                )
            )
            continue
        for source in sources:
            try:
                converted = convert_spine(
                    source,
                    conversion_root,
                    target_version=options.target_version,
                    output_format=options.output_format,
                    converter_path=options.converter_path,
                    remove_curve=options.remove_curve,
                    create_project=options.create_project,
                    editor_path=options.editor_path,
                )
                warning_text = ""
                if converted.warnings:
                    warning_text = " Warnings: " + " | ".join(converted.warnings)
                conversion_children.append(
                    ExtractItemResult(
                        source=source,
                        source_type=item.source_type,
                        success=True,
                        output_dir=converted.output_dir,
                        exported_count=1,
                        message=(
                            f"Converted Spine model {source.name} to "
                            f"{converted.skeleton_path.name} under {converted.output_dir}."
                            + warning_text
                        ),
                    )
                )
            except Exception as exc:
                conversion_children.append(
                    ExtractItemResult(
                        source=source,
                        source_type=item.source_type,
                        success=False,
                        output_dir=conversion_root,
                        error=f"Automatic Spine conversion failed for {source}: {exc}",
                    )
                )

    item.children.extend(conversion_children)
    failed = [child for child in conversion_children if not child.success]
    if failed:
        item.success = False
        detail = f"{len(failed)} automatic Spine conversion(s) failed"
        item.error = f"{item.error}; {detail}" if item.error else detail
    else:
        item.message = (
            f"{item.message} Automatic Spine conversion produced "
            f"{len(conversion_children)} model(s) under {conversion_root}."
        ).strip()
    return item


def _spine_conversion_roots(item: ExtractItemResult) -> list[Path]:
    """Return only directories created by this item or its nested LPKs.

    WPK extraction writes several LPKs below one shared output directory.  A
    recursive scan of that parent would pick up models from older runs, so the
    converter receives the explicit directories recorded by each child.
    """

    roots: list[Path] = []

    def visit(current: ExtractItemResult) -> None:
        if not current.success:
            return
        for path in current.extracted_dirs:
            resolved = Path(path).expanduser().resolve()
            if resolved.is_dir():
                roots.append(resolved)
        for child in current.children:
            visit(child)

    if item.source_type == ExtractSourceType.LPK:
        visit(item)
    else:
        for child in item.children:
            visit(child)

    result: list[Path] = []
    seen: set[str] = set()
    for path in roots:
        key = str(path).casefold()
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


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
