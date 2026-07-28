from __future__ import annotations

import json
import os
import re
from pathlib import Path

from app.core import motion_fixed
from app.core.model.live2d_package import Live2DPackage


MODEL_JSON_PATTERN = re.compile(r"model\d*\.json$", re.IGNORECASE)


class Live2DPackageError(RuntimeError):
    pass


def is_model_json_path(path: str | Path) -> bool:
    return bool(MODEL_JSON_PATTERN.search(str(path or "")))


def find_model_jsons(root: str | Path) -> list[Path]:
    source = Path(root)
    if source.is_file():
        return [source] if is_model_json_path(source) else []
    if not source.is_dir():
        return []

    candidates = [path for path in source.rglob("*.json") if is_model_json_path(path)]
    return sorted(candidates, key=_model_sort_key)


def resolve_live2d_package(source: str | Path) -> Live2DPackage:
    source_path = Path(source).resolve()
    candidates = find_model_jsons(source_path)
    errors: list[str] = []

    for candidate in candidates:
        try:
            return _package_from_model_json(candidate)
        except Exception as exc:
            errors.append(f"{candidate}: {exc}")

    detail = "; ".join(errors[:3])
    if detail:
        raise Live2DPackageError(f"No valid Live2D model found. {detail}")
    raise Live2DPackageError(f"No Live2D model json found under: {source_path}")


def prepare_model_json_for_preview(path: str | Path) -> Path:
    package = resolve_live2d_package(path)
    model_json = package.model_json
    data = _read_json(model_json)
    _fix_model_motions(data, package.root_dir)

    # A number of game-exported model settings are accepted by Python's JSON
    # decoder but rejected by Cubism Native.  Write one normalized, disposable
    # companion file for the preview backend instead of altering the source.
    # Reusing this path also prevents a new ``.prettyN.json`` file from being
    # created every time a user returns to the preview page.
    suffix = model_json.suffix or ".json"
    preview_path = model_json.with_name(f"{model_json.stem}.preview{suffix}")
    temporary_path = preview_path.with_suffix(f"{preview_path.suffix}.tmp")
    with open(temporary_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    temporary_path.replace(preview_path)
    return preview_path


def _package_from_model_json(model_json: Path) -> Live2DPackage:
    data = _read_json(model_json)
    if not _is_live2d_model_json(data):
        raise Live2DPackageError("not a valid Live2D model json")

    root_dir = model_json.parent
    refs = data.get("FileReferences") or {}
    version = data.get("Version") if isinstance(data.get("Version"), int) else None
    moc_path = None
    textures: list[Path] = []

    if isinstance(refs, dict):
        moc = refs.get("Moc")
        if isinstance(moc, str) and moc:
            moc_path = (root_dir / moc).resolve()
        for texture in refs.get("Textures") or []:
            if isinstance(texture, str) and texture:
                textures.append((root_dir / texture).resolve())

    return Live2DPackage(
        root_dir=root_dir.resolve(),
        model_json=model_json.resolve(),
        version=version,
        moc_path=moc_path,
        texture_paths=textures,
    )


def _read_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise Live2DPackageError("json root is not an object")
    return data


def _is_live2d_model_json(data: dict) -> bool:
    refs = data.get("FileReferences") or {}
    if isinstance(refs, dict):
        moc = refs.get("Moc")
        if isinstance(moc, str) and moc.lower().endswith((".moc", ".moc3")):
            return True
    version = data.get("Version")
    return isinstance(version, int) and version >= 3


def _fix_model_motions(model_json: dict, base_dir: Path) -> None:
    refs = model_json.get("FileReferences") or {}
    motions = refs.get("Motions") or {}
    if not isinstance(motions, dict):
        return

    playable_groups: dict[str, list[dict]] = {}
    for group, items in motions.items():
        if not isinstance(items, list):
            continue
        playable_items: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            rel = item.get("File") or ""
            if not isinstance(rel, str) or not rel:
                continue
            motion_path = Path(os.path.normpath(base_dir / rel))
            if not motion_path.name.lower().endswith(".json") or not motion_path.is_file():
                continue
            try:
                motion_fixed.copy_modify_from_motion(
                    str(motion_path),
                    save_root=str(motion_path.parent),
                )
            except Exception:
                pass
            playable_items.append(item)
        if playable_items:
            playable_groups[str(group)] = playable_items

    # Live2DViewer-style model settings sometimes use action entries without a
    # motion file (for example, text/menu commands).  pylive2d maps those to
    # ``NullValue`` and then asks Cubism to parse it as a motion.  They cannot
    # be played in the native renderer, so omit them only from the generated
    # preview copy; the source model remains untouched.
    refs["Motions"] = playable_groups


def _model_sort_key(path: Path) -> tuple[int, int, str]:
    name = path.name.lower()
    if name.endswith(".model3.json"):
        rank = 0
    elif name.endswith("model3.json"):
        rank = 1
    elif name.endswith("model.json"):
        rank = 2
    else:
        rank = 3
    return rank, len(path.parts), str(path).lower()
