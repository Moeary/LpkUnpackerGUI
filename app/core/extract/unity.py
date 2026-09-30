from __future__ import annotations

import json
import tempfile
from pathlib import Path

from app.core.assetstudio_cli import (
    AssetStudioCLI,
    AssetStudioCLIError,
    validate_unityfs_bundle,
)
from app.core.cubism_core import CubismCore, CubismCoreError
from app.core.extract.models import ExtractItemResult, ExtractMode, ExtractSourceType
from app.core.spine_preview import read_spine_version


def extract_unity(
    input_path: str | Path,
    output_dir: str | Path,
    mode: ExtractMode,
    log=None,
    *,
    include_spine: bool = False,
) -> ExtractItemResult:
    source = Path(input_path)
    target = Path(output_dir)

    try:
        # AssetStudioModCLI may return zero for a short UnityFS bundle and
        # still emit partial MOC/model files.  Fail before creating output so
        # an invalid native model cannot be selected as a successful export.
        input_warning = validate_unityfs_bundle(source)
        if input_warning and log:
            log("WARNING", input_warning)
        cli = AssetStudioCLI()
        if log:
            log("INFO", f"AssetStudio CLI: {cli.executable}")

        spine_export = False
        live2d_export = False
        spine_roots: list[Path] = []
        if mode == ExtractMode.LIVE2D:
            result = cli.export_live2d(source, target)
            live2d_export = _has_live2d_model(result.exported_files)
            # AssetStudio's Live2D mode intentionally rejects Spine Unity
            # packages.  When no Cubism model was exported, ask the same CLI
            # for TextAsset + texture data and validate it as Spine before
            # treating the extraction as successful.  This keeps ordinary
            # non-Live2D sources as explicit failures.
            if include_spine:
                export_spine = getattr(cli, "export_spine", None)
                if callable(export_spine):
                    try:
                        # AssetStudio's output root can contain older models.
                        # A fresh workspace scopes recognition and conversion
                        # to this invocation, including repeated bundle imports.
                        spine_parent = target / "SpineOutput"
                        spine_parent.mkdir(parents=True, exist_ok=True)
                        spine_target = Path(tempfile.mkdtemp(
                            prefix=f"{source.stem}_", dir=spine_parent
                        ))
                        spine_result = export_spine(source, spine_target)
                        spine_files = _materialize_spine_text_assets(
                            spine_result.output_dir,
                            spine_result.exported_files,
                        )
                        if _has_spine_asset(
                            spine_result.output_dir,
                            list(spine_result.exported_files) + spine_files,
                        ):
                            if live2d_export:
                                # Keep both exports below the current item
                                # root so formal batch conversion can discover
                                # the Spine child without discarding Cubism.
                                result.exported_files.extend(
                                    list(spine_result.exported_files) + spine_files
                                )
                            else:
                                result = spine_result
                                result.exported_files.extend(spine_files)
                            spine_export = True
                            spine_roots.append(Path(spine_result.output_dir).resolve())
                    except AssetStudioCLIError as exc:
                        if log:
                            log("WARNING", f"AssetStudio Spine export was not available: {exc}")
        elif mode == ExtractMode.TEXTURES:
            result = cli.export_textures(source, target)
        else:
            return ExtractItemResult(
                source=source,
                source_type=ExtractSourceType.UNITY,
                success=False,
                output_dir=target,
                error=f"Unity source does not support mode: {mode.value}",
            )

        if log:
            _emit_cli_output(result.stdout, log, "INFO")
            _emit_cli_output(result.stderr, log, "WARNING")

        validation_warnings: list[str] = []
        if mode == ExtractMode.LIVE2D:
            live2d_export = live2d_export or _has_live2d_model(result.exported_files)
            if not live2d_export and not spine_export:
                return ExtractItemResult(
                    source=source,
                    source_type=ExtractSourceType.UNITY if source.is_file() else ExtractSourceType.FOLDER,
                    success=False,
                    output_dir=result.output_dir,
                    exported_count=0,
                    error=(
                        "No Live2D model was exported. This Unity source is probably not a Live2D "
                        "package, so export was rejected."
                    ),
                )
            if live2d_export:
                validation_warnings = validate_live2d_export(
                    target,
                    result.exported_files,
                )
                for warning in validation_warnings:
                    if log:
                        log("WARNING", warning)

        export_kind = (
            "Live2D + Spine" if live2d_export and spine_export
            else "Spine" if spine_export
            else "Live2D" if mode == ExtractMode.LIVE2D
            else "texture"
        )
        message = f"AssetStudio exported {result.exported_count} {export_kind} file(s) from {source.name}"
        if input_warning:
            message += " " + input_warning
        if validation_warnings:
            message += " " + " ".join(validation_warnings)

        return ExtractItemResult(
            source=source,
            source_type=ExtractSourceType.UNITY if source.is_file() else ExtractSourceType.FOLDER,
            success=True,
            output_dir=result.output_dir,
            exported_count=result.exported_count,
            message=message,
            extracted_dirs=spine_roots,
        )
    except AssetStudioCLIError as exc:
        return ExtractItemResult(
            source=source,
            source_type=ExtractSourceType.UNITY if source.is_file() else ExtractSourceType.FOLDER,
            success=False,
            output_dir=target,
            error=str(exc),
        )
    except Exception as exc:
        return ExtractItemResult(
            source=source,
            source_type=ExtractSourceType.UNITY if source.is_file() else ExtractSourceType.FOLDER,
            success=False,
            output_dir=target,
            error=str(exc),
        )


def _emit_cli_output(text: str, log, level: str) -> None:
    for line in text.splitlines():
        line = line.strip()
        if line:
            log(level, line)


def _has_live2d_model(paths: list[Path]) -> bool:
    for path in paths:
        name = path.name.lower()
        suffix = path.suffix.lower()
        if name.endswith(".model3.json") or name.endswith("model.json"):
            return True
        if suffix in {".moc", ".moc3"}:
            return True
    return False


def _materialize_spine_text_assets(
    output_dir: str | Path,
    exported_files: list[Path] | None,
) -> list[Path]:
    """Give JSON/atlas TextAssets resolver-friendly suffixes.

    AssetStudio commonly writes a Unity ``TextAsset`` as ``.txt`` (or
    ``.bytes``), even when its contents are a Spine JSON skeleton or atlas.
    Keep the exported file untouched and create a sibling with the semantic
    suffix expected by the Spine loader.  Only content that passes a strict
    JSON/atlas shape check is copied.
    """

    root = Path(output_dir).resolve()
    candidates = list(exported_files or [])
    materialized: list[Path] = []
    for raw_path in candidates:
        path = Path(raw_path).resolve()
        if not path.is_file() or path.suffix.lower() not in {".txt", ".bytes"}:
            continue
        try:
            if path.stat().st_size > 64 * 1024 * 1024:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = None
        stripped = text.lstrip() if text is not None else ""
        target: Path | None = None
        if text is not None and stripped.startswith("{"):
            try:
                value = json.loads(stripped)
            except (TypeError, ValueError):
                value = None
            if _looks_like_spine_skeleton_json(value):
                target = path.with_suffix(".json")
        elif text is not None and _looks_like_spine_atlas_text(text):
            target = path.with_suffix(".atlas")
        else:
            # Spine binary TextAssets are often exported as ``.bytes`` and
            # cannot be decoded as UTF-8.  The loader's version marker is the
            # conservative probe available at this boundary; only a known
            # converter family is materialized as ``.skel``.
            version = read_spine_version(path)
            family = version.rsplit(".", 1)[0] if version and version.count(".") >= 2 else ""
            if family in {"3.5", "3.6", "3.7", "3.8", "4.0", "4.1", "4.2"}:
                target = path.with_suffix(".skel")
        if target is None or target == path:
            continue
        # Unity commonly preserves the semantic suffix before .txt/.bytes.
        if path.with_suffix("").suffix.casefold() == target.suffix.casefold():
            target = path.with_suffix("")
        try:
            target.relative_to(root)
        except ValueError:
            continue
        if target.exists():
            continue
        try:
            if target.suffix.casefold() == ".skel":
                target.write_bytes(raw)
            else:
                target.write_text(text or "", encoding="utf-8")
        except OSError:
            continue
        materialized.append(target)
    return materialized


def _looks_like_spine_skeleton_json(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    skeleton = value.get("skeleton")
    return (
        isinstance(skeleton, dict)
        and isinstance(skeleton.get("spine"), str)
        and isinstance(value.get("bones"), list)
        and ("slots" in value or "skins" in value or "animations" in value)
    )


def _looks_like_spine_atlas_text(text: str) -> bool:
    lines = [line.strip().casefold() for line in str(text or "").splitlines()]
    if not lines:
        return False
    # Atlas pages have a size declaration and at least one region/property;
    # requiring both avoids treating arbitrary Unity text as an atlas.
    return any(line.startswith("size:") for line in lines) and any(
        line.startswith(prefix)
        for line in lines
        for prefix in ("format:", "filter:", "repeat:", "xy:", "bounds:")
    )


def _has_spine_asset(output_dir: str | Path, exported_files: list[Path] | None) -> bool:
    """Return true only for exported skeleton data, never old output files."""

    candidates = [Path(path).resolve() for path in (exported_files or [])]
    for path in candidates:
        if not path.is_file():
            continue
        name = path.name.casefold()
        if name.endswith(".skel") or name.endswith(".skel.bytes"):
            if read_spine_version(path):
                return True
        if path.suffix.casefold() == ".json":
            try:
                value = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, ValueError):
                continue
            if _looks_like_spine_skeleton_json(value):
                return True
    return False


def validate_live2d_export(
    output_dir: str | Path,
    exported_files: list[Path] | None = None,
) -> list[str]:
    """Validate an AssetStudio Live2D export before native preview loading.

    The CLI can exit successfully after exporting only a MOC or a model JSON
    with unresolved references.  Such a directory is not a usable Live2D
    package and must not be handed to ``LoadModelJson``.  Native consistency is
    checked when Cubism Core is available; if it is not installed, the result
    carries an explicit warning rather than pretending that check passed.
    """

    root = Path(output_dir).resolve()
    model_paths = _find_exported_model_jsons(root, exported_files)
    if not model_paths:
        raise AssetStudioCLIError(
            f"AssetStudio exported Live2D files but no model3 JSON was found under: {root}"
        )

    moc_paths: list[Path] = []
    texture_error: AssetStudioCLIError | None = None
    for model_path in model_paths:
        try:
            with model_path.open("r", encoding="utf-8-sig") as stream:
                data = json.load(stream)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AssetStudioCLIError(
                f"Live2D model JSON is unreadable: {model_path}: {exc}"
            ) from exc

        refs = data.get("FileReferences") if isinstance(data, dict) else None
        if not isinstance(refs, dict):
            raise AssetStudioCLIError(
                f"Live2D model JSON has no FileReferences object: {model_path}"
            )

        moc_value = refs.get("Moc")
        if not isinstance(moc_value, str) or not moc_value:
            raise AssetStudioCLIError(
                f"Live2D model JSON has no Moc reference: {model_path}"
            )
        moc_path = _resolve_export_reference(model_path.parent, moc_value, root, "Moc")
        if moc_path.suffix.lower() not in {".moc3", ".moc"}:
            raise AssetStudioCLIError(
                f"Live2D model references a non-MOC file: {model_path}: {moc_value}"
            )
        if not moc_path.is_file():
            raise AssetStudioCLIError(
                f"Live2D model references a missing MOC file: {model_path}: {moc_value}"
            )
        moc_paths.append(moc_path)

        textures = refs.get("Textures")
        if not isinstance(textures, list) or not textures:
            texture_error = texture_error or AssetStudioCLIError(
                "Live2D export has no texture references in "
                f"{model_path.name}; the Unity source/export is incomplete."
            )
            continue
        for texture_value in textures:
            if not isinstance(texture_value, str) or not texture_value:
                texture_error = texture_error or AssetStudioCLIError(
                    f"Live2D model contains an invalid texture reference: {model_path}"
                )
                continue
            texture_path = _resolve_export_reference(
                model_path.parent,
                texture_value,
                root,
                "texture",
            )
            if not texture_path.is_file():
                texture_error = texture_error or AssetStudioCLIError(
                    f"Live2D model references a missing texture: {model_path}: {texture_value}"
                )

    unique_mocs = list(dict.fromkeys(moc_paths))
    warnings: list[str] = []
    native_errors: list[str] = []
    try:
        core = CubismCore()
    except (CubismCoreError, OSError, AttributeError) as exc:
        warnings.append(f"Cubism Core consistency validation was skipped: {exc}")
    else:
        for moc_path in unique_mocs:
            try:
                core.validate_moc(moc_path)
            except CubismCoreError as exc:
                native_errors.append(
                    "AssetStudio exported an invalid MOC3; native consistency "
                    f"validation failed for {moc_path}: {exc}"
                )
    if native_errors:
        native_message = "; ".join(native_errors)
        if texture_error:
            native_message += f"; {texture_error}"
        raise AssetStudioCLIError(native_message)
    if texture_error:
        raise texture_error
    return warnings


def _find_exported_model_jsons(
    root: Path,
    exported_files: list[Path] | None,
) -> list[Path]:
    candidates = []
    for path in exported_files or []:
        candidate = Path(path).resolve()
        if candidate.is_file() and _is_model_json(candidate):
            candidates.append(candidate)
    if not candidates:
        candidates = [path.resolve() for path in root.rglob("*.json") if _is_model_json(path)]
    return sorted(set(candidates), key=lambda path: str(path).casefold())


def _is_model_json(path: Path) -> bool:
    name = path.name.casefold()
    return name.endswith(".model3.json") or name == "model.json"


def _resolve_export_reference(
    base_dir: Path,
    raw_value: str,
    root: Path,
    label: str,
) -> Path:
    candidate = (base_dir / raw_value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise AssetStudioCLIError(
            f"Live2D {label} reference escapes the export directory: {raw_value}"
        ) from exc
    return candidate
