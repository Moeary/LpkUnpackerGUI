"""Safe orchestration for the bundled Spine skeleton data converter.

The upstream C++ readers/writers and cross-version conversion passes are built
as a small in-process DLL from the fixed vendored source under
``third_party/wang606_spine_converter``.  This module loads that C ABI through
``ctypes``, resolves one unambiguous skeleton, and copies the related atlas/page
files into a fresh output directory.  Atlas conversion remains here because
the upstream converter only handles skeleton data and the application must
preserve PMA and scaled pages safely for the application's 3.x preview runtime.

Upstream references (checked 2026-09-29):

* https://github.com/wang606/SpineSkeletonDataConverter
* https://github.com/wang606/SpineSkeletonDataConverter/blob/main/%E7%89%88%E6%9C%AC%E5%B7%AE%E5%BC%82%E8%AE%B0%E5%BD%95.md
* https://github.com/wang606/SpineSkeletonDataConverter/blob/main/LICENSE

The upstream project is PolyForm Noncommercial 1.0.0.  The vendored source and
license metadata are kept under ``third_party/wang606_spine_converter``; the
locally built DLL is an ignored runtime artifact.
"""

from __future__ import annotations

import json
import ctypes
import math
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from PIL import Image

from app.core.spine_atlas import (
    AtlasPage,
    AtlasRegion,
    SpineAtlas,
    SpineAtlasError,
    parse_atlas,
    _unpremultiply,
)
from app.core.spine_preview import read_spine_version
from app.core.spine_editor import (
    SpineEditorError,
    SpineEditorProjectResult,
    create_spine_editor_project,
)
from app.paths import BUNDLE_ROOT, PROJECT_ROOT


UPSTREAM_REPOSITORY_URL = "https://github.com/wang606/SpineSkeletonDataConverter"
UPSTREAM_LICENSE_URL = "https://polyformproject.org/licenses/noncommercial/1.0.0/"
UPSTREAM_VERSION_DIFFERENCES_URL = (
    "https://github.com/wang606/SpineSkeletonDataConverter/blob/main/"
    "%E7%89%88%E6%9C%AC%E5%B7%AE%E5%BC%82%E8%AE%B0%E5%BD%95.md"
)

SUPPORTED_SPINE_FAMILIES = frozenset({"3.5", "3.6", "3.7", "3.8", "4.0", "4.1", "4.2"})
_VERSION_RE = re.compile(r"^(3\.(?:5|6|7|8)|4\.(?:0|1|2))\.(\d+)$")
_SPINE_JSON_SUFFIX = ".json"
_SPINE_SKEL_SUFFIXES = (".skel", ".skel.bytes")
_ATLAS_SUFFIXES = (".atlas", ".atlas.txt", ".atlas.bytes")
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
_ATLAS_PAGE_KEYS = frozenset({"size", "format", "filter", "repeat", "pma", "scale"})
_ATLAS_REGION_KEYS = frozenset(
    {"bounds", "xy", "size", "offsets", "orig", "offset", "rotate", "index", "split", "pad"}
)
_NATIVE_CONVERTER_RELATIVE_PATHS = (
    Path("runtime") / "tools" / "SpineSkeletonDataConverter" / "lpk_spine_converter.dll",
    Path("runtime") / "tools" / "lpk_spine_converter.dll",
    Path("tools") / "SpineSkeletonDataConverter" / "lpk_spine_converter.dll",
)
_NATIVE_CONVERTER_ABI_VERSION = "1"
_UPSTREAM_SOURCE_COMMIT = "5ecb2139b0a1af266974f95abeec6bb8562d1249"


class SpineConversionError(RuntimeError):
    """Base error for source resolution, conversion, and output validation."""


class SpineConverterNotFoundError(SpineConversionError):
    """Raised when the bundled native converter library cannot be loaded."""


class SpineSourceError(SpineConversionError, ValueError):
    """Raised when the source is not a supported or complete Spine asset."""


class SpineSourceAmbiguousError(SpineSourceError):
    """Raised when a directory contains more than one skeleton candidate."""


class SpineUnsupportedError(SpineConversionError, ValueError):
    """Raised when a conversion would silently lose unsupported atlas data."""


class SpineConversionProcessError(SpineConversionError):
    """Raised when the native converter rejects a conversion."""


# Compatibility aliases make the API discoverable without forcing callers to
# know the internal distinction between input and converter failures.
SpineConverterError = SpineConversionError
SpineConversionUnsupportedError = SpineUnsupportedError


@dataclass(frozen=True)
class SpineConversionResult:
    """Paths and warnings produced by :func:`convert_spine`."""

    output_dir: Path
    skeleton_path: Path
    report_path: Path
    warnings: tuple[str, ...] = ()
    editor_project_path: Path | None = None
    editor_report_path: Path | None = None
    editor_project_error: str | None = None


@dataclass(frozen=True)
class SpineConversionOptions:
    """Opt-in settings used by the extraction pipeline.

    The standalone :func:`convert_spine` helper and automatic extraction use
    the editor-oriented target default 3.8.75.  Automatic extraction remains
    disabled unless a caller explicitly enables it.
    """

    enabled: bool = False
    target_version: str = "3.8.75"
    output_format: str = "json"
    # Optional explicit native DLL path; legacy EXE settings are ignored.
    converter_path: str | os.PathLike[str] | None = None
    remove_curve: bool = False
    # Core API compatibility keeps project generation opt-in.  The GUI and
    # persisted settings explicitly enable this for editor-ready exports.
    create_project: bool = False
    editor_path: str | os.PathLike[str] | None = None


@dataclass(frozen=True)
class _ResolvedSource:
    source: Path
    source_root: Path
    skeleton_path: Path
    model_config_path: Path | None = None
    atlas_paths: tuple[Path, ...] = ()
    texture_paths: tuple[Path, ...] = ()


@dataclass
class _AtlasCopyState:
    destination_by_source: dict[Path, Path] = field(default_factory=dict)
    transform_by_source: dict[Path, tuple[float, bool, int]] = field(default_factory=dict)


def discover_native_converter(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """Find the bundled native converter DLL.

    A supplied DLL path is authoritative.  Legacy ``.exe`` settings are
    deliberately ignored so conversion never falls back to an external
    process; callers receive the same clear missing-library error as any other
    unavailable native install.
    """

    if explicit is not None:
        explicit_text = str(explicit).strip().strip('"')
        if explicit_text and not explicit_text.casefold().endswith(".exe"):
            explicit_path = Path(explicit_text).expanduser()
            if explicit_path.is_file() and explicit_path.suffix.casefold() in {".dll", ".so", ".dylib"}:
                return _resolved_path(explicit_path)
            # A configured native path is authoritative when it is a DLL, so
            # do not silently replace a missing user-selected library.
            if explicit_path.suffix.casefold() in {".dll", ".so", ".dylib"}:
                return None
        # Older settings may still contain the removed EXE path. Ignore that
        # legacy value and continue with the bundled runtime candidates.

    candidates = [root / relative for root in dict.fromkeys((PROJECT_ROOT, BUNDLE_ROOT))
                  for relative in _NATIVE_CONVERTER_RELATIVE_PATHS]
    # Development builds are accepted, but only at the known third-party path;
    # no repository-wide walk or executable discovery is performed.
    candidates.append(
        PROJECT_ROOT / "third_party" / "wang606_spine_converter" / "build" / "Release" / "lpk_spine_converter.dll"
    )
    candidates.append(
        PROJECT_ROOT / "third_party" / "wang606_spine_converter" / ".build" / "lpk_spine_converter.dll"
    )
    for candidate in candidates:
        if candidate.is_file():
            return _resolved_path(candidate)
    return None


def discover_converter(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """Compatibility alias for :func:`discover_native_converter`.

    The returned path is always a native library; this name remains exported
    for older callers but no executable lookup is retained.
    """

    return discover_native_converter(explicit)


def convert_spine(
    source: str | os.PathLike[str],
    output_dir: str | os.PathLike[str] | None = None,
    target_version: str = "3.8.75",
    output_format: str = "json",
    converter_path: str | os.PathLike[str] | None = None,
    remove_curve: bool = False,
    create_project: bool = False,
    editor_path: str | os.PathLike[str] | None = None,
) -> SpineConversionResult:
    """Convert one Spine skeleton and copy its atlas assets into new output.

    ``source`` may be a single ``.skel``/``.json`` skeleton, a ViewerEX
    ``model0.json`` that references one, or a directory.  Directory discovery
    is deliberately strict and raises :class:`SpineSourceAmbiguousError` when
    more than one skeleton is present.  ``output_dir`` is an optional parent
    root; each invocation creates a new uniquely named child below it. When it
    is empty, the parent beside the selected file is used. For a selected
    directory, its parent is used so the converted copy is a sibling and
    never becomes source input.
    When ``create_project`` is true and the output format is JSON, an
    independent ``editor_project`` package is created below the converted
    directory.  It unpacks atlas regions, rewrites image references, and runs
    the official Spine 3.8.75 CLI to create and reopen a real ``.spine`` file.
    The core API leaves this opt-in for backwards compatibility; the GUI and
    extraction settings can enable it explicitly.
    """

    target = _validate_target_version(target_version)
    target_version = ".".join(str(part) for part in target)
    format_name = _validate_output_format(output_format)
    requested_source = Path(source).expanduser().resolve()
    resolved = _resolve_source(requested_source)
    output_text = str(output_dir or "").strip()
    if output_text:
        output_root = Path(output_text).expanduser().resolve()
    else:
        output_root = _default_output_root(
            resolved,
            source_was_directory=requested_source.is_dir(),
        )
    output_inside_selected_directory = False
    if requested_source.is_dir():
        try:
            output_root.relative_to(requested_source)
            output_inside_selected_directory = True
        except ValueError:
            pass
    if output_root == resolved.source or output_inside_selected_directory:
        raise SpineSourceError(
            "output_dir must be independent of the source directory; source files are never overwritten."
        )
    converter = discover_native_converter(converter_path)
    if converter is None:
        requested = (
            str(converter_path)
            if converter_path
            else "runtime/tools/SpineSkeletonDataConverter/lpk_spine_converter.dll"
        )
        raise SpineConverterNotFoundError(
            "Unable to find the bundled native Spine converter DLL: "
            f"{requested}. Build it with third_party/wang606_spine_converter/build_native.ps1."
        )

    output_root.mkdir(parents=True, exist_ok=True)
    destination = _create_output_directory(output_root, resolved.skeleton_path, target_version, format_name)
    output_skeleton = destination / _output_skeleton_name(resolved.skeleton_path, format_name)
    report_path = destination / "spine_conversion_report.json"
    warnings: list[str] = []
    if resolved.skeleton_path.suffix.casefold() not in {_SPINE_JSON_SUFFIX, ".skel"}:
        warnings.append(
            f"Input skeleton uses non-standard suffix {resolved.skeleton_path.name!r}; a temporary .skel/.json name was used for the native converter."
        )

    source_version = _safe_read_version(resolved.skeleton_path)
    source_family = _version_family(source_version)
    target_family = f"{target[0]}.{target[1]}"
    cross_family = source_family is not None and source_family != target_family
    if target_version.strip() == "3.8.75":
        warnings.append(
            "目标版本 3.8.75 已由内置 native converter 按完整版本号写出；预览能否加载取决于所选 runtime，Spine 编辑器导入和无损往返均不保证。"
        )
    if source_family is None:
        warnings.append("Unable to pre-read the source Spine version; the native converter will report the authoritative input-version error.")
    if cross_family:
        warnings.extend(_cross_version_warnings(source_family or "unknown", target_family, remove_curve))

    report: dict[str, Any] = {
        "source": str(resolved.source),
        "source_root": str(resolved.source_root),
        "model_config": str(resolved.model_config_path) if resolved.model_config_path else None,
        "skeleton_source": str(resolved.skeleton_path),
        "source_version": source_version,
        "target_version": target_version,
        "output_format": format_name,
        "output_dir": str(destination),
        "skeleton_path": str(output_skeleton),
        "converter": str(converter),
        "converter_type": "in-process native DLL",
        "converter_abi_version": _NATIVE_CONVERTER_ABI_VERSION,
        "converter_source_commit": _UPSTREAM_SOURCE_COMMIT,
        "converter_repository": UPSTREAM_REPOSITORY_URL,
        "converter_license": UPSTREAM_LICENSE_URL,
        "version_differences": UPSTREAM_VERSION_DIFFERENCES_URL,
        "remove_curve": bool(remove_curve),
        "create_project": bool(create_project),
        "editor_path": str(editor_path) if editor_path else None,
        "warnings": warnings,
        "risks": list(warnings),
        "stdout": "",
        "stderr": "",
        "returncode": None,
        "command": [],
        "atlases": [],
    }

    try:
        command, stdout, stderr, returncode = _run_native_converter(
            converter,
            resolved.skeleton_path,
            output_skeleton,
            target_version,
            remove_curve,
        )
        report["command"] = command
        report["stdout"] = stdout
        report["stderr"] = stderr
        report["returncode"] = returncode
        if returncode != 0:
            raise SpineConversionProcessError(
                f"Spine converter exited with {returncode}: {stderr.strip() or stdout.strip() or 'no diagnostic output'}"
            )
        if not output_skeleton.is_file() or output_skeleton.stat().st_size == 0:
            raise SpineConversionProcessError(f"Spine converter produced no output file: {output_skeleton}")

        actual_version = _safe_read_version(output_skeleton)
        report["output_version"] = actual_version
        if actual_version is None:
            raise SpineConversionProcessError(
                f"Unable to validate the Spine version of converter output: {output_skeleton}"
            )
        if _version_tuple(actual_version) != target:
            raise SpineConversionProcessError(
                f"Converter output version {actual_version!r} does not match requested {target_version!r}."
            )

        atlas_state = _AtlasCopyState()
        atlas_warnings = _copy_atlas_assets(
            resolved,
            destination,
            target_major=target[0],
            warnings=warnings,
            state=atlas_state,
        )
        report["atlases"] = atlas_warnings
        report["warnings"] = warnings
        report["risks"] = list(warnings)
        report["asset_policy"] = "converted skeleton plus referenced atlas/page assets; ViewerEX wrapper is not copied"
        editor_result: SpineEditorProjectResult | None = None
        editor_project_error: str | None = None
        if create_project:
            if format_name != "json":
                raise SpineConversionError(
                    "Editor project generation requires JSON output; choose output_format='json' so the official Spine CLI can import it."
                )
            converted_atlases = [Path(item["output"]) for item in atlas_warnings if item.get("output")]
            try:
                editor_result = create_spine_editor_project(
                    output_skeleton,
                    converted_atlases,
                    destination,
                    editor_path=editor_path,
                    target_version=target_version,
                    project_name=output_skeleton.stem,
                )
            except SpineEditorError as exc:
                # Conversion has already produced a validated skeleton and
                # copied atlas/page assets.  A missing/rejecting official CLI
                # must be reported as a project-generation failure while
                # preserving that usable conversion output.
                editor_project_error = str(exc)
                warnings.append(
                    "Spine skeleton conversion succeeded, but the .spine editor "
                    f"project was not generated: {editor_project_error}"
                )
                report["editor_project_error"] = editor_project_error
            else:
                report["editor_project"] = {
                    "project_path": str(editor_result.project_path),
                    "import_json_path": str(editor_result.import_json_path),
                    "images_dir": str(editor_result.images_dir),
                    "report_path": str(editor_result.report_path),
                    "warnings": list(editor_result.warnings),
                }
                warnings.extend(editor_result.warnings)
            # Include editor-side atlas/path diagnostics in the top-level
            # conversion report as well as the dedicated editor report.
            report["warnings"] = warnings
            report["risks"] = list(warnings)
        _write_report(report_path, report)
        return SpineConversionResult(
            output_dir=destination,
            skeleton_path=output_skeleton,
            report_path=report_path,
            warnings=tuple(warnings),
            editor_project_path=editor_result.project_path if editor_result else None,
            editor_report_path=editor_result.report_path if editor_result else None,
            editor_project_error=editor_project_error,
        )
    except Exception as exc:
        report["error"] = str(exc)
        report["warnings"] = warnings
        report["risks"] = list(warnings)
        try:
            _write_report(report_path, report)
        except OSError:
            pass
        if isinstance(exc, SpineConversionError):
            raise
        if isinstance(exc, SpineEditorError):
            raise SpineConversionError(f"Spine editor project generation failed: {exc}") from exc
        raise SpineConversionError(f"Spine conversion failed: {exc}") from exc


def _run_native_converter(
    converter: Path,
    source: Path,
    output: Path,
    target_version: str,
    remove_curve: bool,
) -> tuple[list[str], str, str, int]:
    """Call the vendored converter DLL through its stable C ABI."""

    output.parent.mkdir(parents=True, exist_ok=True)
    input_path = source
    temporary_dir: tempfile.TemporaryDirectory[str] | None = None
    if source.suffix.casefold() not in {".json", ".skel"}:
        temporary_dir = tempfile.TemporaryDirectory(prefix="lpk-spine-input-")
        normal_suffix = ".json" if _looks_like_skeleton_json_file(source) else ".skel"
        input_path = Path(temporary_dir.name) / f"input{normal_suffix}"
        shutil.copy2(source, input_path)
    # This list is a diagnostic representation of the native call, not a
    # command line. Keeping it in the report helps users reproduce options
    # without implying that an executable process was launched.
    command = [
        str(converter),
        "spine_converter_convert",
        str(input_path),
        str(output),
        str(target_version),
        output.suffix.lstrip(".").lower(),
    ]
    if remove_curve:
        command.append("--remove-curve")
    try:
        loader_factory = getattr(ctypes, "WinDLL", ctypes.CDLL) if os.name == "nt" else ctypes.CDLL
        library = loader_factory(str(converter))
        native_convert = library.spine_converter_convert
        native_convert.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_char),
            ctypes.c_size_t,
        ]
        native_convert.restype = ctypes.c_int
        error_buffer = ctypes.create_string_buffer(8192)
        status = native_convert(
            os.fsencode(str(input_path)),
            os.fsencode(str(output)),
            str(target_version).encode("utf-8"),
            output.suffix.lstrip(".").lower().encode("ascii"),
            int(bool(remove_curve)),
            error_buffer,
            ctypes.sizeof(error_buffer),
        )
        diagnostic = error_buffer.value.decode("utf-8", errors="replace")
        return command, "", diagnostic, int(status)
    except OSError as exc:
        raise SpineConverterNotFoundError(f"Unable to load native Spine converter {converter}: {exc}") from exc
    finally:
        if temporary_dir is not None:
            temporary_dir.cleanup()


def _resolve_source(source: Path) -> _ResolvedSource:
    try:
        path = source.expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise SpineSourceError(f"Invalid Spine source path {source!s}: {exc}") from exc
    if path.is_file():
        return _resolve_source_file(path)
    if path.is_dir():
        return _resolve_source_directory(path)
    raise SpineSourceError(f"Spine source does not exist: {path}")


def _resolve_source_file(path: Path) -> _ResolvedSource:
    if path.suffix.casefold() == ".json":
        data = _read_json(path)
        if _looks_like_skeleton_json(data):
            return _resolved_for_skeleton(path, path.parent, None, _nearby_atlases_for_direct_source(path.parent))
        if _looks_like_model_config(data):
            return _resolved_from_model_config(path, data)
        raise SpineSourceError(
            f"JSON source is neither a real Spine skeleton nor a ViewerEX model config: {path}"
        )
    if _is_skeleton_file(path):
        return _resolved_for_skeleton(path, path.parent, None, _nearby_atlases_for_direct_source(path.parent))
    raise SpineSourceError(f"Unsupported Spine source file: {path}")


def _resolve_source_directory(root: Path) -> _ResolvedSource:
    configs: list[tuple[Path, Mapping[str, Any]]] = []
    for candidate in sorted(root.rglob("*"), key=lambda item: str(item).casefold()):
        if not candidate.is_file() or candidate.suffix.casefold() != ".json":
            continue
        data = _read_json(candidate, missing_ok=True)
        if _looks_like_model_config(data):
            configs.append((candidate.resolve(), data))

    config_refs: list[tuple[Path, Path]] = []
    for config_path, data in configs:
        skeleton_ref = _mapping_reference(data.get("skeleton"))
        if not skeleton_ref:
            raise SpineSourceError(f"ViewerEX model config has no skeleton path: {config_path}")
        skeleton = _resolve_reference(config_path.parent, skeleton_ref, _SPINE_SKEL_SUFFIXES + (".json",))
        if skeleton is None:
            raise SpineSourceError(f"Skeleton reference not found in {config_path}: {skeleton_ref}")
        config_refs.append((config_path, skeleton))

    candidates = _discover_skeleton_candidates(root)
    if config_refs:
        distinct_refs = _unique_paths(item[1] for item in config_refs)
        if len(distinct_refs) != 1:
            raise SpineSourceAmbiguousError(
                "Directory contains model configs referring to multiple skeletons: "
                + ", ".join(str(item) for item in distinct_refs)
            )
        if len(candidates) != 1 or candidates[0] != distinct_refs[0]:
            raise SpineSourceAmbiguousError(
                "Directory must contain exactly one skeleton; found: "
                + ", ".join(str(item) for item in candidates)
            )
        config_path, skeleton = config_refs[0]
        data = next(item for item_path, item in configs if item_path == config_path)
        return _resolved_from_model_config(config_path, data, source_root=root, skeleton_override=skeleton)

    if len(candidates) != 1:
        if not candidates:
            raise SpineSourceError(f"No Spine skeleton found under {root}")
        raise SpineSourceAmbiguousError(
            "Directory contains multiple skeletons; select one file or a model0.json reference: "
            + ", ".join(str(item) for item in candidates)
        )
    skeleton = candidates[0]
    atlases = tuple(_discover_atlases(root))
    return _resolved_for_skeleton(skeleton, root, None, atlases)


def _resolved_for_skeleton(
    skeleton: Path,
    source_root: Path,
    model_config: Path | None,
    atlases: Sequence[Path],
    texture_paths: Sequence[Path] = (),
) -> _ResolvedSource:
    atlas_paths = tuple(_unique_paths(atlases))
    if not atlas_paths:
        # A skeleton-only conversion is useful for callers with atlas files in
        # a separately managed asset store.  The missing atlas is explicit in
        # the report warning rather than silently ignored.
        atlas_paths = ()
    return _ResolvedSource(
        source=skeleton,
        source_root=source_root.resolve(),
        skeleton_path=skeleton.resolve(),
        model_config_path=model_config.resolve() if model_config else None,
        atlas_paths=atlas_paths,
        texture_paths=tuple(_unique_paths(texture_paths)),
    )


def _resolved_from_model_config(
    config_path: Path,
    data: Mapping[str, Any],
    *,
    source_root: Path | None = None,
    skeleton_override: Path | None = None,
) -> _ResolvedSource:
    root = (source_root or config_path.parent).resolve()
    skeleton_ref = _mapping_reference(data.get("skeleton"))
    skeleton = skeleton_override or (
        _resolve_reference(config_path.parent, skeleton_ref, _SPINE_SKEL_SUFFIXES + (".json",))
        if skeleton_ref
        else None
    )
    if skeleton is None:
        raise SpineSourceError(f"Skeleton reference not found in model config: {config_path}")
    if skeleton.suffix.casefold() == ".json" and not _looks_like_skeleton_json(_read_json(skeleton)):
        raise SpineSourceError(f"Referenced JSON is not a real Spine skeleton: {skeleton}")

    atlases: list[Path] = []
    textures: list[Path] = []
    atlas_items = data.get("atlases")
    if isinstance(atlas_items, Mapping):
        atlas_items = [atlas_items]
    if atlas_items is not None and not isinstance(atlas_items, Sequence):
        raise SpineSourceError(f"Invalid atlases value in model config: {config_path}")
    for item in atlas_items or ():
        if not isinstance(item, Mapping):
            continue
        atlas_ref = _mapping_reference(item.get("atlas"))
        if not atlas_ref:
            continue
        atlas_path = _resolve_reference(config_path.parent, atlas_ref, _ATLAS_SUFFIXES)
        if atlas_path is None:
            raise SpineSourceError(f"Atlas reference not found in {config_path}: {atlas_ref}")
        atlases.append(atlas_path)
        raw_textures = item.get("textures")
        if isinstance(raw_textures, str):
            raw_textures = [raw_textures]
        if isinstance(raw_textures, Sequence):
            for raw_texture in raw_textures:
                texture_ref = _mapping_reference(raw_texture)
                if not texture_ref:
                    continue
                texture = _resolve_reference(config_path.parent, texture_ref, _IMAGE_SUFFIXES)
                if texture is None:
                    raise SpineSourceError(f"Texture reference not found in {config_path}: {texture_ref}")
                textures.append(texture)
    if not atlases:
        atlases = _nearby_atlases_for_direct_source(root)
    return _resolved_for_skeleton(skeleton, root, config_path, atlases, textures)


def discover_spine_conversion_sources(
    root: str | os.PathLike[str],
    *,
    excluded_dir_names: Iterable[str] = ("spine_converted",),
) -> list[Path]:
    """Find one conversion input per Spine model in a fresh extraction.

    A ViewerEX ``model0.json`` is preferred over its sibling skeleton file so
    explicit atlas mappings survive conversion.  Standalone skeletons are
    returned when no model config refers to them.  Paths below
    ``spine_converted`` are excluded by default; this prevents a second run on
    the same extraction directory from treating prior conversion output as a
    new source.  The helper only inspects *root*, never a project-wide output
    history.
    """

    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        return []
    excluded = {str(name).casefold() for name in excluded_dir_names if str(name).strip()}
    config_by_skeleton: dict[str, Path] = {}
    skeleton_paths: dict[str, Path] = {}

    for candidate in _iter_spine_source_files(root_path, excluded):
        name = candidate.name.casefold()
        if name.endswith(".json"):
            data = _read_json(candidate, missing_ok=True)
            if _looks_like_model_config(data):
                skeleton_ref = _mapping_reference(data.get("skeleton"))
                if not skeleton_ref:
                    raise SpineSourceError(
                        f"Spine model config has no skeleton path: {candidate}"
                    )
                skeleton = _resolve_reference(
                    candidate.parent,
                    skeleton_ref,
                    _SPINE_SKEL_SUFFIXES + (".json",),
                )
                if skeleton is None:
                    raise SpineSourceError(
                        f"Skeleton reference not found in Spine model config {candidate}: {skeleton_ref}"
                    )
                if not _is_spine_skeleton_source(skeleton):
                    raise SpineSourceError(
                        f"Referenced file is not a Spine skeleton in {candidate}: {skeleton}"
                    )
                skeleton_key = str(skeleton.resolve()).casefold()
                config_by_skeleton.setdefault(skeleton_key, candidate.resolve())
                skeleton_paths.setdefault(skeleton_key, skeleton.resolve())
                continue
            if _looks_like_skeleton_json(data):
                skeleton = candidate.resolve()
                skeleton_paths.setdefault(str(skeleton).casefold(), skeleton)
            continue
        if not _is_skeleton_file(candidate):
            continue
        skeleton = candidate.resolve()
        skeleton_paths.setdefault(str(skeleton).casefold(), skeleton)

    selected: list[Path] = []
    for key, skeleton in sorted(skeleton_paths.items(), key=lambda item: str(item[1]).casefold()):
        selected.append(config_by_skeleton.get(key, skeleton))
    return _unique_paths(selected)


def _iter_spine_source_files(root: Path, excluded_dir_names: set[str]) -> Iterable[Path]:
    """Yield files below *root* while pruning prior automatic outputs."""

    try:
        for current, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name.casefold() not in excluded_dir_names]
            current_path = Path(current)
            for filename in sorted(filenames, key=str.casefold):
                path = current_path / filename
                if path.is_file():
                    yield path
    except (OSError, RuntimeError):
        return


def _is_spine_skeleton_source(path: Path) -> bool:
    if _is_skeleton_file(path):
        return True
    if path.suffix.casefold() != ".json":
        return False
    return _looks_like_skeleton_json(_read_json(path, missing_ok=True))


def _discover_skeleton_candidates(root: Path) -> list[Path]:
    candidates: list[Path] = []
    for item in sorted(root.rglob("*"), key=lambda entry: str(entry).casefold()):
        if not item.is_file():
            continue
        suffix = item.suffix.casefold()
        name = item.name.casefold()
        if suffix == ".json":
            data = _read_json(item, missing_ok=True)
            if _looks_like_skeleton_json(data):
                candidates.append(item.resolve())
        elif _is_skeleton_file(item) or ".skel" in name:
            candidates.append(item.resolve())
    return _unique_paths(candidates)


def _discover_atlases(root: Path) -> list[Path]:
    return _unique_paths(
        item.resolve()
        for item in sorted(root.rglob("*"), key=lambda entry: str(entry).casefold())
        if item.is_file() and item.name.casefold().endswith(_ATLAS_SUFFIXES)
    )


def _nearby_atlases_for_direct_source(root: Path) -> list[Path]:
    atlases = _discover_atlases(root)
    if len(atlases) > 1:
        raise SpineSourceAmbiguousError(
            "A direct skeleton source has multiple nearby atlas files with no explicit mapping: "
            + ", ".join(str(item) for item in atlases)
        )
    return atlases


def _copy_atlas_assets(
    resolved: _ResolvedSource,
    destination: Path,
    *,
    target_major: int,
    warnings: list[str],
    state: _AtlasCopyState,
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    if not resolved.atlas_paths:
        warnings.append("未找到关联 atlas；输出只包含转换后的骨骼，运行时需另行提供图集与贴图。")
    for atlas_path in resolved.atlas_paths:
        try:
            atlas = parse_atlas(atlas_path)
        except (OSError, SpineAtlasError, ValueError) as exc:
            raise SpineConversionError(f"Unable to parse atlas {atlas_path}: {exc}") from exc
        atlas_destination = _asset_destination(atlas_path, resolved.source_root, destination)
        atlas_destination.parent.mkdir(parents=True, exist_ok=True)
        page_reports: list[dict[str, Any]] = []
        page_outputs: dict[int, Path] = {}
        for page in atlas.pages:
            source_page = _resolve_atlas_page(atlas_path.parent, page.name, resolved.source_root)
            page_destination = _asset_destination(source_page, resolved.source_root, destination)
            _copy_page(
                source_page,
                page_destination,
                page,
                downgrade=target_major == 3,
                state=state,
                warnings=warnings,
            )
            page_outputs[page.index] = page_destination
            page_reports.append(
                {
                    "source": str(source_page),
                    "output": str(page_destination),
                    "pma": bool(page.pma),
                    "scale": page.scale,
                }
            )
        if target_major == 3:
            _write_downgraded_atlas(atlas, atlas_destination, page_outputs, warnings)
            warnings.append(
                f"Atlas {atlas_path.name} rewritten to 3.x xy/size/orig/offset fields; ViewerEX model wrapper was not copied."
            )
        else:
            _copy_file_once(atlas_path, atlas_destination)
        reports.append(
            {
                "source": str(atlas_path),
                "output": str(atlas_destination),
                "downgraded_for_target_3x": target_major == 3,
                "pages": page_reports,
            }
        )

    # Explicit model config texture entries can include files which are not
    # page names in a malformed or custom atlas.  Preserve them visibly rather
    # than discarding them, while the atlas page references remain authoritative.
    for texture_path in resolved.texture_paths:
        if texture_path.resolve() in state.destination_by_source:
            continue
        texture_destination = _asset_destination(texture_path, resolved.source_root, destination)
        _copy_file_once(texture_path, texture_destination)
    return reports


def _copy_page(
    source: Path,
    destination: Path,
    page: AtlasPage,
    *,
    downgrade: bool,
    state: _AtlasCopyState,
    warnings: list[str],
) -> None:
    scale = float(page.scale if page.scale is not None else 1.0)
    if not math.isfinite(scale) or scale <= 0:
        raise SpineUnsupportedError(f"Atlas page {page.name!r} has invalid scale {page.scale!r}.")
    signature = (scale, bool(page.pma), 1 if downgrade else 0)
    source = source.resolve()
    destination = destination.resolve()
    previous = state.destination_by_source.get(source)
    if previous is not None:
        if previous != destination:
            raise SpineUnsupportedError(
                f"Atlas page {source} maps to conflicting output paths {previous} and {destination}."
            )
        if downgrade and state.transform_by_source.get(source) != signature:
            raise SpineUnsupportedError(
                f"Atlas page {source} is referenced with conflicting PMA/scale settings."
            )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not downgrade or (scale == 1.0 and not page.pma):
        _copy_file_once(source, destination)
        state.destination_by_source[source] = destination
        state.transform_by_source[source] = signature
        return

    try:
        with Image.open(source) as original:
            image = original.convert("RGBA") if page.pma else original.copy()
            if page.pma:
                image = _unpremultiply(image)
                warnings.append(
                    f"Atlas page {source.name} declared pma:true; output page was unpremultiplied and pma was removed for 3.x."
                )
            if scale != 1.0:
                new_size = (max(1, round(image.width / scale)), max(1, round(image.height / scale)))
                image = image.resize(new_size, Image.Resampling.LANCZOS)
                warnings.append(
                    f"Atlas page {source.name} scale={scale:g}; page image and atlas metrics were rescaled by 1/scale."
                )
            _save_image_like_source(image, destination, original.format, source)
            image.close()
    except SpineUnsupportedError:
        raise
    except Exception as exc:
        raise SpineConversionError(f"Unable to convert atlas page {source}: {exc}") from exc
    state.destination_by_source[source] = destination
    state.transform_by_source[source] = signature


def _write_downgraded_atlas(
    atlas: SpineAtlas,
    destination: Path,
    page_outputs: Mapping[int, Path],
    warnings: list[str],
) -> None:
    lines: list[str] = []
    for page in atlas.pages:
        _reject_unknown_properties(page.properties, _ATLAS_PAGE_KEYS, f"atlas page {page.name}")
        page_output = page_outputs.get(page.index)
        if page_output is None or not page_output.is_file():
            raise SpineConversionError(f"Converted atlas page is missing: {page.name}")
        scale = float(page.scale if page.scale is not None else 1.0)
        with Image.open(page_output) as image:
            page_width, page_height = image.size
        # Use actual output dimensions so a valid image and atlas cannot drift
        # when an atlas omitted its page size declaration.
        lines.append(page.name)
        lines.append(f"size: {page_width}, {page_height}")
        lines.append(f"format: {page.format or 'RGBA8888'}")
        lines.append(f"filter: {page.filter_min or 'Nearest'}, {page.filter_mag or page.filter_min or 'Nearest'}")
        if page.repeat:
            lines.append(f"repeat: {page.repeat}")
        for region in atlas.regions_for_page(page.index):
            _write_downgraded_region(lines, region, scale, page.name, (page_width, page_height), warnings)
        lines.append("")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines), encoding="utf-8")


def _write_downgraded_region(
    lines: list[str],
    region: AtlasRegion,
    scale: float,
    page_name: str,
    page_size: tuple[int, int],
    warnings: list[str],
) -> None:
    _reject_unknown_properties(region.properties, _ATLAS_REGION_KEYS, f"atlas region {region.name}")
    if region.rotation not in (0, 90):
        raise SpineUnsupportedError(
            f"Atlas region {region.name!r} on {page_name!r} uses rotation {region.rotation}; only 0/90 can be downgraded safely."
        )
    _validate_region_for_downgrade(region)
    x = _scaled_int(region.x, scale)
    y = _scaled_int(region.y, scale)
    width = _scaled_int(region.width, scale)
    height = _scaled_int(region.height, scale)
    orig_width = _scaled_int(region.original_width, scale)
    orig_height = _scaled_int(region.original_height, scale)
    offset_x = _scaled_int(region.offset_x, scale)
    offset_bottom = _scaled_int(region.offset_bottom, scale)
    if width <= 0 or height <= 0 or orig_width <= 0 or orig_height <= 0:
        raise SpineUnsupportedError(
            f"Atlas region {region.name!r} becomes zero-sized after scale={scale:g}; refusing lossy downgrade."
        )
    physical_width, physical_height = (height, width) if region.rotation == 90 else (width, height)
    if x < 0 or y < 0 or x + physical_width > page_size[0] or y + physical_height > page_size[1]:
        raise SpineUnsupportedError(
            f"Atlas region {region.name!r} scaled bounds ({x}, {y}, {physical_width}, {physical_height}) "
            f"do not fit converted page {page_size[0]}x{page_size[1]}."
        )
    offset_top = orig_height - offset_bottom - height
    if offset_x < 0 or offset_bottom < 0 or offset_x + width > orig_width or offset_top < 0 or offset_top + height > orig_height:
        raise SpineUnsupportedError(
            f"Atlas region {region.name!r} trim offsets become invalid after scale={scale:g}; refusing lossy downgrade."
        )
    lines.append(region.name)
    lines.append(f"  rotate: {'true' if region.rotation == 90 else 'false'}")
    lines.append(f"  xy: {x}, {y}")
    lines.append(f"  size: {width}, {height}")
    if "split" in region.properties:
        lines.append(f"  split: {_scaled_values(region.properties['split'], scale, 4, region.name)}")
    if "pad" in region.properties:
        lines.append(f"  pad: {_scaled_values(region.properties['pad'], scale, 4, region.name)}")
    lines.append(
        f"  orig: {orig_width}, {orig_height}"
    )
    lines.append(f"  offset: {offset_x}, {offset_bottom}")
    lines.append(f"  index: {region.index}")


def _validate_region_for_downgrade(region: AtlasRegion) -> None:
    width, height = region.physical_size
    if region.x < 0 or region.y < 0 or width <= 0 or height <= 0:
        raise SpineConversionError(f"Atlas region {region.name!r} has invalid page bounds.")
    if region.original_width <= 0 or region.original_height <= 0:
        raise SpineConversionError(f"Atlas region {region.name!r} has invalid original size.")
    if region.offset_x + region.width > region.original_width:
        raise SpineConversionError(f"Atlas region {region.name!r} has an invalid horizontal trim offset.")
    if region.offset_top < 0 or region.offset_top + region.height > region.original_height:
        raise SpineConversionError(f"Atlas region {region.name!r} has an invalid vertical trim offset.")


def _reject_unknown_properties(properties: Mapping[str, str], known: Iterable[str], label: str) -> None:
    unknown = sorted(set(properties) - set(known))
    if unknown:
        raise SpineUnsupportedError(f"{label} contains unsupported atlas properties: {', '.join(unknown)}")


def _scaled_int(value: int | float, scale: float) -> int:
    return int(round(float(value) / scale))


def _scaled_values(value: str, scale: float, expected: int, region_name: str) -> str:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) < expected:
        raise SpineUnsupportedError(f"Atlas region {region_name!r} has malformed {expected}-value property {value!r}.")
    try:
        numbers = [int(round(int(part) / scale)) for part in parts[:expected]]
    except ValueError as exc:
        raise SpineUnsupportedError(f"Atlas region {region_name!r} has non-integer property {value!r}.") from exc
    return ", ".join(str(item) for item in numbers)


def _save_image_like_source(image: Image.Image, destination: Path, original_format: str | None, source: Path) -> None:
    output_format = original_format
    if not output_format:
        output_format = Image.registered_extensions().get(source.suffix.casefold(), "PNG")
    if output_format.upper() in {"JPEG", "JPG"} and image.mode in {"RGBA", "LA"}:
        if image.getchannel("A").getextrema() != (255, 255):
            raise SpineUnsupportedError(
                f"Cannot write alpha-bearing converted atlas page {source} as JPEG without losing pixels."
            )
        image = image.convert("RGB")
    try:
        image.save(destination, format=output_format)
    except (KeyError, OSError, ValueError) as exc:
        raise SpineUnsupportedError(f"Unsupported atlas page image format for {source}: {output_format}") from exc


def _copy_file_once(source: Path, destination: Path) -> None:
    source = source.resolve()
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.samefile(source):
            return
        # Existing output is allowed only when it is byte-identical; never
        # overwrite a user's prior conversion.
        if destination.read_bytes() == source.read_bytes():
            return
        raise SpineConversionError(f"Refusing to overwrite existing output asset: {destination}")
    shutil.copy2(source, destination)


def _asset_destination(source: Path, source_root: Path, destination: Path) -> Path:
    source = source.resolve()
    try:
        relative = source.relative_to(source_root.resolve())
    except ValueError as exc:
        raise SpineUnsupportedError(
            f"Asset {source} is outside the source root {source_root}; refusing to flatten a reference silently."
        ) from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise SpineUnsupportedError(f"Unsafe relative asset path: {relative}")
    return (destination / relative).resolve()


def _resolve_atlas_page(atlas_root: Path, raw_name: str, source_root: Path) -> Path:
    candidate = (atlas_root / raw_name.replace("\\", "/")).resolve()
    if candidate.is_file():
        try:
            candidate.relative_to(source_root.resolve())
        except ValueError as exc:
            raise SpineUnsupportedError(f"Atlas page escapes source root: {raw_name!r}") from exc
        return candidate
    wanted = Path(raw_name.replace("\\", "/")).name.casefold()
    for item in atlas_root.iterdir():
        if item.is_file() and item.name.casefold() == wanted:
            return item.resolve()
    for suffix in _IMAGE_SUFFIXES:
        alternate = candidate.with_suffix(candidate.suffix + suffix if candidate.suffix else suffix)
        if alternate.is_file():
            return alternate.resolve()
    raise SpineConversionError(f"Atlas page not found for {raw_name!r} in {atlas_root}")


def _default_output_root(
    resolved: _ResolvedSource,
    *,
    source_was_directory: bool = False,
) -> Path:
    """Choose a safe adjacent parent when the caller leaves output empty."""

    if source_was_directory or resolved.source.is_dir():
        return resolved.source_root.parent.resolve()
    return resolved.source_root.resolve()


def _create_output_directory(root: Path, skeleton: Path, target_version: str, output_format: str) -> Path:
    base = _safe_component(skeleton.stem or skeleton.name)
    name = f"{base}_to_{target_version}_{output_format}"
    candidate = root / name
    index = 2
    while candidate.exists():
        candidate = root / f"{name}_{index}"
        index += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate.resolve()


def _output_skeleton_name(source: Path, output_format: str) -> str:
    stem = source.name
    lowered = stem.casefold()
    for suffix in _SPINE_SKEL_SUFFIXES:
        if lowered.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    else:
        marker = lowered.find(".skel")
        stem = stem[:marker] if marker >= 0 else source.stem
    return f"{stem}.{output_format}"


def _validate_target_version(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise SpineSourceError(f"target_version must be a complete x.y.z string, got {value!r}")
    match = _VERSION_RE.fullmatch(value.strip())
    if not match:
        raise SpineSourceError(
            f"Unsupported target_version {value!r}; use a complete 3.5.x/3.6.x/3.7.x/3.8.x/4.0.x/4.1.x/4.2.x version."
        )
    major, minor = (int(part) for part in match.group(1).split("."))
    return major, minor, int(match.group(2))


def _validate_output_format(value: str) -> str:
    normalized = str(value).strip().lower().lstrip(".")
    if normalized not in {"json", "skel"}:
        raise SpineSourceError(f"output_format must be 'json' or 'skel', got {value!r}")
    return normalized


def _version_tuple(value: str | None) -> tuple[int, int, int] | None:
    if value is None:
        return None
    match = re.search(r"(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?(?!\d)", str(value))
    if not match:
        return None
    return tuple(int(group or 0) for group in match.groups())  # type: ignore[return-value]


def _version_family(value: str | None) -> str | None:
    parsed = _version_tuple(value)
    return f"{parsed[0]}.{parsed[1]}" if parsed else None


def _safe_read_version(path: Path) -> str | None:
    try:
        return read_spine_version(path)
    except (OSError, RuntimeError, ValueError, TypeError):
        return None


def _cross_version_warnings(source_family: str, target_family: str, remove_curve: bool) -> list[str]:
    warnings = [
        f"Cross-family Spine conversion {source_family}→{target_family} follows upstream compatibility rules; single-axis timeline/alpha fields may be folded or lost.",
        "Constraint fields may be merged or downgraded (for example Path position Proportional→Length); verify IK/transform/path animation after conversion.",
    ]
    if remove_curve:
        warnings.append("--remove-curve was requested; animation curves crossing 3.x/4.x are stripped instead of converted.")
    else:
        warnings.append("Curve control points crossing 3.x/4.x are converted by the bundled upstream conversion passes; inspect stepped and long-rotation animation.")
    return warnings


def _read_json(path: Path, *, missing_ok: bool = False) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if missing_ok:
            return None
        raise SpineSourceError(f"Unable to read JSON source {path}: {exc}") from exc
    return value if isinstance(value, Mapping) else None


def _looks_like_skeleton_json(data: Mapping[str, Any] | None) -> bool:
    return isinstance(data, Mapping) and isinstance(data.get("skeleton"), Mapping) and isinstance(data.get("bones"), list)


def _looks_like_model_config(data: Mapping[str, Any] | None) -> bool:
    return isinstance(data, Mapping) and isinstance(data.get("skeleton"), (str, Mapping)) and "atlases" in data


def _looks_like_skeleton_json_file(path: Path) -> bool:
    return _looks_like_skeleton_json(_read_json(path, missing_ok=True))


def _is_skeleton_file(path: Path) -> bool:
    name = path.name.casefold()
    return name.endswith(_SPINE_SKEL_SUFFIXES) or ".skel" in name


def _mapping_reference(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("path", "file", "name", "skeleton", "atlas"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _resolve_reference(root: Path, raw: str | None, suffixes: Sequence[str]) -> Path | None:
    if not raw:
        return None
    candidate = (root / raw.replace("\\", "/")).resolve()
    if candidate.is_file():
        return candidate
    wanted = candidate.name.casefold()
    try:
        for item in root.iterdir():
            if item.is_file() and item.name.casefold() == wanted:
                return item.resolve()
    except OSError:
        return None
    for suffix in suffixes:
        alternate = candidate.with_suffix(candidate.suffix + suffix if candidate.suffix else suffix)
        if alternate.is_file():
            return alternate.resolve()
    return None


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            resolved = path
        key = str(resolved).casefold()
        if key not in seen:
            seen.add(key)
            result.append(resolved)
    return result


def _resolved_path(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError):
        return path


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._\-\u0080-\uffff]+", "_", value).strip(" ._")
    return cleaned or "spine"


def _decode_process_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


__all__ = [
    "SUPPORTED_SPINE_FAMILIES",
    "SpineConversionError",
    "SpineConversionOptions",
    "SpineConversionProcessError",
    "SpineConversionResult",
    "SpineConversionUnsupportedError",
    "SpineEditorError",
    "SpineEditorProjectResult",
    "SpineConverterError",
    "SpineConverterNotFoundError",
    "SpineSourceAmbiguousError",
    "SpineSourceError",
    "SpineUnsupportedError",
    "convert_spine",
    "discover_spine_conversion_sources",
    "discover_native_converter",
    "discover_converter",
]
