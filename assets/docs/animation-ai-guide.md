# Live2D / Spine animation MCP: guide for AI clients

<!-- Repository template: Settings and resources/read fill every placeholder with actual connection data and live tool schemas. This source file itself is not a runnable client configuration. -->
{{GUIDE_STATUS}}

## Current connection

```json
{{CONNECTION}}
```

Streamable HTTP is served only on `127.0.0.1` at `/mcp`. Start it in Settings before connecting. Its port belongs to that process; changing a configured port requires stopping and restarting. The default is 8765. Stdio uses the command and args below; the AI client starts and owns that process. Stdio does not use a listening port. The application never modifies client settings automatically.

The generated client configuration uses the common `mcpServers` JSON shape. Different clients use different field names/configuration formats; translate the HTTP URL or stdio command/args/env into the client's documented format.

```json
{{CONFIGURATION}}
```

After `initialize`, use `resources/list` and `resources/read` for `lpk-animation://guide`, then `tools/list` for the authoritative schemas.

## Local paths and persistence

- Absolute input paths can read a chosen local model **outside** the workspace. Relative inputs resolve from the workspace. This is not a sandbox for all reads.
- Every saved output must be a **new directory inside the output workspace**, including resolved links/junctions. Existing directories and source files are never overwritten.
- Model operations edit an in-memory project. `project_id` is valid only in the current server process; stopping/restarting discards unsaved projects. At most 32 projects may be open.
- A saved package includes its model dependencies and `lpk_animation_project.json`. Reopen its returned `model_path` to continue editing. Close in-memory projects only after saving or deliberately discarding their changes.
- Tools accept JSON model/animation data, not shell commands, scripts or arbitrary code.

## Recommended workflow

1. `inspect_model(model_path, parameter_inventory_path?)`: read the actual model type, version, targets, ranges and warnings. Do not guess bone/parameter IDs.
2. `create_project(model_path, parameter_inventory_path?)`: retain the returned `project_id`.
3. `inspect_project(project_id)` and `get_keyframes(project_id, animation_name, target, channel)` before changing an existing track.
4. `create_animation(project_id, name, duration, loop=false, fps=30)` or `clone_animation(project_id, source_name, new_name)`. Existing names cannot be overwritten.
5. `set_keyframes(project_id, animation_name, target, channel, keyframes, mode="replace"|"merge")`. Times are finite seconds, strictly increasing, within `[0,duration]`. Values must be finite numbers; booleans are not numbers. The MCP API authors `linear` or `stepped` interpolation.
6. `save_package(project_id, output_dir)` using a new workspace-relative directory. Return `model_path`, animation paths, warnings and the independent output directory to the user.
7. Preview the saved package in the GUI, check the requested motion and then `close_project(project_id)`.

`replace` rewrites only the selected timeline; other timelines are retained. `merge` combines supported keyframes by time, replacing a frame at an identical time. Existing Bezier/inverse-stepped tracks are preserved, reported `editable=false` and cannot be merged; replacing them explicitly changes their interpolation. GUI curve editing has broader support than this MCP API.

## Live2D runtime limits

Use Cubism Version 3 model JSON with `FileReferences` (commonly `.model3.json`; `.model0.json` is also supported). Parameter IDs and ranges come from the local Cubism Core/MOC, with display names from model metadata. When native Core is unavailable, an explicitly supplied inventory must match the MOC SHA256; inspection reports that fallback. A saved package carries its inventory.

Use `target=<parameter ID>`, `channel="value"` and frames such as `{"time":0.5,"value":-12,"interpolation":"linear"}`. Values are absolute parameter values within the reported range. Existing motion names are `Group[index]`; new motions produce `.motion3.json` and update the copied model's references.

Only existing runtime bindings can move. A model with no leg/crouch parameters cannot acquire a real knee bend by inventing IDs or moving unrelated head parameters. The service does not add ArtMeshes/bindings or recreate a Cubism `.cmo3` authoring project.

## Spine runtime limits

Use Spine `3.8.x` / `4.0.x` skeleton JSON, `.skel`, `.skel.bytes`, or their model wrappers. Binary inputs need this application's native converter; errors are reported when it is unavailable. Saved editing output uses skeleton JSON with atlas, textures and referenced audio preserved. This does not recreate an official `.spine` authoring project.

Use actual bone names. `translate` frames have `time/x/y` (setup-pose offsets), `rotate` frames have `time/angle` (offset degrees), and `scale` frames have `time/x/y` (setup-scale multipliers). The service encodes the version-specific rotation fields. A crouch may need coordinated hip, upper/lower leg and IK changes; inspect the model rather than guessing the rig. Unsupported constraints/deform/events are retained but are not exposed for arbitrary editing.

## Authoritative tool schemas

This section is filled from the registered MCP tools when the guide is generated. `tools/list` on the connected server remains authoritative.

```json
{{TOOL_SCHEMAS}}
```
