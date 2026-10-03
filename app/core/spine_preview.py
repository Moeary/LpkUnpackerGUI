"""Spine asset discovery, native-runtime gating, and pose export helpers.

The preview uses the versioned native Spine bridge installed under
``runtime/tools/spine_native`` (or a user-selected native runtime root).  No
browser, ``spine-ts`` bundle, HTML manifest, or web runtime is involved here.
Spine 2.1 and assets without a skeleton remain explicit atlas-only fallbacks;
validated native families are 3.8 and 4.0.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.core.spine_native import find_native_library


SPINE_RUNTIME_SUPPORTED_FAMILIES = frozenset({"3.8", "4.0"})
SPINE_RUNTIME_KNOWN_FAMILIES = frozenset({"3.8", "4.0", "4.2"})
SPINE_COMPATIBILITY_VERSION = "3.8.75"
SPINE_RUNTIME_SUPPORTED_VERSIONS = ("3.8.75", "4.0")
MAX_POSE_CANVAS_PIXELS = 64 * 1024 * 1024
MAX_POSE_LAYER_PIXELS = 128 * 1024 * 1024
SPINE_VERSION_RE = re.compile(r"(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?(?!\d)")
SPINE_ATLAS_SUFFIXES = (".atlas", ".atlas.txt", ".atlas.bytes")
SPINE_SKEL_SUFFIXES = (".skel", ".skel.bytes")


class SpinePreviewError(RuntimeError):
    """Base error for safe Spine preview preparation."""


class SpineAssetNotFoundError(SpinePreviewError):
    """Raised when a source does not contain a usable Spine asset."""


class SpineRuntimeUnavailableError(SpinePreviewError):
    """Raised when dynamic preview needs a user runtime that was not found."""


class SpineRuntimeUnsupportedError(SpinePreviewError):
    """Raised for a runtime family that has not been validated by the app."""


class SpineVersionMismatchError(SpinePreviewError):
    """Raised when the runtime and skeleton major/minor versions differ."""


class SpinePoseExportError(SpinePreviewError):
    """Raised when a constrained pose-to-layer export cannot be completed."""


@dataclass(frozen=True)
class SpineRuntimePolicy:
    """Explicit runtime decision captured before a preview worker starts.

    Preview/core code accepts this value instead of reaching into
    ``SettingsManager``.  ``compatibility_mode`` forces the verified
    3.8.75 bridge, while ``selected_version`` remains the user's preserved
    choice for the normal mode.  ``prefer_installed`` allows the compatibility
    path to bypass an obsolete manually selected 4.0 directory and locate the
    matching verified package installed by the catalog.
    """

    compatibility_mode: bool = False
    selected_version: str = SPINE_COMPATIBILITY_VERSION
    runtime_root: Path | None = None
    prefer_installed: bool = False

    @property
    def effective_version(self) -> str:
        if self.compatibility_mode:
            return SPINE_COMPATIBILITY_VERSION
        text = str(self.selected_version or "").strip()
        if text == SPINE_COMPATIBILITY_VERSION:
            return text
        if text == "4.0" or text.startswith("4.0."):
            return "4.0"
        # Keep the low-level policy conservative: unknown versions are not
        # converted into an apparently supported runtime.
        return text

    @classmethod
    def from_values(
        cls,
        *,
        compatibility_mode: bool = False,
        selected_version: str = SPINE_COMPATIBILITY_VERSION,
        runtime_root: str | Path | None = None,
        prefer_installed: bool = False,
    ) -> "SpineRuntimePolicy":
        return cls(
            compatibility_mode=bool(compatibility_mode),
            selected_version=str(selected_version or SPINE_COMPATIBILITY_VERSION).strip(),
            runtime_root=Path(runtime_root).expanduser().resolve() if runtime_root else None,
            prefer_installed=bool(prefer_installed),
        )


@dataclass(frozen=True)
class SpineRuntime:
    """One verified native Spine bridge and its provenance metadata.

    ``webgl_script`` and ``core_script`` remain as optional compatibility
    fields for callers that used the old description object.  Native callers
    must use ``library_path`` (or resolve it again from ``root_dir`` and
    ``family``); no JavaScript runtime is ever returned.
    """

    root_dir: Path
    webgl_script: Path | None = None
    core_script: Path | None = None
    version: str | None = None
    source_manifest: Path | None = None
    combined_webgl: bool = True
    compatible_exact_versions: tuple[str, ...] = ()
    api_style: str = "native"
    library_path: Path | None = None

    @property
    def family(self) -> str | None:
        return spine_version_family(self.version)

    @property
    def scripts(self) -> tuple[Path, ...]:
        """Return the native bridge as a one-item compatibility tuple."""

        library = self.library_path or self.webgl_script
        if library is None:
            return ()
        if self.library_path is not None:
            return (self.library_path,)
        if self.combined_webgl:
            return (library,)
        values = []
        if self.core_script and self.core_script != library:
            values.append(self.core_script)
        values.append(library)
        return tuple(values)


@dataclass(frozen=True)
class SpinePreviewAsset:
    """Resolved skeleton, atlas, and page references under one root."""

    root_dir: Path
    skeleton_path: Path | None = None
    skeleton_format: str | None = None
    spine_version: str | None = None
    model_config_path: Path | None = None
    atlas_paths: tuple[Path, ...] = ()
    texture_paths: tuple[Path, ...] = ()
    skin_names: tuple[str, ...] = ()
    animation_names: tuple[str, ...] = ()
    attachment_names: tuple[str, ...] = ()
    unsupported_features: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def family(self) -> str | None:
        return spine_version_family(self.spine_version)

    @property
    def has_skeleton(self) -> bool:
        return self.skeleton_path is not None and self.skeleton_path.is_file()


@dataclass(frozen=True)
class SpinePreviewPlan:
    """A deterministic decision for runtime or atlas-page preview."""

    mode: str
    asset: SpinePreviewAsset
    runtime: SpineRuntime | None = None
    reason: str = ""
    warnings: tuple[str, ...] = ()
    requested_runtime_version: str | None = None
    runtime_error: str | None = None

    @property
    def runtime_missing(self) -> bool:
        """Whether animation preview needs an installed runtime decision."""

        return self.runtime_error is not None

    @property
    def dynamic(self) -> bool:
        return self.mode in {"native", "runtime"} and self.runtime is not None

    @property
    def can_animate(self) -> bool:
        """Whether a native/runtime bridge is available for animation."""

        return self.dynamic


@dataclass(frozen=True)
class SpinePoseLayer:
    """One axis-aligned attachment layer for constrained PSD export."""

    name: str
    image: Path
    left: int = 0
    top: int = 0
    opacity: float = 1.0
    visible: bool = True
    attachment: str = ""


@dataclass(frozen=True)
class SpinePoseExportReport:
    output_path: Path
    layer_names: tuple[str, ...]
    unsupported: tuple[str, ...] = ()


def parse_spine_version(value: Any) -> tuple[int, int, int] | None:
    """Parse a Spine version from text, JSON values, or bytes."""

    if value is None:
        return None
    if isinstance(value, bytes):
        value = value[:4096].decode("latin-1", errors="ignore")
    match = SPINE_VERSION_RE.search(str(value))
    if not match:
        return None
    return tuple(int(group or 0) for group in match.groups())  # type: ignore[return-value]


def normalize_spine_version(value: Any) -> str | None:
    parsed = parse_spine_version(value)
    if parsed is None:
        return None
    return ".".join(str(item) for item in parsed)


def spine_version_family(value: Any) -> str | None:
    parsed = parse_spine_version(value)
    if parsed is None:
        return None
    return f"{parsed[0]}.{parsed[1]}"


def read_spine_version(path: str | Path) -> str | None:
    """Read ``skeleton.spine`` from JSON or a version marker from binary."""

    source = Path(path)
    try:
        data = source.read_bytes()
    except OSError:
        return None
    if source.suffix.lower() == ".json" or data.lstrip()[:1] in {b"{", b"["}:
        try:
            root = json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            root = None
        if isinstance(root, dict):
            skeleton = root.get("skeleton")
            if isinstance(skeleton, dict):
                version = normalize_spine_version(skeleton.get("spine"))
                if version:
                    return version
            version = normalize_spine_version(root.get("spine"))
            if version:
                return version
    return normalize_spine_version(data[:4096])


def load_spine_asset(source: str | Path) -> SpinePreviewAsset:
    """Resolve a model config, skeleton, or atlas without modifying it."""

    path = Path(source).resolve()
    if path.is_file():
        if path.suffix.lower() == ".json":
            parsed = _try_read_json(path)
            if _looks_like_model_config(parsed):
                asset = _asset_from_model_config(path, parsed)
                if asset.skeleton_path or asset.atlas_paths:
                    return asset
            if _looks_like_skeleton_json(parsed):
                return _asset_from_skeleton(path, parsed)
        if _is_atlas_path(path):
            return _asset_from_atlas(path)
        if _is_skeleton_path(path):
            return _asset_from_skeleton(path, None)
        raise SpineAssetNotFoundError(f"Unsupported Spine source: {path}")

    if not path.is_dir():
        raise SpineAssetNotFoundError(f"Spine source does not exist: {path}")
    return _find_spine_asset_in_dir(path)


def find_spine_asset(source: str | Path) -> SpinePreviewAsset | None:
    """Best-effort variant of :func:`load_spine_asset` for drag/drop probes."""

    try:
        return load_spine_asset(source)
    except (OSError, SpinePreviewError, ValueError, TypeError):
        return None


def discover_spine_runtime(
    root: str | Path | None = None,
    requested_family: str | None = None,
    requested_version: str | None = None,
) -> SpineRuntime:
    """Find a versioned native bridge for a Spine family.

    ``root`` may be a selected family/version directory or the common
    ``spine_native`` directory.  With no root, the installed common directory
    is searched through :func:`find_native_library`.  A selected directory is
    never replaced with a global fallback, and an exact model version is only
    accepted when the sibling ``spine_native.json`` identifies that version.
    """

    requested = str(requested_family or "").strip() or None
    exact_requested = normalize_spine_version(requested_version) if requested_version else None
    if requested is None and exact_requested:
        requested = spine_version_family(exact_requested)

    explicit_root = Path(root).expanduser().resolve() if root else None
    if explicit_root is not None and not explicit_root.is_dir():
        raise SpineRuntimeUnavailableError(
            f"Native Spine runtime directory does not exist: {explicit_root}"
        )

    default_root = _default_native_runtime_root()
    roots: list[Path] = []
    if explicit_root is not None:
        roots.append(explicit_root)
    else:
        # Keep the native adapter as the source of truth for default lookup.
        # Its result also tells us which versioned child should be inspected.
        try:
            default_library = find_native_library(None, requested)
        except (OSError, RuntimeError):
            default_library = None
        if default_library is not None:
            roots.append(default_library.resolve().parent)
        if default_root.is_dir():
            roots.append(default_root)

    candidates: list[tuple[Path, Path, Mapping[str, Any] | None]] = []
    for candidate_root in _native_runtime_dirs(roots):
        try:
            library = find_native_library(candidate_root, requested)
        except (OSError, RuntimeError):
            library = None
        if library is None or not library.is_file():
            continue
        library = library.resolve()
        actual_root = library.parent.resolve()
        metadata = _native_runtime_metadata(actual_root)
        metadata_family = _native_metadata_family(metadata)
        if requested and metadata_family and metadata_family != requested:
            continue
        candidates.append((actual_root, library, metadata))

    # ``find_native_library`` can search an installed common root recursively;
    # include its direct result even when the runtime directory had no obvious
    # child entry (for example, a junction or a packaged one-file layout).
    if not candidates:
        try:
            library = find_native_library(explicit_root, requested) if explicit_root else find_native_library(None, requested)
        except (OSError, RuntimeError):
            library = None
        if library is not None and library.is_file():
            library = library.resolve()
            candidates.append((library.parent.resolve(), library, _native_runtime_metadata(library.parent)))

    if not candidates:
        selected = explicit_root or default_root
        family_text = requested or "the requested"
        raise SpineRuntimeUnavailableError(
            f"No native Spine bridge is installed for family {family_text} under {selected}. "
            "Install the matching runtime/tools/spine_native/<version> package."
        )

    if exact_requested:
        exact_matches = [
            item for item in candidates
            if _native_metadata_version(item[2])
            and normalize_spine_version(_native_metadata_version(item[2])) == exact_requested
        ]
        # The installed 4.0 bridge is intentionally marked as family-only
        # (``version: 4.0``) and has been validated with 4.0.37 assets.  A
        # family-only bridge is therefore valid for ordinary 4.0 requests;
        # the historical 3.8.75 bridge remains an exact-version exception.
        if not exact_matches and exact_requested != "3.8.75":
            exact_matches = [
                item for item in candidates
                if _native_metadata_family(item[2]) == requested
                and not _native_metadata_has_patch(item[2])
            ]
        if not exact_matches:
            found_versions = sorted(
                {
                    _native_metadata_version(metadata) or _native_version_from_path(runtime_root)
                    for runtime_root, _library, metadata in candidates
                    if _native_metadata_version(metadata) or _native_version_from_path(runtime_root)
                }
            )
            found_text = ", ".join(found_versions) if found_versions else "unknown"
            raise SpineVersionMismatchError(
                f"Requested exact Spine runtime {exact_requested}, but the native installation "
                f"provides {found_text}; an exact-version bridge is required for this model."
            )
        candidates = exact_matches

    runtime_root, library, metadata = sorted(
        candidates,
        key=lambda item: str(item[1]).casefold(),
    )[0]
    runtime = _runtime_from_native_library(runtime_root, library, metadata, requested)
    if runtime.family and requested and runtime.family != requested:
        raise SpineVersionMismatchError(
            f"Skeleton Spine {requested} does not match native runtime {runtime.family}."
        )
    return runtime


def _default_native_runtime_root() -> Path:
    return Path(__file__).resolve().parents[2] / "runtime" / "tools" / "spine_native"


def list_installed_spine_runtimes(
    root: str | Path | None = None,
) -> tuple[SpineRuntime, ...]:
    """List only native runtimes with a verifiable supported manifest.

    The catalog is intentionally local and conservative.  A random folder
    named ``4.1`` or a bridge without ``spine_native.json`` is not exposed as
    an installed choice, so the GUI cannot imply support for unverified code.
    """

    selected = Path(root).expanduser().resolve() if root else None
    roots = [selected] if selected is not None else [_default_native_runtime_root()]
    # A configured root may point at one family while the packaged catalog is
    # installed beside it.  The caller can opt into this helper explicitly;
    # it never makes this fallback during an ordinary user-selected preview.
    candidates: list[SpineRuntime] = []
    seen: set[str] = set()
    for candidate_root in _native_runtime_dirs([item for item in roots if item is not None]):
        try:
            metadata = _native_runtime_metadata(candidate_root)
        except Exception:
            metadata = None
        family = _native_metadata_family(metadata)
        version = _native_metadata_version(metadata) or _native_version_from_path(candidate_root)
        if family not in SPINE_RUNTIME_SUPPORTED_FAMILIES or not version:
            continue
        normalized_version = normalize_spine_version(version)
        if family == "3.8" and normalized_version != SPINE_COMPATIBILITY_VERSION:
            continue
        if family == "4.0":
            normalized_version = "4.0"
        library = find_native_library(candidate_root, family)
        if library is None or not library.is_file():
            continue
        key = str(library.resolve()).casefold()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(_runtime_from_native_library(
            candidate_root.resolve(), library.resolve(), metadata, family
        ))
    return tuple(sorted(candidates, key=lambda item: (item.family or "", item.version or "", str(item.root_dir).casefold())))


def _native_runtime_dirs(roots: Iterable[Path]) -> tuple[Path, ...]:
    """Return selected roots and their direct versioned children only."""

    values: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        try:
            resolved = root.resolve()
        except (OSError, RuntimeError):
            continue
        if not resolved.is_dir() or resolved in seen:
            continue
        seen.add(resolved)
        values.append(resolved)
        try:
            children = sorted(resolved.iterdir(), key=lambda item: item.name.casefold())
        except (OSError, RuntimeError):
            children = ()
        for child in children:
            if child.is_dir() and not child.name.startswith(".") and child not in seen:
                seen.add(child)
                values.append(child)
    return tuple(values)


def _native_runtime_metadata(root: Path) -> Mapping[str, Any] | None:
    data = _try_read_json(root / "spine_native.json")
    return data if isinstance(data, Mapping) else None


def _native_metadata_family(metadata: Mapping[str, Any] | None) -> str | None:
    if not isinstance(metadata, Mapping):
        return None
    declared = metadata.get("runtimeFamily") or metadata.get("family")
    return spine_version_family(declared) if declared else spine_version_family(_native_metadata_version(metadata))


def _native_metadata_version(metadata: Mapping[str, Any] | None) -> str | None:
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get("version") or metadata.get("spineVersion") or metadata.get("runtimeVersion")
    parsed = parse_spine_version(raw)
    if parsed is None:
        return None
    # Preserve family-only metadata such as the installed 4.0 bridge while
    # keeping exact patch versions such as 3.8.75 exact.
    text = str(raw)
    return f"{parsed[0]}.{parsed[1]}.{parsed[2]}" if len(text.split(".")) >= 3 else f"{parsed[0]}.{parsed[1]}"


def _native_metadata_has_patch(metadata: Mapping[str, Any] | None) -> bool:
    if not isinstance(metadata, Mapping):
        return False
    raw = metadata.get("version") or metadata.get("spineVersion") or metadata.get("runtimeVersion")
    match = SPINE_VERSION_RE.search(str(raw or ""))
    return bool(match and match.group(3) is not None)


def _native_version_from_path(root: Path) -> str | None:
    return normalize_spine_version(root.name)


def _runtime_from_native_library(
    root: Path,
    library: Path,
    metadata: Mapping[str, Any] | None,
    requested: str | None,
) -> SpineRuntime:
    version = _native_metadata_version(metadata) or _native_version_from_path(root) or requested
    exact_versions: list[str] = []
    if isinstance(metadata, Mapping):
        raw_exact = metadata.get("compatibleExactVersions") or metadata.get("compatibleVersions") or ()
        if isinstance(raw_exact, (str, bytes)):
            raw_exact = (raw_exact,)
        if isinstance(raw_exact, Iterable):
            exact_versions.extend(
                normalized
                for item in raw_exact
                for normalized in [normalize_spine_version(item)]
                if normalized
            )
    raw_version = metadata.get("version") if isinstance(metadata, Mapping) else None
    if raw_version is not None and len(str(raw_version).split(".")) >= 3:
        normalized = normalize_spine_version(version)
        if normalized:
            exact_versions.append(normalized)
    manifest = root / "spine_native.json"
    source_manifest = manifest.resolve() if manifest.is_file() else None
    library = library.resolve()
    return SpineRuntime(
        root_dir=root.resolve(),
        webgl_script=library,
        core_script=None,
        version=version,
        source_manifest=source_manifest,
        combined_webgl=True,
        compatible_exact_versions=tuple(dict.fromkeys(exact_versions)),
        api_style="native",
        library_path=library,
    )


def make_spine_preview_plan(
    asset: SpinePreviewAsset,
    runtime_root: str | Path | None = None,
    *,
    allow_unverified_runtime: bool = False,
    requested_runtime_version: str | None = None,
    prefer_installed: bool = False,
) -> SpinePreviewPlan:
    """Choose the native bridge or an explicit atlas-only fallback.

    A missing bridge is reported in the returned plan so the UI can explain
    how to install it.  A selected bridge with the wrong family or exact
    version remains an error: silently loading a 3.8.75 model with a 3.8.99
    bridge would produce misleading preview results.
    """

    family = asset.family
    if not asset.has_skeleton:
        return SpinePreviewPlan(
            mode="atlas",
            asset=asset,
            reason="未找到 skeleton，已降级为 Spine atlas 页面预览。",
            warnings=asset.warnings,
            requested_runtime_version=requested_runtime_version,
        )
    if family == "2.1":
        return SpinePreviewPlan(
            mode="atlas",
            asset=asset,
            reason="Spine 2.1 与当前 native bridge 不兼容，已降级为 atlas 页面预览。",
            warnings=asset.warnings + ("Spine 2.1 不进入 native 动画预览。",),
            requested_runtime_version=requested_runtime_version,
        )
    if not family:
        return SpinePreviewPlan(
            mode="atlas",
            asset=asset,
            reason="无法确认 skeleton Spine 版本，已降级为 atlas 页面预览。",
            warnings=asset.warnings + ("缺少 skeleton.spine 或二进制版本标记。",),
            requested_runtime_version=requested_runtime_version,
        )
    if family not in SPINE_RUNTIME_SUPPORTED_FAMILIES and not allow_unverified_runtime:
        # Keep the skeleton in the result so the caller can offer the one
        # verified conversion path (to 3.8.75).  Returning an atlas-only plan
        # without a structured error made an unsupported 4.1/4.2 asset look
        # like a harmless atlas and prevented the missing-runtime dialog from
        # explaining the available fallback.
        runtime_error = (
            f"Spine {family} has no verified native runtime in this build; "
            "a compatible preview requires conversion to 3.8.75."
        )
        return SpinePreviewPlan(
            mode="atlas",
            asset=asset,
            reason=f"Spine {family} 尚未在本程序中验证，已降级为 atlas 页面预览。",
            warnings=asset.warnings + (f"未声明支持 Spine {family} 动态 runtime。", runtime_error),
            requested_runtime_version=requested_runtime_version,
            runtime_error=runtime_error,
        )
    requested_exact = normalize_spine_version(requested_runtime_version) if requested_runtime_version else None
    requested_family = spine_version_family(requested_exact) if requested_exact else None
    # Compatibility mode supplies an exact target.  Normal mode keeps source
    # version fidelity and therefore follows the asset family/version first.
    lookup_family = requested_family if requested_family else family
    lookup_version = requested_exact if requested_exact else asset.spine_version
    runtime: SpineRuntime | None = None
    try:
        runtime = discover_spine_runtime(
            runtime_root,
            requested_family=lookup_family,
            requested_version=lookup_version,
        )
    except (SpineRuntimeUnavailableError, SpineVersionMismatchError) as exc:
        # In compatibility mode an obsolete manually selected root (for
        # example a standalone 4.0 directory) must not mask the verified
        # 3.8.75 catalog install.  Retry the managed default only when the
        # caller explicitly opted into that policy.
        # A configured root can be a single family directory.  If that root
        # does not contain the source-matching bridge, inspect the managed
        # catalog root as a source-fidelity fallback.  This does not convert
        # the asset or silently select a different family; it only allows a
        # common settings path to discover its sibling installation.
        selected_root = Path(runtime_root).expanduser() if runtime_root is not None else None
        catalog_like_root = False
        if selected_root is not None:
            try:
                root_name = normalize_spine_version(selected_root.name) or ""
                catalog_like_root = spine_version_family(root_name) in SPINE_RUNTIME_SUPPORTED_FAMILIES
                if selected_root.is_dir():
                    catalog_like_root = catalog_like_root or any(
                        spine_version_family(child.name) in SPINE_RUNTIME_SUPPORTED_FAMILIES
                        for child in selected_root.iterdir()
                        if child.is_dir()
                    )
            except (OSError, RuntimeError):
                catalog_like_root = False
        if runtime_root is not None and catalog_like_root:
            try:
                runtime = discover_spine_runtime(
                    None,
                    requested_family=lookup_family,
                    requested_version=lookup_version,
                )
            except SpinePreviewError:
                runtime = None
            if runtime is not None:
                exc = None
        if runtime is None:
            error_text = str(exc)
            return SpinePreviewPlan(
                mode="atlas",
                asset=asset,
                reason="未找到匹配的 Spine native bridge，已降级为 atlas 页面预览。",
                warnings=asset.warnings + (error_text, "请安装匹配的 runtime/tools/spine_native/<version>。"),
                requested_runtime_version=requested_runtime_version,
                runtime_error=error_text,
            )
    runtime_family = runtime.family
    if runtime_family and runtime_family != family:
        raise SpineVersionMismatchError(
            f"Skeleton Spine {family} 与 runtime {runtime_family} 不匹配。"
        )
    requested_version = normalize_spine_version(asset.spine_version)
    if requested_version and runtime.compatible_exact_versions:
        if requested_version not in runtime.compatible_exact_versions:
            raise SpineVersionMismatchError(
                f"Skeleton Spine {requested_version} is outside the runtime's exact compatibility set "
                f"{', '.join(runtime.compatible_exact_versions)}."
            )
    if requested_version == "3.8.75" and normalize_spine_version(runtime.version) != requested_version:
        raise SpineVersionMismatchError(
            f"Skeleton Spine {requested_version} requires the dedicated historical runtime; "
            f"selected runtime reports {runtime.version or 'unknown'}."
        )
    if runtime_family is None:
        raise SpineRuntimeUnavailableError(
            f"Unable to verify runtime major/minor version under {runtime.root_dir}"
        )
    if runtime_family not in SPINE_RUNTIME_SUPPORTED_FAMILIES and not allow_unverified_runtime:
        return SpinePreviewPlan(
            mode="atlas",
            asset=asset,
            reason=f"Spine native runtime {runtime_family} 尚未完成真实样本验证，已降级为 atlas 页面预览。",
            warnings=asset.warnings + (f"native runtime {runtime_family} 仅记录版本，不宣称动态支持。",),
            requested_runtime_version=requested_runtime_version,
        )
    return SpinePreviewPlan(
        mode="native",
        asset=asset,
        runtime=runtime,
        reason=f"使用 Spine {runtime_family} native bridge。",
        warnings=asset.warnings,
        requested_runtime_version=requested_runtime_version,
    )


def prepare_spine_preview_import(
    source: str | Path,
    temp_root: str | Path,
    runtime_root: str | Path | None = None,
    log=None,
) -> "SpinePreviewImportResult":
    """Extract an archive to a disposable directory and prepare its plan."""

    source_path = Path(source).resolve()
    try:
        asset = load_spine_asset(source_path)
        return SpinePreviewImportResult(asset=asset, plan=make_spine_preview_plan(asset, runtime_root))
    except SpineAssetNotFoundError:
        pass

    from app.core.extract import ExtractMode, ExtractSourceType, detect_source_type, run_extraction_batch

    source_type = detect_source_type(source_path)
    if source_type not in {
        ExtractSourceType.LPK,
        ExtractSourceType.WPK,
        ExtractSourceType.FOLDER,
        ExtractSourceType.UNITY,
    }:
        raise SpineAssetNotFoundError(f"Unsupported Spine preview source: {source_path}")
    root = Path(temp_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(__import__("tempfile").mkdtemp(prefix="spine_preview_", dir=root))
    try:
        result = run_extraction_batch([source_path], temp_dir, ExtractMode.FULL, log=log)
        if result.has_failures:
            message = result.failed_items[0].error if result.failed_items else "Spine extraction failed"
            raise SpinePreviewError(message or "Spine extraction failed")
        asset = load_spine_asset(temp_dir)
        plan = make_spine_preview_plan(asset, runtime_root)
        return SpinePreviewImportResult(asset=asset, plan=plan, temp_dir=temp_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


@dataclass(frozen=True)
class SpinePreviewImportResult:
    asset: SpinePreviewAsset
    plan: SpinePreviewPlan
    temp_dir: Path | None = None


def export_spine_pose_psd(
    layers: Iterable[SpinePoseLayer],
    output_path: str | Path,
    *,
    canvas_size: tuple[int, int] | None = None,
    unsupported: Iterable[str] = (),
) -> SpinePoseExportReport:
    """Write a constrained attachment-layer PSD.

    This intentionally accepts only already-rasterized, axis-aligned attachment
    layers.  Mesh deformation, clipping, weighted transforms, and blend modes
    must be reported by the caller instead of being silently flattened.
    """

    layer_list = [layer for layer in layers if layer.visible]
    if not layer_list:
        raise SpinePoseExportError("No visible raster attachment layers were provided.")
    try:
        from PIL import Image
        from psd_tools import PSDImage
        from psd_tools.api.layers import PixelLayer
        from psd_tools.constants import Compression
    except Exception as exc:
        raise SpinePoseExportError("Pose PSD export requires Pillow and psd-tools.") from exc

    resolved: list[tuple[SpinePoseLayer, Any]] = []
    max_right = max_bottom = 0
    min_left = min_top = 0
    for layer in layer_list:
        path = Path(layer.image).resolve()
        if not path.is_file():
            raise SpinePoseExportError(f"Pose layer image does not exist: {path}")
        try:
            image = Image.open(path).convert("RGBA")
        except Exception as exc:
            raise SpinePoseExportError(f"Unable to read pose layer {path}: {exc}") from exc
        if not 0 <= float(layer.opacity) <= 1:
            raise SpinePoseExportError(f"Pose layer opacity must be between 0 and 1: {layer.opacity}")
        bbox = image.getchannel("A").getbbox()
        if bbox:
            image = image.crop(bbox)
            layer = SpinePoseLayer(
                name=layer.name,
                image=layer.image,
                left=int(layer.left) + int(bbox[0]),
                top=int(layer.top) + int(bbox[1]),
                opacity=layer.opacity,
                visible=layer.visible,
                attachment=layer.attachment,
            )
        resolved.append((layer, image))
        min_left = min(min_left, int(layer.left))
        min_top = min(min_top, int(layer.top))
        max_right = max(max_right, int(layer.left) + image.width)
        max_bottom = max(max_bottom, int(layer.top) + image.height)
        if sum(item.width * item.height for _layer, item in resolved) > MAX_POSE_LAYER_PIXELS:
            raise SpinePoseExportError("Pose attachment layers exceed the export pixel budget.")
    if canvas_size:
        width, height = (int(canvas_size[0]), int(canvas_size[1]))
        if width <= 0 or height <= 0:
            raise SpinePoseExportError(f"Invalid PSD canvas size: {canvas_size}")
    else:
        width, height = max_right - min_left, max_bottom - min_top
    if width <= 0 or height <= 0:
        raise SpinePoseExportError("Pose PSD canvas is empty.")
    if width * height > MAX_POSE_CANVAS_PIXELS:
        raise SpinePoseExportError(
            f"Pose PSD canvas is too large ({width}x{height}); export a smaller preview."
        )

    destination = Path(output_path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    psd = PSDImage.new("RGBA", (width, height))
    names: list[str] = []
    for layer, image in resolved:
        if layer.opacity != 1.0:
            alpha = image.getchannel("A").point(lambda value: int(round(value * float(layer.opacity))))
            image.putalpha(alpha)
        left = int(layer.left) - min_left
        top = int(layer.top) - min_top
        PixelLayer.frompil(
            image,
            psd,
            name=str(layer.name),
            top=top,
            left=left,
            compression=Compression.RAW,
        )
        names.append(str(layer.name))
    try:
        psd.save(str(destination))
    except Exception as exc:
        raise SpinePoseExportError(f"Unable to write pose PSD {destination}: {exc}") from exc
    return SpinePoseExportReport(
        output_path=destination,
        layer_names=tuple(names),
        unsupported=tuple(str(item) for item in unsupported if str(item)),
    )


def _find_spine_asset_in_dir(root: Path) -> SpinePreviewAsset:
    configs = sorted(root.rglob("*.json"), key=lambda item: (len(item.parts), item.name.lower(), str(item).lower()))
    for path in configs:
        data = _try_read_json(path)
        if _looks_like_model_config(data):
            asset = _asset_from_model_config(path, data)
            if asset.skeleton_path or asset.atlas_paths:
                return asset
    for path in configs:
        data = _try_read_json(path)
        if _looks_like_skeleton_json(data):
            return _asset_from_skeleton(path, data)
    skeletons = sorted(
        [path for path in root.rglob("*") if path.is_file() and _is_skeleton_path(path)],
        key=lambda item: str(item).lower(),
    )
    if skeletons:
        return _asset_from_skeleton(skeletons[0], None)
    atlases = sorted(
        [path for path in root.rglob("*") if path.is_file() and _is_atlas_path(path)],
        key=lambda item: str(item).lower(),
    )
    if atlases:
        return _asset_from_atlas(atlases[0])
    raise SpineAssetNotFoundError(f"No Spine skeleton/atlas found under {root}")


def _asset_from_model_config(path: Path, data: Mapping[str, Any]) -> SpinePreviewAsset:
    root = path.parent.resolve()
    skeleton_ref = data.get("skeleton")
    skeleton_path = _resolve_reference(root, skeleton_ref, SPINE_SKEL_SUFFIXES + (".json",))
    skeleton_data = _try_read_json(skeleton_path) if skeleton_path and skeleton_path.suffix.lower() == ".json" else None
    if skeleton_path and skeleton_data is not None and not _looks_like_skeleton_json(skeleton_data):
        skeleton_data = None
    version = read_spine_version(skeleton_path) if skeleton_path else None
    if isinstance(skeleton_data, dict):
        version = version or read_spine_version(skeleton_path)

    atlas_paths: list[Path] = []
    texture_paths: list[Path] = []
    atlases = data.get("atlases")
    if isinstance(atlases, Mapping):
        atlases = [atlases]
    if isinstance(atlases, list):
        for item in atlases:
            if not isinstance(item, Mapping):
                continue
            atlas = _resolve_reference(root, item.get("atlas"), SPINE_ATLAS_SUFFIXES)
            if atlas and atlas not in atlas_paths:
                atlas_paths.append(atlas)
            for raw in item.get("textures") or []:
                texture = _resolve_reference(root, raw, _image_suffixes())
                if texture and texture not in texture_paths:
                    texture_paths.append(texture)
            if atlas:
                for page in _atlas_page_names(atlas):
                    texture = _resolve_reference(atlas.parent, page, _image_suffixes())
                    if texture and texture not in texture_paths:
                        texture_paths.append(texture)
    if not atlas_paths:
        atlas_paths = _nearby_atlases(root, skeleton_path)
    if not texture_paths:
        for atlas in atlas_paths:
            for page in _atlas_page_names(atlas):
                texture = _resolve_reference(atlas.parent, page, _image_suffixes())
                if texture and texture not in texture_paths:
                    texture_paths.append(texture)
    meta = _skeleton_metadata(skeleton_data)
    warnings: list[str] = []
    if isinstance(skeleton_ref, str) and skeleton_path is None:
        warnings.append(f"skeleton reference not found: {skeleton_ref}")
    return SpinePreviewAsset(
        root_dir=root,
        skeleton_path=skeleton_path,
        skeleton_format="json" if skeleton_path and skeleton_path.suffix.lower() == ".json" else "binary" if skeleton_path else None,
        spine_version=version,
        model_config_path=path.resolve(),
        atlas_paths=tuple(atlas_paths),
        texture_paths=tuple(texture_paths),
        skin_names=meta[0],
        animation_names=meta[1],
        attachment_names=meta[2],
        unsupported_features=meta[3],
        warnings=tuple(warnings),
    )


def _asset_from_skeleton(path: Path, data: Mapping[str, Any] | None) -> SpinePreviewAsset:
    root = path.parent.resolve()
    atlas_paths = _nearby_atlases(root, path)
    atlas_paths = _match_skeleton_atlases(path, data, atlas_paths)
    textures: list[Path] = []
    for atlas in atlas_paths:
        for page in _atlas_page_names(atlas):
            texture = _resolve_reference(atlas.parent, page, _image_suffixes())
            if texture and texture not in textures:
                textures.append(texture)
    meta = _skeleton_metadata(data)
    return SpinePreviewAsset(
        root_dir=root,
        skeleton_path=path.resolve(),
        skeleton_format="json" if path.suffix.lower() == ".json" else "binary",
        spine_version=read_spine_version(path),
        atlas_paths=tuple(atlas_paths),
        texture_paths=tuple(textures),
        skin_names=meta[0],
        animation_names=meta[1],
        attachment_names=meta[2],
        unsupported_features=meta[3],
    )


def _asset_from_atlas(path: Path) -> SpinePreviewAsset:
    textures = []
    for page in _atlas_page_names(path):
        texture = _resolve_reference(path.parent, page, _image_suffixes())
        if texture and texture not in textures:
            textures.append(texture)
    return SpinePreviewAsset(
        root_dir=path.parent.resolve(),
        atlas_paths=(path.resolve(),),
        texture_paths=tuple(textures),
        warnings=("仅找到 atlas，未加载 skeleton 动画。",),
    )


def _try_read_json(path: Path | None) -> dict[str, Any] | None:
    try:
        if not path or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _looks_like_model_config(data: Mapping[str, Any] | None) -> bool:
    if not isinstance(data, Mapping):
        return False
    return isinstance(data.get("skeleton"), (str, Mapping)) and "atlases" in data


def _looks_like_skeleton_json(data: Mapping[str, Any] | None) -> bool:
    if not isinstance(data, Mapping):
        return False
    skeleton = data.get("skeleton")
    return isinstance(skeleton, Mapping) and isinstance(data.get("bones"), list)


def _resolve_reference(root: Path, raw: Any, suffixes: Sequence[str]) -> Path | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    candidate = (root / raw.replace("\\", "/")).resolve()
    if candidate.is_file():
        return candidate
    # LPK recovery can leave a page name without the original extension.  A
    # case-insensitive basename lookup keeps the resolver useful on Windows.
    wanted = Path(raw.replace("\\", "/")).name.lower()
    for item in root.glob("*"):
        if item.is_file() and item.name.lower() == wanted:
            return item.resolve()
    for suffix in suffixes:
        with_ext = candidate.with_suffix(candidate.suffix + suffix if candidate.suffix else suffix)
        if with_ext.is_file():
            return with_ext.resolve()
    return None


def _nearby_atlases(root: Path, skeleton: Path | None) -> list[Path]:
    candidates = [path for path in root.rglob("*") if path.is_file() and _is_atlas_path(path)]
    if skeleton:
        candidates.sort(key=lambda item: (0 if item.parent == skeleton.parent else 1, str(item).lower()))
    else:
        candidates.sort(key=lambda item: str(item).lower())
    return candidates


def _match_skeleton_atlases(
    skeleton: Path, data: Mapping[str, Any] | None, candidates: list[Path],
) -> list[Path]:
    """Disambiguate adjacent models by their actual attachment image paths."""
    if len(candidates) < 2:
        return candidates
    required: set[str] = set()
    raw_skins = data.get("skins", {}) if isinstance(data, Mapping) else {}
    if isinstance(raw_skins, list):
        skins = [skin.get("attachments", {}) for skin in raw_skins if isinstance(skin, Mapping)]
    elif isinstance(raw_skins, Mapping):
        skins = list(raw_skins.values())
    else:
        skins = []
    for skin in skins:
        if not isinstance(skin, Mapping):
            continue
        for slot in skin.values():
            if not isinstance(slot, Mapping):
                continue
            for name, attachment in slot.items():
                if not isinstance(attachment, Mapping):
                    continue
                kind = attachment.get("type", "region")
                if kind not in {"region", "mesh", "weightedmesh", "skinnedmesh"}:
                    continue
                image_path = attachment.get("path", name)
                if isinstance(image_path, str):
                    required.add(image_path)
    if required:
        from app.core.spine_atlas import SpineAtlasError, parse_atlas

        matching = []
        for candidate in candidates:
            try:
                names = {region.name for region in parse_atlas(candidate).regions}
            except (OSError, ValueError, SpineAtlasError):
                continue
            if required.issubset(names):
                matching.append(candidate)
        if len(matching) == 1:
            return matching
        if matching:
            candidates = matching
    # Binary skeletons have no cheap attachment inventory; use conventional
    # editor export suffixes only when one unambiguous basename matches.
    def base(path: Path) -> str:
        return re.sub(r"(?:[-_](?:pro|ess|pma))+$", "", path.stem.casefold())

    named = [candidate for candidate in candidates if base(candidate) == base(skeleton)]
    return named if len(named) == 1 else candidates


def _atlas_page_names(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return []

    # Atlas page metadata is normally unindented and region metadata is
    # indented.  A few modern writers omit ``size``/``format`` or use an
    # extensionless page name, so retain a small structural fallback for a
    # page at the start of a blank-separated block.  Do not treat every
    # region name followed by indented fields as another page: that used to
    # produce bogus texture URLs when a region happened to share an image
    # suffix with a real page.
    page_metadata = {
        "size",
        "format",
        "filter",
        "repeat",
        "pma",
        "scale",
        "minificationfilter",
        "magnificationfilter",
    }
    names: list[str] = []
    at_block_start = True
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            at_block_start = True
            continue
        if line[:1].isspace() or ":" in line:
            continue
        candidate = stripped
        if _looks_like_image_name(candidate):
            names.append(candidate)
            at_block_start = False
            continue
        if at_block_start:
            probe = index + 1
            while probe < len(lines) and not lines[probe].strip():
                probe += 1
            if probe < len(lines):
                next_line = lines[probe].strip()
                key = next_line.split(":", 1)[0].strip().lower() if ":" in next_line else ""
                if key in page_metadata:
                    names.append(candidate)
            at_block_start = False
    return list(dict.fromkeys(names))


def _skeleton_metadata(data: Mapping[str, Any] | None) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    if not isinstance(data, Mapping):
        return (), (), (), ()
    skins: list[str] = []
    attachments: list[str] = []
    raw_skins = data.get("skins")
    if isinstance(raw_skins, Mapping):
        skin_items = raw_skins.items()
    elif isinstance(raw_skins, list):
        skin_items = ((item.get("name"), item) for item in raw_skins if isinstance(item, Mapping))
    else:
        skin_items = ()
    unsupported: list[str] = []
    for skin_name, skin in skin_items:
        if isinstance(skin_name, str) and skin_name not in skins:
            skins.append(skin_name)
        if isinstance(skin, Mapping):
            for slot in skin.values():
                if isinstance(slot, Mapping):
                    for name, attachment in slot.items():
                        if isinstance(name, str) and name not in attachments:
                            attachments.append(name)
                        if isinstance(attachment, Mapping):
                            typ = str(attachment.get("type", "region")).lower()
                            if typ in {"mesh", "linkedmesh", "clipping", "path", "point", "boundingbox", "transform"}:
                                unsupported.append(f"{typ}:{name}")
    raw_animations = data.get("animations")
    if isinstance(raw_animations, Mapping):
        animations = tuple(str(name) for name in raw_animations)
    elif isinstance(raw_animations, list):
        animations = tuple(str(item.get("name")) for item in raw_animations if isinstance(item, Mapping) and item.get("name"))
    else:
        animations = ()
    return tuple(skins), animations, tuple(attachments), tuple(dict.fromkeys(unsupported))


def _is_atlas_path(path: Path) -> bool:
    lower = path.name.lower()
    return lower.endswith(SPINE_ATLAS_SUFFIXES)


def _is_skeleton_path(path: Path) -> bool:
    lower = path.name.lower()
    return lower.endswith(SPINE_SKEL_SUFFIXES)


def _looks_like_image_name(value: str) -> bool:
    return value.lower().endswith(_image_suffixes())


def _image_suffixes() -> tuple[str, ...]:
    return (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")


__all__ = [
    "MAX_POSE_CANVAS_PIXELS",
    "MAX_POSE_LAYER_PIXELS",
    "SPINE_RUNTIME_KNOWN_FAMILIES",
    "SPINE_RUNTIME_SUPPORTED_FAMILIES",
    "SPINE_COMPATIBILITY_VERSION",
    "SPINE_RUNTIME_SUPPORTED_VERSIONS",
    "SpineAssetNotFoundError",
    "SpinePoseExportError",
    "SpinePoseExportReport",
    "SpinePoseLayer",
    "SpinePreviewAsset",
    "SpinePreviewError",
    "SpinePreviewImportResult",
    "SpinePreviewPlan",
    "SpineRuntime",
    "SpineRuntimePolicy",
    "SpineRuntimeUnavailableError",
    "SpineRuntimeUnsupportedError",
    "SpineVersionMismatchError",
    "discover_spine_runtime",
    "export_spine_pose_psd",
    "find_spine_asset",
    "load_spine_asset",
    "list_installed_spine_runtimes",
    "make_spine_preview_plan",
    "normalize_spine_version",
    "parse_spine_version",
    "prepare_spine_preview_import",
    "read_spine_version",
    "spine_version_family",
]
