"""Build the pinned Windows drawable-opacity preview overlay, without pip install."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "third_party/live2d_native"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def download(pin, cache):
    path = cache / pin["url"].rsplit("/", 1)[1]
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        print(f"Download {pin['url']}", flush=True)
        with urllib.request.urlopen(pin["url"], timeout=60) as response, path.open("wb") as output:
            shutil.copyfileobj(response, output)
    if sha256(path) != pin["sha256"]:
        raise RuntimeError(f"Pinned archive SHA256 mismatch: {path}")
    return path


def safe_target(root, name):
    target = (root / name).resolve()
    if not target.is_relative_to(root.resolve()):
        raise RuntimeError(f"Escaping archive member: {name}")
    return target


def prepare_source(work, cache, metadata):
    source_root = work / "source/live2d_py-0.7.0"
    if not source_root.exists():
        archive = download(metadata["upstream"], cache)
        with tarfile.open(archive) as bundle:
            for item in bundle.getmembers():
                if item.issym() or item.islnk():
                    raise RuntimeError("Source archive links are not accepted.")
                safe_target(work / "source", item.name)
            bundle.extractall(work / "source")
        sdk = download(metadata["sdk"], cache)
        sdk_root = source_root / "Live2D/V3"
        with zipfile.ZipFile(sdk) as bundle:
            for item in bundle.infolist():
                relative = Path(*Path(item.filename).parts[1:])
                if not relative.parts or relative.parts[0] not in {"Core", "Framework"}:
                    continue
                target = safe_target(sdk_root, relative)
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(item) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
    patch = SOURCE / metadata["patch"]
    result = subprocess.run(["git", "apply", "--reverse", "--check", str(patch)], cwd=source_root,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        subprocess.run(["git", "apply", "--check", str(patch)], cwd=source_root, check=True)
        subprocess.run(["git", "apply", str(patch)], cwd=source_root, check=True)
    return source_root


def find_tool(name, candidate):
    path = shutil.which(name) or candidate
    if not path or not Path(path).is_file():
        raise FileNotFoundError(f"Cannot find {name}; pass its toolchain directory explicitly.")
    return str(Path(path).resolve())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, default=ROOT / "runtime/tools/live2d_native/build/reproducible")
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/tools/live2d_native/opacity-v1")
    parser.add_argument("--toolchain-dir", type=Path, default=Path("D:/Programs_Dev/w64devkit/bin"))
    parser.add_argument("--cmake", default=None)
    parser.add_argument("--jobs", type=int, default=6)
    args = parser.parse_args(argv)
    if sys.platform != "win32" or platform.architecture()[0] != "64bit" or sys.version_info < (3, 10):
        raise RuntimeError("This controlled overlay build requires 64-bit Windows CPython 3.10 or newer.")
    metadata = json.loads((SOURCE / "SOURCE_METADATA.json").read_text(encoding="utf-8"))
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    cache = ROOT / "runtime/tools/live2d_native/source"
    source = prepare_source(work, cache, metadata)
    toolchain = args.toolchain_dir.resolve()
    cmake = find_tool("cmake", args.cmake or "D:/Programs_Dev/CMake/bin/cmake.exe")
    tools = {name: find_tool(name, toolchain / (name + ".exe"))
             for name in ("gcc", "g++", "gendef", "dlltool", "mingw32-make")}
    import_root = work / "import"
    import_root.mkdir(exist_ok=True)
    core = source / "Live2D/V3/Core/dll/windows/x86_64/Live2DCubismCore.dll"
    subprocess.run([tools["gendef"], str(core)], cwd=import_root, check=True)
    import_library = import_root / "libLpkPreviewCubismCore.a"
    subprocess.run([tools["dlltool"], "--input-def", "Live2DCubismCore.def", "--dllname", "LpkPreviewCubismCore.dll",
                    "--output-lib", str(import_library)], cwd=import_root, check=True)
    env = dict(os.environ, PATH=str(toolchain) + os.pathsep + os.environ.get("PATH", ""))
    build = work / "cmake"
    subprocess.run([cmake, "-S", str(source), "-B", str(build), "-G", "MinGW Makefiles",
                    "-DCMAKE_BUILD_TYPE=Release", "-DBUILD_V2CPP=OFF",
                    "-DPYTHON_INSTALLATION_PATH=" + Path(sys.prefix).as_posix(),
                    "-DLPK_CORE_IMPORT_LIBRARY=" + import_library.as_posix(),
                    "-DCMAKE_C_COMPILER=" + Path(tools["gcc"]).as_posix(),
                    "-DCMAKE_CXX_COMPILER=" + Path(tools["g++"]).as_posix(),
                    "-DCMAKE_MAKE_PROGRAM=" + Path(tools["mingw32-make"]).as_posix(),
                    "-DCMAKE_SHARED_LINKER_FLAGS=-static-libgcc -static-libstdc++"], env=env, check=True)
    subprocess.run([cmake, "--build", str(build), "--target", "Live2DWrapper", "--parallel", str(args.jobs)],
                   env=env, check=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(build / "Wrapper/V3/_v3cpp.pyd", output / "_v3cpp.pyd")
    shutil.copy2(core, output / "LpkPreviewCubismCore.dll")
    shutil.copytree(source / "package/live2d/v3/FrameworkShaders", output / "FrameworkShaders", dirs_exist_ok=True)
    for name, origin in {
        "LICENSE.live2d-py": source / "LICENSE",
        "LICENSE.CubismFramework.md": source / "Live2D/V3/Framework/LICENSE.md",
        "LICENSE.CubismCore.md": source / "Live2D/V3/Core/LICENSE.md",
        "THIRD_PARTY_NOTICES.md": SOURCE / "THIRD_PARTY_NOTICES.md",
        "SOURCE_METADATA.json": SOURCE / "SOURCE_METADATA.json",
    }.items():
        shutil.copy2(origin, output / name)
    runtime = {
        "format": "LpkUnpacker.Live2DPreviewRuntime", "version": 1,
        "upstream_version": metadata["upstream"]["version"], "sdk_version": metadata["sdk"]["version"],
        "drawable_opacity_api_version": 1, "architecture": "win_amd64", "python_abi": "cp310-abi3",
        "source_sha256": metadata["upstream"]["sha256"], "sdk_sha256": metadata["sdk"]["sha256"],
        "patch_sha256": sha256(SOURCE / metadata["patch"]),
        "compiler": subprocess.check_output([tools["g++"], "--version"], text=True).splitlines()[0],
        "files": {path.relative_to(output).as_posix(): sha256(path) for path in sorted(output.rglob("*"))
                  if path.is_file() and path.name != "live2d_native.json"},
    }
    (output / "live2d_native.json").write_text(json.dumps(runtime, indent=2) + "\n", encoding="utf-8")
    print(f"Built isolated preview runtime: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
