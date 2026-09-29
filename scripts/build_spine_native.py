"""Build one official Spine native bridge from a verified source directory.

Example (PowerShell)::

    pixi run python scripts/build_spine_native.py --family 3.8 \
      --source-dir D:/cache/spine-runtimes-375/spine-cpp \
      --output-dir runtime/tools/spine/native/3.8 \
      --manifest runtime/tools/spine/3.8.75/spine_runtime.json

The source archive is intentionally supplied by the toolchain/downloader so
its SHA-256 and exact upstream commit can be checked before compilation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Running a script directly sets sys.path[0] to ``scripts``.  Add the project
# root explicitly so the same entry point works from a source checkout and
# from the pixi task runner.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.spine_embed import build_native_runtime


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", required=True, choices=("3.8", "4.0"))
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    metadata = json.loads(args.manifest.read_text(encoding="utf-8")) if args.manifest else {"runtimeFamily": args.family}
    metadata.setdefault("runtimeFamily", args.family)
    build_native_runtime(args.source_dir, args.output_dir, metadata, progress=print)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
