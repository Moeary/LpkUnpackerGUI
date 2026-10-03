"""Safe, version-pinned installation helpers for optional native tools.

Only the AssetStudio archive is directly downloadable from the built-in
manifest.  Live2D Cubism Core is a proprietary library and therefore uses an
official download link or a DLL already present in ``live2d-py``.  Spine native
is represented by a pinned source archive for the ``spine_embed`` builder; it
is deliberately not confused with the browser ``spine-ts`` runtime.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import shutil
import stat
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, IO, Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from app.core.toolchain_manifest import (
    CUBISM_CORE_MANIFEST,
    TOOLCHAIN_MANIFEST,
    ToolPackageManifest,
    get_tool_package_manifest,
    register_tool_package_manifest,
)
from app.paths import PROJECT_ROOT


DownloadCallback = Callable[["ToolDownloadProgress"], None]
CancelCallback = Callable[[], bool]

_CHUNK_SIZE = 1024 * 1024
_DOWNLOAD_TIMEOUT_SECONDS = 60
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024
_MANIFEST_FILENAME = ".toolchain-manifest.json"


class ToolchainDownloadError(RuntimeError):
    """Base error for download, integrity and installation failures."""


class ToolchainIntegrityError(ToolchainDownloadError):
    """Raised when a pinned archive does not match its manifest."""


class ToolchainInstallError(ToolchainDownloadError):
    """Raised when an archive cannot be safely installed."""


class ToolchainCancelledError(ToolchainDownloadError):
    """Raised when the caller cancels a download before installation."""


class ToolchainBuildRequiredError(ToolchainDownloadError):
    """Raised when a source package needs an external native builder."""


@dataclass(frozen=True)
class ToolDownloadProgress:
    package_id: str
    phase: str
    completed: int = 0
    total: int = 0
    message: str = ""

    @property
    def fraction(self) -> float | None:
        if self.total <= 0:
            return None
        return max(0.0, min(1.0, self.completed / self.total))


@dataclass(frozen=True)
class ToolInstallResult:
    package_id: str
    version: str
    install_dir: Path
    entrypoint: Path | None = None
    archive_path: Path | None = None
    manifest_path: Path | None = None
    source_path: Path | None = None


def list_tool_package_manifests() -> tuple[ToolPackageManifest, ...]:
    """Return built-in packages in stable display order."""

    return tuple(TOOLCHAIN_MANIFEST[key] for key in sorted(TOOLCHAIN_MANIFEST))


def official_download_url(package_id: str) -> str | None:
    """Return the official manual download page for a package, if any."""

    return get_tool_package_manifest(package_id).official_download_url


def download_tool_package(
    package_id: str,
    install_root: str | os.PathLike[str] | None = None,
    *,
    progress: DownloadCallback | None = None,
    cancel: CancelCallback | None = None,
) -> ToolInstallResult:
    """Download and atomically install one pinned archive package.

    Existing installation directories are never removed or overwritten.  A
    new sibling directory is selected when a previous install does not carry
    the same pinned manifest.
    """

    manifest = get_tool_package_manifest(package_id)
    if manifest.requires_builder:
        provider = manifest.build_provider or "the native builder"
        raise ToolchainBuildRequiredError(
            f"{manifest.display_name} requires {provider}; the pinned source archive "
            "is not a ready-to-run native runtime."
        )
    if manifest.is_manual_license_download:
        url = manifest.official_download_url or "the official vendor download page"
        raise ToolchainDownloadError(
            f"{manifest.display_name} is distributed under a proprietary license. "
            f"Open the official download page instead: {url}"
        )
    if not manifest.is_pinned_archive:
        raise ToolchainDownloadError(
            f"No pinned downloadable artifact is configured for {manifest.package_id}."
        )

    tools_root = _tools_root(install_root)
    tools_root.mkdir(parents=True, exist_ok=True)
    base_dir = tools_root / _safe_relative_path(manifest.install_dir, "install_dir")
    existing = _find_matching_install(base_dir, manifest)
    if existing is not None:
        _emit(
            progress,
            ToolDownloadProgress(
                manifest.package_id,
                "already-installed",
                message=f"Using existing {manifest.display_name} {manifest.version}",
            ),
        )
        return _result_from_install(manifest, existing)

    final_dir = _next_install_dir(base_dir)
    staging = Path(tempfile.mkdtemp(prefix=f".{base_dir.name}-", dir=str(tools_root)))
    try:
        archive = download_pinned_archive(
            manifest,
            staging,
            progress=progress,
            cancel=cancel,
        )
        _emit(
            progress,
            ToolDownloadProgress(
                manifest.package_id,
                "extracting",
                message=f"Extracting {archive.name}",
            ),
        )
        _extract_zip_safely(archive, staging, manifest.package_id, cancel=cancel)
        _validate_expected_files(staging, manifest)
        manifest_path = _write_install_manifest(staging, manifest, archive)
        entrypoint = _resolve_expected_file(staging, manifest.entrypoint) if manifest.entrypoint else None
        # Do not replace a directory that appeared while the network request
        # was running.  Select a new sibling and leave both installs intact.
        if final_dir.exists():
            final_dir = _next_install_dir(base_dir)
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, final_dir)
        installed_manifest = final_dir / manifest_path.name
        _emit(
            progress,
            ToolDownloadProgress(
                manifest.package_id,
                "installed",
                completed=1,
                total=1,
                message=f"Installed {manifest.display_name} {manifest.version}",
            ),
        )
        return ToolInstallResult(
            package_id=manifest.package_id,
            version=manifest.version,
            install_dir=final_dir.resolve(),
            entrypoint=(final_dir / entrypoint.relative_to(staging)).resolve()
            if entrypoint
            else None,
            archive_path=(final_dir / archive.relative_to(staging)).resolve(),
            manifest_path=installed_manifest.resolve(),
        )
    except Exception:
        # The staging directory is ours and has never been exposed as an
        # installation.  Existing tool directories remain untouched.
        shutil.rmtree(staging, ignore_errors=True)
        raise


def build_spine_native_runtime(
    package_id: str = "spine_native",
    install_root: str | os.PathLike[str] | None = None,
    *,
    progress: DownloadCallback | None = None,
    cancel: CancelCallback | None = None,
) -> ToolInstallResult:
    """Download the pinned Spine sources and invoke the native builder.

    ``app.core.spine_embed.build_native_runtime`` is the deliberately small
    integration contract shared with the native builder component.  It must
    have this signature::

        build_native_runtime(
            source_dir, output_dir, manifest, progress=None, cancel=None
        ) -> Path | ToolInstallResult | None

    The builder writes its native output below ``output_dir`` and returns the
    runtime DLL/entry point (or ``None`` when exactly one native library can
    be discovered there).  This wrapper owns downloading, source extraction,
    provenance, validation and the final atomic directory move.  A source
    archive is never presented as a browser runtime or silently treated as a
    finished native install.
    """

    manifest = get_tool_package_manifest(package_id)
    if not manifest.requires_builder:
        raise ToolchainDownloadError(
            "The Spine native package is not configured for a native builder."
        )
    try:
        module = importlib.import_module("app.core.spine_embed")
        builder = getattr(module, "build_native_runtime")
    except (ImportError, AttributeError) as exc:
        provider = manifest.build_provider or "app.core.spine_embed.build_native_runtime"
        raise ToolchainBuildRequiredError(
            f"Spine native installation requires the builder API {provider}."
        ) from exc

    tools_root = _tools_root(install_root)
    tools_root.mkdir(parents=True, exist_ok=True)
    base_dir = tools_root / _safe_relative_path(manifest.install_dir, "install_dir")
    existing = _find_matching_install(base_dir, manifest)
    if existing is not None:
        _emit(
            progress,
            ToolDownloadProgress(
                manifest.package_id,
                "already-installed",
                message=f"Using existing {manifest.display_name} {manifest.version}",
            ),
        )
        return _result_from_install(manifest, existing)

    final_dir = _next_install_dir(base_dir)
    staging = Path(tempfile.mkdtemp(prefix=f".{base_dir.name}-", dir=str(tools_root)))
    try:
        source_download_dir = staging / "source"
        archive = download_pinned_archive(
            manifest,
            source_download_dir,
            progress=progress,
            cancel=cancel,
        )
        source_tree = staging / "source-tree"
        source_tree.mkdir()
        _emit(
            progress,
            ToolDownloadProgress(
                manifest.package_id,
                "extracting-source",
                message=f"Extracting {archive.name}",
            ),
        )
        _extract_zip_safely(
            archive,
            source_tree,
            manifest.package_id,
            cancel=cancel,
            include=_is_spine_native_source_member,
        )

        build_dir = staging / "install"
        build_dir.mkdir()
        _emit(
            progress,
            ToolDownloadProgress(
                manifest.package_id,
                "building",
                message="Building Spine native runtime",
            ),
        )
        metadata = manifest.as_dict()
        # spine_embed builds one ABI family at a time; keep the explicit field
        # in provenance instead of making the builder infer it from a generic
        # package version.
        metadata["runtimeFamily"] = manifest.runtime_family or "3.8"

        def builder_progress(message: str) -> None:
            _emit(
                progress,
                ToolDownloadProgress(
                    manifest.package_id,
                    "building",
                    message=str(message),
                ),
            )

        try:
            built = builder(
                source_dir=source_tree,
                output_dir=build_dir,
                manifest=metadata,
                progress=builder_progress,
                cancel=cancel,
            )
        except TypeError as exc:
            # A builder with the wrong signature is a contract error.  Do not
            # retry positionally: a TypeError raised inside a valid builder
            # must remain visible instead of running it twice.
            raise ToolchainBuildRequiredError(
                "app.core.spine_embed.build_native_runtime has an incompatible signature; "
                "expected source_dir/output_dir/manifest/progress/cancel keywords."
            ) from exc
        if cancel and cancel():
            raise ToolchainCancelledError("Spine native build cancelled.")

        entrypoint = _normalise_builder_entrypoint(built, build_dir)
        if entrypoint is None:
            entrypoint = _discover_native_entrypoint(build_dir)
        if entrypoint is None:
            raise ToolchainInstallError(
                "Spine native builder produced no unambiguous native DLL or library."
            )

        # Keep the upstream license beside the small native runtime artifact.
        # The full source archive is retained for provenance, but is not
        # expected to be bundled with the application.  Refuse an absent or
        # conflicting license rather than shipping an untraceable binary.
        _copy_source_license(source_tree, build_dir, manifest)

        # Keep the verified source archive beside the resulting runtime so a
        # later audit can reproduce the build without reaching into staging.
        retained_archive = build_dir / archive.name
        if retained_archive.exists():
            retained_archive = build_dir / f"source-{archive.name}"
        shutil.copy2(archive, retained_archive)
        manifest_path = _write_install_manifest(
            build_dir,
            manifest,
            retained_archive,
            extra={
                "source_kind": "pinned-source-build",
                "source_commit": manifest.source_commit,
                "builder": manifest.build_provider,
                "entrypoint": str(entrypoint.relative_to(build_dir)).replace("\\", "/"),
            },
        )
        if final_dir.exists():
            final_dir = _next_install_dir(base_dir)
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(build_dir, final_dir)
        installed_entrypoint = final_dir / entrypoint.relative_to(build_dir)
        installed_archive = final_dir / retained_archive.relative_to(build_dir)
        _emit(
            progress,
            ToolDownloadProgress(
                manifest.package_id,
                "installed",
                completed=1,
                total=1,
                message=f"Installed {manifest.display_name} {manifest.version}",
            ),
        )
        return ToolInstallResult(
            package_id=manifest.package_id,
            version=manifest.version,
            install_dir=final_dir.resolve(),
            entrypoint=installed_entrypoint.resolve(),
            archive_path=installed_archive.resolve(),
            manifest_path=(final_dir / manifest_path.name).resolve(),
            source_path=installed_archive.resolve(),
        )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _normalise_builder_entrypoint(value: object, build_dir: Path) -> Path | None:
    if isinstance(value, ToolInstallResult):
        value = value.entrypoint
    if value is None:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = build_dir / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(build_dir.resolve())
    except ValueError as exc:
        raise ToolchainInstallError(
            f"Spine native builder returned a path outside its output directory: {candidate}"
        ) from exc
    if not candidate.is_file():
        raise ToolchainInstallError(
            f"Spine native builder returned a missing entry point: {candidate}"
        )
    return candidate


def _discover_native_entrypoint(build_dir: Path) -> Path | None:
    suffixes = {".dll", ".so", ".dylib"}
    candidates = sorted(
        (
            path.resolve()
            for path in build_dir.rglob("*")
            if path.is_file() and path.suffix.casefold() in suffixes
        ),
        key=lambda path: str(path).casefold(),
    )
    return candidates[0] if len(candidates) == 1 else None


def download_pinned_archive(
    manifest_or_id: ToolPackageManifest | str,
    destination_dir: str | os.PathLike[str],
    *,
    progress: DownloadCallback | None = None,
    cancel: CancelCallback | None = None,
) -> Path:
    """Fetch and verify an archive into *destination_dir* atomically.

    This lower-level operation is intended for ``spine_embed``: it may build
    the pinned Spine source archive itself instead of treating source code as
    a runtime install.
    """

    manifest = (
        get_tool_package_manifest(manifest_or_id)
        if isinstance(manifest_or_id, str)
        else manifest_or_id
    )
    if not manifest.artifact_url or not manifest.artifact_sha256:
        raise ToolchainDownloadError(
            f"{manifest.display_name} has no pinned archive to download."
        )
    _validate_https_url(manifest.artifact_url)
    expected_hash = manifest.artifact_sha256.strip().casefold()
    if len(expected_hash) != 64 or any(char not in "0123456789abcdef" for char in expected_hash):
        raise ToolchainIntegrityError(
            f"Manifest for {manifest.package_id} has an invalid SHA-256 digest."
        )

    destination = Path(destination_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    filename = manifest.artifact_filename or Path(urlparse(manifest.artifact_url).path).name
    filename = _safe_filename(filename, "artifact_filename")
    final_path = destination / filename
    temporary_path = destination / f".{filename}.{os.getpid()}.part"
    # Avoid clobbering a caller's partial file from another worker.
    if temporary_path.exists():
        temporary_path = destination / f".{filename}.{os.getpid()}.{time.time_ns()}.part"

    digest = hashlib.sha256()
    completed = 0
    total = int(manifest.artifact_size or 0)
    try:
        request = Request(
            manifest.artifact_url,
            headers={"User-Agent": "LpkUnpackerGUI-toolchain/1"},
        )
        _emit(
            progress,
            ToolDownloadProgress(
                manifest.package_id,
                "downloading",
                total=total,
                message=f"Downloading {manifest.display_name} {manifest.version}",
            ),
        )
        with urlopen(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
            final_url = getattr(response, "geturl", lambda: manifest.artifact_url)()
            _validate_https_url(str(final_url))
            header_total = response.headers.get("Content-Length")
            if header_total and str(header_total).isdigit():
                total = int(header_total)
            with temporary_path.open("wb") as stream:
                while True:
                    if cancel and cancel():
                        raise ToolchainCancelledError(
                            f"Download cancelled for {manifest.display_name}."
                        )
                    chunk = response.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    stream.write(chunk)
                    digest.update(chunk)
                    completed += len(chunk)
                    _emit(
                        progress,
                        ToolDownloadProgress(
                            manifest.package_id,
                            "downloading",
                            completed=completed,
                            total=total,
                            message=f"Downloading {manifest.display_name} {manifest.version}",
                        ),
                    )
                stream.flush()
                os.fsync(stream.fileno())
        if manifest.artifact_size is not None and completed != manifest.artifact_size:
            raise ToolchainIntegrityError(
                f"Size mismatch for {manifest.display_name}: expected "
                f"{manifest.artifact_size:,} bytes, received {completed:,}."
            )
        actual_hash = digest.hexdigest().casefold()
        if actual_hash != expected_hash:
            raise ToolchainIntegrityError(
                f"SHA-256 mismatch for {manifest.display_name}: expected "
                f"{expected_hash}, received {actual_hash}."
            )
        if final_path.exists():
            existing_hash = _sha256_file(final_path)
            if existing_hash == expected_hash:
                temporary_path.unlink(missing_ok=True)
                return final_path.resolve()
            final_path = _next_file_path(final_path)
        os.replace(temporary_path, final_path)
        return final_path.resolve()
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def extract_cubism_core_from_live2d_py(
    install_root: str | os.PathLike[str] | None = None,
    *,
    source: str | os.PathLike[str] | None = None,
    progress: DownloadCallback | None = None,
) -> ToolInstallResult:
    """Copy an exact ``Live2DCubismCore.dll`` from an installed live2d-py.

    A Python extension module is not treated as Cubism Core merely because it
    contains the word ``live2d``; only the vendor-named DLL is accepted.
    """

    manifest = CUBISM_CORE_MANIFEST
    source_path = _find_live2d_core(source)
    tools_root = _tools_root(install_root)
    base_dir = tools_root / _safe_relative_path(manifest.install_dir, "install_dir")
    tools_root.mkdir(parents=True, exist_ok=True)
    source_hash = _sha256_file(source_path)
    existing = _find_file_install(base_dir, manifest.entrypoint or "Live2DCubismCore.dll", source_hash)
    if existing is not None:
        return ToolInstallResult(
            package_id=manifest.package_id,
            version=manifest.version,
            install_dir=existing.parent.resolve(),
            entrypoint=existing.resolve(),
            source_path=source_path.resolve(),
        )

    final_dir = _next_install_dir(base_dir)
    staging = Path(tempfile.mkdtemp(prefix=f".{base_dir.name}-", dir=str(tools_root)))
    try:
        target = staging / "Live2DCubismCore.dll"
        shutil.copy2(source_path, target)
        if target.stat().st_size <= 0:
            raise ToolchainInstallError("The extracted Cubism Core DLL is empty.")
        manifest_path = _write_install_manifest(
            staging,
            manifest,
            target,
            extra={"source_kind": "live2d-py", "source_path": str(source_path.resolve())},
        )
        if final_dir.exists():
            final_dir = _next_install_dir(base_dir)
        os.replace(staging, final_dir)
        _emit(
            progress,
            ToolDownloadProgress(
                manifest.package_id,
                "installed",
                completed=1,
                total=1,
                message="Installed Cubism Core from live2d-py",
            ),
        )
        return ToolInstallResult(
            package_id=manifest.package_id,
            version=manifest.version,
            install_dir=final_dir.resolve(),
            entrypoint=(final_dir / target.relative_to(staging)).resolve(),
            manifest_path=(final_dir / manifest_path.name).resolve(),
            source_path=source_path.resolve(),
        )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _find_live2d_core(source: str | os.PathLike[str] | None) -> Path:
    if source:
        candidate = Path(source).expanduser().resolve()
        if candidate.is_file() and candidate.name.casefold() == "live2dcubismcore.dll":
            return candidate
        raise ToolchainInstallError(
            f"Selected source is not Live2DCubismCore.dll: {candidate}"
        )
    try:
        spec = importlib.util.find_spec("live2d")
    except (ImportError, ModuleNotFoundError, ValueError) as exc:
        raise ToolchainInstallError(
            "live2d-py is not installed; download Cubism SDK for Native from Live2D."
        ) from exc
    if spec is None or not spec.origin:
        raise ToolchainInstallError(
            "Unable to locate live2d-py; download Cubism SDK for Native from Live2D."
        )
    package_root = Path(spec.origin).resolve().parent
    candidates = sorted(
        (
            path.resolve()
            for path in package_root.rglob("*")
            if path.is_file() and path.name.casefold() == "live2dcubismcore.dll"
        ),
        key=lambda path: str(path).casefold(),
    )
    if not candidates:
        raise ToolchainInstallError(
            "The installed live2d-py package does not contain Live2DCubismCore.dll. "
            f"Open the official Live2D download page: {CUBISM_CORE_MANIFEST.official_download_url}"
        )
    return candidates[0]


def _tools_root(value: str | os.PathLike[str] | None) -> Path:
    if value is None or not str(value).strip():
        return (PROJECT_ROOT / "runtime" / "tools").resolve()
    path = Path(value).expanduser().resolve()
    if path.exists() and not path.is_dir():
        raise ToolchainInstallError(f"Tool install root is not a directory: {path}")
    return path


def _find_matching_install(base_dir: Path, manifest: ToolPackageManifest) -> Path | None:
    candidates = [base_dir]
    if base_dir.parent.is_dir():
        candidates.extend(
            path
            for path in base_dir.parent.iterdir()
            if path.is_dir() and path.name.casefold().startswith(base_dir.name.casefold() + "-")
        )
    for candidate in sorted(candidates, key=lambda path: str(path).casefold()):
        marker = candidate / _MANIFEST_FILENAME
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            if (
                data.get("package_id") == manifest.package_id
                and data.get("version") == manifest.version
                and str(data.get("artifact_sha256") or "").casefold()
                == str(manifest.artifact_sha256 or "").casefold()
            ):
                _validate_expected_files(candidate, manifest)
                return candidate.resolve()
        except (OSError, UnicodeError, json.JSONDecodeError, ToolchainInstallError):
            continue
    return None


def _find_file_install(base_dir: Path, filename: str, digest: str) -> Path | None:
    candidates = [base_dir]
    if base_dir.parent.is_dir():
        candidates.extend(
            path
            for path in base_dir.parent.iterdir()
            if path.is_dir() and path.name.casefold().startswith(base_dir.name.casefold() + "-")
        )
    relative = _safe_relative_path(filename, "entrypoint")
    for install_dir in sorted(candidates, key=lambda path: str(path).casefold()):
        candidate = install_dir / Path(*relative.parts)
        if candidate.is_file() and _sha256_file(candidate) == digest:
            return candidate
    return None


def _result_from_install(manifest: ToolPackageManifest, install_dir: Path) -> ToolInstallResult:
    marker = install_dir / _MANIFEST_FILENAME
    entrypoint = _resolve_expected_file(install_dir, manifest.entrypoint) if manifest.entrypoint else None
    if entrypoint is None and marker.is_file():
        try:
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
            marker_entrypoint = marker_data.get("entrypoint")
            if marker_entrypoint:
                entrypoint = _resolve_expected_file(install_dir, str(marker_entrypoint))
        except (OSError, UnicodeError, json.JSONDecodeError, ToolchainInstallError):
            entrypoint = None
    return ToolInstallResult(
        package_id=manifest.package_id,
        version=manifest.version,
        install_dir=install_dir.resolve(),
        entrypoint=entrypoint.resolve() if entrypoint else None,
        archive_path=_find_archive_file(install_dir, manifest),
        manifest_path=marker.resolve() if marker.is_file() else None,
    )


def _find_archive_file(install_dir: Path, manifest: ToolPackageManifest) -> Path | None:
    if manifest.artifact_filename:
        candidate = install_dir / _safe_filename(manifest.artifact_filename, "artifact_filename")
        return candidate.resolve() if candidate.is_file() else None
    return None


def _next_install_dir(base_dir: Path) -> Path:
    if not base_dir.exists():
        return base_dir
    index = 2
    while True:
        candidate = base_dir.parent / f"{base_dir.name}-{index}"
        if not candidate.exists():
            return candidate
        index += 1


def _next_file_path(path: Path) -> Path:
    index = 2
    while True:
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def _extract_zip_safely(
    archive: Path,
    destination: Path,
    package_id: str,
    *,
    cancel: CancelCallback | None = None,
    include: Callable[[PurePosixPath], bool] | None = None,
) -> None:
    root = destination.resolve()
    seen: set[str] = set()
    total_uncompressed = 0
    try:
        with zipfile.ZipFile(archive) as handle:
            infos = handle.infolist()
            if len(infos) > _MAX_ARCHIVE_MEMBERS:
                raise ToolchainInstallError(
                    f"{package_id} archive contains too many entries ({len(infos):,})."
                )
            for info in infos:
                relative = _safe_archive_member(info.filename)
                key = str(relative).casefold()
                if key in seen:
                    raise ToolchainInstallError(
                        f"{package_id} archive contains duplicate path: {info.filename!r}"
                    )
                seen.add(key)
                total_uncompressed += max(0, int(info.file_size))
                if total_uncompressed > _MAX_ARCHIVE_BYTES:
                    raise ToolchainInstallError(
                        f"{package_id} archive expands beyond the safety limit."
                    )
                target = (root / Path(*relative.parts)).resolve()
                try:
                    target.relative_to(root)
                except ValueError as exc:
                    raise ToolchainInstallError(
                        f"Archive member escapes install root: {info.filename!r}"
                    ) from exc
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise ToolchainInstallError(
                        f"Archive member is a symlink and is not allowed: {info.filename!r}"
                    )
                if include is not None and not include(relative):
                    # Every member has already passed path, duplicate, link
                    # and expansion-limit checks.  Only the selected source
                    # subtree is materialized to avoid Windows MAX_PATH
                    # failures in unrelated iOS/sample projects.
                    continue
                if info.is_dir() or info.filename.endswith(("/", "\\")):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if cancel and cancel():
                    raise ToolchainCancelledError(f"Download cancelled for {package_id}.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with handle.open(info, "r") as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, _CHUNK_SIZE)
    except zipfile.BadZipFile as exc:
        raise ToolchainInstallError(f"Invalid ZIP archive for {package_id}: {exc}") from exc


def _is_spine_native_source_member(relative: PurePosixPath) -> bool:
    """Keep only spine-cpp and short license files from a full runtime zip."""

    parts = tuple(part.casefold() for part in relative.parts)
    if "spine-cpp" in parts:
        return True
    # Preserve the upstream license/provenance documents without expanding
    # every platform sample and Xcode project from the monorepo archive.
    return len(parts) <= 3 and parts[-1] in {
        "license",
        "license.txt",
        "copying",
        "notice",
    }


def _copy_source_license(source_tree: Path, build_dir: Path, manifest: ToolPackageManifest) -> Path:
    """Copy the pinned source license into the final native install root."""

    target_relative = manifest.license_file or "LICENSE"
    target = build_dir / Path(*_safe_relative_path(target_relative, "license file").parts)
    try:
        target.relative_to(build_dir.resolve())
    except ValueError as exc:
        raise ToolchainInstallError(f"Manifest license path escapes install root: {target_relative!r}") from exc

    names = {"license", "license.txt", "copying", "notice"}
    candidates = [
        path
        for path in source_tree.rglob("*")
        if path.is_file() and path.name.casefold() in names
    ]
    if not candidates:
        raise ToolchainInstallError(
            f"Pinned {manifest.display_name} source archive has no usable license file."
        )
    candidates.sort(
        key=lambda path: (
            len(path.relative_to(source_tree).parts),
            str(path).casefold(),
        )
    )
    source = candidates[0]
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if not target.is_file() or _sha256_file(target) != _sha256_file(source):
            raise ToolchainInstallError(
                f"Native builder produced a conflicting {target_relative} license file."
            )
        return target
    shutil.copy2(source, target)
    return target


def _validate_expected_files(root: Path, manifest: ToolPackageManifest) -> None:
    for relative in manifest.expected_files:
        _resolve_expected_file(root, relative)


def _resolve_expected_file(root: Path, relative: str | None) -> Path:
    if not relative:
        raise ToolchainInstallError("Manifest does not declare an expected file.")
    safe = _safe_relative_path(relative, "expected file")
    direct = (Path(root).resolve() / Path(*safe.parts)).resolve()
    try:
        direct.relative_to(Path(root).resolve())
    except ValueError as exc:
        raise ToolchainInstallError(f"Manifest path escapes install root: {relative!r}") from exc
    if direct.is_file():
        return direct
    # ZIP names are case-sensitive in the archive but Windows installs are
    # case-insensitive; tolerate a case-only difference without broadening the
    # path to arbitrary files.
    wanted = str(safe).casefold()
    for path in Path(root).rglob("*"):
        if path.is_file() and str(path.relative_to(Path(root))).replace("\\", "/").casefold() == wanted:
            return path.resolve()
    raise ToolchainInstallError(f"Expected package file is missing: {relative}")


def _write_install_manifest(
    root: Path,
    manifest: ToolPackageManifest,
    archive_or_file: Path,
    *,
    extra: dict[str, Any] | None = None,
) -> Path:
    payload = manifest.as_dict()
    payload.update(
        {
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "artifact_sha256_actual": _sha256_file(archive_or_file),
        }
    )
    if extra:
        payload.update(extra)
    target = Path(root) / _MANIFEST_FILENAME
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def _safe_archive_member(name: str) -> PurePosixPath:
    normalized = str(name or "").replace("\\", "/")
    if not normalized or normalized.startswith("/") or normalized.startswith("\\"):
        raise ToolchainInstallError(f"Unsafe archive member path: {name!r}")
    # A drive prefix is absolute even when pathlib on the host is not Windows.
    if len(normalized) >= 2 and normalized[1] == ":":
        raise ToolchainInstallError(f"Unsafe archive member path: {name!r}")
    path = PurePosixPath(normalized)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ToolchainInstallError(f"Unsafe archive member path: {name!r}")
    return path


def _safe_relative_path(value: str | os.PathLike[str], label: str) -> PurePosixPath:
    raw = os.fspath(value).replace("\\", "/")
    if not raw or raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise ToolchainInstallError(f"Unsafe {label}: {value!r}")
    path = PurePosixPath(raw)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ToolchainInstallError(f"Unsafe {label}: {value!r}")
    return path


def _safe_filename(value: str, label: str) -> str:
    path = _safe_relative_path(value, label)
    if len(path.parts) != 1:
        raise ToolchainInstallError(f"{label} must be a filename: {value!r}")
    return path.name


def _validate_https_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme.casefold() != "https" or not parsed.netloc:
        raise ToolchainDownloadError(f"Only HTTPS tool URLs are allowed: {value!r}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest().casefold()


def _emit(callback: DownloadCallback | None, progress: ToolDownloadProgress) -> None:
    if callback:
        callback(progress)


__all__ = [
    "DownloadCallback",
    "ToolDownloadProgress",
    "ToolInstallResult",
    "ToolchainBuildRequiredError",
    "ToolchainCancelledError",
    "ToolchainDownloadError",
    "ToolchainInstallError",
    "ToolchainIntegrityError",
    "build_spine_native_runtime",
    "download_pinned_archive",
    "download_tool_package",
    "extract_cubism_core_from_live2d_py",
    "list_tool_package_manifests",
    "official_download_url",
    "register_tool_package_manifest",
]
