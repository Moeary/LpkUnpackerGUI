# Native build prerequisites and release tasks

Windows release builds include the in-process converter DLL and both native
Spine bridge families.  A release build must run the native build task first;
`pixi run build` therefore cannot silently produce a package without these
artifacts.

## Toolchain

Install CMake 3.20 or newer and one x64 C++ toolchain.  The Spine bridge is
compiled as C++17.  The vendored `wang606/SpineSkeletonDataConverter` source
requires CMake 3.15 or newer and C++20.  The supported Windows choices are
Visual Studio 2022 with its Desktop C++ workload, or w64devkit/MinGW with
`g++` and `mingw32-make` on `PATH`.  The CI workflow activates the Visual
Studio developer environment before running Pixi.

The Live2D preview overlay additionally requires CMake 3.26+ and x64
MinGW-w64 (`gcc`, `g++`, `gendef`, `dlltool`, `mingw32-make`). CI downloads
w64devkit 1.23.0 with a pinned SHA256 and adds its tools to `PATH`; MSVC
remains the compiler for the Spine converter and Nuitka. For local builds,
put these tools on `PATH` or pass `--toolchain-dir` to
`scripts/build_live2d_native.py`. See the [overlay notice](../../third_party/live2d_native/THIRD_PARTY_NOTICES.md)
for its pinned sources and licensing.

The converter script selects a generator from the active compiler environment
unless `-Generator` is supplied.  Its persistent CMake directories are kept
separate (`runtime/build/spine-converter-msvc` and
`runtime/build/spine-converter-mingw`) so a generator change cannot reuse an
incompatible cache.  The Spine bridge builder uses a fresh temporary CMake
directory for each family.

## Pixi tasks

Run the following from the project root:

```powershell
pixi run native-converter
pixi run native-spine
pixi run native-build
pixi run build
```

`native-converter` builds the fixed upstream converter source in
`third_party/wang606_spine_converter/`.  `native-spine` builds the pinned
official `spine-runtimes` commits for 3.8.75 and 4.0.  `native-build` validates
the converter, both bridges, their metadata, and all three license files.
`build` depends on that validation and passes `--require-native` to Nuitka.

The CMake bridge source is kept in `app/native/spine_bridge/`.  Nuitka includes
that source at `app/native/spine_bridge` in the standalone package so the
settings/download build flow can still locate its CMake source when compiling
a locally installed runtime.  The compiled runtime DLLs keep their separate
locations under `runtime/tools/spine_native/<family>` and are packaged under
`tools/spine_native/<family>`.

The Spine task first checks for the exact, SHA-256 verified source ZIP retained
beside each existing bridge.  It reuses that archive and extracts only the
needed `spine-cpp` source to a temporary directory.  If no verified local
archive exists, it downloads the URL and digest from
`app/core/toolchain_manifest.py`, retains the verified ZIP beside the bridge,
and uses it on later builds.  It never treats a source archive as a browser
runtime.

For a standalone directory after Nuitka completes, run:

```powershell
pixi run python scripts/verify_native_build.py --dist .\build\LpkUnpackerGUI.dist --require-notices
```

The check requires these bundled native files: the converter DLL, the 3.8.75
and 4.0 bridge DLLs, each bridge's `spine_native.json`, three `LICENSE` files,
the converter `SOURCE_METADATA.json`, and the converter
`THIRD_PARTY_NOTICES.md`.
