"""Discovery helpers for optional external tools and runtimes.

The settings page is intentionally the only place that writes discovered
paths.  The helpers in this module only inspect the filesystem and return a
path when the corresponding tool is actually usable.  Keeping discovery
separate from :mod:`settings_manager` also prevents importing a settings
object from recursively importing one of the tool backends.
"""

from __future__ import annotations

import os
import re
import shutil
import string
import sys
from pathlib import Path
from typing import Iterable, Mapping

from app.paths import PROJECT_ROOT


ARCHIVE_TOOL_NAMES = (
    "bz.exe",
    "bz",
    "7z.exe",
    "7z",
    "7za.exe",
    "7za",
    "7zr.exe",
    "7zr",
    "WinRAR.exe",
    "WinRAR",
    "UnRAR.exe",
    "UnRAR",
    "rar.exe",
    "rar",
)
ASSETSTUDIO_TOOL_NAMES = ("AssetStudioModCLI.exe", "AssetStudioModCLI")
CUBISM_CORE_NAMES = ("Live2DCubismCore.dll",)
SPINE_WEBGL_NAMES = {"spine-webgl.js", "spine-webgl.min.js"}
SPINE_CORE_NAMES = {"spine-core.js", "spine-core.min.js"}
PHOTOSHOP_NAMES = ("Photoshop.exe", "Photoshop")


def _clean_path(value: str | os.PathLike[str] | None) -> Path | None:
    if value is None:
        return None
    text = str(value).strip().strip('"')
    if not text:
        return None
    try:
        return Path(text).expanduser()
    except (TypeError, ValueError, OSError):
        return None


def _existing_file(value: str | os.PathLike[str] | None) -> Path | None:
    path = _clean_path(value)
    try:
        return path.resolve() if path and path.is_file() else None
    except (OSError, RuntimeError):
        return None


def _existing_dir(value: str | os.PathLike[str] | None) -> Path | None:
    path = _clean_path(value)
    try:
        return path.resolve() if path and path.is_dir() else None
    except (OSError, RuntimeError):
        return None


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            key = str(path.expanduser().resolve()).casefold()
        except (OSError, RuntimeError):
            key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def tool_roots(extra_roots: Iterable[str | os.PathLike[str]] = ()) -> list[Path]:
    """Return likely roots for packaged and project-local tools.

    Recursive scans are deliberately limited to these roots.  Scanning the
    complete project would walk user output and sample assets and makes a
    settings-page refresh surprisingly expensive.
    """

    roots: list[Path] = []
    for value in extra_roots:
        path = _clean_path(value)
        if path:
            roots.append(path)

    roots.extend(
        (
            PROJECT_ROOT / "app" / "tools",
            PROJECT_ROOT / "tools",
            PROJECT_ROOT / "runtime" / "tools",
        )
    )
    if getattr(sys, "frozen", False):
        executable_root = Path(sys.executable).resolve().parent
        roots.extend((executable_root / "tools", executable_root / "app" / "tools"))

    for env_name in ("LPK_TOOLS_DIR", "LPK_RUNTIME_TOOLS_DIR"):
        path = _clean_path(os.environ.get(env_name))
        if path:
            roots.append(path)
    return _unique_paths(roots)


def _iter_files(root: Path, names: Iterable[str]) -> Iterable[Path]:
    """Yield files with one of *names*, matching case-insensitively."""

    wanted = {str(name).casefold() for name in names}
    try:
        if root.is_file() and root.name.casefold() in wanted:
            yield root
            return
        if not root.is_dir():
            return
        # Tool layouts are shallow.  Walking with pruning avoids traversing
        # the large Spine source checkout merely to find AssetStudio or 7-Zip
        # beside it.  The dedicated Spine detector below performs its own
        # recursive scan when needed.
        skip_dirs = {".git", "node_modules", "__pycache__", "examples", "tests", "test"}
        if not (wanted & (SPINE_WEBGL_NAMES | SPINE_CORE_NAMES)):
            skip_dirs.update({"spine", "spine-runtimes"})
        root_depth = len(root.parts)
        for current, dirnames, filenames in os.walk(root):
            current_path = Path(current)
            if len(current_path.parts) - root_depth >= 7:
                dirnames[:] = []
            else:
                dirnames[:] = [name for name in dirnames if name.casefold() not in skip_dirs]
            for filename in filenames:
                if filename.casefold() in wanted:
                    yield current_path / filename
    except (OSError, RuntimeError):
        return


def _first_named_file(
    names: Iterable[str],
    *,
    explicit: str | os.PathLike[str] | None = None,
    roots: Iterable[Path] = (),
) -> Path | None:
    explicit_file = _existing_file(explicit)
    if explicit_file:
        return explicit_file
    for root in roots:
        for path in _iter_files(root, names):
            try:
                return path.resolve()
            except (OSError, RuntimeError):
                return path
    return None


def find_archive_extractor(
    explicit: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Find an archive extractor suitable for the preview page."""

    configured = _existing_file(explicit)
    if configured:
        return configured
    for env_name in ("LPK_ARCHIVE_EXTRACTOR", "LPK_ARCHIVE_TOOL"):
        path = _existing_file(os.environ.get(env_name))
        if path:
            return path
    for name in ARCHIVE_TOOL_NAMES:
        found = shutil.which(name)
        if found:
            path = _existing_file(found)
            if path:
                return path
    return _first_named_file(ARCHIVE_TOOL_NAMES, roots=tool_roots())


def find_assetstudio_cli(
    explicit: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Find AssetStudioModCLI in local, packaged, or runtime tool layouts."""

    configured = _existing_file(explicit)
    if configured:
        return configured
    env_path = _existing_file(os.environ.get("LPK_ASSETSTUDIO_CLI"))
    if env_path:
        return env_path
    return _first_named_file(ASSETSTUDIO_TOOL_NAMES, roots=tool_roots())


def _steam_library_roots() -> list[Path]:
    """Discover Steam library roots without requiring the Steam client."""

    roots: list[Path] = []
    # The common locations are useful even when the registry is unavailable
    # (for example when the app is running as a portable build).
    for drive in string.ascii_uppercase:
        drive_root = Path(f"{drive}:/")
        roots.extend(
            (
                drive_root / "SteamLibrary",
                drive_root / "Program Files (x86)" / "Steam",
                drive_root / "Program Files" / "Steam",
            )
        )

    for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "ProgramW6432", "LOCALAPPDATA"):
        value = os.environ.get(env_name)
        if value:
            base = Path(value)
            roots.extend((base / "Steam", base / "SteamLibrary"))

    # Parse the portable text format used by Steam for additional libraries.
    config_files: list[Path] = []
    for root in list(roots):
        config_files.append(root / "steamapps" / "libraryfolders.vdf")
    for config in _unique_paths(config_files):
        try:
            if not config.is_file():
                continue
            text = config.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeError):
            continue
        for match in re.finditer(r'"path"\s+"([^"]+)"', text, re.IGNORECASE):
            raw = match.group(1).replace("\\\\", "\\")
            roots.append(Path(raw))
    return _unique_paths(roots)


def _steam_cubism_candidates() -> Iterable[Path]:
    relative = Path(
        "steamapps"
    ) / "common" / "Live2DViewerEX" / "bin" / "exstudio" / "exstudio_Data" / "Plugins" / "x86_64" / "Live2DCubismCore.dll"
    for root in _steam_library_roots():
        yield root / relative


def find_cubism_core(
    explicit: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Find Live2DCubismCore.dll, including Live2DViewerEX's Steam copy."""

    configured = _existing_file(explicit)
    if configured:
        return configured
    for env_name in ("LPK_CUBISM_CORE_DLL",):
        env_path = _existing_file(os.environ.get(env_name))
        if env_path:
            return env_path
    env_dir = _existing_dir(os.environ.get("LPK_CUBISM_CORE_DIR"))
    if env_dir:
        env_file = _existing_file(env_dir / "Live2DCubismCore.dll")
        if env_file:
            return env_file

    bundled = _first_named_file(CUBISM_CORE_NAMES, roots=tool_roots())
    if bundled:
        return bundled

    for candidate in _steam_cubism_candidates():
        found = _existing_file(candidate)
        if found:
            return found
    return None


def find_photoshop(
    explicit: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Find Photoshop.exe in common Adobe installation locations."""

    configured = _clean_path(explicit)
    if configured:
        if configured.is_file() and configured.name.casefold() == "photoshop.exe":
            return _existing_file(configured)
        if configured.is_dir():
            candidate = _existing_file(configured / "Photoshop.exe")
            if candidate:
                return candidate
    for env_name in ("LPK_PHOTOSHOP_PATH",):
        env_path = find_photoshop_path(os.environ.get(env_name))
        if env_path:
            return env_path

    roots: list[Path] = []
    for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "ProgramW6432"):
        value = os.environ.get(env_name)
        if value:
            roots.append(Path(value) / "Adobe")
    for drive in string.ascii_uppercase:
        root = Path(f"{drive}:/")
        roots.extend((root / "Program Files" / "Adobe", root / "Program Files (x86)" / "Adobe"))
    return _first_named_file(PHOTOSHOP_NAMES, roots=_unique_paths(roots))


def find_photoshop_path(value: str | os.PathLike[str] | None) -> Path | None:
    """Resolve a Photoshop file or installation directory value."""

    path = _clean_path(value)
    if not path:
        return None
    if path.is_file() and path.name.casefold() == "photoshop.exe":
        return _existing_file(path)
    if path.is_dir():
        return _existing_file(path / "Photoshop.exe")
    return None


def _contains_spine_scripts(root: Path) -> bool:
    try:
        scripts = [
            path
            for path in root.rglob("*.js")
            if path.name.casefold() in SPINE_WEBGL_NAMES
        ]
        return bool(scripts)
    except (OSError, RuntimeError):
        return False


def _has_spine_manifest(root: Path) -> bool:
    try:
        return any(
            (root / name).is_file()
            for name in ("spine_runtime.json", "runtime.json", "manifest.json")
        )
    except OSError:
        return False


def find_spine_runtime(
    explicit: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Find an extracted spine-ts core/webgl runtime directory.

    A zip file is intentionally not returned: the preview backend needs the
    JavaScript files on disk and cannot load a runtime directly from an
    archive.  The caller can still select an extracted directory manually.
    """

    configured = _existing_dir(explicit)
    if configured and (_contains_spine_scripts(configured) or _has_spine_manifest(configured)):
        return configured

    roots = tool_roots()
    for root in roots:
        if not root.is_dir():
            continue
        # Prefer an explicitly packaged/versioned runtime root.  A source
        # checkout may contain several example builds; returning its first
        # lexicographic ``build`` directory would make the settings path
        # fragile and bypass a manifest placed at ``spine/3.8``.
        try:
            candidates = [root]
            candidates.extend(
                path
                for path in root.rglob("*")
                if path.is_dir()
                and path.name.casefold() in {"3.8", "runtime", "spine", "spine-ts"}
            )
            candidates = sorted(
                _unique_paths(candidates),
                key=lambda path: (
                    0 if _has_spine_manifest(path) else 1,
                    len(path.parts),
                    str(path).casefold(),
                ),
            )
            for candidate in candidates:
                if _has_spine_manifest(candidate) and _contains_spine_scripts(candidate):
                    return candidate.resolve()
        except (OSError, RuntimeError):
            pass
        try:
            webgl_scripts = [
                path
                for path in root.rglob("*.js")
                if path.name.casefold() in SPINE_WEBGL_NAMES
            ]
        except (OSError, RuntimeError):
            continue
        for script in sorted(webgl_scripts, key=lambda item: str(item).casefold()):
            # Prefer the directory that directly contains both scripts.  If
            # core is in a sibling folder, the parent remains discoverable by
            # ``discover_spine_runtime`` and is a better setting value.
            parent = script.parent
            try:
                sibling_core = any(
                    path.name.casefold() in SPINE_CORE_NAMES
                    for path in parent.rglob("*.js")
                )
            except (OSError, RuntimeError):
                sibling_core = False
            if sibling_core:
                return parent.resolve()
            if _contains_spine_scripts(parent.parent):
                return parent.parent.resolve()
            return parent.resolve()
    return None


def detect_toolchain_paths(
    configured: Mapping[str, str | os.PathLike[str] | None] | None = None,
) -> dict[str, str]:
    """Return detected paths keyed by SettingsManager setting names.

    ``configured`` is passed to each detector only as a hint; it is never
    changed.  This makes the function useful for status labels as well as for
    the explicit "detect tools" action on the settings page.
    """

    values = dict(configured or {})
    result: dict[str, str] = {}
    detectors = (
        ("tools.archive_extractor_path", find_archive_extractor),
        ("tools.assetstudio_cli_path", find_assetstudio_cli),
        ("tools.cubism_core_dll_path", find_cubism_core),
        ("tools.photoshop_path", find_photoshop),
        ("preview.spine_runtime_dir", find_spine_runtime),
    )
    for key, detector in detectors:
        try:
            found = detector(values.get(key))
        except Exception:
            found = None
        if found:
            result[key] = str(found)
    return result


__all__ = [
    "ARCHIVE_TOOL_NAMES",
    "ASSETSTUDIO_TOOL_NAMES",
    "CUBISM_CORE_NAMES",
    "PHOTOSHOP_NAMES",
    "detect_toolchain_paths",
    "find_archive_extractor",
    "find_assetstudio_cli",
    "find_cubism_core",
    "find_photoshop",
    "find_photoshop_path",
    "find_spine_runtime",
    "tool_roots",
]
