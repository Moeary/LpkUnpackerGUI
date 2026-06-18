from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from app.core.model import Live2DPackage, Live2DPackageError, resolve_live2d_package
from app.core.preview.session import prepare_preview_import
from app.core.settings_manager import SettingsManager


PROJECT_FORMAT = "LpkUnpacker.Live2DPSDProject"
PROJECT_VERSION = 1
PROJECT_FILE_NAME = "project.lpkpsd_project.json"
LogCallback = Callable[[str], None]


class Live2DPSDProjectError(RuntimeError):
    pass


@dataclass(frozen=True)
class Live2DPSDProject:
    project_dir: Path
    project_file: Path
    data: dict[str, Any]

    @property
    def project_name(self) -> str:
        return str(self.data.get("project_name") or self.project_dir.name)

    @property
    def live2d_dir(self) -> Path:
        value = str(self.data.get("workspace", {}).get("live2d_dir") or "live2d")
        return (self.project_dir / value).resolve()

    @property
    def base_model_json(self) -> Path:
        value = str(self.data.get("workspace", {}).get("base_model_json") or "")
        return (self.project_dir / value).resolve() if value else self.live2d_dir


def create_project_from_source(
    source: str | Path,
    project_name: str | None = None,
    output_root: str | Path | None = None,
    temp_root: str | Path | None = None,
    log: LogCallback | None = None,
) -> Live2DPSDProject:
    source_path = Path(source).resolve()
    package, temp_dir, warnings = _import_package(source_path, temp_root, log)

    resolved_name = sanitize_project_name(project_name or _default_project_name(source_path, package))
    project_root = _project_output_root(output_root)
    project_dir = _unique_project_dir(project_root / resolved_name)
    live2d_dir = project_dir / "live2d"

    try:
        project_dir.mkdir(parents=True, exist_ok=True)
        _copy_workspace(package.root_dir, live2d_dir)
        workspace_package = resolve_live2d_package(live2d_dir)
        data = new_project_data(
            project_name=project_dir.name,
            base_source=source_path,
            base_package=workspace_package,
            project_dir=project_dir,
            warnings=warnings,
        )
        project_file = save_project(project_dir, data)
        _ensure_project_dirs(project_dir)
        return Live2DPSDProject(project_dir, project_file, data)
    except Exception:
        shutil.rmtree(project_dir, ignore_errors=True)
        raise
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


def new_project_data(
    project_name: str,
    base_source: Path,
    base_package: Live2DPackage,
    project_dir: Path,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "format": PROJECT_FORMAT,
        "version": PROJECT_VERSION,
        "project_name": project_name,
        "base_source": {
            "path": str(base_source),
            "name": base_source.name,
        },
        "workspace": {
            "live2d_dir": "live2d",
            "base_model_json": _relative_to_project(base_package.model_json, project_dir),
        },
        "psd_exports": [],
        "repack_history": [],
        "selected_repack": "",
        "preview": {
            "current_dir": "preview/current",
            "mode": "original",
        },
        "warnings": warnings or [],
    }


def load_project(project_file: str | Path) -> Live2DPSDProject:
    path = Path(project_file).resolve()
    if path.is_dir():
        path = path / PROJECT_FILE_NAME
    if not path.is_file():
        raise Live2DPSDProjectError(f"Project file does not exist: {path}")
    with path.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict) or data.get("format") != PROJECT_FORMAT:
        raise Live2DPSDProjectError(f"Unsupported PSD project file: {path}")
    normalized = normalize_project_data(data, path.parent.name)
    _ensure_project_dirs(path.parent)
    return Live2DPSDProject(path.parent, path, normalized)


def save_project(project_dir: str | Path, data: dict[str, Any]) -> Path:
    directory = Path(project_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    payload = normalize_project_data(data, directory.name)
    project_file = directory / PROJECT_FILE_NAME
    with project_file.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return project_file


def normalize_project_data(data: dict[str, Any], fallback_name: str) -> dict[str, Any]:
    payload = dict(data)
    payload["format"] = PROJECT_FORMAT
    payload["version"] = int(payload.get("version") or PROJECT_VERSION)
    payload["project_name"] = sanitize_project_name(payload.get("project_name") or fallback_name)
    payload.setdefault("base_source", {})
    payload.setdefault("workspace", {})
    payload["workspace"].setdefault("live2d_dir", "live2d")
    payload["workspace"].setdefault("base_model_json", "")
    payload.setdefault("psd_exports", [])
    payload.setdefault("repack_history", [])
    payload.setdefault("selected_repack", "")
    payload.setdefault("preview", {})
    payload["preview"].setdefault("current_dir", "preview/current")
    payload["preview"].setdefault("mode", "original")
    payload.setdefault("warnings", [])
    return payload


def record_psd_export(
    project: Live2DPSDProject,
    psd_path: str | Path,
    metadata_path: str | Path | None,
    mode: str,
    layer_count: int,
) -> Live2DPSDProject:
    data = normalize_project_data(project.data, project.project_name)
    entry = {
        "id": timestamp_id(),
        "mode": mode,
        "psd": _relative_to_project(Path(psd_path), project.project_dir),
        "metadata": _relative_to_project(Path(metadata_path), project.project_dir)
        if metadata_path
        else "",
        "layer_count": int(layer_count),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    data.setdefault("psd_exports", []).append(entry)
    save_project(project.project_dir, data)
    return Live2DPSDProject(project.project_dir, project.project_file, data)


def create_repack_dir(project: Live2DPSDProject) -> tuple[str, Path]:
    repack_root = project.project_dir / "repacks"
    repack_root.mkdir(parents=True, exist_ok=True)
    version_id = timestamp_id()
    candidate = repack_root / version_id
    index = 1
    while candidate.exists():
        candidate = repack_root / f"{version_id}_{index}"
        index += 1
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate.name, candidate


def record_repack(
    project: Live2DPSDProject,
    version_id: str,
    source_psd: str | Path,
    metadata_path: str | Path | None,
    textures_dir: str | Path,
    output_paths: list[Path],
) -> Live2DPSDProject:
    data = normalize_project_data(project.data, project.project_name)
    entry = {
        "id": version_id,
        "source_psd": _relative_to_project(Path(source_psd), project.project_dir),
        "metadata": _relative_to_project(Path(metadata_path), project.project_dir)
        if metadata_path
        else "",
        "textures_dir": _relative_to_project(Path(textures_dir), project.project_dir),
        "output_paths": [_relative_to_project(Path(path), project.project_dir) for path in output_paths],
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    data.setdefault("repack_history", []).append(entry)
    data["selected_repack"] = version_id
    save_project(project.project_dir, data)
    _write_repack_manifest(Path(textures_dir), entry)
    return Live2DPSDProject(project.project_dir, project.project_file, data)


def select_repack(project: Live2DPSDProject, version_id: str) -> Live2DPSDProject:
    if not find_repack_entry(project, version_id):
        raise Live2DPSDProjectError(f"Repack version does not exist: {version_id}")
    data = normalize_project_data(project.data, project.project_name)
    data["selected_repack"] = version_id
    save_project(project.project_dir, data)
    return Live2DPSDProject(project.project_dir, project.project_file, data)


def set_preview_state(
    project: Live2DPSDProject,
    mode: str,
    repack_id: str = "",
) -> Live2DPSDProject:
    data = normalize_project_data(project.data, project.project_name)
    preview = dict(data.get("preview") or {})
    preview["mode"] = mode
    preview["repack_id"] = repack_id
    data["preview"] = preview
    save_project(project.project_dir, data)
    return Live2DPSDProject(project.project_dir, project.project_file, data)


def find_repack_entry(project: Live2DPSDProject, version_id: str | None = None) -> dict[str, Any] | None:
    data = normalize_project_data(project.data, project.project_name)
    target_id = str(version_id or data.get("selected_repack") or "")
    history = data.get("repack_history") or []
    if target_id:
        for entry in history:
            if str(entry.get("id") or "") == target_id:
                return dict(entry)
    return dict(history[-1]) if history else None


def prepare_repack_preview_workspace(
    project: Live2DPSDProject,
    version_id: str | None = None,
    log: LogCallback | None = None,
) -> Path:
    entry = find_repack_entry(project, version_id)
    if not entry:
        raise Live2DPSDProjectError("No repacked texture version is available for preview.")

    preview_dir = project.project_dir / str(
        normalize_project_data(project.data, project.project_name)
        .get("preview", {})
        .get("current_dir", "preview/current")
    )
    if preview_dir.exists():
        shutil.rmtree(preview_dir)
    _copy_workspace(project.live2d_dir, preview_dir)

    package = resolve_live2d_package(preview_dir)
    outputs = [
        resolve_project_path(project, value)
        for value in (entry.get("output_paths") or [])
        if str(value or "").strip()
    ]
    if not outputs:
        raise Live2DPSDProjectError(f"Repack version has no output texture paths: {entry.get('id')}")

    _overlay_preview_textures(outputs, package.texture_paths, log)
    return package.model_json


def resolve_project_path(project: Live2DPSDProject, value: str | Path) -> Path:
    path = Path(str(value))
    return path.resolve() if path.is_absolute() else (project.project_dir / path).resolve()


def timestamp_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def sanitize_project_name(name: Any) -> str:
    text = str(name or "").strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text)
    text = text.strip(" ._")
    return text or "psd_project"


def _import_package(
    source_path: Path,
    temp_root: str | Path | None,
    log: LogCallback | None,
) -> tuple[Live2DPackage, Path | None, list[str]]:
    try:
        return resolve_live2d_package(source_path), None, []
    except Live2DPackageError:
        pass

    settings = SettingsManager()
    result = prepare_preview_import(
        source_path,
        temp_root or settings.get_temp_dir(),
        log=log,
    )
    return result.package, result.temp_dir, result.warnings


def _project_output_root(output_root: str | Path | None) -> Path:
    if output_root:
        root = Path(output_root).resolve()
    else:
        root = Path(SettingsManager().get_output_dir("psd_projects")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _default_project_name(source_path: Path, package: Live2DPackage) -> str:
    if source_path.is_file() and source_path.suffix.lower() in {".json", ".moc3"}:
        return package.root_dir.name
    if source_path.name:
        return source_path.stem if source_path.is_file() else source_path.name
    return package.name


def _unique_project_dir(path: Path) -> Path:
    candidate = path
    index = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}_{index}")
        index += 1
    return candidate


def _copy_workspace(source_dir: Path, target_dir: Path) -> None:
    shutil.copytree(
        source_dir,
        target_dir,
        ignore=shutil.ignore_patterns("*.pretty.json", "__pycache__"),
    )


def _ensure_project_dirs(project_dir: Path) -> None:
    for name in ("live2d", "psd", "repacks", "preview/current"):
        (project_dir / name).mkdir(parents=True, exist_ok=True)


def _write_repack_manifest(textures_dir: Path, entry: dict[str, Any]) -> None:
    textures_dir.mkdir(parents=True, exist_ok=True)
    with (textures_dir / "manifest.json").open("w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _overlay_preview_textures(
    outputs: list[Path],
    target_textures: list[Path],
    log: LogCallback | None,
) -> None:
    copied_targets: set[Path] = set()
    for output, target in zip(outputs, target_textures):
        if not output.is_file():
            raise Live2DPSDProjectError(f"Repacked texture is missing: {output}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(output, target)
        copied_targets.add(target.resolve())
        if log:
            log(f"Preview texture replaced: {target.name}")

    remaining_targets = {
        target.name.lower(): target
        for target in target_textures
        if target.resolve() not in copied_targets
    }
    for output in outputs[len(target_textures):]:
        target = remaining_targets.get(output.name.lower())
        if not target:
            continue
        if not output.is_file():
            raise Live2DPSDProjectError(f"Repacked texture is missing: {output}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(output, target)
        if log:
            log(f"Preview texture replaced: {target.name}")


def _relative_to_project(path: Path, project_dir: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(project_dir.resolve()).as_posix()
    except ValueError:
        return str(resolved)
