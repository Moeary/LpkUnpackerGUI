import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.assetstudio_cli import AssetStudioCLI
from app.paths import ensure_runtime_dirs, runtime_output_dir


def main():
    ensure_runtime_dirs()
    parser = argparse.ArgumentParser(description="Extract Unity assets through AssetStudio CLI.")
    parser.add_argument("input", nargs="+", help="Input files or folders")
    parser.add_argument("-o", "--output", default=str(runtime_output_dir("unity")), help="Output directory")
    parser.add_argument(
        "-m",
        "--mode",
        choices=("textures", "live2d"),
        default="textures",
        help="Export mode",
    )
    args = parser.parse_args()

    output_dir = os.path.abspath(args.output)
    os.makedirs(output_dir, exist_ok=True)

    cli = AssetStudioCLI()
    print(f"AssetStudio CLI: {cli.executable}")

    total = 0
    for item in args.input:
        input_path = os.path.abspath(item)
        if args.mode == "live2d":
            result = cli.export_live2d(input_path, output_dir)
        else:
            result = cli.export_textures(input_path, output_dir)
        total += result.exported_count
        print(f"{input_path}: exported {result.exported_count} file(s)")

    print("\nExtraction complete")
    print(f"Total exported: {total}")
    print(f"Output saved to: {output_dir}")


if __name__ == "__main__":
    main()
