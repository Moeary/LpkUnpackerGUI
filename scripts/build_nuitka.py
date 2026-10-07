from __future__ import annotations

import argparse
import importlib.metadata
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.verify_native_build import validate_native_sources  # noqa: E402


def existing_file(path: str | Path | None) -> Path | None:
    if not path:
        return None

    candidate = Path(path)
    return candidate if candidate.is_file() else None


def find_vsdevcmd() -> Path:
    env_path = existing_file(os.environ.get("LPK_VSDEVCMD"))
    if env_path:
        return env_path

    candidates = [
        ROOT / "tools" / "VsDevCmd.bat",
        Path(r"D:\Programs\VisualStudio\Common7\Tools\VsDevCmd.bat"),
        Path(r"D:\Programs\Visual Stduio Community 2022\Common7\Tools\VsDevCmd.bat"),
        Path(r"C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\Tools\VsDevCmd.bat"),
        Path(r"C:\Program Files\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat"),
        Path(r"C:\Program Files\Microsoft Visual Studio\2022\Enterprise\Common7\Tools\VsDevCmd.bat"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate

    program_files_x86 = os.environ.get("ProgramFiles(x86)")
    vswhere = existing_file(
        Path(program_files_x86) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        if program_files_x86
        else None
    )
    if vswhere:
        output = subprocess.check_output(
            [
                str(vswhere),
                "-latest",
                "-products",
                "*",
                "-requires",
                "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                "-property",
                "installationPath",
            ],
            text=True,
        ).strip()
        candidate = existing_file(Path(output) / "Common7" / "Tools" / "VsDevCmd.bat")
        if candidate:
            return candidate

    raise FileNotFoundError(
        "Cannot find VsDevCmd.bat. Set LPK_VSDEVCMD to the Visual Studio VsDevCmd.bat path."
    )


def build_nuitka_args(compiler: str, *, require_native: bool = False) -> list[str]:
    if require_native:
        # Release builds must fail before Nuitka starts when a locally built
        # native artifact is absent.  The optional developer invocation keeps
        # the old source-only behavior for contributors who only need to
        # inspect the Python application.
        validate_native_sources(ROOT, require_notices=True)
        if importlib.metadata.version("live2d-py") != "0.7.0":
            raise RuntimeError("Release preview requires the pinned live2d-py 0.7.0 Python wrapper.")

    compiler_args = ["--msvc=14.3"]
    if compiler == "mingw":
        compiler_args = ["--mingw64", "--low-memory", "--jobs=1", "--lto=no"]

    args = [
        sys.executable,
        "-m",
        "nuitka",
        # One EXE that extracts itself to a temporary directory per launch;
        # app.paths keeps runtime/ beside the EXE instead of in that folder.
        "--onefile",
        "--assume-yes-for-downloads",
        *compiler_args,
        "--enable-plugin=pyside6",
        "--output-dir=build",
        "--output-filename=LpkUnpackerGUI.exe",
        # Preserve redirected stdin/stdout for --mcp-animation; launching the
        # GUI from Explorer still creates no console with attach mode.
        "--windows-console-mode=attach",
        "--include-data-dir=./assets=assets",
        # Data dirs silently drop .exe/.dll files; AssetStudio needs both.
        "--include-raw-dir=./app/tools/AssetStudioCLI=tools/AssetStudioCLI",
        "--include-data-dir=./app/i18n/locales=app/i18n/locales",
        "--include-package=qfluentwidgets",
        "--include-package=filetype",
        "--include-package=cv2",
        # PyOpenGL discovers its optional Cython wrappers at runtime.  Keep
        # the accelerator package in standalone releases when it is installed
        # alongside the pinned PyOpenGL version.
        "--include-package=OpenGL_accelerate",
        "--include-package=psd_tools",
        "--include-module=app.core.psd_worker",
        "--include-distribution-metadata=live2d-py",
        # live2d_preview_native imports live2d.v3 via importlib after picking
        # the overlay, so Nuitka cannot see it; without this the EXE fails at
        # startup with "No module named 'live2d.v3'".
        "--include-package=live2d.v3",
        "--include-package-data=live2d",
        "--include-package=mcp",
        "--include-package=uvicorn",
        "--windows-icon-from-ico=assets/app/icon.ico",
        "--nofollow-import-to=matplotlib,scipy,pandas,tkinter",
        "--python-flag=no_site",
        # FastMCP builds tool descriptions from docstrings at runtime, so do
        # not pass --python-flag=no_docstrings.  build/main.dist is kept (no
        # --remove-output) so release checks can inspect the onefile payload.
        "app/main.py",
    ]
    native_converter = ROOT / "runtime" / "tools" / "SpineSkeletonDataConverter" / "lpk_spine_converter.dll"
    if native_converter.is_file():
        args.insert(
            -1,
            f"--include-data-file={native_converter}=tools/SpineSkeletonDataConverter/lpk_spine_converter.dll",
        )
        for name in ("LICENSE", "SOURCE_METADATA.json", "THIRD_PARTY_NOTICES.md"):
            source = ROOT / "third_party" / "wang606_spine_converter" / name
            if source.is_file():
                args.insert(-1, f"--include-data-file={source}=tools/SpineSkeletonDataConverter/{name}")
    # Include only runtime artifacts and their provenance, never downloaded
    # source archives, CMake build trees, or test models.
    native_root = ROOT / "runtime" / "tools" / "spine_native"
    for family in ("3.8.75", "4.0"):
        library = native_root / family / "spine_bridge.dll"
        if not library.is_file():
            continue
        for name in ("spine_bridge.dll", "spine_native.json", "LICENSE"):
            source = library.parent / name
            if source.is_file():
                target = f"tools/spine_native/{library.parent.name}/{name}"
                args.insert(-1, f"--include-data-file={source}={target}")
    bridge_source = ROOT / "app" / "native" / "spine_bridge"
    if bridge_source.is_dir():
        args.insert(-1, f"--include-data-dir={bridge_source}=app/native/spine_bridge")
    live2d_root = ROOT / "runtime/tools/live2d_native/opacity-v1"
    if (live2d_root / "live2d_native.json").is_file():
        args.insert(-1, f"--user-package-configuration-file={ROOT / 'scripts/live2d-native.nuitka-package.config.yml'}")
        for source in sorted(live2d_root.rglob("*")):
            if source.is_file() and source.suffix.lower() not in {".dll", ".pyd"}:
                target = "tools/live2d_native/opacity-v1/" + source.relative_to(live2d_root).as_posix()
                args.insert(-1, f"--include-data-file={source}={target}")
    return args


def run_msvc_build(vsdevcmd: Path, nuitka_args: list[str]) -> int:
    r"""Activate MSVC in a temporary batch file, then invoke Nuitka.

    Passing the complete ``call "...\VsDevCmd.bat" && ...`` expression as a
    list element makes Python escape its embedded quotes as ``\"`` on
    Windows.  ``cmd.exe`` treats those backslashes literally, so paths under
    ``Program Files`` fail on GitHub-hosted runners.  A batch file avoids that
    second layer of command-line quoting entirely.
    """
    command = subprocess.list2cmdline(nuitka_args)
    batch_text = (
        "@echo off\n"
        f'call "{vsdevcmd}" -arch=x64 -host_arch=x64 -no_logo\n'
        "if errorlevel 1 exit /b %errorlevel%\n"
        f"{command}\n"
        "exit /b %errorlevel%\n"
    )
    with tempfile.TemporaryDirectory(prefix="lpk_nuitka_") as temp_dir:
        batch_file = Path(temp_dir) / "build.cmd"
        batch_file.write_text(batch_text, encoding="utf-8")
        return subprocess.call(
            ["cmd.exe", "/d", "/c", str(batch_file)],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build LpkUnpackerGUI with Nuitka.")
    parser.add_argument(
        "--compiler",
        choices=("msvc", "mingw"),
        default="msvc",
        help="Backend compiler preset. Default: msvc.",
    )
    parser.add_argument(
        "--require-native",
        action="store_true",
        help="Require the converter and both native Spine bridge families before building.",
    )
    args = parser.parse_args()

    nuitka_args = build_nuitka_args(args.compiler, require_native=args.require_native)
    if args.compiler == "mingw":
        result = subprocess.call(nuitka_args, cwd=ROOT, stdin=subprocess.DEVNULL)
    else:
        result = run_msvc_build(find_vsdevcmd(), nuitka_args)
    if result == 0:
        remove_intermediate_build_dirs()
    return result


def remove_intermediate_build_dirs(build_root: Path | None = None) -> None:
    """Drop C sources/objects but keep main.dist, the onefile payload."""
    build_root = build_root or ROOT / "build"
    for name in ("main.build", "main.onefile-build"):
        shutil.rmtree(build_root / name, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
