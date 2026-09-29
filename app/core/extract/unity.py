from __future__ import annotations

import json
from pathlib import Path

from app.core.assetstudio_cli import (
    AssetStudioCLI,
    AssetStudioCLIError,
    validate_unityfs_bundle,
)
from app.core.cubism_core import CubismCore, CubismCoreError
from app.core.extract.models import ExtractItemResult, ExtractMode, ExtractSourceType


def extract_unity(
    input_path: str | Path,
    output_dir: str | Path,
    mode: ExtractMode,
    log=None,
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

        if mode == ExtractMode.LIVE2D:
            result = cli.export_live2d(source, target)
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
            if not _has_live2d_model(result.exported_files):
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
            validation_warnings = validate_live2d_export(
                result.output_dir,
                result.exported_files,
            )
            for warning in validation_warnings:
                if log:
                    log("WARNING", warning)

        message = f"AssetStudio exported {result.exported_count} file(s) from {source.name}"
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
