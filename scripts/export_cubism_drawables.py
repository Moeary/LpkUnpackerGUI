import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.cubism_core import CubismCoreError, export_drawables_sidecar


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export Live2D Cubism drawable mesh metadata with Live2DCubismCore.dll."
    )
    parser.add_argument("source", help="model3.json, .moc3, or a model folder")
    parser.add_argument(
        "-o",
        "--output-dir",
        default=None,
        help="Directory for the generated <model>.drawables.json. Defaults to the model folder.",
    )
    parser.add_argument(
        "--dll",
        default=None,
        help="Explicit path to Live2DCubismCore.dll. Otherwise LPK_CUBISM_CORE_DLL/LPK_CUBISM_CORE_DIR or app/tools/CubismCore is used.",
    )
    args = parser.parse_args()

    try:
        path = export_drawables_sidecar(args.source, args.output_dir, args.dll, _print_progress)
    except CubismCoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(path)
    return 0


def _print_progress(value: int, message: str) -> None:
    if message:
        print(f"[{value:3d}%] {message}")


if __name__ == "__main__":
    raise SystemExit(main())
