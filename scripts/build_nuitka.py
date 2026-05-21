from __future__ import annotations

import os
import subprocess
import sys
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


def main() -> int:
    vsdevcmd = find_vsdevcmd()
    nuitka_args = [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        "--msvc=14.3",
        "--enable-plugin=pyside6",
        "--output-dir=build",
        "--output-filename=LpkUnpackerGUI.exe",
        "--windows-console-mode=disable",
        "--include-data-dir=./assets=assets",
        "--include-data-dir=./app/i18n/locales=app/i18n/locales",
        "--include-package=qfluentwidgets",
        "--include-package=filetype",
        "--windows-icon-from-ico=assets/app/icon.ico",
        "--nofollow-import-to=matplotlib,scipy,pandas,tkinter",
        "--python-flag=no_site",
        "--python-flag=no_docstrings",
        "--remove-output",
        "app/main.py",
    ]
    command = (
        f'call "{vsdevcmd}" -arch=x64 -host_arch=x64 -no_logo '
        f"&& {subprocess.list2cmdline(nuitka_args)}"
    )
    return subprocess.call(
        ["cmd.exe", "/d", "/s", "/c", command],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
    )


if __name__ == "__main__":
    raise SystemExit(main())
