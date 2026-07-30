from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.core.extract import ExtractMode, ExtractSourceType, detect_source_type
from app.core.extract.unity import extract_unity
from app.core.model.live2d_package import Live2DPackage
from app.core.model.resolver import (
    Live2DPackageError,
    is_model_json_path,
    resolve_live2d_package,
)
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
        return Live2DSourceImportResult(package=_resolve_local_live2d_package(source_path))
    except Live2DPackageError:
        pass

    settings = SettingsManager()
    result = prepare_preview_import(
        source_path,
        temp_root or settings.get_temp_dir(),
        log=_adapt_log_callback(log),
    )
    return Live2DSourceImportResult(
        package=result.package,
        temp_dir=result.temp_dir,
        warnings=result.warnings,
    )


@dataclass(frozen=True)
class ImportedTextureSource:
    source: Path
    workspace_dir: Path | None
    model_json: Path | None
    texture_paths: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def import_texture_source_to_workspace(
    source: str | Path,
    workspace_dir: str | Path,
    temp_root: str | Path | None = None,
    log: LogCallback | None = None,
    image_suffixes: set[str] | None = None,
) -> ImportedTextureSource:
    source_path = Path(source).resolve()
    workspace_path = Path(workspace_dir).resolve()
    workspace_path.mkdir(parents=True, exist_ok=True)
    suffixes = {suffix.lower() for suffix in (image_suffixes or {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tga"})}

    try:
        package = _resolve_local_live2d_package(source_path)
        if workspace_path.exists():
            shutil.rmtree(workspace_path)
        shutil.copytree(
            package.root_dir,
            workspace_path,
            ignore=shutil.ignore_patterns("*.pretty.json", "__pycache__"),
        )
        copied_package = resolve_live2d_package(workspace_path)
        return ImportedTextureSource(
            source=source_path,
            workspace_dir=workspace_path,
            model_json=copied_package.model_json,
            texture_paths=[path for path in copied_package.texture_paths if path.is_file()],
        )
    except Live2DPackageError:
        if source_path.is_file() and source_path.suffix.lower() in suffixes:
            copied = _copy_unique_file(source_path, workspace_path / source_path.name)
            return ImportedTextureSource(
                source=source_path,
                workspace_dir=workspace_path,
                model_json=None,
                texture_paths=[copied],
            )
        direct_images = _collect_images(source_path, suffixes)
        if direct_images:
            copied_images = [_copy_unique_file(path, workspace_path / path.name) for path in direct_images]
            return ImportedTextureSource(
                source=source_path,
                workspace_dir=workspace_path,
                model_json=None,
                texture_paths=copied_images,
            )

    try:
        imported = prepare_live2d_source_import(source_path, temp_root=temp_root, log=log)
    except Live2DPackageError as exc:
        texture_import = _try_import_unity_textures_to_workspace(
            source_path,
            workspace_path,
            temp_root=temp_root,
            log=log,
            suffixes=suffixes,
        )
        if texture_import:
            return texture_import
        raise exc

    if workspace_path.exists():
        shutil.rmtree(workspace_path)
    shutil.copytree(
        imported.package.root_dir,
        workspace_path,
        ignore=shutil.ignore_patterns("*.pretty.json", "__pycache__"),
    )
    package = resolve_live2d_package(workspace_path)
    return ImportedTextureSource(
        source=source_path,
        workspace_dir=workspace_path,
        model_json=package.model_json,
        texture_paths=[path for path in package.texture_paths if path.is_file()],
        warnings=imported.warnings,
    )


def _adapt_log_callback(log: LogCallback | None):
    if not log:
        return None

    def emit(*parts):
        if not parts:
            return
        if len(parts) == 1:
            log(str(parts[0]))
            return
        level = str(parts[0])
        message = " ".join(str(part) for part in parts[1:])
        log(f"[{level}] {message}" if level else message)

    return emit


def _resolve_local_live2d_package(source_path: Path) -> Live2DPackage:
    """Resolve a package even when the user picked a file inside its folder."""
    direct_error: Live2DPackageError | None = None
    try:
        return resolve_live2d_package(source_path)
    except Live2DPackageError as exc:
        direct_error = exc
        if not source_path.is_file():
            raise

    # Extensionless AssetBundles are common.  They must go through Unity
    # extraction instead of being mistaken for an arbitrary file beside an
    # already-extracted, unrelated model.
    if detect_source_type(source_path) in {
        ExtractSourceType.LPK,
        ExtractSourceType.WPK,
        ExtractSourceType.UNITY,
    }:
        raise direct_error

    companion_suffixes = {
        ".json",
        ".moc",
        ".moc3",
        ".png",
        ".jpg",
        ".jpeg",
        ".bmp",
        ".gif",
        ".webp",
        ".tga",
        ".wav",
        ".mp3",
        ".ogg",
    }
    if source_path.suffix.lower() not in companion_suffixes:
        raise direct_error

    # Texture, motion, physics and moc files may be nested below model3.json.
    # Only inspect model JSON files directly in each ancestor.  Recursive
    # ancestor resolution can cross into sibling character folders and silently
    # select the first unrelated model there.
    errors: list[Exception] = []
    for parent in list(source_path.parents)[:4]:
        candidates = sorted(
            (
                path
                for path in parent.glob("*.json")
                if path.is_file() and is_model_json_path(path)
            ),
            key=lambda path: path.name.lower(),
        )
        for candidate in candidates:
            try:
                return resolve_live2d_package(candidate)
            except Live2DPackageError as exc:
                errors.append(exc)
    raise errors[-1] if errors else direct_error


def _collect_images(source_path: Path, suffixes: set[str]) -> list[Path]:
    if not source_path.is_dir():
        return []
    return [
        item.resolve()
        for item in sorted(source_path.rglob("*"), key=lambda p: str(p).lower())
        if item.is_file() and item.suffix.lower() in suffixes
    ]


def _try_import_unity_textures_to_workspace(
    source_path: Path,
    workspace_path: Path,
    temp_root: str | Path | None,
    log: LogCallback | None,
    suffixes: set[str],
) -> ImportedTextureSource | None:
    source_type = detect_source_type(source_path)
    if source_type not in {ExtractSourceType.UNITY, ExtractSourceType.FOLDER}:
        return None

    settings = SettingsManager()
    temp_root_path = Path(temp_root or settings.get_temp_dir()).resolve()
    temp_root_path.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="mod_texture_import_", dir=temp_root_path))
    try:
        if log:
            log(f"Live2D package import failed; trying Unity texture export: {source_path}")
        result = extract_unity(
            source_path,
            temp_dir,
            ExtractMode.TEXTURES,
            log=_adapt_log_callback(log),
        )
        if not result.success:
            if source_type == ExtractSourceType.FOLDER:
                return None
            raise Live2DPackageError(result.error or "Unity texture export failed")

        exported_dir = result.output_dir if result.output_dir.exists() else temp_dir
        exported_images = _collect_images(exported_dir, suffixes)
        if not exported_images:
            if source_type == ExtractSourceType.FOLDER:
                return None
            raise Live2DPackageError("Unity texture export did not produce image files")

        if workspace_path.exists():
            shutil.rmtree(workspace_path)
        workspace_path.mkdir(parents=True, exist_ok=True)
        copied_images = [_copy_unique_file(path, workspace_path / path.name) for path in exported_images]
        return ImportedTextureSource(
            source=source_path,
            workspace_dir=workspace_path,
            model_json=None,
            texture_paths=copied_images,
            warnings=["Imported textures from Unity source without a Live2D model."],
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _copy_unique_file(source: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    candidate = target
    index = 1
    while candidate.exists():
        candidate = target.with_name(f"{target.stem}_{index}{target.suffix}")
        index += 1
    shutil.copy2(source, candidate)
    return candidate
