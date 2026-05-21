# Live2D Texture Atlas to PSD Notes

Live2D runtime assets cannot restore the original PSD exactly. A `.moc3` model plus `model3.json` and texture atlases can only be used to reconstruct an approximate PSD where each Drawable or ArtMesh is exported as a layer.

The original editable structure lives in the source PSD and Cubism editor project (`.cmo3`). Runtime files are flattened for playback:

- `model3.json` links the model, textures, physics, and related runtime assets.
- `.moc3` contains runtime Drawable data such as vertex positions, UVs, indices, opacity, draw order, and render order.
- texture atlas PNG files contain packed pixels that may be scaled, rotated, trimmed, or rearranged.
- `.cdi3.json` can help with display names, but it does not contain original PSD layer hierarchy.

Only having texture atlas PNG files is not enough to infer original layers. Without `.moc3`, there is no reliable mapping from atlas pixels to Drawable meshes, draw order, masks, blend mode, or part grouping.

Practical reconstruction flow:

1. Read `model3.json` and locate `.moc3`, textures, and optional `.cdi3.json`.
2. Use Live2D Cubism Core or a wrapper such as `live2d-py` to load `.moc3`.
3. For each Drawable, read texture index, vertex positions, UVs, triangle indices, opacity, masks, and render order.
4. Convert UV coordinates to atlas pixel coordinates.
5. Rebuild each Drawable layer by triangle-wise affine warping from atlas pixels to the model canvas.
6. Write a PSD with one RGBA layer per Drawable using `psd-tools`, Pillow, and optionally OpenCV.
7. Validate by compositing the generated PSD and comparing it against a default-pose SDK render.

Expected limitations:

- Original layer groups, hidden layers, masks, effects, and draft layers are not recoverable.
- Atlas scaling or downsampling is irreversible.
- Semi-transparent edges, blend modes, masks, and clipped parts can differ from the original.
- The output is useful for inspection or rough editing, not for recovering the creator's PSD.

References:

- Live2D Cubism file types and embedded data documentation.
- Live2D Source Image and Model Guide Image documentation.
- Live2D Texture Atlas documentation.
- Live2D Native Core API Drawable accessors.
- `psd-tools`, Pillow, and OpenCV for PSD and image reconstruction.
