from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.core.model.live2d_package import Live2DPackage
from app.core.model.resolver import Live2DPackageError, resolve_live2d_package
from app.core.preview.session import prepare_preview_import
from app.core.settings_manager import SettingsManager


LogCallback = Callable[[str], None]


@dataclass(frozen=True)
class Live2DSourceImportResult:
    package: Live2DPackage
    temp_dir: Path | None = None
    warnings: list[str] = field(default_factory=list)


def prepare_live2d_source_import(
    source: str | Path,
    temp_root: str | Path | None = None,
    log: LogCallback | None = None,
) -> Live2DSourceImportResult:
    source_path = Path(source).resolve()
    try:
        return Live2DSourceImportResult(package=resolve_live2d_package(source_path))
    except Live2DPackageError:
        pass

    settings = SettingsManager()
    result = prepare_preview_import(
        source_path,
        temp_root or settings.get_temp_dir(),
        log=log,
    )
    return Live2DSourceImportResult(
        package=result.package,
        temp_dir=result.temp_dir,
        warnings=result.warnings,
    )
