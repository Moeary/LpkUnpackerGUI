from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.core.model import Live2DPackage, Live2DPackageError, resolve_live2d_package
from app.core.preview.session import prepare_preview_import
from app.core.settings_manager import SettingsManager


PROJECT_FORMAT = "LpkUnpacker.Live2DViewerModProject"
PROJECT_VERSION = 1
PROJECT_FILE_NAME = "project.live2dviewer_mod.json"
LogCallback = Callable[[str], None]


class Live2DViewerModProjectError(RuntimeError):
    pass


@dataclass(frozen=True)
class Live2DViewerModProject:
    project_dir: Path
    project_file: Path
    data: dict[str, Any]

    @property
    def project_name(self) -> str:
        return str(self.data.get("project_name") or self.project_dir.name)

    @property
    def base_model_json(self) -> Path:
        value = str(self.data.get("base_model_json") or "")
        return (self.project_dir / value).resolve() if value else self.project_dir


def create_project_from_base_source(
    source: str | Path,
    project_name: str | None = None,
    output_root: str | Path | None = None,
    temp_root: str | Path | None = None,
    log: LogCallback | None = None,
) -> Live2DViewerModProject:
    source_path = Path(source).resolve()
    package, temp_dir, warnings = _import_base_package(source_path, temp_root, log)

    resolved_name = sanitize_project_name(project_name or _default_project_name(source_path, package))
    project_root = _project_output_root(output_root)
    project_dir = _unique_project_dir(project_root / resolved_name)
    workspace_dir = project_dir / "workspace" / "base"
    workspace_dir.parent.mkdir(parents=True, exist_ok=True)

    try:
        _copy_workspace(package.root_dir, workspace_dir)
        workspace_package = resolve_live2d_package(workspace_dir)
        hit_areas = read_hit_areas(workspace_package.model_json)
        project_data = build_project_data(
            project_name=project_dir.name,
            base_source=source_path,
            base_package=workspace_package,
            workspace_dir=workspace_dir,
            project_dir=project_dir,
            hit_areas=hit_areas,
            warnings=warnings,
        )
        project_file = save_project(project_dir, project_data)
        return Live2DViewerModProject(project_dir, project_file, project_data)
    except Exception:
        shutil.rmtree(project_dir, ignore_errors=True)
        raise
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


def build_project_data(
    project_name: str,
    base_source: Path,
    base_package: Live2DPackage,
    workspace_dir: Path,
    project_dir: Path,
    hit_areas: list[dict[str, str]],
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
        "base_model_json": _relative_to_project(base_package.model_json, project_dir),
        "imported_workspace_paths": {
            "base": _relative_to_project(workspace_dir, project_dir),
        },
        "skins": [],
        "texture_replacements": [],
        "selected_hit_area": "",
        "hit_areas": hit_areas,
        "generated_skin_json_paths": [],
        "generated_model_json_paths": [],
        "warnings": warnings or [],
    }


def save_project(project_dir: str | Path, data: dict[str, Any]) -> Path:
    directory = Path(project_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    payload = normalize_project_data(data, directory.name)
    project_file = directory / PROJECT_FILE_NAME
    with project_file.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return project_file


def load_project(project_file: str | Path) -> Live2DViewerModProject:
    path = Path(project_file).resolve()
    if path.is_dir():
        path = path / PROJECT_FILE_NAME
    if not path.is_file():
        raise Live2DViewerModProjectError(f"Project file does not exist: {path}")
    with path.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict) or data.get("format") != PROJECT_FORMAT:
        raise Live2DViewerModProjectError(f"Unsupported Live2DViewer mod project: {path}")
    return Live2DViewerModProject(path.parent, path, normalize_project_data(data, path.parent.name))


def normalize_project_data(data: dict[str, Any], fallback_name: str) -> dict[str, Any]:
    payload = dict(data)
    payload["format"] = PROJECT_FORMAT
    payload["version"] = int(payload.get("version") or PROJECT_VERSION)
    payload["project_name"] = sanitize_project_name(payload.get("project_name") or fallback_name)
    payload.setdefault("base_source", {})
    payload.setdefault("base_model_json", "")
    payload.setdefault("imported_workspace_paths", {})
    payload.setdefault("skins", [])
    payload.setdefault("texture_replacements", [])
    payload.setdefault("selected_hit_area", "")
    payload.setdefault("hit_areas", [])
    payload.setdefault("generated_skin_json_paths", [])
    payload.setdefault("generated_model_json_paths", [])
    payload.setdefault("warnings", [])
    return payload


def read_hit_areas(model_json: str | Path) -> list[dict[str, str]]:
    path = Path(model_json).resolve()
    with path.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)
    hit_areas = data.get("HitAreas") if isinstance(data, dict) else None
    if not isinstance(hit_areas, list):
        return []

    result: list[dict[str, str]] = []
    for item in hit_areas:
        if not isinstance(item, dict):
            continue
        hit_id = str(item.get("Id") or item.get("id") or "").strip()
        name = str(item.get("Name") or item.get("name") or hit_id).strip()
        if hit_id:
            result.append({"id": hit_id, "name": name or hit_id})
    return result


def sanitize_project_name(name: Any) -> str:
    text = str(name or "").strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text)
    text = text.strip(" ._")
    return text or "live2dviewer_mod"


def _import_base_package(
    source_path: Path,
    temp_root: str | Path | None,
    log: LogCallback | None,
) -> tuple[Live2DPackage, Path | None, list[str]]:
    try:
        return resolve_live2d_package(source_path), None, []
    except Live2DPackageError:
        pass

    settings = SettingsManager()
    import_result = prepare_preview_import(
        source_path,
        temp_root or settings.get_temp_dir(),
        log=log,
    )
    return import_result.package, import_result.temp_dir, import_result.warnings


def _project_output_root(output_root: str | Path | None) -> Path:
    if output_root:
        root = Path(output_root).resolve()
    else:
        root = Path(SettingsManager().get_output_root()).resolve() / "live2dviewer_mod"
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


def _relative_to_project(path: Path, project_dir: Path) -> str:
    try:
        return path.resolve().relative_to(project_dir.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())
