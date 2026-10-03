# Controlled Live2D preview overlay

This overlay uses the exact `live2d-py 0.7.0` PyPI source (MIT, copyright Arkueid)
and the official Live2D Cubism SDK for Native `5-r.4.1`. URLs and SHA256 values
are locked in `SOURCE_METADATA.json`. `drawable-opacity.patch` adds validated
per-drawable preview multipliers to the normal OpenGL colour shader only.
Mask generation and Cubism Core model memory are unchanged.

The build copies upstream license files into the runtime alongside this notice:

- `LICENSE.live2d-py`: upstream MIT license.
- `LICENSE.CubismFramework.md`: Live2D Open Software License.
- `LICENSE.CubismCore.md`: Live2D Proprietary Software License links.

Cubism Core is a proprietary, unmodified official binary. Its redistribution
and use remain governed by the [Live2D Proprietary Software License](https://www.live2d.com/eula/live2d-proprietary-software-license-agreement_en.html).
The framework remains governed by the [Live2D Open Software License](https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html).
This patch does not grant additional rights to either SDK component or models.

Reproduce from the repository with PowerShell 7:

```powershell
.pixi/envs/default/python.exe scripts/build_live2d_native.py
```

Requires CMake 3.26+, x64 MinGW-w64 tools (`gcc`, `g++`, `gendef`, `dlltool`,
`mingw32-make`) and Windows CPython 3.10+. Pass `--cmake` and
`--toolchain-dir` to select local tools. Only hashed downloads, isolated build
directories and `runtime/tools/live2d_native/opacity-v1` are written. No pip
installation, installed wrapper replacement or application packaging occurs.
The wrapper links Python's stable ABI and the uniquely named
`LpkPreviewCubismCore.dll`; GCC runtime libraries are linked statically.
