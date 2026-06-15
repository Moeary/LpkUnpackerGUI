from __future__ import annotations

import json
import shutil
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
    by_names = _classify_archive_names(source)
    if by_names != PackageContentKind.UNKNOWN:
        return CONTENT_INFO[by_names]

    config_path = find_config_for_lpk(source, config_files)
    loader = None
    try:
        loader = LpkLoader(str(source), str(config_path) if config_path else None)
        kinds: set[PackageContentKind] = set()
        for entry in _iter_lpk_entry_json(loader):
            kind = _classify_entry_json(entry)
            if kind != PackageContentKind.UNKNOWN:
                kinds.add(kind)

        if len(kinds) > 1:
            return CONTENT_INFO[PackageContentKind.MIXED]
        if kinds:
            return CONTENT_INFO[next(iter(kinds))]
    except Exception:
        return CONTENT_INFO[PackageContentKind.UNKNOWN]
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
    try:
        import zipfile

        with zipfile.ZipFile(source) as zip_file:
            names = [name.lower() for name in zip_file.namelist()]
    except Exception:
        return PackageContentKind.UNKNOWN

    if any(name.endswith((".skel", ".atlas")) for name in names):
        return PackageContentKind.SPINE
    if any(name.endswith((".model3.json", ".moc3", ".moc")) for name in names):
        return PackageContentKind.LIVE2D
    return PackageContentKind.UNKNOWN


def _iter_lpk_entry_json(loader: LpkLoader):
    for chara in loader.mlve_config.get("list", []):
        for costume in chara.get("costume", []):
            path = costume.get("path")
            if not path:
                continue
            raw = loader.decrypt_file(path).decode("utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                yield data


def _classify_entry_json(data: dict) -> PackageContentKind:
    file_refs = data.get("FileReferences")
    if isinstance(file_refs, dict):
        moc = file_refs.get("Moc")
        textures = file_refs.get("Textures")
        if isinstance(moc, str) and moc.lower().endswith((".moc", ".moc3")):
            return PackageContentKind.LIVE2D
        if isinstance(textures, list) and textures:
            return PackageContentKind.LIVE2D

    if isinstance(data.get("skeleton"), str) and data.get("atlases"):
        return PackageContentKind.SPINE

    if {"bones", "slots"}.issubset(data.keys()) and (
        "skins" in data or "animations" in data
    ):
        return PackageContentKind.SPINE

    text = json.dumps(data, ensure_ascii=False).lower()
    if ".moc3" in text or ".moc" in text or "model3.json" in text:
        return PackageContentKind.LIVE2D
    if ".skel" in text or ".atlas" in text:
        return PackageContentKind.SPINE
    return PackageContentKind.UNKNOWN
