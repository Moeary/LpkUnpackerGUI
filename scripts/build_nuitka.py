from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


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


def build_nuitka_args(compiler: str) -> list[str]:
    compiler_args = ["--msvc=14.3"]
    if compiler == "mingw":
        compiler_args = ["--mingw64", "--low-memory", "--jobs=1", "--lto=no"]

    return [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        *compiler_args,
        "--enable-plugin=pyside6",
        "--output-dir=build",
        "--output-filename=LpkUnpackerGUI.exe",
        "--windows-console-mode=disable",
        "--include-data-dir=./assets=assets",
        "--include-data-dir=./app/tools/AssetStudioCLI=tools/AssetStudioCLI",
        "--include-data-dir=./app/i18n/locales=app/i18n/locales",
        "--include-package=qfluentwidgets",
        "--include-package=filetype",
        "--include-package=cv2",
        "--include-package=psd_tools",
        "--windows-icon-from-ico=assets/app/icon.ico",
        "--nofollow-import-to=matplotlib,scipy,pandas,tkinter",
        "--python-flag=no_site",
        "--python-flag=no_docstrings",
        "--remove-output",
        "app/main.py",
    ]


def run_msvc_build(vsdevcmd: Path, nuitka_args: list[str]) -> int:
    """Activate MSVC in a temporary batch file, then invoke Nuitka.

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
    args = parser.parse_args()

    nuitka_args = build_nuitka_args(args.compiler)
    if args.compiler == "mingw":
        return subprocess.call(nuitka_args, cwd=ROOT, stdin=subprocess.DEVNULL)

    vsdevcmd = find_vsdevcmd()
    return run_msvc_build(vsdevcmd, nuitka_args)


if __name__ == "__main__":
    raise SystemExit(main())
