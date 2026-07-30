from __future__ import annotations

import json
import re
import shutil
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from app.core.cubism_core import CubismCore
from app.core.model import Live2DPackage, resolve_live2d_package
from app.core.model.importer import (
    import_texture_source_to_workspace,
    prepare_live2d_source_import,
)
from app.core.settings_manager import SettingsManager


PROJECT_FORMAT = "LpkUnpacker.Live2DViewerModProject"
PROJECT_VERSION = 3
PROJECT_FILE_NAME = "project.live2dviewer_mod.json"
SOURCES_DIR = "sources"
BUILD_DIR = "build/live2d"
EXPORTS_DIR = "exports"
SKIN_MANIFEST_FILE = "skin_manifest.json"
MAPPING_TABLE_FILE = "texture_mapping.json"
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
    def models(self) -> list[dict[str, Any]]:
        return [
            item
            for item in self.data.get("models") or []
            if isinstance(item, dict)
        ]

    @property
    def base_model_json(self) -> Path:
        models = self.models
        value = str(models[0].get("model_json") or "") if models else ""
        if not value:
            value = str(self.data.get("base_model_json") or "")
        return _resolve_project_path(self.project_dir, value) if value else self.project_dir


def create_empty_project(
    project_name: str,
    output_root: str | Path | None = None,
    log: LogCallback | None = None,
) -> Live2DViewerModProject:
    """Create a named MOD project before a main model is imported."""
    resolved_name = sanitize_project_name(project_name)
    project_root = _project_output_root(output_root)
    project_dir = _unique_project_dir(project_root / resolved_name)
    project_data = {
        "format": PROJECT_FORMAT,
        "version": PROJECT_VERSION,
        "project_name": project_dir.name,
        "models": [],
        "base_model_json": "",
        "artmesh_areas": [],
        "selected_artmesh_id": "",
        "generated_output_dir": "",
        "generated_model_json_paths": [],
        "last_export_path": "",
        "warnings": [],
    }
    project_file = save_project(project_dir, project_data)
    _log(log, f"Created empty Live2DViewerEX project: {project_file}")
    return Live2DViewerModProject(project_dir, project_file, project_data)


def create_project_from_base_source(
    source: str | Path,
    project_name: str | None = None,
    output_root: str | Path | None = None,
    temp_root: str | Path | None = None,
    log: LogCallback | None = None,
) -> Live2DViewerModProject:
    source_path = Path(source).resolve()
    package, temp_dir, warnings = _import_base_package(source_path, temp_root, log)
    resolved_name = sanitize_project_name(
        project_name or _default_project_name(source_path, package)
    )
    project_root = _project_output_root(output_root)
    project_dir = _unique_project_dir(project_root / resolved_name)
    workspace_dir = project_dir / SOURCES_DIR / "main"
    workspace_dir.parent.mkdir(parents=True, exist_ok=True)

    try:
        _copy_workspace(package.root_dir, workspace_dir)
        workspace_package = resolve_live2d_package(workspace_dir)
        skin_name = "原皮"
        main_model = _model_entry(
            model_id="main",
            skin_name=skin_name,
            source=source_path,
            project_dir=project_dir,
            workspace_dir=workspace_dir,
            model_json=workspace_package.model_json,
            texture_paths=workspace_package.texture_paths,
            warnings=warnings,
        )
        project_data = {
            "format": PROJECT_FORMAT,
            "version": PROJECT_VERSION,
            "project_name": project_dir.name,
            "models": [main_model],
            "base_model_json": main_model["model_json"],
            "artmesh_areas": read_artmesh_areas(workspace_package.model_json),
            "selected_artmesh_id": "",
            "generated_output_dir": "",
            "last_export_path": "",
            "warnings": list(warnings),
        }
        project_file = save_project(project_dir, project_data)
        _log(log, f"Created Live2DViewerEX project: {project_file}")
        return Live2DViewerModProject(project_dir, project_file, project_data)
    except Exception:
        shutil.rmtree(project_dir, ignore_errors=True)
        raise
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


def add_model_to_project(
    project: Live2DViewerModProject,
    source: str | Path,
    skin_name: str | None = None,
    temp_root: str | Path | None = None,
    log: LogCallback | None = None,
) -> Live2DViewerModProject:
    source_path = Path(source).resolve()
    data = normalize_project_data(project.data, project.project_dir.name)
    project_dir = project.project_dir.resolve()
    models = list(data["models"])
    is_main_import = not models
    fallback_name = "原皮" if is_main_import else f"改图{len(models)}版本"
    resolved_skin_name = sanitize_skin_name(
        skin_name or fallback_name or source_path.stem,
        "原皮" if is_main_import else f"Skin {len(models) + 1}",
    )
    model_id = (
        "main"
        if is_main_import
        else _unique_model_id(models, resolved_skin_name)
    )
    workspace_dir = project_dir / SOURCES_DIR / model_id

    _log(log, f"Importing model source: {source_path}")
    try:
        imported = import_texture_source_to_workspace(
            source_path,
            workspace_dir,
            temp_root=temp_root,
            log=log,
        )
        texture_paths = [path for path in imported.texture_paths if path.is_file()]
        if not texture_paths:
            raise Live2DViewerModProjectError(
                f"No Live2D textures were found in source: {source_path}"
            )
        if is_main_import and not imported.model_json:
            raise Live2DViewerModProjectError(
                "The first import in an empty project must be a complete Live2D model."
            )
        model = _model_entry(
            model_id=model_id,
            skin_name=resolved_skin_name,
            source=source_path,
            project_dir=project_dir,
            workspace_dir=workspace_dir,
            model_json=imported.model_json,
            texture_paths=texture_paths,
            warnings=imported.warnings,
        )
        model["texture_mappings"] = suggest_texture_mappings(
            project_dir,
            models[0] if models else None,
            model,
        )
        models.append(model)
        data["models"] = models
        _sync_derived_project_fields(data)
        if is_main_import:
            _refresh_artmesh_state(data, project_dir)
        project_file = save_project(project_dir, data)
        _log(
            log,
            f"Imported {resolved_skin_name}: {len(texture_paths)} texture(s), "
            f"{len(model['texture_mappings'])} mapping(s)",
        )
        return Live2DViewerModProject(project_dir, project_file, data)
    except Exception:
        if workspace_dir.exists():
            shutil.rmtree(workspace_dir, ignore_errors=True)
        raise


def remove_model_from_project(
    project: Live2DViewerModProject,
    model_id: str,
) -> Live2DViewerModProject:
    data = normalize_project_data(project.data, project.project_dir.name)
    project_dir = project.project_dir.resolve()
    models = list(data["models"])
    removed = next(
        (item for item in models if str(item.get("id") or "") == str(model_id)),
        None,
    )
    if removed is None:
        raise Live2DViewerModProjectError(f"Model does not exist: {model_id}")

    removed_index = models.index(removed)
    models = [
        item for item in models if str(item.get("id") or "") != str(model_id)
    ]
    if removed_index == 0 and models:
        complete_index = next(
            (
                index
                for index, item in enumerate(models)
                if _resolve_project_path(
                    project_dir,
                    str(item.get("model_json") or ""),
                ).is_file()
            ),
            -1,
        )
        if complete_index < 0:
            raise Live2DViewerModProjectError(
                "The main model cannot be removed because no other complete Live2D model can replace it."
            )
        if complete_index:
            models.insert(0, models.pop(complete_index))
    data["models"] = models
    workspace = _resolve_project_path(
        project_dir,
        str(removed.get("workspace_path") or ""),
    )
    _remove_owned_model_workspace(project_dir, workspace)
    if removed_index == 0:
        _refresh_all_mappings(project_dir, models)
    _sync_derived_project_fields(data)
    if removed_index == 0:
        _refresh_artmesh_state(data, project_dir)
    _invalidate_build(project_dir, data)
    project_file = save_project(project_dir, data)
    return Live2DViewerModProject(project_dir, project_file, data)


def move_model_in_project(
    project: Live2DViewerModProject,
    model_id: str,
    target_index: int,
) -> Live2DViewerModProject:
    data = normalize_project_data(project.data, project.project_dir.name)
    models = list(data["models"])
    source_index = next(
        (
            index
            for index, item in enumerate(models)
            if str(item.get("id") or "") == str(model_id)
        ),
        -1,
    )
    if source_index < 0:
        raise Live2DViewerModProjectError(f"Model does not exist: {model_id}")
    target_index = max(0, min(int(target_index), len(models) - 1))
    main_changed = source_index == 0 or target_index == 0
    model = models.pop(source_index)
    models.insert(target_index, model)
    main_model_json = _resolve_project_path(
        project.project_dir,
        str(models[0].get("model_json") or ""),
    )
    if not main_model_json.is_file():
        raise Live2DViewerModProjectError(
            "Only a complete Live2D model can be placed in the main-model position."
        )
    data["models"] = models
    if main_changed:
        _refresh_all_mappings(project.project_dir, models)
    _sync_derived_project_fields(data)
    if main_changed:
        _refresh_artmesh_state(data, project.project_dir)
    _invalidate_build(project.project_dir, data)
    project_file = save_project(project.project_dir, data)
    return Live2DViewerModProject(project.project_dir, project_file, data)


def rename_model_skin(
    project: Live2DViewerModProject,
    model_id: str,
    skin_name: str,
) -> Live2DViewerModProject:
    data = normalize_project_data(project.data, project.project_dir.name)
    model = next(
        (
            item
            for item in data["models"]
            if str(item.get("id") or "") == str(model_id)
        ),
        None,
    )
    if model is None:
        raise Live2DViewerModProjectError(f"Model does not exist: {model_id}")
    model["skin_name"] = sanitize_skin_name(skin_name, str(model.get("id") or "Skin"))
    _invalidate_build(project.project_dir, data)
    project_file = save_project(project.project_dir, data)
    return Live2DViewerModProject(project.project_dir, project_file, data)


def update_model_mappings(
    project: Live2DViewerModProject,
    model_id: str,
    mappings: list[tuple[int, str | Path]],
) -> Live2DViewerModProject:
    data = normalize_project_data(project.data, project.project_dir.name)
    models = data["models"]
    if not models:
        raise Live2DViewerModProjectError("The project has no main model.")
    model = next(
        (
            item
            for item in models
            if str(item.get("id") or "") == str(model_id)
        ),
        None,
    )
    if model is None:
        raise Live2DViewerModProjectError(f"Model does not exist: {model_id}")

    main_textures = _model_texture_paths(project.project_dir, models[0])
    source_textures = {
        str(path.resolve()): path
        for path in _model_texture_paths(project.project_dir, model)
    }
    records: list[dict[str, Any]] = []
    used_targets: set[int] = set()
    for target_index, source in mappings:
        source_path = Path(source).resolve()
        if (
            target_index < 0
            or target_index >= len(main_textures)
            or target_index in used_targets
            or str(source_path) not in source_textures
        ):
            continue
        used_targets.add(target_index)
        records.append(
            _mapping_entry(
                project.project_dir,
                source_path,
                target_index,
                main_textures[target_index],
            )
        )
    model["texture_mappings"] = records
    _invalidate_build(project.project_dir, data)
    project_file = save_project(project.project_dir, data)
    return Live2DViewerModProject(project.project_dir, project_file, data)


def model_texture_paths(
    project: Live2DViewerModProject,
    model_id: str,
) -> list[Path]:
    model = next(
        (
            item
            for item in project.models
            if str(item.get("id") or "") == str(model_id)
        ),
        None,
    )
    return _model_texture_paths(project.project_dir, model) if model else []


def suggest_texture_mappings(
    project_dir: str | Path,
    main_model: dict[str, Any] | None,
    source_model: dict[str, Any],
) -> list[dict[str, Any]]:
    if not main_model:
        return []
    root = Path(project_dir).resolve()
    targets = _model_texture_paths(root, main_model)
    sources = _model_texture_paths(root, source_model)
    if not targets or not sources:
        return []

    target_sizes = [_image_size(path) for path in targets]
    unused = set(range(len(targets)))
    result: list[dict[str, Any]] = []
    for source_index, source in enumerate(sources):
        source_name = source.stem.lower()
        source_size = _image_size(source)
        ranked: list[tuple[int, int]] = []
        for target_index in unused:
            target = targets[target_index]
            score = 0
            if source.name.lower() == target.name.lower():
                score += 100
            elif source_name == target.stem.lower():
                score += 80
            if source_size and source_size == target_sizes[target_index]:
                score += 50
            if source_index == target_index:
                score += 10
            ranked.append((score, target_index))
        if not ranked:
            break
        _score, target_index = max(ranked, key=lambda item: (item[0], -item[1]))
        unused.remove(target_index)
        result.append(_mapping_entry(root, source, target_index, targets[target_index]))
    return result


def generate_live2dviewer_mod(
    project: Live2DViewerModProject,
    log: LogCallback | None = None,
) -> Live2DViewerModProject:
    data = normalize_project_data(project.data, project.project_dir.name)
    project_dir = project.project_dir.resolve()
    models = data["models"]
    if not models:
        raise Live2DViewerModProjectError("The project has no main model.")
    for index, model in enumerate(models, start=1):
        if not str(model.get("skin_name") or "").strip():
            raise Live2DViewerModProjectError(
                f"Model #{index} does not have a skin name."
            )
    selected_artmesh_id = str(data.get("selected_artmesh_id") or "").strip()
    if len(models) > 1:
        selected_area = next(
            (
                item
                for item in data.get("artmesh_areas") or []
                if isinstance(item, dict)
                and str(item.get("id") or "") == selected_artmesh_id
            ),
            None,
        )
        if selected_area is None:
            raise Live2DViewerModProjectError(
                "Please select an unused ArtMesh/HitArea trigger before exporting multiple skins."
            )
        if str(selected_area.get("motion") or "").strip():
            raise Live2DViewerModProjectError(
                "The selected ArtMesh/HitArea already has an event bound to it."
            )

    main_model_json = _resolve_project_path(
        project_dir,
        str(models[0].get("model_json") or ""),
    )
    if not main_model_json.is_file():
        raise Live2DViewerModProjectError(
            "The first card must contain a complete Live2D model."
        )
    main_package = resolve_live2d_package(main_model_json)
    output_dir = project_dir / BUILD_DIR
    _log(log, f"Preparing build directory: {output_dir}")
    if output_dir.exists():
        shutil.rmtree(output_dir)
    _copy_workspace(main_package.root_dir, output_dir)

    generated_package = resolve_live2d_package(output_dir)
    source_main_json = generated_package.model_json
    generated_main_json = output_dir / "model0.json"
    main_json_data = _read_json(source_main_json)
    main_texture_refs = _texture_refs(main_json_data)
    generated_models: list[dict[str, Any]] = [
        {
            "id": str(models[0].get("id") or "main"),
            "skin_name": str(models[0].get("skin_name") or "Original"),
            "model_json": generated_main_json.name,
            "textures": list(main_texture_refs),
            "main": True,
        }
    ]
    generated_payloads: list[tuple[Path, dict[str, Any]]] = [
        (generated_main_json, main_json_data)
    ]

    for index, model in enumerate(models[1:], start=1):
        mappings = [
            item
            for item in model.get("texture_mappings") or []
            if isinstance(item, dict)
        ]
        if not mappings:
            raise Live2DViewerModProjectError(
                f"No texture mapping is configured for {model.get('skin_name') or model.get('id')}."
            )
        model_json_data = deepcopy(main_json_data)
        new_refs = list(main_texture_refs)
        model_id = sanitize_identifier(model.get("id"), f"model-{index:03d}")
        applied_mappings = 0
        for mapping in mappings:
            target_index = _mapping_target_index(mapping)
            source = _resolve_project_path(
                project_dir,
                str(mapping.get("source_texture") or mapping.get("source") or ""),
            )
            if not source.is_file() or not 0 <= target_index < len(new_refs):
                continue
            copied = _copy_skin_texture(
                source,
                output_dir,
                index,
                target_index,
                main_texture_refs[target_index],
            )
            new_refs[target_index] = _relative_posix(copied, output_dir)
            applied_mappings += 1
        if not applied_mappings:
            raise Live2DViewerModProjectError(
                f"No valid texture mapping could be applied for {model.get('skin_name') or model_id}."
            )
        _set_texture_refs(model_json_data, new_refs)
        model_file_name = f"model{index}.json"
        model_path = output_dir / model_file_name
        generated_payloads.append((model_path, model_json_data))
        generated_models.append(
            {
                "id": str(model.get("id") or model_id),
                "skin_name": str(model.get("skin_name") or model_id),
                "model_json": model_file_name,
                "textures": list(new_refs),
                "main": False,
            }
        )
        _log(log, f"Built skin model: {model['skin_name']} -> {model_path}")

    for model_path, model_json_data in generated_payloads:
        _inject_switch_menu(
            model_json_data,
            generated_main_json.name,
            generated_models,
            selected_artmesh_id,
        )
        _write_json(model_path, model_json_data)
    _write_json(
        output_dir / SKIN_MANIFEST_FILE,
        {
            "format": "Live2DViewerEX.ModSkinManifest",
            "version": 1,
            "project_name": data["project_name"],
            "artmesh_trigger": {
                "id": selected_artmesh_id,
                "motion": "TapSwitchSkin" if selected_artmesh_id else "",
            },
            "models": generated_models,
        },
    )
    _write_json(
        output_dir / MAPPING_TABLE_FILE,
        {
            "format": "LpkUnpacker.Live2DTextureMapping",
            "version": 1,
            "main_model_id": str(models[0].get("id") or ""),
            "models": [
                {
                    "id": str(generated.get("id") or ""),
                    "skin_name": str(generated.get("skin_name") or ""),
                    "texture_mappings": (
                        []
                        if index == 0
                        else [
                            {
                                "source_texture": generated["textures"][target_index],
                                "target_index": target_index,
                                "target_texture": main_texture_refs[target_index],
                            }
                            for target_index in range(
                                min(
                                    len(generated.get("textures") or []),
                                    len(main_texture_refs),
                                )
                            )
                            if generated["textures"][target_index]
                            != main_texture_refs[target_index]
                        ]
                    ),
                }
                for index, generated in enumerate(generated_models)
            ],
        },
    )
    data["generated_output_dir"] = _relative_to_project(output_dir, project_dir)
    data["generated_model_json_paths"] = [
        _relative_to_project(output_dir / item["model_json"], project_dir)
        for item in generated_models
    ]
    project_file = save_project(project_dir, data)
    _log(log, f"Built Live2DViewerEX mod: {output_dir}")
    return Live2DViewerModProject(project_dir, project_file, data)


def export_live2dviewer_mod(
    project: Live2DViewerModProject,
    destination: str | Path | None = None,
    log: LogCallback | None = None,
) -> tuple[Live2DViewerModProject, Path]:
    built = generate_live2dviewer_mod(project, log=log)
    output_dir = _resolve_project_path(
        built.project_dir,
        str(built.data.get("generated_output_dir") or BUILD_DIR),
    )
    if destination:
        export_root = Path(destination).resolve()
    else:
        export_root = built.project_dir / EXPORTS_DIR
    export_root.mkdir(parents=True, exist_ok=True)
    if export_root == output_dir or _is_relative_to(export_root, output_dir):
        raise Live2DViewerModProjectError(
            "The export folder cannot be placed inside the build directory."
        )
    export_dir = _unique_project_dir(
        export_root / sanitize_project_name(built.project_name)
    )
    shutil.copytree(output_dir, export_dir)
    data = dict(built.data)
    data["last_export_path"] = str(export_dir)
    project_file = save_project(built.project_dir, data)
    _log(log, f"Exported Live2DViewerEX workshop folder: {export_dir}")
    return Live2DViewerModProject(built.project_dir, project_file, data), export_dir


def rename_project(
    project: Live2DViewerModProject,
    new_name: str,
) -> Live2DViewerModProject:
    """Rename both the project and its owning directory."""
    old_dir = project.project_dir.resolve()
    expected_file = (old_dir / PROJECT_FILE_NAME).resolve()
    if project.project_file.resolve() != expected_file or not expected_file.is_file():
        raise Live2DViewerModProjectError(
            "The project file is not inside the expected project directory."
        )

    resolved_name = sanitize_project_name(new_name)
    new_dir = (old_dir.parent / resolved_name).resolve()
    if new_dir != old_dir:
        if new_dir.exists():
            raise Live2DViewerModProjectError(
                f"A project directory with this name already exists: {new_dir}"
            )
        if new_dir.parent != old_dir.parent:
            raise Live2DViewerModProjectError(
                "The renamed project directory must remain under the same project root."
            )
        shutil.move(str(old_dir), str(new_dir))

    data = normalize_project_data(project.data, resolved_name)
    data["project_name"] = resolved_name
    data["last_export_path"] = ""
    project_file = save_project(new_dir, data)
    return Live2DViewerModProject(new_dir, project_file, data)


def delete_project_directory(project: Live2DViewerModProject) -> Path:
    """Permanently remove one validated project directory."""
    project_dir = project.project_dir.resolve()
    expected_file = (project_dir / PROJECT_FILE_NAME).resolve()
    if project.project_file.resolve() != expected_file or not expected_file.is_file():
        raise Live2DViewerModProjectError(
            "Refusing to delete a directory that is not a valid MOD project."
        )
    if project_dir == project_dir.parent:
        raise Live2DViewerModProjectError("Refusing to delete a filesystem root.")
    shutil.rmtree(project_dir)
    return project_dir


def save_project(project_dir: str | Path, data: dict[str, Any]) -> Path:
    directory = Path(project_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    payload = normalize_project_data(data, directory.name)
    project_file = directory / PROJECT_FILE_NAME
    temporary = project_file.with_suffix(f"{project_file.suffix}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temporary.replace(project_file)
    return project_file


def load_project(project_file: str | Path) -> Live2DViewerModProject:
    path = Path(project_file).resolve()
    if path.is_dir():
        path = path / PROJECT_FILE_NAME
    if not path.is_file():
        raise Live2DViewerModProjectError(f"Project file does not exist: {path}")
    with path.open("r", encoding="utf-8-sig") as file:
        data = json.load(file)
    if not isinstance(data, dict) or data.get("format") != PROJECT_FORMAT:
        raise Live2DViewerModProjectError(
            f"Unsupported Live2DViewer mod project: {path}"
        )
    normalized = normalize_project_data(data, path.parent.name)
    if not normalized.get("artmesh_areas"):
        _refresh_artmesh_state(normalized, path.parent)
    return Live2DViewerModProject(path.parent, path, normalized)


def normalize_project_data(data: dict[str, Any], fallback_name: str) -> dict[str, Any]:
    payload = dict(data)
    payload["format"] = PROJECT_FORMAT
    payload["version"] = PROJECT_VERSION
    payload["project_name"] = sanitize_project_name(
        payload.get("project_name") or fallback_name
    )
    models = payload.get("models")
    if not isinstance(models, list):
        models = _migrate_legacy_models(payload)
    payload["models"] = [
        _normalize_model_entry(item, index)
        for index, item in enumerate(models)
        if isinstance(item, dict)
    ]
    payload.setdefault("warnings", [])
    legacy_selected = str(payload.get("selected_hit_area") or "")
    legacy_areas = payload.get("hit_areas")
    payload.setdefault("selected_artmesh_id", legacy_selected)
    payload.setdefault(
        "artmesh_areas",
        legacy_areas if isinstance(legacy_areas, list) else [],
    )
    payload.setdefault("generated_output_dir", "")
    payload.setdefault("generated_model_json_paths", [])
    payload.setdefault("last_export_path", "")
    _sync_derived_project_fields(payload)
    for legacy_key in (
        "base_source",
        "imported_workspace_paths",
        "auxiliary_sources",
        "skins",
        "texture_replacements",
        "selected_hit_area",
        "hit_areas",
        "generated_skin_json_paths",
    ):
        payload.pop(legacy_key, None)
    return payload


def sanitize_project_name(name: Any) -> str:
    text = str(name or "").strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text)
    text = text.strip(" ._")
    return text or "live2dviewer_mod"


def sanitize_identifier(name: Any, fallback: str = "model") -> str:
    text = sanitize_project_name(name)
    text = re.sub(r"[^0-9A-Za-z_.-]+", "-", text)
    text = text.strip("._-").lower()
    return text or fallback


def sanitize_skin_name(name: Any, fallback: str = "Skin") -> str:
    text = str(name or "").strip()
    text = re.sub(r"[\x00-\x1f]+", " ", text)
    return text[:80].strip() or fallback


def read_artmesh_areas(model_json: str | Path) -> list[dict[str, str]]:
    """Read clickable IDs and all ArtMesh drawables from the main model."""
    path = Path(model_json).resolve()
    data = _read_json(path)
    raw_areas = data.get("HitAreas")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_areas if isinstance(raw_areas, list) else []:
        if not isinstance(item, dict):
            continue
        area_id = str(item.get("Id") or item.get("id") or "").strip()
        if not area_id or area_id in seen:
            continue
        seen.add(area_id)
        result.append(
            {
                "id": area_id,
                "name": str(
                    item.get("Name") or item.get("name") or area_id
                ).strip()
                or area_id,
                "motion": str(
                    item.get("Motion") or item.get("motion") or ""
                ).strip(),
            }
        )

    # Many AssetStudio exports do not contain HitAreas at all.  Drawable IDs
    # live in the MOC3 binary, so query Cubism Core and merge them with the
    # event metadata above.  Fail softly when a test fixture or legacy model
    # contains an invalid/missing MOC file.
    try:
        package = resolve_live2d_package(path)
        if package.moc_path and package.moc_path.is_file():
            model = CubismCore().load_moc(package.moc_path)
            for drawable_id in model.drawable_ids():
                area_id = str(drawable_id or "").strip()
                if not area_id or area_id in seen:
                    continue
                seen.add(area_id)
                result.append(
                    {
                        "id": area_id,
                        "name": area_id,
                        "motion": "",
                    }
                )
    except Exception:
        pass
    return result


def _model_entry(
    model_id: str,
    skin_name: str,
    source: Path,
    project_dir: Path,
    workspace_dir: Path,
    model_json: Path | None,
    texture_paths: list[Path],
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": model_id,
        "skin_name": sanitize_skin_name(skin_name),
        "source_path": str(source),
        "workspace_path": _relative_to_project(workspace_dir, project_dir),
        "model_json": (
            _relative_to_project(model_json, project_dir)
            if model_json and model_json.is_file()
            else ""
        ),
        "textures": [
            _relative_to_project(path, project_dir)
            for path in texture_paths
            if path.is_file()
        ],
        "texture_mappings": [],
        "status": "ready",
        "warnings": list(warnings or []),
    }


def _normalize_model_entry(item: dict[str, Any], index: int) -> dict[str, Any]:
    model = dict(item)
    model["id"] = sanitize_identifier(
        model.get("id") or f"model-{index + 1:03d}",
        f"model-{index + 1:03d}",
    )
    model["skin_name"] = sanitize_skin_name(
        model.get("skin_name") or model.get("name"),
        "Original" if index == 0 else f"Skin {index}",
    )
    model.setdefault("source_path", str(model.get("source") or ""))
    model.setdefault("workspace_path", str(model.get("import_workspace") or ""))
    model.setdefault("model_json", str(model.get("import_model_json") or ""))
    textures = model.get("textures")
    if not isinstance(textures, list):
        textures = []
    model["textures"] = [str(path) for path in textures if str(path)]
    mappings = model.get("texture_mappings")
    if not isinstance(mappings, list):
        mappings = model.get("texture_replacements") or []
    model["texture_mappings"] = [
        _normalize_mapping(item)
        for item in mappings
        if isinstance(item, dict)
    ]
    model.setdefault("status", "ready")
    model.setdefault("warnings", [])
    for legacy_key in (
        "name",
        "source",
        "import_workspace",
        "import_model_json",
        "texture_replacements",
        "generated_skin_json_path",
        "generated_model_json_path",
    ):
        model.pop(legacy_key, None)
    return model


def _normalize_mapping(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_texture": str(
            item.get("source_texture")
            or item.get("workspace_path")
            or item.get("source")
            or ""
        ),
        "target_index": _mapping_target_index(item),
        "target_texture": str(item.get("target_texture") or ""),
    }


def _migrate_legacy_models(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    base_source = payload.get("base_source") or {}
    base_workspace = payload.get("imported_workspace_paths") or {}
    base_model_json = str(payload.get("base_model_json") or "")
    if base_model_json:
        result.append(
            {
                "id": "main",
                "skin_name": str(base_source.get("name") or "Original"),
                "source_path": str(base_source.get("path") or ""),
                "workspace_path": str(base_workspace.get("base") or ""),
                "model_json": base_model_json,
                "textures": [],
                "texture_mappings": [],
                "status": "ready",
            }
        )
    for index, skin in enumerate(payload.get("skins") or [], start=1):
        if not isinstance(skin, dict):
            continue
        result.append(
            {
                "id": str(skin.get("id") or f"model-{index:03d}"),
                "skin_name": str(skin.get("name") or f"Skin {index}"),
                "source_path": str(skin.get("source") or ""),
                "workspace_path": str(skin.get("import_workspace") or ""),
                "model_json": str(skin.get("import_model_json") or ""),
                "textures": [
                    str(
                        replacement.get("workspace_path")
                        or replacement.get("source")
                        or ""
                    )
                    for replacement in skin.get("texture_replacements") or []
                    if isinstance(replacement, dict)
                ],
                "texture_mappings": list(
                    skin.get("texture_replacements") or []
                ),
                "status": str(skin.get("status") or "ready"),
            }
        )
    return result


def _sync_derived_project_fields(data: dict[str, Any]) -> None:
    models = data.get("models") or []
    data["base_model_json"] = (
        str(models[0].get("model_json") or "")
        if models and isinstance(models[0], dict)
        else ""
    )


def _refresh_artmesh_state(
    data: dict[str, Any],
    project_dir: str | Path,
) -> None:
    models = [
        item for item in data.get("models") or [] if isinstance(item, dict)
    ]
    areas: list[dict[str, str]] = []
    if models:
        model_json = _resolve_project_path(
            Path(project_dir).resolve(),
            str(models[0].get("model_json") or ""),
        )
        if model_json.is_file():
            try:
                areas = read_artmesh_areas(model_json)
            except Exception:
                areas = []
    data["artmesh_areas"] = areas
    selected = str(data.get("selected_artmesh_id") or "")
    if selected and not any(
        str(item.get("id") or "") == selected
        and not str(item.get("motion") or "").strip()
        for item in areas
    ):
        data["selected_artmesh_id"] = ""


def _refresh_all_mappings(
    project_dir: str | Path,
    models: list[dict[str, Any]],
) -> None:
    if not models:
        return
    models[0]["texture_mappings"] = []
    for model in models[1:]:
        model["texture_mappings"] = suggest_texture_mappings(
            project_dir,
            models[0],
            model,
        )


def _invalidate_build(project_dir: str | Path, data: dict[str, Any]) -> None:
    data["generated_output_dir"] = ""
    data["generated_model_json_paths"] = []
    # The next build always recreates BUILD_DIR.  Deleting a previous build
    # here can block the GUI for seconds when it contains large textures.


def _remove_owned_model_workspace(project_dir: Path, workspace: Path) -> None:
    if not workspace.exists():
        return
    owned_roots = (
        project_dir / SOURCES_DIR,
        project_dir / "workspace" / "imports",
        project_dir / "workspace" / "base",
    )
    if workspace == project_dir or not any(
        _is_relative_to(workspace, root.resolve()) for root in owned_roots
    ):
        raise Live2DViewerModProjectError(
            f"Refusing to delete a directory outside this project's model storage: {workspace}"
        )
    if workspace.is_dir():
        shutil.rmtree(workspace)
    elif workspace.is_file():
        workspace.unlink()


def _model_texture_paths(
    project_dir: str | Path,
    model: dict[str, Any],
) -> list[Path]:
    root = Path(project_dir).resolve()
    result = [
        _resolve_project_path(root, str(value))
        for value in model.get("textures") or []
        if str(value)
    ]
    existing = [path for path in result if path.is_file()]
    if existing:
        return existing
    model_json = _resolve_project_path(root, str(model.get("model_json") or ""))
    if model_json.is_file():
        try:
            return [
                path
                for path in resolve_live2d_package(model_json).texture_paths
                if path.is_file()
            ]
        except Exception:
            return []
    workspace = _resolve_project_path(root, str(model.get("workspace_path") or ""))
    if workspace.is_dir():
        suffixes = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tga"}
        return [
            path
            for path in sorted(workspace.rglob("*"), key=lambda item: str(item).lower())
            if path.is_file() and path.suffix.lower() in suffixes
        ]
    return []


def _mapping_entry(
    project_dir: str | Path,
    source: Path,
    target_index: int,
    target: Path,
) -> dict[str, Any]:
    root = Path(project_dir).resolve()
    return {
        "source_texture": _relative_to_project(source, root),
        "target_index": int(target_index),
        "target_texture": _relative_to_project(target, root),
    }


def _mapping_target_index(mapping: dict[str, Any]) -> int:
    value = mapping.get("target_index")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return -1


def _image_size(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as image:
            return image.size
    except Exception:
        return None


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
        root = (
            Path(SettingsManager().get_output_root()).resolve()
            / "live2dviewer_mod"
        )
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
        candidate = path.with_name(f"{path.name}_{index:02d}")
        index += 1
    return candidate


def _unique_model_id(models: list[dict[str, Any]], name: str) -> str:
    base = f"model-{sanitize_identifier(name, 'skin')}"
    existing = {str(item.get("id") or "") for item in models}
    candidate = base
    index = 2
    while candidate in existing:
        candidate = f"{base}-{index:02d}"
        index += 1
    return candidate


def _copy_workspace(source_dir: Path, target_dir: Path) -> None:
    shutil.copytree(
        source_dir,
        target_dir,
        ignore=shutil.ignore_patterns(
            "*.pretty.json",
            "*.preview.json",
            "__pycache__",
        ),
    )


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


def _copy_skin_texture(
    source: Path,
    output_dir: Path,
    model_index: int,
    target_index: int,
    target_ref: str,
) -> Path:
    suffix = source.suffix.lower() or Path(
        target_ref.replace("\\", "/")
    ).suffix.lower() or ".png"
    target_path = output_dir / f"{model_index}_{target_index}{suffix}"
    shutil.copy2(source, target_path)
    return target_path


def _inject_switch_menu(
    model_data: dict[str, Any],
    generated_model_name: str,
    generated_models: list[dict[str, Any]],
    selected_artmesh_id: str,
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
    motions[menu_motion] = [
        {
            "Name": "0",
            "Text": "Switch Skin",
            "Choices": [
                {
                    "Text": str(item["skin_name"]),
                    "NextMtn": f"{switch_motion}:{index}",
                }
                for index, item in enumerate(generated_models)
            ],
        }
    ]
    motions[switch_motion] = [
        {
            "Name": str(index),
            "Command": (
                f"change_model {generated_model_name}"
                if index == 0
                else f"change_model {item['model_json']}"
            ),
        }
        for index, item in enumerate(generated_models)
    ]
    if not selected_artmesh_id:
        return
    hit_areas = model_data.setdefault("HitAreas", [])
    if not isinstance(hit_areas, list):
        hit_areas = []
        model_data["HitAreas"] = hit_areas
    for item in hit_areas:
        if (
            isinstance(item, dict)
            and str(item.get("Id") or item.get("id") or "")
            == selected_artmesh_id
        ):
            item["Id"] = selected_artmesh_id
            item["Motion"] = menu_motion
            item["Order"] = max(
                [
                    int(area.get("Order") or area.get("order") or 0)
                    for area in hit_areas
                    if isinstance(area, dict)
                ]
                or [0]
            ) + 1
            item["IgnoreVisibility"] = True
            item["Enabled"] = True
            return
    highest_order = max(
        [
            int(area.get("Order") or area.get("order") or 0)
            for area in hit_areas
            if isinstance(area, dict)
        ]
        or [0]
    )
    hit_areas.append(
        {
            "Name": "Switch Skin",
            "Id": selected_artmesh_id,
            "Order": highest_order + 1,
            "IgnoreVisibility": True,
            "Motion": menu_motion,
            "Enabled": True,
        }
    )


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise Live2DViewerModProjectError(f"JSON root is not an object: {path}")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")


def _resolve_project_path(project_dir: Path, value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate.resolve()
    return (project_dir / candidate).resolve()


def _relative_posix(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _relative_to_project(path: Path, project_dir: Path) -> str:
    try:
        return path.resolve().relative_to(project_dir.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _log(log: LogCallback | None, message: str) -> None:
    if log:
        log(message)
