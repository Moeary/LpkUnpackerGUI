# Optional native tool installation

The Settings page can install the optional tools used by extraction and native
preview. Downloads are performed in a temporary directory, checked against the
manifested SHA-256 and size, then moved into `runtime/tools` with one atomic
rename. A failed or interrupted operation does not replace an existing
installation. ZIP entries are rejected when they are absolute, escape the
destination, duplicate an installed path, or are symlinks.

## AssetStudioModCLI

The built-in manifest pins AssetStudioModCLI `0.19.0` from the official GitHub
release and records the MIT license and release URL. The archive is accepted
only when its pinned SHA-256 matches and its expected CLI entry point exists.
The installed executable path is written to the AssetStudio setting.

## Live2D Cubism Core

Cubism Core is a proprietary component distributed under Live2D's software
license. It is not an ordinary open-source dependency and the application does
not invent a public archive URL or bundle a replacement. The Settings page
opens Live2D's official Native SDK download page, or can copy the exact
`Live2DCubismCore.dll` from an installed `live2d-py` package when that package
actually contains the vendor-named DLL. A Python extension module with a
similar name is not accepted as Cubism Core.

## Spine native bridge

Spine native uses the local `app.core.spine_embed.build_native_runtime` bridge.
The downloader verifies an official, commit-pinned `spine-runtimes` source
archive before handing it to the builder; it never treats this source archive
as browser `spine-ts` files or as a ready native runtime.

Two source families are available:

- Spine `3.8.75`, commit `c0699e23a0c8799710323bdf0e076e18f6ba41a2`, for the
  default preview target.
- Spine `4.0`, commit `425ce416bb218b28caeec47b317aa57cd7140375`, for 4.0.x
  skeletons.

The build output is stored below the common `spine_native` tool root in its
family subdirectory. Settings stores that common root under
`preview.spine_runtime_dir`; the native preview chooses a family-specific
bridge from its provenance metadata. The existing web runtime is not used by
this installer.

The Spine Runtimes license remains applicable to the downloaded source. A
compiler/CMake toolchain is required; a missing compiler or failed bridge
build is shown as an installation error and does not update the selected path.
The pinned upstream `LICENSE` is copied into each installed family directory
and recorded as `license_file` in its manifest; the full verified source ZIP
is retained there for audit.

## Provenance and settings

Each installed package contains `.toolchain-manifest.json` with its source URL,
commit, digest, license information and installation time. The source archive
is retained beside a built Spine bridge so the build input can be audited.
The app writes a path setting only after validation and installation complete.
