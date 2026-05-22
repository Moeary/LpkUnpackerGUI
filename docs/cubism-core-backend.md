# Optional Cubism Core backend

Cubism Core is a closed-source native library distributed by Live2D in the
official Cubism SDK packages. The application uses it only as an optional
runtime backend for exporting drawable mesh metadata.

## DLL placement

Development layout:

```text
app/tools/CubismCore/
  Live2DCubismCore.dll
```

Packaged layout:

```text
tools/CubismCore/
  Live2DCubismCore.dll
```

The project does not bundle the DLL in release builds. Users can also point the
application to their own SDK copy:

```powershell
$env:LPK_CUBISM_CORE_DLL = "D:\SDK\CubismSdkForNative\Core\dll\windows\x86_64\Live2DCubismCore.dll"
```

or:

```powershell
$env:LPK_CUBISM_CORE_DIR = "D:\SDK\CubismSdkForNative\Core\dll\windows\x86_64"
```

## What it exports

`app.core.cubism_core` loads `model3.json`, revives the `.moc3` with Core,
initializes a model, resets parameters to defaults, updates it once, then writes
`<model>.drawables.json`.

The sidecar contains:

- canvas size, origin, and pixels-per-unit;
- Cubism Part IDs, parent Part indices, opacity, and offscreen indices when
  exposed by the Core version;
- drawable id;
- texture index;
- pixel-space vertices;
- original Core-space vertices;
- UVs;
- triangle indices;
- draw order and render order;
- opacity;
- mask indices;
- blend flags and blend mode;
- multiply/screen colors when exposed by the Core version.

The PSD full-character mode consumes this sidecar directly. It flips Cubism UVs
into PNG pixel coordinates, applies drawable visibility, opacity, and clipping
masks, groups drawables by broad semantic category and parent Part ID, then
crops rendered layers to their visible alpha bounds while preserving PSD offsets.
Large model PSDs are written with a simple RAW RGBA PSD writer to avoid slow
`psd-tools` RLE encoding on hundreds of layers.

Cubism Core does not expose the original authoring PSD folder tree. Grouping in
exported PSD files is therefore approximate and based on runtime metadata such
as Part IDs, drawable parent Part indices, blend mode, opacity, and drawable
names.

## Command-line check

```powershell
pixi run -e default python scripts/export_cubism_drawables.py path\to\model.model3.json -o output\drawables
```

## Build behavior

`scripts/build_nuitka.py` includes `app/tools/AssetStudioCLI` only. It
intentionally does not include `app/tools/CubismCore`, so an official Core DLL
placed in the development tree is not silently shipped inside release builds.

## Preview note

Cubism Core computes model data but does not implement the full application
preview stack by itself. It does not replace the existing native or web preview
pages yet; those still provide rendering, motion, physics, interaction, and
browser-based inspection.
