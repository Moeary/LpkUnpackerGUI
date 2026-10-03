"""Validate native build inputs and the files bundled into a release."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LIVE2D_FILES = ("_v3cpp.pyd", "LpkPreviewCubismCore.dll", "live2d_native.json", "SOURCE_METADATA.json",
                "THIRD_PARTY_NOTICES.md", "LICENSE.live2d-py", "LICENSE.CubismFramework.md",
                "LICENSE.CubismCore.md", "FrameworkShaders/FragShaderSrc.frag")


def source_artifacts(root: Path = ROOT) -> dict[str, Path]:
    """Return every native source artifact required by a release build."""

    converter_root = root / "runtime" / "tools" / "SpineSkeletonDataConverter"
    third_party_root = root / "third_party" / "wang606_spine_converter"
    result = {
        "converter DLL": converter_root / "lpk_spine_converter.dll",
        "converter LICENSE": third_party_root / "LICENSE",
        "converter SOURCE_METADATA": third_party_root / "SOURCE_METADATA.json",
        "Spine 3.8.75 bridge": root / "runtime" / "tools" / "spine_native" / "3.8.75" / "spine_bridge.dll",
        "Spine 3.8.75 metadata": root / "runtime" / "tools" / "spine_native" / "3.8.75" / "spine_native.json",
        "Spine 3.8.75 LICENSE": root / "runtime" / "tools" / "spine_native" / "3.8.75" / "LICENSE",
        "Spine 4.0 bridge": root / "runtime" / "tools" / "spine_native" / "4.0" / "spine_bridge.dll",
        "Spine 4.0 metadata": root / "runtime" / "tools" / "spine_native" / "4.0" / "spine_native.json",
        "Spine 4.0 LICENSE": root / "runtime" / "tools" / "spine_native" / "4.0" / "LICENSE",
    }
    notices = third_party_root / "THIRD_PARTY_NOTICES.md"
    if notices.is_file():
        result["converter third-party notices"] = notices
    live2d_root = root / "runtime/tools/live2d_native/opacity-v1"
    result.update({"Live2D preview " + name: live2d_root / name for name in LIVE2D_FILES})
    return result


def packaged_artifacts(dist_root: Path, *, require_notices: bool = False) -> dict[str, Path]:
    """Return the exact relative paths expected in a Nuitka standalone dir."""

    result = {
        "converter DLL": dist_root / "tools" / "SpineSkeletonDataConverter" / "lpk_spine_converter.dll",
        "converter LICENSE": dist_root / "tools" / "SpineSkeletonDataConverter" / "LICENSE",
        "converter SOURCE_METADATA": dist_root / "tools" / "SpineSkeletonDataConverter" / "SOURCE_METADATA.json",
        "Spine 3.8.75 bridge": dist_root / "tools" / "spine_native" / "3.8.75" / "spine_bridge.dll",
        "Spine 3.8.75 metadata": dist_root / "tools" / "spine_native" / "3.8.75" / "spine_native.json",
        "Spine 3.8.75 LICENSE": dist_root / "tools" / "spine_native" / "3.8.75" / "LICENSE",
        "Spine 4.0 bridge": dist_root / "tools" / "spine_native" / "4.0" / "spine_bridge.dll",
        "Spine 4.0 metadata": dist_root / "tools" / "spine_native" / "4.0" / "spine_native.json",
        "Spine 4.0 LICENSE": dist_root / "tools" / "spine_native" / "4.0" / "LICENSE",
    }
    notices = ROOT / "third_party" / "wang606_spine_converter" / "THIRD_PARTY_NOTICES.md"
    if require_notices or notices.is_file():
        result["converter third-party notices"] = (
            dist_root / "tools" / "SpineSkeletonDataConverter" / "THIRD_PARTY_NOTICES.md"
        )
    live2d_root = dist_root / "tools/live2d_native/opacity-v1"
    result.update({"Live2D preview " + name: live2d_root / name for name in LIVE2D_FILES})
    return result


def _validate(files: dict[str, Path], *, label: str) -> tuple[Path, ...]:
    missing = tuple(path for path in files.values() if not path.is_file())
    if missing:
        detail = "\n".join(f"  - {name}: {path}" for name, path in files.items() if not path.is_file())
        raise FileNotFoundError(f"{label} is incomplete; required native artifacts are missing:\n{detail}")
    return tuple(files.values())


def validate_native_sources(root: Path = ROOT, *, require_notices: bool = False) -> tuple[Path, ...]:
    files = source_artifacts(root)
    if require_notices:
        notices = root / "third_party" / "wang606_spine_converter" / "THIRD_PARTY_NOTICES.md"
        files["converter third-party notices"] = notices
    result = _validate(files, label="Native source build")
    from app.core.live2d_preview_native import validate_preview_runtime
    validate_preview_runtime(root / "runtime/tools/live2d_native/opacity-v1")
    return result


def validate_packaged_dist(dist_root: Path, *, require_notices: bool = False) -> tuple[Path, ...]:
    result = _validate(
        packaged_artifacts(dist_root.resolve(), require_notices=require_notices),
        label="Packaged native runtime",
    )
    from app.core.live2d_preview_native import validate_preview_runtime
    validate_preview_runtime(dist_root.resolve() / "tools/live2d_native/opacity-v1")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dist",
        type=Path,
        help="Validate a Nuitka standalone directory instead of source build outputs.",
    )
    parser.add_argument(
        "--require-notices",
        action="store_true",
        help="Require the vendored converter third-party notices file.",
    )
    args = parser.parse_args()
    if args.dist:
        files = validate_packaged_dist(args.dist, require_notices=args.require_notices)
    else:
        files = validate_native_sources(ROOT, require_notices=args.require_notices)
    print(f"Validated {len(files)} native artifacts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
