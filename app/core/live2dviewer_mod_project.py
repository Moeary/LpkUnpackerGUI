from __future__ import annotations

import json
import re
import shutil
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.core.model import Live2DPackage, resolve_live2d_package
from app.core.model.importer import prepare_live2d_source_import
from app.core.settings_manager import SettingsManager


PROJECT_FORMAT = "LpkUnpacker.Live2DViewerModProject"
PROJECT_VERSION = 1
PROJECT_FILE_NAME = "project.live2dviewer_mod.json"
GENERATED_DIR = "generated/live2d"
TEMP_PREVIEW_DIR = "preview/temporary"
SKIN_MANIFEST_FILE = "skin_manifest.json"
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
        "auxiliary_sources": [],
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


def generate_live2dviewer_mod(
    project: Live2DViewerModProject,
    log: LogCallback | None = None,
) -> Live2DViewerModProject:
    data = normalize_project_data(project.data, project.project_dir.name)
    project_dir = project.project_dir.resolve()
    base_package = resolve_live2d_package(project.base_model_json)
    output_dir = project_dir / GENERATED_DIR

    _log(log, f"Preparing generated workspace: {output_dir}")
    if output_dir.exists():
        shutil.rmtree(output_dir)
    _copy_workspace(base_package.root_dir, output_dir)

    generated_package = resolve_live2d_package(output_dir)
    base_model_json = generated_package.model_json
    base_model = _read_json(base_model_json)
    texture_refs = _texture_refs(base_model)
    skins = _normalize_skins_for_generation(data)

    if not skins:
        raise Live2DViewerModProjectError("No skins are configured.")

    selected_hit_area = str(data.get("selected_hit_area") or "").strip()
    if not selected_hit_area:
        _log(log, "Warning: no HitArea selected; generated models can be previewed, but tap switching is not wired.")

    _log(log, f"Writing base model switch menu with {len(skins)} skin(s)")
    model_records: list[dict[str, str]] = [
        {
            "name": "Original",
            "model_json": _relative_to_project(base_model_json, project_dir),
        }
    ]
    generated_model_paths = [_relative_to_project(base_model_json, project_dir)]

    for index, skin in enumerate(skins, start=1):
        skin_name = str(skin.get("name") or f"Skin {index}").strip() or f"Skin {index}"
        skin_id = _skin_id(skin, index)
        skin_model = deepcopy(base_model)
        new_texture_refs = list(texture_refs)
        replacements = _skin_replacements(data, skin)

        for replacement in replacements:
            target_index = _target_texture_index(texture_refs, replacement)
            if target_index < 0 or target_index >= len(texture_refs):
                continue
            source = Path(str(replacement.get("source") or "")).resolve()
            if not source.is_file():
                continue
            copied = _copy_skin_texture(source, output_dir, skin_id, target_index, texture_refs[target_index])
            new_texture_refs[target_index] = _relative_posix(copied, output_dir)
            replacement["workspace_path"] = _relative_to_project(copied, project_dir)
            replacement["status"] = "generated"

        _set_texture_refs(skin_model, new_texture_refs)
        skin_model_name = f"skin_{index}.json"
        skin_model_path = output_dir / skin_model_name
        _write_json(skin_model_path, skin_model)
        skin["generated_model_json_path"] = _relative_to_project(skin_model_path, project_dir)
        skin["status"] = "generated"
        generated_model_paths.append(skin["generated_model_json_path"])
        model_records.append(
            {
                "name": skin_name,
                "model_json": _relative_to_project(skin_model_path, project_dir),
            }
        )
        _log(log, f"Generated skin model: {skin_name} -> {skin_model_path}")

    _inject_switch_menu(
        base_model,
        generated_model_name=base_model_json.name,
        skins=skins,
        selected_hit_area=selected_hit_area,
    )
    _write_json(base_model_json, base_model)

    manifest_path = output_dir / SKIN_MANIFEST_FILE
    _write_json(
        manifest_path,
        {
            "version": 1,
            "project_name": data.get("project_name") or project.project_name,
            "selected_hit_area": selected_hit_area,
            "models": model_records,
        },
    )

    data["skins"] = skins
    data["generated_skin_json_paths"] = [_relative_to_project(manifest_path, project_dir)]
    data["generated_model_json_paths"] = generated_model_paths
    data["generated_output_dir"] = _relative_to_project(output_dir, project_dir)
    project_file = save_project(project_dir, data)
    _log(log, f"Generated Live2DViewer mod files: {output_dir}")
    return Live2DViewerModProject(project_dir, project_file, data)


def build_temporary_texture_preview_model(
    project: Live2DViewerModProject,
    source_texture: str | Path,
    target_index: int,
    log: LogCallback | None = None,
) -> Path:
    project_dir = project.project_dir.resolve()
    source = Path(source_texture).resolve()
    if not source.is_file():
        raise Live2DViewerModProjectError(f"Texture source does not exist: {source}")

    base_package = resolve_live2d_package(project.base_model_json)
    preview_dir = project_dir / TEMP_PREVIEW_DIR
    if preview_dir.exists():
        shutil.rmtree(preview_dir)
    _copy_workspace(base_package.root_dir, preview_dir)

    preview_package = resolve_live2d_package(preview_dir)
    model_json = preview_package.model_json
    model_data = _read_json(model_json)
    texture_refs = _texture_refs(model_data)
    if target_index < 0 or target_index >= len(texture_refs):
        raise Live2DViewerModProjectError(f"Invalid texture target index: {target_index}")

    copied = _copy_skin_texture(source, preview_dir, "__temporary__", target_index, texture_refs[target_index])
    texture_refs[target_index] = _relative_posix(copied, preview_dir)
    _set_texture_refs(model_data, texture_refs)
    _write_json(model_json, model_data)
    _log(log, f"Temporary texture preview model: {model_json}")
    return model_json


def normalize_project_data(data: dict[str, Any], fallback_name: str) -> dict[str, Any]:
    payload = dict(data)
    payload["format"] = PROJECT_FORMAT
    payload["version"] = int(payload.get("version") or PROJECT_VERSION)
    payload["project_name"] = sanitize_project_name(payload.get("project_name") or fallback_name)
    payload.setdefault("base_source", {})
    payload.setdefault("base_model_json", "")
    payload.setdefault("imported_workspace_paths", {})
    payload.setdefault("auxiliary_sources", [])
    payload.setdefault("skins", [])
    payload.setdefault("texture_replacements", [])
    payload.setdefault("selected_hit_area", "")
    payload.setdefault("hit_areas", [])
    payload.setdefault("generated_skin_json_paths", [])
    payload.setdefault("generated_model_json_paths", [])
    payload.setdefault("generated_output_dir", "")
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


def sanitize_identifier(name: Any, fallback: str = "skin") -> str:
    text = sanitize_project_name(name)
    text = re.sub(r"[^0-9A-Za-z_.-]+", "_", text)
    text = text.strip("._-")
    return text or fallback


def _import_base_package(
    source_path: Path,
    temp_root: str | Path | None,
    log: LogCallback | None,
) -> tuple[Live2DPackage, Path | None, list[str]]:
    result = prepare_live2d_source_import(source_path, temp_root=temp_root, log=log)
    return result.package, result.temp_dir, result.warnings


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


def _normalize_skins_for_generation(data: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, item in enumerate(data.get("skins") or [], start=1):
        if not isinstance(item, dict):
            continue
        skin = dict(item)
        skin.setdefault("id", f"skin_{index}")
        skin.setdefault("name", f"Skin {index}")
        skin.setdefault("texture_replacements", [])
        result.append(skin)
    return result


def _skin_id(skin: dict[str, Any], index: int) -> str:
    return sanitize_identifier(skin.get("id") or skin.get("name") or f"skin_{index}", f"skin_{index}")


def _skin_replacements(data: dict[str, Any], skin: dict[str, Any]) -> list[dict[str, Any]]:
    replacements = []
    for item in skin.get("texture_replacements") or []:
        if isinstance(item, dict):
            replacements.append(item)
    skin_id = str(skin.get("id") or "")
    for item in data.get("texture_replacements") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("skin_id") or "") == skin_id:
            replacements.append(item)
    return replacements


def _texture_refs(model_data: dict[str, Any]) -> list[str]:
    refs = model_data.get("FileReferences") or {}
    textures = refs.get("Textures") if isinstance(refs, dict) else []
    return [str(item) for item in textures if isinstance(item, str)]


def _set_texture_refs(model_data: dict[str, Any], texture_refs: list[str]) -> None:
    refs = model_data.setdefault("FileReferences", {})
    if not isinstance(refs, dict):
        refs = {}
        model_data["FileReferences"] = refs
    refs["Textures"] = list(texture_refs)


def _target_texture_index(texture_refs: list[str], replacement: dict[str, Any]) -> int:
    raw_index = replacement.get("target_index")
    if isinstance(raw_index, int):
        return raw_index
    if isinstance(raw_index, str) and raw_index.isdigit():
        return int(raw_index)

    target = str(replacement.get("target_texture") or "").replace("\\", "/")
    if not target:
        return -1
    target_name = Path(target).name.lower()
    for index, texture in enumerate(texture_refs):
        normalized = texture.replace("\\", "/")
        if normalized == target or Path(normalized).name.lower() == target_name:
            return index
    return -1


def _copy_skin_texture(
    source: Path,
    output_dir: Path,
    skin_id: str,
    target_index: int,
    target_ref: str,
) -> Path:
    texture_dir = output_dir / "textures" / skin_id
    texture_dir.mkdir(parents=True, exist_ok=True)
    target_name = Path(target_ref.replace("\\", "/")).name or f"texture_{target_index}.png"
    target_path = texture_dir / target_name
    if source.suffix and source.suffix.lower() != target_path.suffix.lower():
        target_path = target_path.with_suffix(source.suffix.lower())
    shutil.copy2(source, target_path)
    return target_path


def _inject_switch_menu(
    model_data: dict[str, Any],
    generated_model_name: str,
    skins: list[dict[str, Any]],
    selected_hit_area: str,
) -> None:
    refs = model_data.setdefault("FileReferences", {})
    if not isinstance(refs, dict):
        refs = {}
        model_data["FileReferences"] = refs
    motions = refs.setdefault("Motions", {})
    if not isinstance(motions, dict):
        motions = {}
        refs["Motions"] = motions

    menu_motion = "TapSwitchSkin"
    switch_motion = "SwitchSkin"
    choices = [{"Text": "Original", "NextMtn": f"{switch_motion}:0"}]
    switch_items = [{"Name": "0", "Command": f"change_model {generated_model_name}"}]
    for index, skin in enumerate(skins, start=1):
        name = str(skin.get("name") or f"Skin {index}").strip() or f"Skin {index}"
        choices.append({"Text": name, "NextMtn": f"{switch_motion}:{index}"})
        switch_items.append({"Name": str(index), "Command": f"change_model skin_{index}.json"})

    motions[menu_motion] = [
        {
            "Name": "0",
            "Text": "Switch Skin",
            "Choices": choices,
        }
    ]
    motions[switch_motion] = switch_items

    if not selected_hit_area:
        return
    hit_areas = model_data.setdefault("HitAreas", [])
    if not isinstance(hit_areas, list):
        hit_areas = []
        model_data["HitAreas"] = hit_areas
    for item in hit_areas:
        if isinstance(item, dict) and str(item.get("Name") or "") == "Switch Skin":
            item["Id"] = selected_hit_area
            item["Motion"] = menu_motion
            return
    hit_areas.append(
        {
            "Name": "Switch Skin",
            "Id": selected_hit_area,
            "Motion": menu_motion,
        }
    )


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise Live2DViewerModProjectError(f"JSON root is not an object: {path}")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _relative_posix(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _log(log: LogCallback | None, message: str) -> None:
    if log:
        log(message)


def _relative_to_project(path: Path, project_dir: Path) -> str:
    try:
        return path.resolve().relative_to(project_dir.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())
