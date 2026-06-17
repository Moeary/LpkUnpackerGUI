# Build Notes

This project uses pixi for environment management and Nuitka for Windows packaging.

## Local Commands

```powershell
pixi install
pixi run check
pixi run build
pixi run build-gcc
```

`pixi run check` performs a lightweight syntax parse of files under `app/` and `scripts/`.

`pixi run build` runs `scripts/build_nuitka.py`. The script locates `VsDevCmd.bat`, initializes the Visual Studio 2022 toolchain, then runs `python -m nuitka`.

`pixi run build-gcc` is an experimental MinGW path. It uses:

```powershell
python -m nuitka --standalone --mingw64 --low-memory --jobs=1 --lto=no ...
```

Resolution order:

1. `LPK_VSDEVCMD`, if set.
2. Known local Visual Studio paths, including `D:\Programs\VisualStudio\Common7\Tools\VsDevCmd.bat`.
3. `vswhere.exe` from the Visual Studio Installer.

The Nuitka command pins MSVC with `--msvc=14.3`. Nuitka's `--msvc` option accepts a version selector such as `14.3`, `latest`, or `list`; it does not take a raw `cl.exe` path. Calling the exact `VsDevCmd.bat` from the intended Visual Studio installation is the reliable way to lock the compiler location.

## Why MSVC

MSVC is not theoretically required for every Nuitka build on Windows. The previous MinGW/GCC run reached Nuitka's C backend, then failed inside `cc1.exe` while compiling generated C files from large Python dependencies.

That failure is not the same as total system RAM being exhausted. GCC's `cc1.exe` can fail because of compiler process address space, huge generated translation units, parallel compile pressure, or MinGW runtime limits even when Task Manager still shows available memory.

There is no simple GCC switch that "gives cc1 more RAM" in the way a JVM heap flag would. Useful knobs are about lowering peak memory:

- `--low-memory`
- `--jobs=1`
- `--lto=no`
- excluding unused heavy imports with `--nofollow-import-to`

If an older project compiled fine, it likely generated smaller C translation units, included fewer large packages, used different Nuitka/GCC versions, or compiled with lower parallelism.

Unity asset parsing is intentionally delegated to the external AssetStudioModCLI binary under `app/tools/AssetStudioCLI/`, so Nuitka no longer needs to compile a Python Unity parser.

The local toolchain has been verified to expose:

- `D:\Programs\VisualStudio\VC\Tools\MSVC\14.36.32532\bin\Hostx64\x64\cl.exe`
- `C:\Program Files (x86)\Windows Kits\10\bin\10.0.20348.0\x64\rc.exe`
- `D:\Programs\VisualStudio\VC\Tools\MSVC\14.36.32532\bin\Hostx64\x64\link.exe`

## Download Behavior

The build command intentionally does not use `--assume-yes-for-downloads`, and the build script redirects stdin to `NUL`. That means Nuitka should not silently consent to downloading helper tools or a fallback compiler. If MSVC or Windows SDK detection fails, fix the local Visual Studio installation or the `VsDevCmd.bat` path instead of letting Nuitka fall back to MinGW.

Useful diagnostics:

```powershell
$env:LPK_VSDEVCMD = "D:\Programs\VisualStudio\Common7\Tools\VsDevCmd.bat"
pixi run cmd /c ""%LPK_VSDEVCMD%" -arch=x64 -host_arch=x64 -no_logo && where cl && where rc && where link"
pixi run python -m nuitka --msvc=list
```

## GitHub Actions

The release workflow installs pixi and runs:

```powershell
pixi run build
```

The workflow checks for `LpkUnpackerGUI.exe` under `build/` and packages the generated standalone directory as a zip artifact.
