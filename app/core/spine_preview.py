"""Spine preview discovery, runtime gating, and pose export helpers.

The application does not ship a Spine runtime.  A user may point the preview
page at an official ``spine-ts`` runtime directory containing a matching
``spine-core``/``spine-webgl`` build.  This module only reads that directory;
it never downloads, copies, or mutates runtime or model files.

Spine editor and runtime major/minor versions must match.  Spine 2.1 assets
are intentionally downgraded to atlas-page preview.  The validated dynamic
families are 3.8 and 4.0; newer families remain explicit and are not silently
loaded by an older runtime.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SPINE_RUNTIME_SUPPORTED_FAMILIES = frozenset({"3.8", "4.0"})
SPINE_RUNTIME_KNOWN_FAMILIES = frozenset({"3.8", "4.0", "4.2"})
MAX_POSE_CANVAS_PIXELS = 64 * 1024 * 1024
MAX_POSE_LAYER_PIXELS = 128 * 1024 * 1024
SPINE_VERSION_RE = re.compile(r"(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?(?!\d)")
SPINE_SCRIPT_NAMES = {
    "spine-core.js",
    "spine-core.min.js",
    "spine-webgl.js",
    "spine-webgl.min.js",
}
_SPINE_RUNTIME_MANIFEST_NAMES = frozenset({"spine_runtime.json", "runtime.json", "manifest.json"})
_SPINE_RUNTIME_SKIP_DIR_NAMES = frozenset({".git", "node_modules"})
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
class SpineRuntime:
    """A user-provided official spine-ts runtime build."""

    root_dir: Path
    webgl_script: Path
    core_script: Path | None = None
    version: str | None = None
    source_manifest: Path | None = None
    combined_webgl: bool = False
    compatible_exact_versions: tuple[str, ...] = ()
    # spine-ts 3.8 exposes WebGL classes under ``spine.webgl`` while 4.0
    # publishes one self-contained IIFE whose WebGL classes are top-level.
    # Keep this in the runtime description so the browser renderer can select
    # the API from the verified manifest rather than guessing from script text.
    api_style: str = "legacy"

    @property
    def family(self) -> str | None:
        return spine_version_family(self.version)

    @property
    def scripts(self) -> tuple[Path, ...]:
        if self.combined_webgl:
            return (self.webgl_script,)
        values = []
        if self.core_script and self.core_script != self.webgl_script:
            values.append(self.core_script)
        values.append(self.webgl_script)
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

    @property
    def dynamic(self) -> bool:
        return self.mode == "runtime" and self.runtime is not None


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
    root: str | Path,
    requested_family: str | None = None,
    requested_version: str | None = None,
) -> SpineRuntime:
    """Find a user-provided core/webgl runtime pair.

    A small ``spine_runtime.json``/``runtime.json`` manifest is preferred when
    present.  Without one, official script names are located recursively and
    the version is inferred from the manifest/path/file name when possible.
    ``requested_version`` is used for exact-version builds such as the
    historical 3.8.75 runtime; ordinary 3.8/4.0 requests retain family-level
    compatibility when a runtime manifest does not declare an exact set.
    """

    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise SpineRuntimeUnavailableError(f"Spine runtime directory does not exist: {base}")
    requested = str(requested_family or "").strip() or None
    exact_requested = normalize_spine_version(requested_version) if requested_version else None

    explicit_manifest_families: list[str] = []
    explicit_manifest_versions: list[str] = []

    # The configured path may be a unified ``tools/spine`` root.  Check only
    # its own manifest and the requested version directory first, so a large
    # source checkout or a broken dependency link cannot block a valid build.
    prioritized = list(_direct_runtime_manifests(base))
    if exact_requested:
        requested_dir = _requested_runtime_version_dir(base, exact_requested)
        if requested_dir is not None:
            prioritized.extend(_direct_runtime_manifests(requested_dir))
    if requested:
        requested_dir = _requested_runtime_family_dir(base, requested)
        if requested_dir is not None:
            prioritized.extend(_direct_runtime_manifests(requested_dir))

    seen_manifests: set[Path] = set()

    def inspect_manifest(manifest: Path) -> SpineRuntime | None:
        try:
            identity = manifest.resolve()
        except (OSError, RuntimeError):
            identity = manifest
        if identity in seen_manifests:
            return None
        seen_manifests.add(identity)
        manifest_data = _try_read_json(manifest)
        manifest_family = (
            spine_version_family(
                manifest_data.get("version")
                or manifest_data.get("spineVersion")
                or manifest_data.get("runtimeVersion")
            )
            if isinstance(manifest_data, Mapping)
            else None
        )
        if manifest_family:
            explicit_manifest_families.append(manifest_family)
        manifest_version = (
            normalize_spine_version(
                manifest_data.get("version")
                or manifest_data.get("spineVersion")
                or manifest_data.get("runtimeVersion")
            )
            if isinstance(manifest_data, Mapping)
            else None
        )
        if manifest_version:
            explicit_manifest_versions.append(manifest_version)
        try:
            return _runtime_from_manifest(manifest, requested, exact_requested)
        except (OSError, RuntimeError):
            return None

    for manifest in prioritized:
        runtime = inspect_manifest(manifest)
        if runtime is not None:
            return runtime

    # Recursive discovery is deliberately a fallback.  The iterator prunes
    # dependency/source metadata directories and treats disappearing or
    # inaccessible entries as non-matches rather than aborting discovery.
    manifests = sorted(
        _iter_runtime_files(base, names=_SPINE_RUNTIME_MANIFEST_NAMES),
        key=lambda path: str(path).casefold(),
    )
    for manifest in manifests:
        runtime = inspect_manifest(manifest)
        if runtime is not None:
            return runtime
    if exact_requested and explicit_manifest_versions and exact_requested not in explicit_manifest_versions:
        raise SpineVersionMismatchError(
            f"Requested Spine runtime {exact_requested}, manifests declare {', '.join(sorted(set(explicit_manifest_versions)))}"
        )
    if requested and explicit_manifest_families and requested not in explicit_manifest_families:
        raise SpineVersionMismatchError(
            f"Requested Spine runtime {requested}, manifests declare {', '.join(sorted(set(explicit_manifest_families)))}"
        )

    scripts = list(_iter_runtime_files(base, names=SPINE_SCRIPT_NAMES))
    if not scripts:
        raise SpineRuntimeUnavailableError(
            f"No official spine-core.js/spine-webgl.js build found under {base}"
        )
    webgl = _pick_script(scripts, "spine-webgl", requested, exact_requested)
    if webgl is None:
        if requested:
            found_families = sorted({family for family in (_infer_runtime_family(path) for path in scripts if path.stem.lower().startswith("spine-webgl")) if family})
            if found_families:
                raise SpineVersionMismatchError(
                    f"Requested Spine runtime {requested}, found {', '.join(found_families)} under {base}"
                )
        raise SpineRuntimeUnavailableError(f"No matching spine-webgl.js build found under {base}")
    family = _infer_runtime_family(webgl)
    core = _pick_script(scripts, "spine-core", requested or family, exact_requested)
    combined = core is None
    if requested and family and family != requested:
        raise SpineVersionMismatchError(
            f"Requested Spine runtime {requested}, found {family} at {webgl}"
        )
    version = _infer_runtime_version(webgl) or family or requested
    if exact_requested == "3.8.75" and version != exact_requested:
        raise SpineVersionMismatchError(
            f"Requested exact Spine runtime {exact_requested}, but the selected build reports {version} at {webgl}"
        )
    return SpineRuntime(
        root_dir=base,
        webgl_script=webgl,
        core_script=core,
        version=version,
        combined_webgl=combined,
        api_style=_runtime_api_style(None, version or family or requested),
    )


def _direct_runtime_manifests(root: Path) -> tuple[Path, ...]:
    """Return manifest files directly below *root*, tolerating stale entries."""

    candidates: list[Path] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                try:
                    if entry.name.casefold() not in _SPINE_RUNTIME_MANIFEST_NAMES:
                        continue
                    if entry.is_file(follow_symlinks=False):
                        candidates.append(Path(entry.path))
                except (OSError, RuntimeError):
                    continue
    except (OSError, RuntimeError):
        return ()
    return tuple(sorted(candidates, key=lambda path: str(path).casefold()))


def _requested_runtime_family_dir(base: Path, requested: str) -> Path | None:
    """Resolve a direct requested-family child without allowing parent escape."""

    try:
        candidate = (base / requested).resolve()
        if candidate == base or candidate.parent != base or not candidate.is_dir():
            return None
    except (OSError, RuntimeError):
        return None
    return candidate


def _requested_runtime_version_dir(base: Path, requested: str) -> Path | None:
    """Resolve a direct exact-version child for a precise runtime request."""

    try:
        candidate = (base / requested).resolve()
        if candidate == base or candidate.parent != base or not candidate.is_dir():
            return None
    except (OSError, RuntimeError):
        return None
    return candidate


def _iter_runtime_files(
    root: Path,
    *,
    names: Iterable[str] = (),
    suffixes: Iterable[str] = (),
) -> Iterable[Path]:
    """Walk below *root* without following links into broken dependencies."""

    wanted_names = {str(name).casefold() for name in names}
    wanted_suffixes = tuple(str(suffix).casefold() for suffix in suffixes)
    pending = [Path(root)]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                children = sorted(entries, key=lambda item: item.name.casefold(), reverse=True)
        except (OSError, RuntimeError):
            continue
        for entry in children:
            try:
                entry_name = entry.name.casefold()
                if entry_name in _SPINE_RUNTIME_SKIP_DIR_NAMES:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                if wanted_names and entry_name not in wanted_names:
                    continue
                if wanted_suffixes and not entry_name.endswith(wanted_suffixes):
                    continue
                yield Path(entry.path)
            except (OSError, RuntimeError):
                continue


def make_spine_preview_plan(
    asset: SpinePreviewAsset,
    runtime_root: str | Path | None = None,
    *,
    allow_unverified_runtime: bool = False,
) -> SpinePreviewPlan:
    """Choose dynamic runtime or explicit atlas fallback.

    2.1 and unverified families remain viewable as atlas pages.  A supplied
    runtime with a different family is rejected so a user cannot accidentally
    parse a 3.8 skeleton with a 4.2 runtime (or vice versa).
    """

    family = asset.family
    if not asset.has_skeleton:
        return SpinePreviewPlan(
            mode="atlas",
            asset=asset,
            reason="未找到 skeleton，已降级为 Spine atlas 页面预览。",
            warnings=asset.warnings,
        )
    if family == "2.1":
        return SpinePreviewPlan(
            mode="atlas",
            asset=asset,
            reason="Spine 2.1 与当前动态 runtime 不兼容，已降级为 atlas 页面预览。",
            warnings=asset.warnings + ("Spine 2.1 不进入动态 runtime。",),
        )
    if not family:
        return SpinePreviewPlan(
            mode="atlas",
            asset=asset,
            reason="无法确认 skeleton Spine 版本，已降级为 atlas 页面预览。",
            warnings=asset.warnings + ("缺少 skeleton.spine 或二进制版本标记。",),
        )
    if family not in SPINE_RUNTIME_SUPPORTED_FAMILIES and not allow_unverified_runtime:
        return SpinePreviewPlan(
            mode="atlas",
            asset=asset,
            reason=f"Spine {family} 尚未在本程序中验证，已降级为 atlas 页面预览。",
            warnings=asset.warnings + (f"未声明支持 Spine {family} 动态 runtime。",),
        )
    if not runtime_root:
        return SpinePreviewPlan(
            mode="atlas",
            asset=asset,
            reason="未配置匹配的官方 Spine runtime，已降级为 atlas 页面预览。",
            warnings=asset.warnings + ("请在设置中选择同主次版本的 spine-ts core/webgl。",),
        )
    runtime = discover_spine_runtime(
        runtime_root,
        requested_family=family,
        requested_version=asset.spine_version,
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
            reason=f"Spine runtime {runtime_family} 尚未完成真实样本验证，已降级为 atlas 页面预览。",
            warnings=asset.warnings + (f"runtime {runtime_family} 仅记录版本，不宣称动态支持。",),
        )
    return SpinePreviewPlan(
        mode="runtime",
        asset=asset,
        runtime=runtime,
        reason=f"使用官方 Spine {runtime_family} core/webgl runtime。",
        warnings=asset.warnings,
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

    suffix = source_path.suffix.lower()
    if suffix not in {".lpk", ".wpk"} and not source_path.is_dir():
        raise SpineAssetNotFoundError(f"No Spine asset found under {source_path}")

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


def build_spine_web_manifest(
    plan: SpinePreviewPlan,
    asset_base_url: str,
    runtime_base_url: str | None = None,
) -> dict[str, Any]:
    """Build URL-only data for ``assets/spine/preview.html``."""

    asset = plan.asset
    root = asset.root_dir.resolve()
    manifest: dict[str, Any] = {
        "mode": plan.mode,
        "reason": plan.reason,
        "warnings": list(plan.warnings),
        "version": asset.spine_version,
        "family": asset.family,
        "skeletonFormat": asset.skeleton_format,
        "skeleton": _relative_url(asset.skeleton_path, root, asset_base_url),
        "atlases": [_relative_url(path, root, asset_base_url) for path in asset.atlas_paths],
        "textures": [_relative_url(path, root, asset_base_url) for path in asset.texture_paths],
        "skins": list(asset.skin_names),
        "animations": list(asset.animation_names),
        "attachments": list(asset.attachment_names),
        "unsupported": list(asset.unsupported_features),
    }
    if plan.runtime:
        runtime_root = plan.runtime.root_dir.resolve()
        runtime_base_url = runtime_base_url or asset_base_url
        manifest["runtime"] = {
            "version": plan.runtime.version,
            "family": plan.runtime.family,
            "core": _relative_url(plan.runtime.core_script, runtime_root, runtime_base_url),
            "webgl": _relative_url(plan.runtime.webgl_script, runtime_root, runtime_base_url),
            "combined": plan.runtime.combined_webgl,
            "api": plan.runtime.api_style,
        }
    else:
        manifest["runtime"] = None
    return manifest


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


def _runtime_from_manifest(
    path: Path,
    requested: str | None,
    requested_version: str | None = None,
) -> SpineRuntime | None:
    data = _try_read_json(path)
    if not isinstance(data, Mapping):
        return None
    version = normalize_spine_version(data.get("version") or data.get("spineVersion") or data.get("runtimeVersion"))
    family = spine_version_family(version)
    if requested and family and family != requested:
        return None
    exact_versions = tuple(
        dict.fromkeys(
            normalized
            for item in (data.get("compatibleExactVersions") or data.get("compatibleVersions") or ())
            for normalized in [normalize_spine_version(item)]
            if normalized
        )
    )
    if requested_version:
        if exact_versions and requested_version not in exact_versions:
            return None
        # 3.8.75 is deliberately distributed as a dedicated historical build;
        # never silently substitute the current 3.8.99 runtime for it.
        if requested_version == "3.8.75" and version and version != requested_version:
            return None
    # Official spine-ts 3.8 backend builds are self-contained: the WebGL
    # artifact includes the core classes.  A manifest may still record the
    # separate core artifact for provenance, but callers must not load it a
    # second time before the combined WebGL build.
    combined = bool(data.get("combined") or data.get("webglIncludesCore"))
    core = _manifest_script(path.parent, data.get("core") or data.get("spineCore"))
    webgl = _manifest_script(path.parent, data.get("webgl") or data.get("spineWebgl") or data.get("webGL"))
    if webgl is None:
        scripts = list(_iter_runtime_files(path.parent, names=SPINE_SCRIPT_NAMES))
        webgl = _pick_script(scripts, "spine-webgl", requested or family, requested_version)
    if webgl is None:
        return None
    if core is None and not combined:
        core = _pick_script(
            [webgl.parent / "spine-core.js", *_iter_runtime_files(webgl.parent, suffixes=(".js",))],
            "spine-core",
            requested or family,
            requested_version,
        )
    return SpineRuntime(
        root_dir=path.parent.resolve(),
        webgl_script=webgl,
        core_script=core,
        version=version or _infer_runtime_version(webgl) or family or requested,
        source_manifest=path.resolve(),
        combined_webgl=combined or core is None,
        compatible_exact_versions=exact_versions,
        api_style=_runtime_api_style(
            data.get("api") or data.get("apiStyle") or data.get("namespace"),
            version or _infer_runtime_version(webgl) or family or requested,
        ),
    )


def _manifest_script(root: Path, raw: Any) -> Path | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        path = (root / raw).resolve()
        return path if path.is_file() and path.suffix.lower() == ".js" else None
    except (OSError, RuntimeError):
        return None


def _pick_script(
    scripts: Sequence[Path],
    stem: str,
    family: str | None,
    exact_version: str | None = None,
) -> Path | None:
    candidates: list[Path] = []
    for path in scripts:
        try:
            if path.is_file() and path.stem.lower().startswith(stem):
                candidates.append(path.resolve())
        except (OSError, RuntimeError):
            continue
    if family:
        matching = [path for path in candidates if _infer_runtime_family(path) == family]
        if matching:
            candidates = matching
        elif candidates and any(_infer_runtime_family(path) for path in candidates):
            return None
    if exact_version:
        exact = [path for path in candidates if _infer_runtime_version(path) == exact_version]
        if exact:
            candidates = exact
        elif exact_version == "3.8.75":
            return None
    return sorted(candidates, key=lambda path: (len(path.parts), str(path).lower()))[0] if candidates else None


def _infer_runtime_version(path: Path) -> str | None:
    for value in (path.name, *path.parts[::-1]):
        version = normalize_spine_version(value)
        if version:
            return version
    return None


def _infer_runtime_family(path: Path) -> str | None:
    return spine_version_family(_infer_runtime_version(path))


def _runtime_api_style(value: Any, version_or_family: Any) -> str:
    """Return the browser API layout for a validated spine-ts build.

    The 3.8 repository keeps WebGL classes under ``spine.webgl``.  Starting
    with the 4.0 runtime, the IIFE build exports those classes directly on
    ``spine``.  A manifest may state the layout explicitly; otherwise the
    major/minor family is sufficient for official builds.
    """

    value_text = str(value or "").strip().casefold().replace("-", "_")
    if value_text in {"flat", "top_level", "toplevel", "modern"}:
        return "flat"
    if value_text in {"legacy", "nested", "webgl_namespace", "webgl"}:
        return "legacy"
    family = spine_version_family(version_or_family)
    if family:
        major = int(family.split(".", 1)[0])
        if major >= 4:
            return "flat"
    return "legacy"


def _relative_url(path: Path | None, root: Path, base_url: str) -> str | None:
    if path is None:
        return None
    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None
    return str(base_url).rstrip("/") + "/" + "/".join(_quote_url_part(item) for item in relative.split("/"))


def _quote_url_part(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="._-()[]~")


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
    "SpineAssetNotFoundError",
    "SpinePoseExportError",
    "SpinePoseExportReport",
    "SpinePoseLayer",
    "SpinePreviewAsset",
    "SpinePreviewError",
    "SpinePreviewImportResult",
    "SpinePreviewPlan",
    "SpineRuntime",
    "SpineRuntimeUnavailableError",
    "SpineRuntimeUnsupportedError",
    "SpineVersionMismatchError",
    "build_spine_web_manifest",
    "discover_spine_runtime",
    "export_spine_pose_psd",
    "find_spine_asset",
    "load_spine_asset",
    "make_spine_preview_plan",
    "normalize_spine_version",
    "parse_spine_version",
    "prepare_spine_preview_import",
    "read_spine_version",
    "spine_version_family",
]
