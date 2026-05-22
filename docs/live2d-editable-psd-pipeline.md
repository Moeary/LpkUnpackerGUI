# Live2D editable PSD pipeline

## How runtime textures become a moving model

`live2d-py` has two very different code paths:

- Cubism v3 is delegated to the native `_v3cpp.Model` wrapper. The Python API exposes high-level operations such as `LoadModelJson`, `Update`, `Draw`, parameter access, hit testing, and motion playback, but it does not expose drawable vertex arrays, UV arrays, indices, texture indices, draw order, clipping masks, or per-drawable opacity.
- Cubism v2 includes a Python implementation of the old model pipeline. That source is useful as a readable map of the rendering model even though it cannot parse modern `.moc3` data directly.

The v2 path shows the core idea:

1. `live2d/v2/core/draw/mesh.py` reads a mesh with `textureNo`, vertex points, UVs, triangle indices, and interpolation data.
2. `Mesh.setupInterpolate()` evaluates parameter-dependent pivot points into `interpolatedPoints`.
3. `Mesh.setupTransform()` asks parent deformers to transform those points into final per-frame vertices.
4. `live2d/v2/core/deformer/warp_deformer.py` applies grid-based warp deformation to mesh vertices.
5. `live2d/v2/core/model_context.py` updates deformers first, then drawables, sorts by draw order, and calls every mesh draw command.
6. `live2d/v2/core/graphics/draw_param_opengl.py` binds the texture atlas, vertex positions, UV coordinates, and triangle index buffer, then renders triangles with blend mode, opacity, and clipping state.

So the atlas PNG is only the pixel source. The movable part is the drawable mesh: UV triangles sample pixels from the atlas, and parameter/deformer evaluation changes the triangle vertices every frame.

## Reverse feasibility

Recovering the original authoring PSD from runtime files is not fully reversible. Runtime assets usually do not contain original Photoshop groups, hidden paint layers, effect layers, masks, untrimmed art outside the mesh UV area, or the artist's layer naming. A tool can still generate a useful editing PSD, but it is a pseudo-source PSD, not the original source document.

Practical modes:

- **Full-character/scene PSD:** Render each visible drawable mesh into assembled model-pose layers. This is the default UI mode because it is the usable Photoshop view for identifying and painting character parts.
- **Texture atlas layers PSD:** Split texture atlas islands into Photoshop layers at their atlas coordinates. This is reversible because layer pixels map back to the same atlas coordinates used by the model UVs, but it looks like atlas fragments rather than a character.
- **Original PSD recovery:** Not realistic from runtime-only assets. It can only be approximated.

## Current implementation

The experimental PSD tool now writes a `.lpkpsd.json` sidecar next to every generated PSD.

For `mesh` mode:

- If drawable mesh sidecar data exists, the tool warps texture triangles into assembled drawable layers.
- If no sidecar exists, the tool tries the optional Cubism Core backend and exports one first.
- Cubism UVs are converted to PNG pixel coordinates with the V axis flipped.
- Drawable visibility, opacity, and clipping masks are applied before a layer is written.
- Drawable layers are grouped into PSD folders using available Cubism runtime data:
  first broad semantic rules such as background/effects/hit areas, then the
  drawable parent Part ID. This is only an approximate runtime grouping, not
  the original authoring PSD tree.
- Each rendered ArtMesh layer is cropped to its alpha bounding box while keeping its PSD `left`/`top` offset, so Photoshop does not have to load hundreds of full-canvas pixel layers.
- Full-character PSD output uses a simple RAW RGBA PSD writer to avoid slow `psd-tools` RLE encoding on large multi-layer models.
- Mesh PSD output is the preferred editing view, but it is not treated as repackable yet.

For `atlas-components` mode:

- Texture PNG files are loaded with OpenCV.
- Non-transparent connected components are split into individual PSD pixel layers.
- Every layer keeps its atlas `left`/`top` position.
- Metadata records the target texture index, texture dimensions, layer name, layer position, bounding box, and alpha area.
- The reverse operation reads the PSD and metadata, composites each named layer back into the recorded texture canvas, and writes atlas PNG files.

This mode is the safest first target for texture PNG round-trip editing because it preserves the coordinate system that Live2D UVs expect. It is not the right mode for painting on an assembled character.

## Metadata shape

The sidecar format is intentionally simple:

```json
{
  "format": "LpkUnpacker.Live2DAtlasPSD",
  "version": 1,
  "mode": "atlas-components",
  "canvas": { "width": 4096, "height": 4096 },
  "textures": [
    { "index": 0, "name": "texture_00.png", "width": 4096, "height": 4096 }
  ],
  "layers": [
    {
      "kind": "atlas-component",
      "name": "tex00_piece_001",
      "texture_index": 0,
      "left": 120,
      "top": 240,
      "bbox": [120, 240, 160, 80]
    }
  ]
}
```

The reverse packer trusts layer names and current PSD layer offsets. Moving a layer in Photoshop will move that painted island in the output atlas. That can be useful for atlas editing, but it can also break UV sampling if the model expects the old coordinates.

## Mesh PSD round-trip limits

The full-character/scene PSD is a pose-space editing view. Returning edits from
that PSD to the atlas requires inverse triangle warping:

1. Read every drawable layer by name from the PSD.
2. Use the `.lpkpsd.json` sidecar to recover the drawable vertices, UVs,
   texture index, and layer offset.
3. For every triangle, map pixels from posed canvas space back into the atlas
   UV triangle.
4. Merge all edited drawables into the original atlas canvas.

This is possible but not lossless. Overlapping drawables, clipping masks,
semi-transparent effects, blend modes, antialiasing, and pixels that were
hidden in the posed view all need policy decisions. The current implementation
therefore only treats `atlas-components` PSD files as repackable.

Texture format note: official Cubism model setting documentation describes
`.model3.json` as linking texture data with `.png`. Runtime integrations can
load other image formats only if their host loader and renderer support them,
but WebP is not a portable Cubism texture assumption.

## Cubism v3 geometry extraction

To get real ArtMesh layers from `.moc3`, the application now supports an optional
Cubism Core backend. It loads a user-provided `Live2DCubismCore.dll`, revives the
MOC3, updates the default pose, and writes `<model>.drawables.json`.

Supported DLL discovery:

1. `LPK_CUBISM_CORE_DLL`
2. `LPK_CUBISM_CORE_DIR`
3. packaged `tools/CubismCore/Live2DCubismCore.dll`
4. development `app/tools/CubismCore/Live2DCubismCore.dll`

The DLL is not bundled by release builds. See `docs/cubism-core-backend.md`.

Remaining advanced options:

1. Extend or fork `live2d-py` v3 native bindings to expose drawable getters: drawable ids, vertex positions, vertex UVs, indices, texture indices, opacity, render order, draw order, clipping masks, and canvas info.
2. Accept an external mesh sidecar generated by another tool.

Once drawable geometry is available, the same metadata sidecar can support model-pose PSD output plus inverse triangle warping back into the atlas.
