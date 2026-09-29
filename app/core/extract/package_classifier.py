from __future__ import annotations

import json
import shutil
import re
import zipfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from app.core.extract.detector import find_config_for_lpk
from app.core.lpk_loader import LpkLoader
from app.core.wpk_handler import WPKHandler


class PackageContentKind(str, Enum):
    LIVE2D = "live2d"
    SPINE = "spine"
    MIXED = "mixed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PackageContentInfo:
    kind: PackageContentKind
    label: str
    output_subdir: str
    note: str


CONTENT_INFO = {
    PackageContentKind.LIVE2D: PackageContentInfo(
        kind=PackageContentKind.LIVE2D,
        label="Live2D",
        output_subdir="live2d",
        note="检测到 Live2D 模型入口，会输出到 output/live2d",
    ),
    PackageContentKind.SPINE: PackageContentInfo(
        kind=PackageContentKind.SPINE,
        label="Spine",
        output_subdir="spine",
        note="检测到 Spine skeleton/atlas 入口，会输出到 output/spine",
    ),
    PackageContentKind.MIXED: PackageContentInfo(
        kind=PackageContentKind.MIXED,
        label="混合",
        output_subdir="",
        note="容器内包含多种模型资源，内部 LPK 会分别输出到对应目录",
    ),
    PackageContentKind.UNKNOWN: PackageContentInfo(
        kind=PackageContentKind.UNKNOWN,
        label="未知",
        output_subdir="unknown",
        note="未能从入口 JSON 判断模型类型，会输出到 output/unknown",
    ),
}


def classify_lpk_content(
    lpk_path: str | Path,
    config_files: list[str | Path] | None = None,
) -> PackageContentInfo:
    source = Path(lpk_path)
    kinds: set[PackageContentKind] = _kind_set(_classify_archive_names(source))

    config_path = find_config_for_lpk(source, config_files)
    loader = None
    try:
        loader = LpkLoader(str(source), str(config_path) if config_path else None)
        for entry in _iter_lpk_entry_json(loader):
            kind = _classify_entry_json(entry)
            kinds.update(_kind_set(kind))

        if len(kinds) > 1:
            return CONTENT_INFO[PackageContentKind.MIXED]
        if kinds:
            return CONTENT_INFO[next(iter(kinds))]
    except Exception:
        # Preserve any direct archive evidence if loading the LPK config or
        # one encrypted entry fails.  A bad optional entry must not erase a
        # reliable extension-based classification.
        detected = _kind_from_set(kinds)
        return CONTENT_INFO[detected]
    finally:
        if loader and hasattr(loader, "lpkfile"):
            loader.lpkfile.close()

    return CONTENT_INFO[PackageContentKind.UNKNOWN]


def classify_wpk_content(wpk_path: str | Path) -> PackageContentInfo:
    temp_dir = None
    try:
        temp_dir, lpk_files, config_files = WPKHandler.extract_wpk(str(wpk_path), "")
        kinds = {
            classify_lpk_content(lpk, config_files).kind
            for lpk in lpk_files
        }
        kinds.discard(PackageContentKind.UNKNOWN)
        if len(kinds) > 1:
            return CONTENT_INFO[PackageContentKind.MIXED]
        if kinds:
            return CONTENT_INFO[next(iter(kinds))]
    except Exception:
        return CONTENT_INFO[PackageContentKind.UNKNOWN]
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)

    return CONTENT_INFO[PackageContentKind.UNKNOWN]


def output_subdir_for_lpk(
    lpk_path: str | Path,
    config_files: list[str | Path] | None = None,
) -> str:
    return classify_lpk_content(lpk_path, config_files).output_subdir


def _classify_archive_names(source: Path) -> PackageContentKind:
    """Classify direct archive entries, retaining all detected kinds.

    A package may contain Live2D and Spine assets together.  The previous
    implementation returned as soon as it saw the first ``.atlas``/``.skel``
    entry, hiding a later Live2D model (and vice versa).
    """

    kinds: set[PackageContentKind] = set()
    try:
        with zipfile.ZipFile(source) as zip_file:
            for raw_name in zip_file.namelist():
                name = raw_name.replace("\\", "/").lower().rstrip("/")
                kinds.update(_kind_set(_classify_archive_name(name)))
                if not name.endswith(".json"):
                    continue
                # A direct ZIP may retain a skeleton JSON extension even
                # though LPK payloads are encrypted names.  Read only a
                # bounded prefix and use the full decoder when it is small.
                try:
                    with zip_file.open(raw_name) as payload:
                        raw = payload.read(4 * 1024 * 1024)
                    data = json.loads(raw.decode("utf-8-sig"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                kinds.update(_kind_set(_classify_entry_json(data)))
    except Exception:
        return PackageContentKind.UNKNOWN

    return _kind_from_set(kinds)


def _classify_archive_name(name: str) -> PackageContentKind:
    name = name.lower().replace("\\", "/").rstrip("/")
    if _is_spine_filename(name):
        return PackageContentKind.SPINE
    if _is_live2d_filename(name):
        return PackageContentKind.LIVE2D
    return PackageContentKind.UNKNOWN


def _iter_lpk_entry_json(loader: LpkLoader):
    # Costume paths are the reliable entry points.  Follow model-command
    # references too, because a mixed package can keep its second model only
    # behind ``change_model``/``add_submodel``.
    pending: list[str] = []
    seen: set[str] = set()
    for chara in loader.mlve_config.get("list", []):
        for costume in chara.get("costume", []):
            path = costume.get("path")
            if path:
                pending.append(path)

    while pending:
        path = pending.pop(0)
        if path in seen:
            continue
        seen.add(path)
        try:
            raw = loader.decrypt_file(path).decode("utf-8-sig")
            data = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        yield data
        for name, value in _iter_string_values(data):
            if not isinstance(value, str) or not value:
                continue
            if not (name.lower().endswith("_command") or name.lower().endswith("_postcommand")):
                continue
            if not any(_is_model_command(loader, command.strip()) for command in value.split(";")):
                continue
            for command in value.split(";"):
                reference = _find_encrypted_file(command)
                if reference and reference not in seen:
                    pending.append(reference)


def _classify_entry_json(data: dict) -> PackageContentKind:
    if not isinstance(data, dict):
        return PackageContentKind.UNKNOWN

    text = json.dumps(data, ensure_ascii=False).lower()
    live2d = False
    spine = False

    file_refs = data.get("FileReferences")
    if isinstance(file_refs, dict):
        moc = file_refs.get("Moc")
        textures = file_refs.get("Textures")
        live2d = (
            isinstance(moc, str)
            and (
                _has_suffix(moc, (".moc", ".moc3", ".moc.bytes", ".moc3.bytes"))
                # Encrypted STM packages often replace the original .moc3
                # suffix with a hash.bin reference.  FileReferences.Moc is
                # still strong Live2D evidence in that shape.
                or bool(moc.strip())
            )
        ) or (isinstance(textures, list) and bool(textures))

    skeleton = data.get("skeleton")
    atlases = data.get("atlases")
    if isinstance(skeleton, dict) and isinstance(skeleton.get("spine"), str):
        spine = True
    elif isinstance(skeleton, str) and (
        isinstance(atlases, (list, dict))
        or _has_suffix(skeleton, (".skel", ".skel.bytes"))
    ):
        spine = True
    elif isinstance(data.get("bones"), list) and (
        isinstance(data.get("slots"), list)
        or isinstance(data.get("skins"), (dict, list))
        or isinstance(data.get("animations"), (dict, list))
    ):
        spine = True

    # Encrypted LPK references often have no extension.  The container shape
    # above handles those; extension/text hints cover direct archives and
    # older exports with explicit ``.skel``/``.atlas`` names.
    if _text_has_spine_reference(text):
        spine = True
    if ".model3.json" in text or ".moc3" in text or ".moc" in text:
        live2d = True

    if live2d and spine:
        return PackageContentKind.MIXED
    if live2d:
        return PackageContentKind.LIVE2D
    if spine:
        return PackageContentKind.SPINE
    return PackageContentKind.UNKNOWN


def _kind_set(kind: PackageContentKind) -> set[PackageContentKind]:
    if kind == PackageContentKind.MIXED:
        return {PackageContentKind.LIVE2D, PackageContentKind.SPINE}
    if kind in {PackageContentKind.LIVE2D, PackageContentKind.SPINE}:
        return {kind}
    return set()


def _kind_from_set(kinds: set[PackageContentKind]) -> PackageContentKind:
    if len(kinds) > 1:
        return PackageContentKind.MIXED
    return next(iter(kinds), PackageContentKind.UNKNOWN)


def _has_suffix(value: str, suffixes: tuple[str, ...]) -> bool:
    lower = value.lower().replace("\\", "/")
    return any(lower.endswith(suffix) for suffix in suffixes)


def _is_spine_filename(name: str) -> bool:
    lower = name.lower()
    return lower.endswith(
        (".skel", ".skel.bytes", ".atlas", ".atlas.txt", ".atlas.bytes")
    )


def _is_live2d_filename(name: str) -> bool:
    lower = name.lower()
    return lower.endswith(
        (".model3.json", ".moc3", ".moc", ".moc3.bytes", ".moc.bytes")
    ) or Path(lower).name == "model.json"


def _text_has_spine_reference(text: str) -> bool:
    return bool(
        re.search(r"(?i)(?:\\|/|\"|')?[^\s\"']+\.(?:skel|atlas)(?:\.bytes|\.txt)?", text)
    )


def _find_encrypted_file(value: str) -> str | None:
    match = re.search(r"(?i)[0-9a-f]{32}\.bin3?", value)
    return match.group(0) if match else None


def _is_model_command(loader, command: str) -> bool:
    checker = getattr(loader, "is_model_command", None)
    if callable(checker):
        return bool(checker(command))
    return command.lower().startswith(
        ("change_cos", "change_model", "add_submodel", "remove_submodel")
    )


def _iter_string_values(value, prefix: str = ""):
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}_{key}" if prefix else str(key)
            yield from _iter_string_values(child, child_prefix)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_string_values(child, f"{prefix}_{index}" if prefix else str(index))
    elif isinstance(value, str):
        yield prefix, value
