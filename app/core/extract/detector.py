from __future__ import annotations

from pathlib import Path

from app.core.extract.models import ExtractSourceType


UNITY_EXTENSIONS = {
    "",
    ".assets",
    ".sharedassets",
    ".bundle",
    ".unity3d",
    ".resource",
    ".ress",
    ".resss",
}

UNITY_SCAN_EXTENSIONS = {
    ".assets",
    ".sharedassets",
    ".bundle",
    ".unity3d",
}


def detect_source_type(path: str | Path) -> ExtractSourceType:
    source = Path(path)
    if source.is_dir():
        return ExtractSourceType.FOLDER
    if not source.exists():
        return ExtractSourceType.UNKNOWN

    suffix = source.suffix.lower()
    if suffix == ".lpk":
        return ExtractSourceType.LPK
    if suffix == ".wpk":
        return ExtractSourceType.WPK
    if suffix in UNITY_EXTENSIONS:
        return ExtractSourceType.UNITY
    return ExtractSourceType.UNKNOWN


def scan_package_folder(folder_path: str | Path) -> tuple[list[str], list[str]]:
    files: list[str] = []
    configs: list[str] = []
    root_path = Path(folder_path)

    if not root_path.is_dir():
        return files, configs

    for path in sorted(root_path.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in {".lpk", ".wpk"}:
            files.append(str(path))
        elif suffix in UNITY_SCAN_EXTENSIONS:
            files.append(str(path))
        elif path.name.lower() == "config.json":
            configs.append(str(path))

    return files, configs


def find_config_for_lpk(
    lpk_path: str | Path,
    config_files: list[str | Path] | None = None,
) -> Path | None:
    lpk = Path(lpk_path)
    config_in_dir = lpk.parent / "config.json"
    if config_in_dir.exists():
        return config_in_dir

    configs = [Path(path) for path in config_files or []]
    for config in configs:
        if config.parent == lpk.parent:
            return config
    return configs[0] if configs else None
