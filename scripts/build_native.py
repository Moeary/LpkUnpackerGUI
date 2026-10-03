"""Build the native Spine bridge families from pinned source archives.

The release build uses this entry point instead of the interactive installer.
It first looks beside an installed bridge for the exact archive recorded by the
manifest.  A verified archive is extracted to a temporary source directory,
compiled, and retained beside the resulting bridge.  Only when no verified
local archive exists does the script download the manifest-pinned archive.

The converter is built by ``third_party/wang606_spine_converter/build_native.ps1``;
this script owns the two official Spine bridge families only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

# A direct script invocation starts with ``scripts`` on sys.path.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.spine_embed import build_native_runtime  # noqa: E402
from app.core.toolchain_download import (  # noqa: E402
    _copy_source_license,
    _extract_zip_safely,
    _is_spine_native_source_member,
    download_pinned_archive,
)
from app.core.toolchain_manifest import (  # noqa: E402
    SPINE_NATIVE_40_MANIFEST,
    SPINE_NATIVE_MANIFEST,
    ToolPackageManifest,
)


FAMILY_MANIFESTS = {
    "3.8": SPINE_NATIVE_MANIFEST,
    "4.0": SPINE_NATIVE_40_MANIFEST,
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().casefold()


def _is_verified_archive(path: Path, manifest: ToolPackageManifest) -> bool:
    if not path.is_file():
        return False
    if manifest.artifact_size is not None and path.stat().st_size != manifest.artifact_size:
        return False
    return _sha256(path) == str(manifest.artifact_sha256 or "").casefold()


def _archive_candidates(directory: Path, manifest: ToolPackageManifest) -> list[Path]:
    filename = manifest.artifact_filename or ""
    if not filename:
        return []
    direct = directory / filename
    candidates = [direct]
    # ``download_pinned_archive`` preserves a conflicting file by adding a
    # numeric suffix.  Such a verified cache entry is safe to reuse too.
    stem = Path(filename).stem.casefold()
    try:
        candidates.extend(
            path
            for path in directory.glob("*.zip")
            if path.name.casefold().startswith(stem)
        )
    except OSError:
        pass
    seen: set[str] = set()
    result: list[Path] = []
    for path in candidates:
        key = str(path).casefold()
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _find_verified_archive(
    manifest: ToolPackageManifest,
    output_dir: Path,
    cache_dir: Path,
) -> Path:
    """Return a pinned archive without downloading it again when possible."""

    output_candidates = _archive_candidates(output_dir, manifest)
    for candidate in output_candidates:
        if _is_verified_archive(candidate, manifest):
            return candidate.resolve()
    exact_output = output_dir / str(manifest.artifact_filename or "")
    if exact_output.exists():
        raise RuntimeError(
            f"Pinned Spine archive has the wrong size or SHA-256: {exact_output}"
        )

    cache_dir.mkdir(parents=True, exist_ok=True)
    for candidate in _archive_candidates(cache_dir, manifest):
        if _is_verified_archive(candidate, manifest):
            return candidate.resolve()

    print(
        f"No verified {manifest.display_name} archive is available locally; "
        "downloading the manifest-pinned source archive."
    )
    downloaded = download_pinned_archive(manifest, cache_dir, progress=print)
    if not _is_verified_archive(downloaded, manifest):
        raise RuntimeError(f"Downloaded archive failed verification: {downloaded}")
    return downloaded.resolve()


def _family_output_dir(runtime_root: Path, manifest: ToolPackageManifest) -> Path:
    install_parts = Path(manifest.install_dir).parts
    if install_parts and install_parts[0].casefold() == "spine_native":
        relative = Path(*install_parts[1:])
    else:
        relative = Path(manifest.version)
    return (runtime_root / relative).resolve()


def _write_provenance(output_dir: Path, manifest: ToolPackageManifest, archive: Path) -> None:
    data = manifest.as_dict()
    data.update(
        {
            "source_kind": "pinned-source-build",
            "source_commit": manifest.source_commit,
            "builder": manifest.build_provider,
            "entrypoint": "spine_bridge.dll",
            "source_archive": archive.name,
            "source_archive_sha256": str(manifest.artifact_sha256 or "").casefold(),
        }
    )
    (output_dir / ".toolchain-manifest.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _normalise_runtime_metadata(output_dir: Path, manifest: ToolPackageManifest, archive: Path) -> None:
    metadata_path = output_dir / "spine_native.json"
    if not metadata_path.is_file():
        raise RuntimeError(f"Native bridge did not produce provenance metadata: {metadata_path}")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid native bridge metadata: {metadata_path}") from exc
    metadata["runtimeFamily"] = manifest.runtime_family
    metadata["sourceArchive"] = archive.name
    metadata["sourceArchiveSha256"] = str(manifest.artifact_sha256 or "").casefold()
    # The compiler extracts into a temporary directory.  Do not leave a
    # machine-specific temporary path pretending to be a reusable source path.
    metadata["sourceDirectory"] = f"temporary extraction of {archive.name}"
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build_family(
    manifest: ToolPackageManifest,
    *,
    runtime_root: Path,
    cache_dir: Path,
) -> Path:
    output_dir = _family_output_dir(runtime_root, manifest)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = _find_verified_archive(manifest, output_dir, cache_dir / manifest.version)
    print(f"Using {archive.name} for Spine {manifest.version} ({_sha256(archive)})")

    with tempfile.TemporaryDirectory(prefix=f"lpk_spine_source_{manifest.version.replace('.', '')}_") as temporary:
        source_dir = Path(temporary) / "source"
        source_dir.mkdir()
        # This helper validates every member before applying the source-only
        # filter, including path traversal and symlink checks.
        _extract_zip_safely(
            archive,
            source_dir,
            manifest.package_id,
            include=_is_spine_native_source_member,
        )
        metadata = manifest.as_dict()
        metadata["runtimeFamily"] = manifest.runtime_family
        built = build_native_runtime(
            source_dir=source_dir,
            output_dir=output_dir,
            manifest=metadata,
            progress=print,
        )
        built_path = Path(built).resolve()
        if not built_path.is_file():
            raise RuntimeError(f"Native Spine bridge build returned no file: {built_path}")
        _copy_source_license(source_dir, output_dir, manifest)

    retained_archive = output_dir / str(manifest.artifact_filename)
    if retained_archive.exists():
        if not _is_verified_archive(retained_archive, manifest):
            raise RuntimeError(f"Refusing to replace an invalid retained archive: {retained_archive}")
    else:
        shutil.copy2(archive, retained_archive)
    _normalise_runtime_metadata(output_dir, manifest, retained_archive)
    _write_provenance(output_dir, manifest, retained_archive)
    print(f"Built Spine {manifest.version}: {built_path}")
    return built_path


def _build_explicit_source(args: argparse.Namespace) -> int:
    if not args.family or not args.source_dir or not args.output_dir:
        raise SystemExit("--family, --source-dir and --output-dir are required without --all")
    metadata = (
        json.loads(args.manifest.read_text(encoding="utf-8"))
        if args.manifest
        else {"runtimeFamily": args.family}
    )
    metadata.setdefault("runtimeFamily", args.family)
    build_native_runtime(args.source_dir, args.output_dir, metadata, progress=print)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="Build both pinned 3.8.75 and 4.0 bridges")
    parser.add_argument("--family", choices=tuple(FAMILY_MANIFESTS))
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=PROJECT_ROOT / "runtime" / "tools" / "spine_native",
        help="Common native runtime root used by --all.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=PROJECT_ROOT / "runtime" / "build" / "native-source-cache",
        help="Cache for verified source archives that are not beside an install.",
    )
    args = parser.parse_args()
    if args.all:
        if any(value is not None for value in (args.family, args.source_dir, args.output_dir, args.manifest)):
            parser.error("--all cannot be combined with explicit source build arguments")
        runtime_root = args.runtime_root.resolve()
        cache_dir = args.cache_dir.resolve()
        for manifest in FAMILY_MANIFESTS.values():
            build_family(manifest, runtime_root=runtime_root, cache_dir=cache_dir)
        return 0
    return _build_explicit_source(args)


if __name__ == "__main__":
    raise SystemExit(main())
