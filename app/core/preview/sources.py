from __future__ import annotations

import os
from pathlib import Path

from app.core.model import is_model_json_path


IMAGE_PREVIEW_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tga"}
PACKAGE_PREVIEW_EXTENSIONS = {".lpk", ".wpk"}
ARCHIVE_PREVIEW_EXTENSIONS = {".zip", ".7z", ".rar"}
SPINE_PREVIEW_EXTENSIONS = {".skel", ".atlas"}
UNITY_PREVIEW_EXTENSIONS = {
    "",
    ".assets",
    ".sharedassets",
    ".bundle",
    ".unity3d",
    ".resource",
    ".ress",
    ".resss",
}


def is_model_json(path: str | os.PathLike[str]) -> bool:
    return is_model_json_path(str(path))


def is_image_file(path: str | os.PathLike[str]) -> bool:
    path_str = str(path)
    return os.path.isfile(path_str) and os.path.splitext(path_str)[1].lower() in IMAGE_PREVIEW_EXTENSIONS


def is_unity_preview_source(path: str | os.PathLike[str]) -> bool:
    path_str = str(path)
    if not os.path.exists(path_str):
        return False
    if os.path.isdir(path_str):
        return True
    return os.path.splitext(path_str)[1].lower() in UNITY_PREVIEW_EXTENSIONS


def is_spine_preview_source(path: str | os.PathLike[str]) -> bool:
    path_str = str(path)
    if not os.path.isfile(path_str):
        return False
    suffix = os.path.splitext(path_str)[1].lower()
    if suffix in SPINE_PREVIEW_EXTENSIONS:
        return True
    if suffix == ".json":
        try:
            with open(path_str, "r", encoding="utf-8") as file:
                head = file.read(4096).lower()
            return '"skeleton"' in head and '"bones"' in head
        except Exception:
            return False
    return False


def is_archive_preview_source(path: str | os.PathLike[str]) -> bool:
    path_str = str(path)
    return os.path.isfile(path_str) and os.path.splitext(path_str)[1].lower() in ARCHIVE_PREVIEW_EXTENSIONS


def is_supported_preview_source(path: str | os.PathLike[str]) -> bool:
    path_str = str(path)
    suffix = os.path.splitext(path_str)[1].lower()
    return (
        is_model_json(path_str)
        or is_image_file(path_str)
        or is_archive_preview_source(path_str)
        or is_spine_preview_source(path_str)
        or suffix in PACKAGE_PREVIEW_EXTENSIONS
        or os.path.isdir(path_str)
        or is_unity_preview_source(path_str)
    )


def collect_preview_images(path: str | os.PathLike[str], limit: int = 48) -> list[str]:
    path_str = str(path)
    if is_image_file(path_str):
        return [path_str]
    if not os.path.isdir(path_str):
        return []
    images: list[str] = []
    for root, _dirs, filenames in os.walk(path_str):
        for filename in sorted(filenames):
            full_path = os.path.join(root, filename)
            if os.path.splitext(filename)[1].lower() in IMAGE_PREVIEW_EXTENSIONS:
                images.append(full_path)
                if len(images) >= limit:
                    return images
    return images


def safe_export_name(label: str | None, fallback: str) -> str:
    raw = os.path.basename(os.path.normpath(label or "")) or fallback
    stem, _ext = os.path.splitext(raw)
    name = stem or raw or fallback
    safe = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in name).strip(" .")
    return safe or fallback


def has_image_extension(path: str | os.PathLike[str]) -> bool:
    return Path(path).suffix.lower() in IMAGE_PREVIEW_EXTENSIONS
