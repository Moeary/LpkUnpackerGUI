from __future__ import annotations

from pathlib import Path

from app.core.extract.batch import LogCallback, ProgressCallback, run_extraction_batch
from app.core.extract.detector import scan_package_folder
from app.core.extract.models import ExtractBatchResult, ExtractMode


def collect_package_sources(folder_path: str | Path) -> tuple[list[str], list[str]]:
    return scan_package_folder(folder_path)


def extract_package_folder(
    folder_path: str | Path,
    output_dir: str | Path,
    mode: ExtractMode | str,
    progress: ProgressCallback | None = None,
    log: LogCallback | None = None,
    texture_output_subdir: bool = False,
) -> ExtractBatchResult:
    files, configs = collect_package_sources(folder_path)
    return run_extraction_batch(
        files,
        output_dir,
        mode,
        config_files=configs,
        progress=progress,
        log=log,
        texture_output_subdir=texture_output_subdir,
    )
