from __future__ import annotations

from pathlib import Path

from app.core.assetstudio_cli import AssetStudioCLI, AssetStudioCLIError
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

        if mode == ExtractMode.LIVE2D and not _has_live2d_model(result.exported_files):
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

        return ExtractItemResult(
            source=source,
            source_type=ExtractSourceType.UNITY if source.is_file() else ExtractSourceType.FOLDER,
            success=True,
            output_dir=result.output_dir,
            exported_count=result.exported_count,
            message=f"AssetStudio exported {result.exported_count} file(s) from {source.name}",
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
