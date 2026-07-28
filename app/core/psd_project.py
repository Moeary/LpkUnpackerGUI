from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageChops

from app.core.model import Live2DPackage, resolve_live2d_package
from app.core.model.importer import prepare_live2d_source_import
from app.core.settings_manager import SettingsManager


PROJECT_FORMAT = "LpkUnpacker.Live2DPSDProject"
PROJECT_VERSION = 4
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
        "parameter_presets": [_default_parameter_preset()],
        "selected_parameter_preset": "default",
        "pose_schemes": [],
        "selected_pose_scheme": "",
        "repack_history": [],
        "selected_repack": "",
        "composite_history": [],
        "selected_composite": "",
        "preview": {
            "current_dir": "preview/current",
            "mode": "original",
        },
        "ui_state": {},
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
    payload["version"] = PROJECT_VERSION
    payload["project_name"] = sanitize_project_name(payload.get("project_name") or fallback_name)
    payload.setdefault("base_source", {})
    payload.setdefault("workspace", {})
    payload["workspace"].setdefault("live2d_dir", "live2d")
    payload["workspace"].setdefault("base_model_json", "")
    payload.setdefault("psd_exports", [])
    payload.setdefault("parameter_presets", [])
    payload.setdefault("selected_parameter_preset", "default")
    payload.setdefault("pose_schemes", [])
    payload.setdefault("selected_pose_scheme", "")
    payload.setdefault("repack_history", [])
    payload.setdefault("selected_repack", "")
    payload.setdefault("composite_history", [])
    payload.setdefault("selected_composite", "")
    payload.setdefault("preview", {})
    payload["preview"].setdefault("current_dir", "preview/current")
    payload["preview"].setdefault("mode", "original")
    payload.setdefault("ui_state", {})
    payload.setdefault("warnings", [])
    for scheme in payload.get("pose_schemes") or []:
        if not isinstance(scheme, dict):
            continue
        scheme.setdefault(
            "pose_source",
            "preview" if scheme.get("parameters") else "initial",
        )
        scheme.setdefault(
            "exported_parameters",
            dict(scheme.get("parameters") or {}),
        )
        for version in scheme.get("versions") or []:
            if isinstance(version, dict):
                version.setdefault("name", str(version.get("id") or "texture"))
        preset_id = str(
            scheme.get("parameter_preset_id") or scheme.get("id") or ""
        )
        scheme.setdefault("parameter_preset_id", preset_id)
        if preset_id and not any(
            str(item.get("id") or "") == preset_id
            for item in payload["parameter_presets"]
            if isinstance(item, dict)
        ):
            payload["parameter_presets"].append(
                {
                    "id": preset_id,
                    "name": str(scheme.get("name") or preset_id),
                    "parameters": dict(
                        scheme.get("exported_parameters")
                        or scheme.get("parameters")
                        or {}
                    ),
                    "source": str(scheme.get("pose_source") or "preview"),
                    "is_default": False,
                    "created_at": str(
                        scheme.get("created_at")
                        or datetime.now().isoformat(timespec="seconds")
                    ),
                }
            )
    if not any(
        isinstance(item, dict) and str(item.get("id") or "") == "default"
        for item in payload["parameter_presets"]
    ):
        payload["parameter_presets"].insert(0, _default_parameter_preset())
    for preset in payload["parameter_presets"]:
        if not isinstance(preset, dict):
            continue
        if str(preset.get("id") or "") == "default":
            preset["is_default"] = True
            preset["source"] = "initial"
            preset["parameters"] = {}
        preset.setdefault("parameters", {})
        preset.setdefault("source", "initial" if preset.get("is_default") else "preview")
        preset.setdefault("is_default", str(preset.get("id") or "") == "default")
    available_presets = {
        str(item.get("id") or "")
        for item in payload["parameter_presets"]
        if isinstance(item, dict)
    }
    if str(payload.get("selected_parameter_preset") or "") not in available_presets:
        payload["selected_parameter_preset"] = "default"
    return payload


def _default_parameter_preset() -> dict[str, Any]:
    return {
        "id": "default",
        "name": "Default",
        "parameters": {},
        "source": "initial",
        "is_default": True,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def find_parameter_preset(
    project: Live2DPSDProject,
    preset_id: str | None = None,
) -> dict[str, Any] | None:
    data = normalize_project_data(project.data, project.project_name)
    target = str(
        preset_id
        if preset_id is not None
        else data.get("selected_parameter_preset") or "default"
    )
    for preset in data.get("parameter_presets") or []:
        if isinstance(preset, dict) and str(preset.get("id") or "") == target:
            return dict(preset)
    return None


def create_parameter_preset(
    project: Live2DPSDProject,
    name: str,
    parameters: dict[str, float],
) -> tuple[Live2DPSDProject, dict[str, Any]]:
    display_name = str(name or "").strip()
    if not display_name:
        raise Live2DPSDProjectError("Parameter preset name cannot be empty.")
    preset_id = sanitize_project_name(display_name)
    if preset_id.lower() == "default":
        raise Live2DPSDProjectError("The default parameter preset is reserved.")
    data = normalize_project_data(project.data, project.project_name)
    if any(
        str(item.get("id") or "").casefold() == preset_id.casefold()
        for item in data.get("parameter_presets") or []
        if isinstance(item, dict)
    ):
        raise Live2DPSDProjectError(f"Parameter preset already exists: {display_name}")
    preset = {
        "id": preset_id,
        "name": display_name,
        "parameters": {
            str(key): float(value)
            for key, value in dict(parameters or {}).items()
        },
        "source": "preview",
        "is_default": False,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    data.setdefault("parameter_presets", []).append(preset)
    data["selected_parameter_preset"] = preset_id
    save_project(project.project_dir, data)
    return Live2DPSDProject(project.project_dir, project.project_file, data), preset


def select_parameter_preset(
    project: Live2DPSDProject,
    preset_id: str,
) -> Live2DPSDProject:
    if find_parameter_preset(project, preset_id) is None:
        raise Live2DPSDProjectError(
            f"Parameter preset does not exist: {preset_id}"
        )
    data = normalize_project_data(project.data, project.project_name)
    data["selected_parameter_preset"] = str(preset_id)
    save_project(project.project_dir, data)
    return Live2DPSDProject(project.project_dir, project.project_file, data)


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


def create_pose_scheme(
    project: Live2DPSDProject,
    name: str,
    priority: int,
    parameters: dict[str, float],
    pose_source: str = "preview",
    parameter_preset_id: str = "",
) -> tuple[Live2DPSDProject, dict[str, Any], Path]:
    """Create a named, PSD-page-owned pose workspace."""
    display_name = str(name or "").strip()
    if not display_name:
        raise Live2DPSDProjectError("Pose scheme name cannot be empty.")
    scheme_id = sanitize_project_name(parameter_preset_id or display_name)
    data = normalize_project_data(project.data, project.project_name)
    existing = find_pose_scheme(project, scheme_id)
    if existing:
        scheme_dir = project.project_dir / str(
            existing.get("directory") or f"psd/{scheme_id}"
        )
        existing_parameters = dict(
            existing.get("exported_parameters")
            or existing.get("parameters")
            or {}
        )
        requested_parameters = {
            str(key): float(value)
            for key, value in dict(parameters or {}).items()
        }
        if existing_parameters != requested_parameters:
            raise Live2DPSDProjectError(
                f"PSD name is already bound to different parameters: {display_name}"
            )
        scheme_dir.mkdir(parents=True, exist_ok=True)
        return project, existing, scheme_dir

    scheme_dir = project.project_dir / "psd" / scheme_id
    scheme_dir.mkdir(parents=True, exist_ok=False)
    package = resolve_live2d_package(project.live2d_dir)
    source_textures: list[str] = []
    for index, source in enumerate(package.texture_paths):
        suffix = source.suffix or ".png"
        target = scheme_dir / f"texture_{index:02d}{suffix}"
        shutil.copy2(source, target)
        source_textures.append(_relative_to_project(target, project.project_dir))

    entry = {
        "id": scheme_id,
        "name": display_name,
        "priority": int(priority),
        "directory": _relative_to_project(scheme_dir, project.project_dir),
        "parameters": {
            str(key): float(value)
            for key, value in dict(parameters or {}).items()
        },
        "exported_parameters": {
            str(key): float(value)
            for key, value in dict(parameters or {}).items()
        },
        "pose_source": "initial" if pose_source == "initial" else "preview",
        "parameter_preset_id": str(parameter_preset_id or scheme_id),
        "source_textures": source_textures,
        "psd": "",
        "metadata": "",
        "mode": "mesh",
        "layer_count": 0,
        "versions": [],
        "selected_version": "",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    data.setdefault("pose_schemes", []).append(entry)
    data["selected_pose_scheme"] = scheme_id
    save_project(project.project_dir, data)
    updated = Live2DPSDProject(project.project_dir, project.project_file, data)
    return updated, entry, scheme_dir


def record_pose_scheme_export(
    project: Live2DPSDProject,
    scheme_id: str,
    psd_path: str | Path,
    metadata_path: str | Path | None,
    mode: str,
    layer_count: int,
) -> Live2DPSDProject:
    data = normalize_project_data(project.data, project.project_name)
    scheme = _mutable_pose_scheme(data, scheme_id)
    if scheme is None:
        raise Live2DPSDProjectError(f"Pose scheme does not exist: {scheme_id}")
    scheme["psd"] = _relative_to_project(Path(psd_path), project.project_dir)
    scheme["metadata"] = (
        _relative_to_project(Path(metadata_path), project.project_dir)
        if metadata_path
        else ""
    )
    scheme["mode"] = str(mode)
    scheme["layer_count"] = int(layer_count)
    scheme["updated_at"] = datetime.now().isoformat(timespec="seconds")
    data["selected_pose_scheme"] = str(scheme_id)
    save_project(project.project_dir, data)
    return Live2DPSDProject(project.project_dir, project.project_file, data)


def find_pose_scheme(
    project: Live2DPSDProject,
    scheme_id: str | None = None,
) -> dict[str, Any] | None:
    data = normalize_project_data(project.data, project.project_name)
    explicit = scheme_id is not None
    target = str(scheme_id or data.get("selected_pose_scheme") or "")
    schemes = data.get("pose_schemes") or []
    if target:
        for entry in schemes:
            if str(entry.get("id") or "") == target:
                return dict(entry)
        if explicit:
            return None
    return dict(schemes[-1]) if schemes else None


def set_pose_scheme_priority(
    project: Live2DPSDProject,
    scheme_id: str,
    priority: int,
) -> Live2DPSDProject:
    data = normalize_project_data(project.data, project.project_name)
    scheme = _mutable_pose_scheme(data, scheme_id)
    if scheme is None:
        raise Live2DPSDProjectError(f"Pose scheme does not exist: {scheme_id}")
    scheme["priority"] = int(priority)
    scheme["updated_at"] = datetime.now().isoformat(timespec="seconds")
    save_project(project.project_dir, data)
    return Live2DPSDProject(project.project_dir, project.project_file, data)


def select_pose_scheme(
    project: Live2DPSDProject,
    scheme_id: str,
) -> Live2DPSDProject:
    if find_pose_scheme(project, scheme_id) is None:
        raise Live2DPSDProjectError(f"Pose scheme does not exist: {scheme_id}")
    data = normalize_project_data(project.data, project.project_name)
    data["selected_pose_scheme"] = str(scheme_id)
    save_project(project.project_dir, data)
    return Live2DPSDProject(project.project_dir, project.project_file, data)


def create_repack_dir(
    project: Live2DPSDProject,
    scheme_id: str | None = None,
    texture_name: str | None = None,
) -> tuple[str, Path]:
    repack_root = project.project_dir / "tex"
    repack_root = repack_root / sanitize_project_name(scheme_id or "unassigned")
    repack_root.mkdir(parents=True, exist_ok=True)
    version_id = timestamp_id()
    existing_ids = {
        str(entry.get("id") or "")
        for entry in (project.data.get("repack_history") or [])
        if isinstance(entry, dict)
    }
    version_index = 1
    base_version_id = version_id
    while version_id in existing_ids:
        version_id = f"{base_version_id}_{version_index}"
        version_index += 1
    folder_name = sanitize_project_name(texture_name or version_id)
    candidate = repack_root / folder_name
    index = 1
    while candidate.exists():
        candidate = repack_root / f"{folder_name}_{index}"
        index += 1
    candidate.mkdir(parents=True, exist_ok=True)
    return version_id, candidate


def record_repack(
    project: Live2DPSDProject,
    version_id: str,
    source_psd: str | Path,
    metadata_path: str | Path | None,
    textures_dir: str | Path,
    output_paths: list[Path],
    scheme_id: str = "",
    display_name: str = "",
) -> Live2DPSDProject:
    data = normalize_project_data(project.data, project.project_name)
    entry = {
        "id": version_id,
        "name": str(display_name or Path(textures_dir).name or version_id),
        "source_psd": _relative_to_project(Path(source_psd), project.project_dir),
        "metadata": _relative_to_project(Path(metadata_path), project.project_dir)
        if metadata_path
        else "",
        "textures_dir": _relative_to_project(Path(textures_dir), project.project_dir),
        "output_paths": [_relative_to_project(Path(path), project.project_dir) for path in output_paths],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "scheme_id": str(scheme_id or ""),
    }
    data.setdefault("repack_history", []).append(entry)
    data["selected_repack"] = version_id
    if scheme_id:
        scheme = _mutable_pose_scheme(data, scheme_id)
        if scheme is None:
            raise Live2DPSDProjectError(f"Pose scheme does not exist: {scheme_id}")
        scheme.setdefault("versions", []).append(dict(entry))
        scheme["selected_version"] = version_id
        scheme["updated_at"] = entry["created_at"]
        data["selected_pose_scheme"] = str(scheme_id)
    save_project(project.project_dir, data)
    _write_repack_manifest(Path(textures_dir), entry)
    return Live2DPSDProject(project.project_dir, project.project_file, data)


def compose_pose_versions(
    project: Live2DPSDProject,
    log: LogCallback | None = None,
) -> tuple[Live2DPSDProject, dict[str, Any]]:
    """Compose all selected pose repacks; higher numeric priority wins overlaps."""
    data = normalize_project_data(project.data, project.project_name)
    schemes = [
        dict(item)
        for item in (data.get("pose_schemes") or [])
        if item.get("versions")
    ]
    if not schemes:
        raise Live2DPSDProjectError("No pose texture version is available to compose.")
    schemes.sort(key=lambda item: (int(item.get("priority") or 0), str(item.get("id") or "")))

    package = resolve_live2d_package(project.live2d_dir)
    composite_id = timestamp_id()
    root = project.project_dir / "composites" / composite_id
    index = 1
    while root.exists():
        root = project.project_dir / "composites" / f"{composite_id}_{index}"
        index += 1
    textures_dir = root / "textures"
    textures_dir.mkdir(parents=True, exist_ok=True)

    selected_inputs: list[dict[str, Any]] = []
    resolved_versions: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for scheme in schemes:
        selected = str(scheme.get("selected_version") or "")
        versions = list(scheme.get("versions") or [])
        version = next(
            (item for item in versions if str(item.get("id") or "") == selected),
            versions[-1],
        )
        resolved_versions.append((scheme, version))
        selected_inputs.append({
            "scheme_id": str(scheme.get("id") or ""),
            "name": str(scheme.get("name") or scheme.get("id") or ""),
            "priority": int(scheme.get("priority") or 0),
            "version_id": str(version.get("id") or ""),
        })

    output_paths: list[Path] = []
    for texture_index, base_path in enumerate(package.texture_paths):
        with Image.open(base_path) as source_image:
            base = source_image.convert("RGBA")
        composed = base.copy()
        for scheme, version in resolved_versions:
            values = version.get("output_paths") or []
            if texture_index >= len(values):
                continue
            candidate = resolve_project_path(project, values[texture_index])
            if not candidate.is_file():
                continue
            with Image.open(candidate) as changed_image:
                changed = changed_image.convert("RGBA")
            if changed.size != base.size:
                raise Live2DPSDProjectError(
                    f"Pose texture size mismatch: {candidate.name} {changed.size} != {base.size}"
                )
            diff = ImageChops.difference(changed, base)
            if diff.getbbox() is None:
                if log:
                    log(
                        f"Skipped unchanged pose {scheme.get('name') or scheme.get('id')} "
                        f"for {base_path.name}"
                    )
                continue
            channels = diff.split()
            mask = channels[0]
            for channel in channels[1:]:
                mask = ImageChops.lighter(mask, channel)
            effective = ImageChops.difference(changed, composed)
            if effective.getbbox() is None:
                if log:
                    log(
                        f"Skipped duplicate pose pixels from "
                        f"{scheme.get('name') or scheme.get('id')}"
                    )
                continue
            effective_channels = effective.split()
            effective_mask = effective_channels[0]
            for channel in effective_channels[1:]:
                effective_mask = ImageChops.lighter(effective_mask, channel)
            changed_mask = ImageChops.multiply(
                mask.point(lambda value: 255 if value else 0),
                effective_mask.point(lambda value: 255 if value else 0),
            )
            if changed_mask.getbbox() is None:
                continue
            composed = Image.composite(changed, composed, changed_mask)
            if log:
                log(
                    f"Applied pose {scheme.get('name') or scheme.get('id')} "
                    f"(priority {int(scheme.get('priority') or 0)})"
                )
        target = textures_dir / base_path.name
        composed.save(target, format="PNG")
        output_paths.append(target)

    entry = {
        "id": root.name,
        "inputs": selected_inputs,
        "textures_dir": _relative_to_project(textures_dir, project.project_dir),
        "output_paths": [
            _relative_to_project(path, project.project_dir)
            for path in output_paths
        ],
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    data.setdefault("composite_history", []).append(entry)
    data["selected_composite"] = entry["id"]
    save_project(project.project_dir, data)
    _write_repack_manifest(textures_dir, entry)
    return Live2DPSDProject(project.project_dir, project.project_file, data), entry


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


def find_composite_entry(
    project: Live2DPSDProject,
    version_id: str | None = None,
) -> dict[str, Any] | None:
    data = normalize_project_data(project.data, project.project_name)
    target_id = str(version_id or data.get("selected_composite") or "")
    history = data.get("composite_history") or []
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
    if version_id and (
        not entry or str(entry.get("id") or "") != str(version_id)
    ):
        entry = find_composite_entry(project, version_id)
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


def _mutable_pose_scheme(
    data: dict[str, Any],
    scheme_id: str,
) -> dict[str, Any] | None:
    target = str(scheme_id or "")
    for entry in data.setdefault("pose_schemes", []):
        if str(entry.get("id") or "") == target:
            return entry
    return None


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
    result = prepare_live2d_source_import(source_path, temp_root=temp_root, log=log)
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
        ignore=shutil.ignore_patterns("*.pretty.json", "*.preview.json", "__pycache__"),
    )


def _ensure_project_dirs(project_dir: Path) -> None:
    for name in ("live2d", "psd", "tex", "repacks", "composites", "preview/current"):
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
