# Third-party notices

This directory vendors source from
[wang606/SpineSkeletonDataConverter](https://github.com/wang606/SpineSkeletonDataConverter)
at commit `5ecb2139b0a1af266974f95abeec6bb8562d1249`. Its converter code is
covered by the [PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/);
the complete text is retained in `LICENSE`.

The `spine/` source families retain their upstream per-file **Spine Runtimes
License Agreement** and copyright headers. Those headers must remain with
redistributed source or binaries; see the license reference in the headers and
the pinned source commit recorded in `SOURCE_METADATA.json`.

The retained headers also carry the notices for bundled dependencies:

- `include/json.hpp` is nlohmann/json, MIT licensed.
- `include/stb_image.h`, `stb_image_resize2.h`, and `stb_image_write.h` retain
  their upstream public-domain/MIT alternative license text.

`CMakeLists.txt`, `build_native.ps1`, `SOURCE_METADATA.json`, and the
`spine_converter_capi` wrapper are this project's integration files. The
generated DLL is a local build artifact and is not part of this source notice.
