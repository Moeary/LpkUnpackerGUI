"""Build/install entry point for the official versioned Spine native bridge.

The downloader/toolchain layer calls :func:`build_native_runtime` after it has
obtained and verified an official ``spine-runtimes`` source tree.  This module
only builds the local bridge; it never changes settings or runtime data.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from app.paths import PROJECT_ROOT


Progress = Callable[[str], None]
Cancel = Callable[[], bool]
_COMMAND_OUTPUT_LIMIT = 12_000
_COMMAND_POLL_SECONDS = 0.05


def _emit(progress: Progress | None, message: str) -> None:
    if progress:
        progress(message)


def _find_spine_root(source_dir: Path) -> Path:
    candidates = [
        source_dir,
        source_dir / "spine-cpp",
        source_dir / "spine-cpp" / "spine-cpp",
    ]
    candidates.extend(source_dir.glob("**/spine-cpp/spine-cpp"))
    for candidate in candidates:
        if (candidate / "include" / "spine" / "spine.h").is_file() and (candidate / "src").is_dir():
            return candidate.resolve()
    raise FileNotFoundError(f"Official spine-cpp source was not found under {source_dir}")


def _read_manifest(manifest: Mapping[str, Any] | str | os.PathLike[str] | None) -> dict[str, Any]:
    if manifest is None:
        return {}
    if isinstance(manifest, Mapping):
        return dict(manifest)
    path = Path(manifest)
    return json.loads(path.read_text(encoding="utf-8"))


def _creation_flags() -> int:
    """Return the Windows flag that prevents a console window from flashing."""

    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _terminate_process(process: subprocess.Popen[str]) -> None:
    """Stop a CMake process and wait for it without masking the original error."""

    try:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        # The process may have exited between poll/terminate.  The caller will
        # report the cancellation or command error that caused this cleanup.
        return


def _safe_build_directory(family: str) -> tuple[Path, Path]:
    """Create a temporary CMake directory and prove it is below the temp root."""

    temp_root = Path(tempfile.gettempdir()).expanduser().resolve()
    created = Path(
        tempfile.mkdtemp(
            prefix=f"lpk_spine_native_{family.replace('.', '')}_",
            dir=str(temp_root),
        )
    ).expanduser()
    if created.is_symlink():
        raise RuntimeError(f"CMake build directory must not be a symlink: {created}")
    build_root = created.resolve()
    try:
        build_root.relative_to(temp_root)
    except ValueError as exc:
        # Never recursively delete a path that did not come from our temp
        # root.  This check also protects the finally block if a test or a
        # future platform changes tempfile's behavior.
        raise RuntimeError(
            f"CMake build directory escaped the temporary root: {build_root}"
        ) from exc
    if build_root == temp_root:
        raise RuntimeError("CMake build directory must be below the temporary root")
    return temp_root, build_root


def _cleanup_build_directory(temp_root: Path, build_root: Path) -> None:
    """Remove only a build directory that was created below the temp root."""

    try:
        resolved_root = temp_root.expanduser().resolve()
        candidate = build_root.expanduser()
        if candidate.is_symlink():
            return
        resolved_build = candidate.resolve()
        resolved_build.relative_to(resolved_root)
        if resolved_build == resolved_root:
            return
        if resolved_build.is_dir():
            shutil.rmtree(resolved_build)
    except (OSError, RuntimeError, ValueError):
        # Cleanup is best effort.  A failed cleanup must not replace a useful
        # CMake/build error, and the containment check prevents unsafe removal.
        return


def _run_cmake(
    command: list[str],
    *,
    phase: str,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
) -> None:
    """Run CMake with streamed, bounded diagnostics and cooperative cancel."""

    kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
    }
    flags = _creation_flags()
    if flags:
        kwargs["creationflags"] = flags
    try:
        process = subprocess.Popen(command, **kwargs)
    except OSError as exc:
        raise RuntimeError(f"Unable to start CMake during {phase}: {exc}") from exc

    lines: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        stream = process.stdout
        if stream is None:
            lines.put(None)
            return
        try:
            for line in stream:
                lines.put(line)
        finally:
            lines.put(None)

    reader = threading.Thread(target=read_output, name="spine-cmake-output", daemon=True)
    reader.start()
    captured: list[str] = []
    captured_size = 0
    output_truncated = False
    try:
        while True:
            if process.poll() is None and cancel and cancel():
                _terminate_process(process)
                raise RuntimeError(f"Native Spine bridge build cancelled during {phase}")
            try:
                line = lines.get(timeout=_COMMAND_POLL_SECONDS)
            except queue.Empty:
                if process.poll() is not None and not reader.is_alive():
                    break
                continue
            if line is None:
                # A child can close its stdout before the process itself has
                # exited.  Keep polling so cancellation remains responsive.
                if process.poll() is not None:
                    break
                continue
            if not line.strip():
                continue
            text = line.rstrip()
            _emit(progress, text)
            if captured_size < _COMMAND_OUTPUT_LIMIT:
                remaining = _COMMAND_OUTPUT_LIMIT - captured_size
                captured.append(text[:remaining])
                captured_size += min(len(text), remaining)
                output_truncated = output_truncated or len(text) > remaining
            else:
                output_truncated = True
        return_code = process.wait()
    except BaseException:
        _terminate_process(process)
        raise
    finally:
        if process.poll() is None:
            _terminate_process(process)
        reader.join(timeout=1)
        stream = process.stdout
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    if return_code:
        output = "\n".join(captured)
        if output_truncated:
            output = "[CMake output truncated]\n" + output
        detail = output or "(CMake produced no output)"
        raise RuntimeError(
            f"CMake {phase} failed with exit code {return_code}:\n{detail}"
        )


def build_native_runtime(
    source_dir: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    manifest: Mapping[str, Any] | str | os.PathLike[str] | None = None,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
) -> Path:
    """Compile the bridge and install one true runtime-family artifact.

    ``source_dir`` must be an official source tree selected by the caller's
    manifest.  The returned path is the installed ``spine_bridge`` library.
    A temporary CMake build directory is used outside the repository; only
    the bridge and a provenance manifest are written under ``output_dir``.
    """

    source = _find_spine_root(Path(source_dir).expanduser().resolve())
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    metadata = _read_manifest(manifest)
    family = str(metadata.get("runtimeFamily") or metadata.get("family") or "").strip()
    if family not in {"3.8", "4.0"}:
        raise ValueError("Native Spine bridge family must be exactly 3.8 or 4.0")
    if cancel and cancel():
        raise RuntimeError("Native Spine bridge build cancelled before configuration")

    repo_root = PROJECT_ROOT
    cmake_source = repo_root / "native" / "spine_bridge"
    if not (cmake_source / "CMakeLists.txt").is_file():
        raise FileNotFoundError(f"Bridge CMake source missing: {cmake_source}")
    temp_root, build_root = _safe_build_directory(family)
    try:
        generator = "MinGW Makefiles" if os.name == "nt" and shutil.which("mingw32-make") else None
        configure = ["cmake", "-S", str(cmake_source), "-B", str(build_root), f"-DSPINE_ROOT={source}"]
        if generator:
            configure.extend(["-G", generator])
        if family == "4.0":
            configure.append("-DSPINE_BRIDGE_RUNTIME_40=ON")
        _emit(progress, f"Configuring official Spine {family} native bridge")
        _run_cmake(configure, phase="configuration", progress=progress, cancel=cancel)
        if cancel and cancel():
            raise RuntimeError("Native Spine bridge build cancelled")
        _emit(progress, f"Compiling official Spine {family} native bridge")
        _run_cmake(
            ["cmake", "--build", str(build_root), "--parallel"],
            phase="build",
            progress=progress,
            cancel=cancel,
        )
        candidates = [
            build_root / "spine_bridge.dll",
            build_root / "spine_bridge.so",
            build_root / "libspine_bridge.so",
        ]
        built = next((path for path in candidates if path.is_file()), None)
        if built is None:
            built = next(build_root.rglob("spine_bridge.dll"), None) or next(
                build_root.rglob("libspine_bridge.so"), None
            )
        if built is None:
            raise FileNotFoundError(
                f"CMake completed but bridge library was not found under {build_root}"
            )
        installed = output / (
            "spine_bridge.dll" if built.suffix.lower() == ".dll" else "spine_bridge.so"
        )
        shutil.copy2(built, installed)
        provenance = dict(metadata)
        provenance.update(
            {
                "runtimeFamily": family,
                "bridge": "official spine-cpp + lpk native C ABI",
                "bridgeAbi": 1,
                "library": installed.name,
                "sourceDirectory": str(source),
                "licenseRequired": True,
            }
        )
        (output / "spine_native.json").write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _emit(progress, f"Installed {installed}")
        return installed
    finally:
        _cleanup_build_directory(temp_root, build_root)


__all__ = ["build_native_runtime"]
