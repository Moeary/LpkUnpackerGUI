"""Prepare Spine assets for the unified native preview.

The formal extraction/export conversion setting deliberately does not control
this module.  Preview conversion is a separate, disposable-cache operation:
the source files are hashed, a converted copy is built with the bundled native
converter, and the resulting cache is reused only after every recorded output
file has passed an integrity check.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import threading
from functools import wraps
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from app.core.spine_converter import (
    SpineConversionError,
    convert_spine,
    discover_native_converter,
)
from app.core.spine_preview import (
    SpinePreviewAsset,
    SpinePreviewPlan,
    load_spine_asset,
    make_spine_preview_plan,
    normalize_spine_version,
)


UNIFIED_PREVIEW_VERSION = "3.8.75"
_CACHE_SCHEMA_VERSION = 1
_MANIFEST_NAME = "spine_preview_conversion_report.json"
_CACHE_LOCK_GUARD = threading.Lock()
_CACHE_LOCKS: dict[str, threading.RLock] = {}


class SpinePreviewConversionError(RuntimeError):
    """A unified-preview conversion failed and must be shown to the user."""


@dataclass(frozen=True)
class SpinePreviewConversionInfo:
    """Inspectable source/target information for one preview preparation."""

    source_version: str | None
    target_version: str
    converted: bool = False
    cache_dir: Path | None = None
    report_path: Path | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreparedSpinePreview:
    """The asset and plan after optional unified-preview preparation."""

    asset: SpinePreviewAsset
    plan: SpinePreviewPlan
    conversion: SpinePreviewConversionInfo


def prepare_spine_preview(
    asset: SpinePreviewAsset,
    cache_root: str | Path,
    runtime_root: str | Path | None = None,
    *,
    unify_version: bool = False,
) -> PreparedSpinePreview:
    """Prepare *asset* and build a native preview plan.

    ``unify_version`` is intentionally explicit.  The low-level preview plan
    remains a pure runtime/version decision and never reads application
    settings.  When enabled, every known non-3.8.75 skeleton is converted to a
    content-addressed copy before planning.  Conversion errors are raised; the
    original version is never silently retried.
    """

    source_version = normalize_spine_version(asset.spine_version)
    if not unify_version or not asset.has_skeleton:
        target = source_version or asset.spine_version
        info = SpinePreviewConversionInfo(
            source_version=source_version,
            target_version=target or UNIFIED_PREVIEW_VERSION,
        )
        return PreparedSpinePreview(
            asset=asset,
            plan=make_spine_preview_plan(asset, runtime_root),
            conversion=info,
        )

    if source_version == UNIFIED_PREVIEW_VERSION:
        info = SpinePreviewConversionInfo(
            source_version=source_version,
            target_version=UNIFIED_PREVIEW_VERSION,
        )
        return PreparedSpinePreview(
            asset=asset,
            plan=make_spine_preview_plan(asset, runtime_root),
            conversion=info,
        )

    if not source_version:
        raise SpinePreviewConversionError(
            "无法确认 Spine 源骨骼版本，无法安全统一转换到 3.8.75；"
            "请关闭统一预览或提供带版本标记的骨骼。"
        )

    source = asset.model_config_path or asset.skeleton_path
    if source is None or not source.is_file():
        raise SpinePreviewConversionError(
            "Spine 统一预览需要可读的 skeleton 或 model 配置文件。"
        )

    cache_root_path = Path(cache_root).expanduser().resolve()
    cache_root_path.mkdir(parents=True, exist_ok=True)
    try:
        cache_dir, report_path, converted_skeleton = _get_or_create_cache(
            asset,
            source,
            cache_root_path,
            source_version,
        )
        converted_asset = load_spine_asset(converted_skeleton)
        # The converter validates this itself, but retain a second boundary at
        # the preview layer so a malformed or hand-edited cache is never loaded
        # as a 3.8.75 asset.
        actual_version = normalize_spine_version(converted_asset.spine_version)
        if actual_version != UNIFIED_PREVIEW_VERSION:
            raise SpinePreviewConversionError(
                f"统一预览缓存版本为 {actual_version or 'unknown'}，"
                f"预期 {UNIFIED_PREVIEW_VERSION}。"
            )
        warning = (
            f"Spine 源版本 {source_version} 已转换为 {UNIFIED_PREVIEW_VERSION} "
            "用于统一预览；动画、约束和曲线效果可能与源文件不同。"
        )
        info = SpinePreviewConversionInfo(
            source_version=source_version,
            target_version=UNIFIED_PREVIEW_VERSION,
            converted=True,
            cache_dir=cache_dir,
            report_path=report_path,
            warnings=(warning,),
        )
        plan = make_spine_preview_plan(converted_asset, runtime_root)
        if warning not in plan.warnings:
            plan = _plan_with_warning(plan, warning)
        return PreparedSpinePreview(
            asset=converted_asset,
            plan=plan,
            conversion=info,
        )
    except SpinePreviewConversionError:
        raise
    except Exception as exc:
        raise SpinePreviewConversionError(
            f"Spine {source_version}→{UNIFIED_PREVIEW_VERSION} 统一预览转换失败：{exc}"
        ) from exc


def _plan_with_warning(plan: SpinePreviewPlan, warning: str) -> SpinePreviewPlan:
    """Add a conversion warning without changing the low-level planner."""

    return SpinePreviewPlan(
        mode=plan.mode,
        asset=plan.asset,
        runtime=plan.runtime,
        reason=plan.reason,
        warnings=tuple(plan.warnings) + (warning,),
    )


def _cache_operation_lock(function):
    """Serialize builds for one content key inside this process."""

    @wraps(function)
    def guarded(asset, source, cache_root, source_version):
        inputs = _input_records(asset)
        converter_record = _converter_record()
        cache_key = _cache_key(inputs, converter_record, source_version)
        with _CACHE_LOCK_GUARD:
            lock = _CACHE_LOCKS.setdefault(cache_key, threading.RLock())
        with lock:
            return function(asset, source, cache_root, source_version)

    return guarded


@_cache_operation_lock
def _get_or_create_cache(
    asset: SpinePreviewAsset,
    source: Path,
    cache_root: Path,
    source_version: str,
) -> tuple[Path, Path, Path]:
    inputs = _input_records(asset)
    converter_record = _converter_record()
    cache_key = _cache_key(inputs, converter_record, source_version)
    cache_dir = (cache_root / f"spine_preview_{cache_key}").resolve()
    _ensure_under(cache_dir, cache_root)
    report_path = cache_dir / _MANIFEST_NAME

    if cache_dir.is_dir():
        cached = _validate_cache(
            cache_dir,
            report_path,
            cache_key,
            inputs,
            converter_record,
        )
        if cached is not None:
            return cache_dir, report_path, cached
        # This path is derived solely from our cache root and content key.  A
        # corrupt cache is disposable; source files are never beneath it.
        shutil.rmtree(cache_dir, ignore_errors=True)
    elif cache_dir.exists():
        cache_dir.unlink()

    staging = Path(tempfile.mkdtemp(prefix=".spine_preview_build_", dir=cache_root))
    published = False
    completed = False
    try:
        try:
            converted = convert_spine(
                source,
                staging,
                target_version=UNIFIED_PREVIEW_VERSION,
                output_format="json",
                create_project=False,
            )
        except Exception as exc:
            raise SpinePreviewConversionError(
                f"Spine {source_version}→{UNIFIED_PREVIEW_VERSION} 统一预览转换失败：{exc}"
            ) from exc

        output_root = converted.output_dir.resolve()
        output_skeleton = converted.skeleton_path.resolve()
        try:
            skeleton_relative = output_skeleton.relative_to(output_root)
        except ValueError as exc:
            raise SpinePreviewConversionError(
                "Spine converter output escaped its own staging directory。"
            ) from exc
        if not output_skeleton.is_file() or output_skeleton.stat().st_size <= 0:
            raise SpinePreviewConversionError("Spine converter did not produce a skeleton cache file。")

        if cache_dir.exists():
            # A concurrent creator may have completed the same key.  Trust it
            # only after the same full validation used on ordinary cache hits.
            cached = _validate_cache(
                cache_dir,
                cache_dir / _MANIFEST_NAME,
                cache_key,
                inputs,
                converter_record,
            )
            if cached is not None:
                completed = True
                return cache_dir, cache_dir / _MANIFEST_NAME, cached
            shutil.rmtree(cache_dir, ignore_errors=True)

        shutil.move(str(output_root), str(cache_dir))
        published = True
        final_skeleton = (cache_dir / skeleton_relative).resolve()
        _ensure_under(final_skeleton, cache_dir)
        if not final_skeleton.is_file():
            raise SpinePreviewConversionError("Spine converter cache skeleton disappeared after publish。")

        output_files = _output_records(cache_dir, skip={_MANIFEST_NAME})
        report = {
            "schema": _CACHE_SCHEMA_VERSION,
            "cache_key": cache_key,
            "source": str(source.resolve()),
            "source_root": str(asset.root_dir.resolve()),
            "source_version": source_version,
            "target_version": UNIFIED_PREVIEW_VERSION,
            "converter": converter_record,
            "inputs": inputs,
            "output": {
                "skeleton": str(skeleton_relative).replace("\\", "/"),
                "files": output_files,
                "native_report": "spine_conversion_report.json",
            },
            "warnings": list(converted.warnings),
            "create_project": False,
        }
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        validated = _validate_cache(
            cache_dir,
            report_path,
            cache_key,
            inputs,
            converter_record,
        )
        if validated is None:
            raise SpinePreviewConversionError("新建的 Spine 统一预览缓存校验失败。")
        completed = True
        return cache_dir, report_path, validated
    except SpinePreviewConversionError:
        raise
    except Exception as exc:
        raise SpinePreviewConversionError(f"无法建立 Spine 统一预览缓存：{exc}") from exc
    finally:
        # ``staging`` is always our own temporary directory.  On success the
        # converted child has moved to the cache, leaving only staging itself.
        shutil.rmtree(staging, ignore_errors=True)
        if not completed and published and cache_dir.exists():
            # No partially published cache may be reused after a failed build.
            shutil.rmtree(cache_dir, ignore_errors=True)


def _input_records(asset: SpinePreviewAsset) -> list[dict[str, Any]]:
    paths: list[tuple[str, Path]] = []
    if asset.model_config_path is not None:
        paths.append(("model_config", asset.model_config_path))
    if asset.skeleton_path is not None:
        paths.append(("skeleton", asset.skeleton_path))
    paths.extend(("atlas", path) for path in asset.atlas_paths)
    paths.extend(("texture", path) for path in asset.texture_paths)
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for role, path in paths:
        resolved = Path(path).expanduser().resolve()
        key = (role, str(resolved).casefold())
        if key in seen:
            continue
        seen.add(key)
        if not resolved.is_file():
            raise SpinePreviewConversionError(f"Spine {role} 文件不存在：{resolved}")
        try:
            relative = resolved.relative_to(asset.root_dir.resolve())
            label = str(relative).replace("\\", "/")
        except ValueError:
            label = resolved.name
        digest, size = _file_digest(resolved)
        records.append(
            {
                "role": role,
                "path": str(resolved),
                "relative": label,
                "sha256": digest,
                "size": size,
            }
        )
    records.sort(key=lambda item: (str(item["role"]), str(item["relative"]).casefold()))
    if not any(item["role"] == "skeleton" for item in records):
        raise SpinePreviewConversionError("Spine 统一预览未找到 skeleton 文件。")
    return records


def _converter_record() -> dict[str, Any]:
    try:
        path = discover_native_converter()
    except Exception:
        path = None
    if path is None or not Path(path).is_file():
        return {"path": "bundled-native-converter", "sha256": None, "size": None}
    resolved = Path(path).resolve()
    digest, size = _file_digest(resolved)
    return {"path": str(resolved), "sha256": digest, "size": size}


def _cache_key(
    inputs: Iterable[dict[str, Any]],
    converter: dict[str, Any],
    source_version: str,
) -> str:
    value = {
        "schema": _CACHE_SCHEMA_VERSION,
        "source_version": source_version,
        "target_version": UNIFIED_PREVIEW_VERSION,
        "inputs": _input_identity(inputs),
        "converter": converter,
    }
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _input_identity(inputs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return cache-stable input records without extraction temp paths."""

    return [
        {
            "role": str(item.get("role") or ""),
            "relative": str(item.get("relative") or ""),
            "sha256": str(item.get("sha256") or ""),
            "size": int(item.get("size") or 0),
        }
        for item in inputs
    ]


def _validate_cache(
    cache_dir: Path,
    report_path: Path,
    cache_key: str,
    inputs: list[dict[str, Any]],
    converter: dict[str, Any],
) -> Path | None:
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            return None
        if report.get("schema") != _CACHE_SCHEMA_VERSION:
            return None
        if report.get("cache_key") != cache_key:
            return None
        if report.get("source_version") is None or report.get("target_version") != UNIFIED_PREVIEW_VERSION:
            return None
        raw_inputs = report.get("inputs")
        if not isinstance(raw_inputs, list):
            return None
        if _input_identity(raw_inputs) != _input_identity(inputs):
            return None
        if report.get("converter") != converter:
            return None
        output = report.get("output")
        if not isinstance(output, dict):
            return None
        raw_skeleton = output.get("skeleton")
        if not isinstance(raw_skeleton, str) or not raw_skeleton:
            return None
        skeleton = (cache_dir / raw_skeleton).resolve()
        _ensure_under(skeleton, cache_dir)
        if not skeleton.is_file():
            return None
        files = output.get("files")
        if not isinstance(files, list) or not files:
            return None
        for record in files:
            if not isinstance(record, dict):
                return None
            raw_path = record.get("path")
            expected_hash = record.get("sha256")
            expected_size = record.get("size")
            if not isinstance(raw_path, str) or not isinstance(expected_hash, str):
                return None
            path = (cache_dir / raw_path).resolve()
            _ensure_under(path, cache_dir)
            if not path.is_file() or path.stat().st_size != int(expected_size):
                return None
            digest, _size = _file_digest(path)
            if digest != expected_hash:
                return None
        actual_version = normalize_spine_version(load_spine_asset(skeleton).spine_version)
        if actual_version != UNIFIED_PREVIEW_VERSION:
            return None
        return skeleton
    except (OSError, ValueError, TypeError, RuntimeError, SpineConversionError):
        return None


def _output_records(cache_dir: Path, *, skip: set[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(cache_dir.rglob("*"), key=lambda item: str(item).casefold()):
        if not path.is_file() or path.name in skip:
            continue
        relative = path.relative_to(cache_dir)
        digest, size = _file_digest(path)
        records.append(
            {
                "path": str(relative).replace("\\", "/"),
                "sha256": digest,
                "size": size,
            }
        )
    return records


def _file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _ensure_under(path: Path, root: Path) -> None:
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise SpinePreviewConversionError(
            f"Spine preview cache path escapes its managed root: {path}"
        ) from exc


__all__ = [
    "PreparedSpinePreview",
    "SpinePreviewConversionError",
    "SpinePreviewConversionInfo",
    "UNIFIED_PREVIEW_VERSION",
    "prepare_spine_preview",
]
