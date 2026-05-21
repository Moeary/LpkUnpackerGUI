from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIRS = (ROOT / "app", ROOT / "scripts")


def main() -> int:
    for source_dir in SOURCE_DIRS:
        for path in sorted(source_dir.rglob("*.py")):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
