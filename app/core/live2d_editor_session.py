"""Isolated, undoable Cubism editing sessions for the desktop editor.

The GUI can author Bezier motion segments without weakening the narrower MCP
animation API. Runtime assets are copied first; no source file is ever opened
for writing and no Cubism authoring source is invented from a MOC3 binary.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import os
import shutil
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Mapping

from PIL import Image, ImageDraw

from app.core.animation_editing import (
    AnimationEditingError, _motion_meta, _number, _read_json, _relative, _rename_directory,
    _create_live2d_project, _read_validated_cubism_motion,
)
from app.core.model.motions import _evaluate_segments
from app.core.psd_reconstructor import resolve_live2d_source
from app.core.live2d_references import iter_live2d_asset_references
from app.core.live2d_editor_project import Live2DEditorProjects
from app.core.live2d_skins import Live2DSkins


EDITOR_MANIFEST = "lpk_live2d_editor.json"


def _track_mutable(value, changed):
    """Observe nested edits without scanning every motion on each UI poll."""
    if isinstance(value, (_DirtyDict, _DirtyList)) and value._changed is changed:
        return value
    if isinstance(value, dict):
        return _DirtyDict(value, changed)
    if isinstance(value, list):
        return _DirtyList(value, changed)
    if isinstance(value, tuple):
        return tuple(_track_mutable(item, changed) for item in value)
    return value


class _DirtyDict(dict):
    def __init__(self, value, changed):
        self._changed = changed
        dict.__init__(self, ((key, _track_mutable(item, changed)) for key, item in value.items()))

    def __deepcopy__(self, memo):
        result = {}
        memo[id(self)] = result
        result.update((copy.deepcopy(key, memo), copy.deepcopy(value, memo)) for key, value in self.items())
        return result

    def __setitem__(self, key, value):
        dict.__setitem__(self, key, _track_mutable(value, self._changed))
        self._changed()

    def __delitem__(self, key):
        dict.__delitem__(self, key)
        self._changed()

    def clear(self):
        if self:
            dict.clear(self)
            self._changed()

    def pop(self, key, *default):
        existed = key in self
        result = dict.pop(self, key, *default)
        if existed:
            self._changed()
        return result

    def popitem(self):
        result = dict.popitem(self)
        self._changed()
        return result

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def update(self, *args, **kwargs):
        incoming = dict(*args, **kwargs)
        if incoming:
            dict.update(self, ((key, _track_mutable(value, self._changed)) for key, value in incoming.items()))
            self._changed()

    def __ior__(self, value):
        self.update(value)
        return self


class _DirtyList(list):
    def __init__(self, value, changed):
        self._changed = changed
        list.__init__(self, (_track_mutable(item, changed) for item in value))

    def __deepcopy__(self, memo):
        result = []
        memo[id(self)] = result
        result.extend(copy.deepcopy(item, memo) for item in self)
        return result

    def __setitem__(self, index, value):
        value = [_track_mutable(item, self._changed) for item in value] if isinstance(index, slice) else _track_mutable(value, self._changed)
        list.__setitem__(self, index, value)
        self._changed()

    def __delitem__(self, index):
        list.__delitem__(self, index)
        self._changed()

    def append(self, value):
        list.append(self, _track_mutable(value, self._changed))
        self._changed()

    def extend(self, values):
        incoming = [_track_mutable(value, self._changed) for value in values]
        if incoming:
            list.extend(self, incoming)
            self._changed()

    def insert(self, index, value):
        list.insert(self, index, _track_mutable(value, self._changed))
        self._changed()

    def pop(self, index=-1):
        result = list.pop(self, index)
        self._changed()
        return result

    def remove(self, value):
        list.remove(self, value)
        self._changed()

    def clear(self):
        if self:
            list.clear(self)
            self._changed()

    def reverse(self):
        list.reverse(self)
        self._changed()

    def sort(self, *args, **kwargs):
        list.sort(self, *args, **kwargs)
        self._changed()

    def __iadd__(self, values):
        self.extend(values)
        return self

    def __imul__(self, count):
        list.__imul__(self, count)
        self._changed()
        return self


def decode_curve(segments: list[Any]) -> list[dict[str, Any]]:
    """Decode Cubism segments to timeline frames, retaining Bezier handles."""
    if len(segments) < 2:
        raise AnimationEditingError("Cubism curve has no first keyframe.")
    frames = [{"time": float(segments[0]), "value": float(segments[1]),
               "interpolation": "linear"}]
    cursor = 2
    while cursor < len(segments):
        kind = int(segments[cursor])
        length = 6 if kind == 1 else 2
        points = [float(value) for value in segments[cursor + 1:cursor + 1 + length]]
        if len(points) != length or kind not in (0, 1, 2, 3):
            raise AnimationEditingError("Invalid Cubism segment.")
        previous = frames[-1]
        time, value = points[-2:]
        previous["interpolation"] = {0: "linear", 1: "bezier", 2: "stepped", 3: "inverse_stepped"}[kind]
        if kind == 1:
            delta_time = time - previous["time"]
            delta_value = value - previous["value"]
            scale = delta_value if abs(delta_value) > 1e-12 else 1.0
            previous["bezier"] = {"value": [
                (points[0] - previous["time"]) / delta_time,
                (points[1] - previous["value"]) / scale,
                (points[2] - previous["time"]) / delta_time,
                (points[3] - previous["value"]) / scale,
            ]}
            if abs(delta_value) <= 1e-12:
                previous["bezier_value_scale"] = {"value": scale}
        frames.append({"time": time, "value": value, "interpolation": "linear"})
        cursor += length + 1
    return frames


def encode_curve(frames: list[Mapping[str, Any]], duration: float,
                 parameter: Mapping[str, Any]) -> list[float | int]:
    """Validate and encode linear, stepped, inverse stepped and Bezier keys."""
    if not frames:
        return []
    validated = []
    last = -1.0
    for frame in frames:
        time = _number(frame.get("time"), "time")
        value = _number(frame.get("value"), "value")
        if not last < time <= duration or time < 0:
            raise AnimationEditingError("Keyframe times must increase strictly within the motion duration.")
        if not parameter["min"] <= value <= parameter["max"]:
            raise AnimationEditingError(f"Value outside parameter range [{parameter['min']}, {parameter['max']}].")
        interpolation = frame.get("interpolation", "linear")
        if interpolation not in {"linear", "stepped", "inverse_stepped", "bezier"}:
            raise AnimationEditingError("Unsupported Cubism interpolation.")
        validated.append(dict(frame, time=time, value=value, interpolation=interpolation))
        last = time
    segments: list[float | int] = [validated[0]["time"], validated[0]["value"]]
    for previous, frame in zip(validated, validated[1:]):
        interpolation = previous["interpolation"]
        if interpolation == "bezier":
            handles = previous.get("bezier", {}).get("value", [.25, .25, .75, .75])
            if not isinstance(handles, (tuple, list)) or len(handles) != 4:
                raise AnimationEditingError("Bezier requires four control-point coordinates.")
            x1, y1, x2, y2 = [_number(point, "Bezier control point") for point in handles]
            if not 0 <= x1 <= x2 <= 1:
                raise AnimationEditingError("Bezier control-point times must be ordered inside the segment.")
            dt = frame["time"] - previous["time"]
            dv = frame["value"] - previous["value"]
            scale = previous.get("bezier_value_scale", {}).get("value", dv if abs(dv) > 1e-12 else 1.0)
            scale = _number(scale, "Bezier value scale")
            segments.extend([1, previous["time"] + x1 * dt, previous["value"] + y1 * scale,
                             previous["time"] + x2 * dt, previous["value"] + y2 * scale,
                             frame["time"], frame["value"]])
        else:
            segments.extend([{"linear": 0, "stepped": 2, "inverse_stepped": 3}[interpolation],
                             frame["time"], frame["value"]])
    return segments


class Live2DEditorSession:
    def __init__(self, source: str | os.PathLike[str]):
        info = resolve_live2d_source(Path(source))
        self.source_path = info.model_json.resolve()
        self.source_root = self.source_path.parent
        self.original_document = _read_json(self.source_path)
        if self.original_document.get("Version") != 3 or not isinstance(self.original_document.get("FileReferences"), dict):
            raise AnimationEditingError("Choose a Cubism 3 model settings JSON.")
        self._temporary = tempfile.TemporaryDirectory(prefix="lpk-live2d-editor-")
        self.root = Path(self._temporary.name) / "model"
        self.root.mkdir()
        self.model_path = self.root / "model.json"
        self._project_revision = 0
        self._signature_revision = 0
        self._signature_cache = None
        self._asset_signature_cache = None
        self._signature_changed = self._invalidate_signature
        self._pose_changed = self._invalidate_pose_signature
        self._parameter_preview: dict[str, float] = {}
        self._local_textures = OrderedDict()
        self._local_crops = OrderedDict()
        self._projects = Live2DEditorProjects(self)
        self.original_bindings: dict[tuple[str, int], int] = {}
        self._deleted_original_bindings: set[tuple[str, int]] = set()
        self._copied: set[str] = set()
        self.warnings: list[str] = []
        try:
            document = copy.deepcopy(self.original_document)
            self._copy_references(document["FileReferences"])
            for required in [document["FileReferences"].get("Moc"), *document["FileReferences"].get("Textures", [])]:
                self._copy_asset(required, required=True)
            groups = document["FileReferences"].get("Motions", {})
            if not isinstance(groups, dict):
                raise AnimationEditingError("Live2D motion groups must be an object.")
            playable = {}
            validated_motions = {}
            for group, entries in groups.items():
                valid = []
                for original_index, item in enumerate(entries if isinstance(entries, list) else []):
                    if not isinstance(item, dict) or not item.get("File"):
                        continue
                    relative = _relative(item["File"])
                    if not (self.root / relative).is_file():
                        self.warnings.append(f"Unplayable motion preserved in export: {group}[{original_index}]")
                        continue
                    try:
                        validated = validated_motions.get(relative)
                        if validated is None:
                            validated = _read_validated_cubism_motion(self.root / relative)
                            validated_motions[relative] = validated
                            if validated.metadata_changed:
                                self._write_json(self.root / relative, validated.data)
                    except AnimationEditingError as exc:
                        self.warnings.append(f"Uneditable motion preserved in export: {group}[{original_index}]: {exc}")
                        continue
                    self.original_bindings[(str(group), len(valid))] = original_index
                    valid.append({key: value for key, value in item.items()
                                  if key in {"File", "Sound", "Name", "FadeInTime", "FadeOutTime"}})
                if valid:
                    playable[str(group)] = valid
            document["FileReferences"]["Motions"] = playable
            self._write_json(self.model_path, document)
            inventory = self.source_root / "live2d_parameter_inventory.json"
            if inventory.is_file():
                shutil.copy2(inventory, self.root / inventory.name)
            self.project = _create_live2d_project(self.model_path, document, validated_motions=validated_motions)
            self.project.references.update({rel: self.root / rel for rel in self._copied})
            self.warnings.extend(self.project.warnings)
            self.texture_paths = [self.root / _relative(rel) for rel in document["FileReferences"].get("Textures", [])]
            self.texture_sizes = [self._image_size(path) for path in self.texture_paths]
            self._texture_data = [path.read_bytes() for path in self.texture_paths]
            self._skins = Live2DSkins(self)
            self.parameters = self.project.parameters
            self._parameters_by_id = {parameter["id"]: parameter for parameter in self.parameters}
            self.parameter_overrides: dict[str, float] = {}
            self.part_overrides: dict[str, float] = {}
            self._core_model = None
            self.mesh_data = copy.deepcopy(info.mesh_data)
            try:
                from app.core.cubism_core import CubismCore
                self._core_model = CubismCore().load_moc(self.root / _relative(document["FileReferences"]["Moc"]))
                self.mesh_data = self._core_model.drawable_snapshot()
            except Exception as exc:
                if not self.mesh_data:
                    self.warnings.append(f"ArtMesh geometry unavailable: {exc}")
            manifest = self.source_root / EDITOR_MANIFEST
            if manifest.is_file():
                state = _read_json(manifest)
                if state.get("format") == "LpkUnpacker.Live2DEditor" and state.get("version") in (1, 2):
                    known = {p["id"]: p for p in self.parameters}
                    self.parameter_overrides = {k: float(v) for k, v in state.get("pose_parameters", {}).items()
                                                if k in known and known[k]["min"] <= float(v) <= known[k]["max"]}
                    self.part_overrides = {k: float(v) for k, v in state.get("preview_part_opacity", {}).items()
                                           if math.isfinite(float(v)) and 0 <= float(v) <= 1}
                    self._projects.restore(self.source_root, state)
                    if state.get("skins") is not None:
                        self._skins.restore(self.source_root, state["skins"])
            self._undo: list[dict] = []
            self._redo: list[dict] = []
            self._saved_signature = self._signature()
        except Exception:
            self.close()
            raise

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")

    @staticmethod
    def _image_size(path: Path) -> tuple[int, int]:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            return image.size

    def _copy_asset(self, value: Any, required: bool = False) -> bool:
        relative = _relative(value)
        path = (self.source_root / relative).resolve()
        if not path.is_relative_to(self.source_root) or not path.is_file():
            if required:
                raise AnimationEditingError(f"Missing or escaping Live2D asset: {value}")
            return False
        target = self.root / relative
        if relative in self._copied:
            return True
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        self._copied.add(relative)
        return True

    def _copy_references(self, value: Any, key: str = "") -> None:
        for relative in iter_live2d_asset_references(value, key):
            self._copy_asset(relative)

    def _snapshot_values(self) -> dict:
        project = self.project
        return {"motions": project.motions, "document": project.document,
                "bindings": project.bindings, "settings": project.settings,
                "modified": project.modified, "parameters": self.parameter_overrides,
                "parts": self.part_overrides, "textures": self._texture_data,
                "skins": self._skins.state,
                "original_bindings": self.original_bindings,
                "deleted_original_bindings": self._deleted_original_bindings}

    def _snapshot(self) -> dict:
        return copy.deepcopy(self._snapshot_values())

    def _restore(self, state: dict) -> None:
        state = copy.deepcopy(state)
        written = []
        previous_textures = list(self._texture_data)
        try:
            for index, (path, data, old_data) in enumerate(zip(self.texture_paths, state["textures"], previous_textures)):
                if data != old_data:
                    self._write_texture(path, data)
                    written.append(index)
        except Exception:
            for index in reversed(written):
                self._write_texture(self.texture_paths[index], previous_textures[index])
            raise
        for name in ("motions", "document", "bindings", "settings", "modified"):
            setattr(self.project, name, state[name])
        self.parameter_overrides = state["parameters"]
        self.part_overrides = state["parts"]
        self._texture_data = state["textures"]
        self._skins.state = state["skins"]
        self.original_bindings = state["original_bindings"]
        self._deleted_original_bindings = state["deleted_original_bindings"]
        self._invalidate_signature()

    def _invalidate_signature(self):
        self._signature_revision += 1
        self._signature_cache = None
        self._asset_signature_cache = None

    def _invalidate_pose_signature(self):
        # Pose edits do not alter any model, motion or texture bytes. Reuse
        # their digest instead of hashing the entire project for every drag.
        self._signature_cache = None

    def _observe_signature_values(self):
        # Also recognize direct attribute replacement by advanced editor code.
        # Dict/list wrappers recursively observe direct curve/pose changes;
        # small set fields are compared by value below.
        for name in ("motions", "document", "bindings", "settings"):
            value = getattr(self.project, name)
            tracked = _track_mutable(value, self._signature_changed)
            if tracked is not value:
                setattr(self.project, name, tracked)
                self._invalidate_signature()
        for name in ("_texture_data", "original_bindings"):
            value = getattr(self, name)
            tracked = _track_mutable(value, self._signature_changed)
            if tracked is not value:
                setattr(self, name, tracked)
                self._invalidate_signature()
        for name in ("parameter_overrides", "part_overrides"):
            value = getattr(self, name)
            tracked = _track_mutable(value, self._pose_changed)
            if tracked is not value:
                setattr(self, name, tracked)
                self._invalidate_pose_signature()

    def _signature(self) -> str:
        self._observe_signature_values()
        token = (self._signature_revision, self._project_revision,
                 frozenset(self.project.modified), frozenset(self._deleted_original_bindings))
        if self._signature_cache is not None and self._signature_cache[0] == token:
            return self._signature_cache[1]
        if self._asset_signature_cache is None or self._asset_signature_cache[0] != token:
            snapshot = self._snapshot_values()
            snapshot.pop("parameters")
            snapshot.pop("parts")
            snapshot["modified"] = sorted(snapshot["modified"])
            snapshot["textures"] = [hashlib.sha256(data).hexdigest() for data in snapshot["textures"]]
            snapshot["original_bindings"] = sorted((group, index, original) for (group, index), original in self.original_bindings.items())
            snapshot["deleted_original_bindings"] = sorted(self._deleted_original_bindings)
            snapshot["project_revision"] = self._project_revision
            asset_signature = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            self._asset_signature_cache = token, asset_signature
        pose = json.dumps([self.parameter_overrides, self.part_overrides], sort_keys=True, allow_nan=False)
        signature = hashlib.sha256((self._asset_signature_cache[1] + pose).encode()).hexdigest()
        self._signature_cache = token, signature
        return signature

    def _record(self, action) -> Any:
        self.commit_parameter_preview()
        previous = self._snapshot()
        try:
            result = action()
        except Exception:
            self._restore(previous)
            raise
        if previous != self._snapshot_values():
            self._invalidate_signature()
            self._undo.append(previous)
            del self._undo[:-32]
            self._redo.clear()
        return result

    @property
    def dirty(self) -> bool:
        return self.parameter_preview_pending or self._signature() != self._saved_signature

    def _pose_history(self) -> dict:
        return {"_history_kind": "pose", "parameters": dict(self.parameter_overrides),
                "parts": dict(self.part_overrides)}

    def _restore_pose(self, state: dict) -> None:
        self.parameter_overrides = _track_mutable(dict(state["parameters"]), self._pose_changed)
        self.part_overrides = _track_mutable(dict(state["parts"]), self._pose_changed)
        self._invalidate_pose_signature()

    def _record_pose(self, action) -> Any:
        previous = self._pose_history()
        try:
            result = action()
        except Exception:
            self._restore_pose(previous)
            raise
        if previous != self._pose_history():
            self._invalidate_pose_signature()
            self._undo.append(previous)
            del self._undo[:-32]
            self._redo.clear()
        return result

    def _parameter_number(self, parameter_id: str, value: float) -> float:
        parameter = self._parameters_by_id.get(parameter_id)
        number = _number(value, "parameter value")
        if parameter is None or not parameter["min"] <= number <= parameter["max"]:
            raise AnimationEditingError("Parameter value is outside the model range.")
        return number

    @property
    def parameter_preview_pending(self) -> bool:
        return bool(self._parameter_preview)

    def preview_parameter(self, parameter_id: str, value: float) -> None:
        """Stage a validated drag value without a snapshot or asset digest."""
        number = self._parameter_number(parameter_id, value)
        if self.parameter_overrides.get(parameter_id) == number:
            self._parameter_preview.pop(parameter_id, None)
        else:
            self._parameter_preview[parameter_id] = number

    def commit_parameter_preview(self) -> bool:
        """Commit the final values of one parameter gesture as one undo step."""
        if not self._parameter_preview:
            return False
        pending = dict(self._parameter_preview)
        self._record_pose(lambda: self.parameter_overrides.update(pending))
        self._parameter_preview.clear()
        return True

    @property
    def active_skin_id(self):
        return self._skins.active_id

    @property
    def skin_modified(self):
        return self._skins.modified

    def list_skins(self):
        return self._skins.list()

    def inspect_skin_source(self, source):
        return self._skins.inspect_source(source)

    def skin_texture_paths(self, skin_id):
        return self._skins.paths(skin_id)

    def import_skin(self, source, name=None, *, mapping=None, source_kind="model",
                    source_metadata=None, metadata=None, activate=False):
        return self._skins.import_skin(source, name, mapping=mapping, source_kind=source_kind,
                                       metadata=metadata if metadata is not None else source_metadata, activate=activate)

    def capture_skin(self, name, *, source_kind="current", source_metadata=None, metadata=None):
        return self._skins.capture(name, source_kind=source_kind,
                                  metadata=metadata if metadata is not None else source_metadata)

    def clone_skin(self, skin_id, name):
        return self._skins.clone(skin_id, name)

    def rename_skin(self, skin_id, name):
        return self._skins.rename(skin_id, name)

    def remove_skin(self, skin_id):
        return self._skins.remove(skin_id)

    delete_skin = remove_skin

    def apply_skin(self, skin_id):
        return self._skins.apply(skin_id)

    def export_skin(self, skin_id, output_dir):
        return self._skins.export_skin(skin_id, output_dir)

    def export_viewerex(self, skin_ids, output_dir, *, trigger_id=None):
        return self._skins.export_viewerex(skin_ids, output_dir, trigger_id=trigger_id)

    @property
    def psd_project(self):
        return self._projects.get("psd")

    @property
    def mod_project(self):
        return self._projects.get("mods")

    @property
    def workspace_dirty(self):
        return self.dirty

    def mark_project_changed(self):
        self._project_revision += 1
        self._invalidate_signature()

    mark_workspace_dirty = mark_project_changed

    def ensure_psd_project(self):
        return self._projects.ensure("psd")

    def ensure_mod_project(self):
        return self._projects.ensure("mods")

    def attach_psd_project(self, source):
        return self._projects.attach("psd", source)

    def attach_mod_project(self, source):
        return self._projects.attach("mods", source)

    import_psd_project = attach_psd_project
    import_mod_project = attach_mod_project

    def bind_psd_project(self, project):
        return self._projects.bind("psd", project)

    def bind_mod_project(self, project):
        return self._projects.bind("mods", project)

    def detach_psd_project(self):
        return self._projects.detach("psd")

    def detach_mod_project(self):
        return self._projects.detach("mods")

    def flush_projects(self, psd_project=None, mod_project=None):
        if psd_project is not None:
            self.bind_psd_project(psd_project)
        if mod_project is not None:
            self.bind_mod_project(mod_project)

    @property
    def can_undo(self) -> bool:
        return self.parameter_preview_pending or bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return not self.parameter_preview_pending and bool(self._redo)

    def undo(self) -> bool:
        self.commit_parameter_preview()
        if not self._undo:
            return False
        state = self._undo[-1]
        pose = state.get("_history_kind") == "pose"
        current = self._pose_history() if pose else self._snapshot()
        (self._restore_pose if pose else self._restore)(state)
        self._undo.pop()
        self._redo.append(current)
        return True

    def redo(self) -> bool:
        self.commit_parameter_preview()
        if not self._redo:
            return False
        state = self._redo[-1]
        pose = state.get("_history_kind") == "pose"
        current = self._pose_history() if pose else self._snapshot()
        (self._restore_pose if pose else self._restore)(state)
        self._redo.pop()
        self._undo.append(current)
        return True

    def create_motion(self, name: str, duration: float, loop: bool = True) -> dict:
        return self._record(lambda: self.project.create_animation(name, duration=duration, loop=loop))

    def clone_motion(self, source: str, name: str) -> dict:
        return self._record(lambda: self.project.clone_animation(source, name))

    def motion_catalog(self) -> list[dict[str, Any]]:
        """List editable motions alongside preserved, read-only Viewer entries."""
        by_original = {(group, original): name
                       for name, (group, index) in self.project.bindings.items()
                       if (original := self.original_bindings.get((group, index))) is not None}
        result = []
        registered = set()
        for group, entries in self.original_document["FileReferences"].get("Motions", {}).items():
            for index, entry in enumerate(entries if isinstance(entries, list) else []):
                if (group, index) in self._deleted_original_bindings:
                    continue
                name = by_original.get((group, index))
                editable = name is not None
                display_name = str(entry.get("Name", "")) if isinstance(entry, dict) else ""
                key = name if editable else f"command:{group}[{index}]"
                result.append({"name": key, "label": name or f"{group}[{index}]" + (f" · {display_name}" if display_name else ""),
                               "group": group, "index": self.project.bindings[name][1] if editable else index,
                               "original_index": index, "editable": editable,
                               "kind": "motion" if editable else "uneditable_motion" if isinstance(entry, dict) and entry.get("File") else "command",
                               "entry": copy.deepcopy(entry)})
                if editable:
                    registered.add(name)
        for name, (group, index) in self.project.bindings.items():
            if name not in registered:
                result.append({"name": name, "label": name, "group": group, "index": index,
                               "original_index": None, "editable": True, "kind": "motion", "entry": {}})
        return result

    def delete_motion(self, name: str) -> bool:
        """Delete one playable entry, preserving commands and shared assets."""
        self.project._animation(name)
        group, index = self.project.bindings[name]

        def apply():
            original_index = self.original_bindings.pop((group, index), None)
            if original_index is not None:
                self._deleted_original_bindings.add((group, original_index))
            del self.project.motions[name]
            del self.project.bindings[name]
            self.project.settings.pop(name, None)
            self.project.modified.discard(name)
            entries = self.project.document["FileReferences"].get("Motions", {}).get(group, [])
            if index < len(entries):
                entries.pop(index)
            for other, (other_group, other_index) in list(self.project.bindings.items()):
                if other_group == group and other_index > index:
                    self.project.bindings[other] = group, other_index - 1
            self.original_bindings = {(entry_group, entry_index - 1 if entry_group == group and entry_index > index else entry_index): original
                                      for (entry_group, entry_index), original in self.original_bindings.items()}
            return True

        return self._record(apply)

    def keyframes(self, motion: str, parameter: str) -> list[dict]:
        animation = self.project._animation(motion)
        curve = next((c for c in animation["Curves"] if c["Target"] == "Parameter" and c["Id"] == parameter), None)
        return decode_curve(curve["Segments"]) if curve else []

    def set_keyframes(self, motion: str, parameter_id: str, frames: list[dict]) -> None:
        parameter = next((p for p in self.parameters if p["id"] == parameter_id), None)
        if parameter is None:
            raise AnimationEditingError(f"Unknown parameter: {parameter_id}")
        animation = self.project._animation(motion)
        segments = encode_curve(frames, float(animation["Meta"]["Duration"]), parameter)
        def apply():
            curves = animation["Curves"]
            index = next((i for i, c in enumerate(curves) if c["Target"] == "Parameter" and c["Id"] == parameter_id), None)
            if not segments:
                if index is not None:
                    curves.pop(index)
            elif index is None:
                curves.append({"Target": "Parameter", "Id": parameter_id, "Segments": segments})
            else:
                curves[index] = dict(curves[index], Segments=segments)
            _motion_meta(animation)
            self.project.modified.add(motion)
        self._record(apply)

    def set_parameter(self, parameter_id: str, value: float) -> None:
        number = self._parameter_number(parameter_id, value)
        self.commit_parameter_preview()
        self._record_pose(lambda: self.parameter_overrides.__setitem__(parameter_id, number))

    def set_part_opacity(self, part_id: str, value: float) -> None:
        number = _number(value, "part opacity")
        if not 0 <= number <= 1:
            raise AnimationEditingError("Part opacity must be between zero and one.")
        self.commit_parameter_preview()
        self._record_pose(lambda: self.part_overrides.__setitem__(part_id, number))

    def pose_at(self, motion: str | None, seconds: float, *, overrides: bool = False) -> dict[str, float]:
        values = {p["id"]: float(p["default"]) for p in self.parameters}
        if motion and motion in self.project.motions:
            for curve in self.project.motions[motion]["Curves"]:
                if curve["Target"] == "Parameter":
                    value = _evaluate_segments(curve["Segments"], max(0.0, float(seconds)))
                    if value is not None:
                        values[curve["Id"]] = value
        if overrides:
            values.update(self.parameter_overrides)
            values.update(self._parameter_preview)
        return values

    def snapshot_mesh(self, parameters: dict[str, float]) -> dict:
        if self._core_model:
            self.mesh_data = self._core_model.drawable_snapshot(parameters)
        return self.mesh_data or {}

    def exact_pose_mesh(self, parameters: Mapping[str, float], parts: Mapping[str, float] | None = None) -> dict:
        """Read geometry from a displayed pose without clamping physics values.

        The renderer supplies actual parameter values. Cubism Core's ordinary
        authoring helper clamps requested values, so copy them directly into
        its arrays before the pure Core update (without SDK Physics/Pose).
        Part values are only those the renderer can read or explicitly apply.
        All drawables remain in the snapshot to keep mask indices meaningful.
        """
        parameters = {str(key): _number(value, "displayed parameter") for key, value in parameters.items()}
        parts = {str(key): _number(value, "displayed part opacity") for key, value in (parts or {}).items()}
        model = self._core_model
        if model:
            core, pointer = model.core, model.model_pointer
            dll = core.dll
            ids = dll.csmGetParameterIds(pointer)
            values = dll.csmGetParameterValues(pointer)
            identifiers = [ids[index].decode("utf-8", errors="replace")
                           for index in range(max(0, int(dll.csmGetParameterCount(pointer))))]
            if any(identifier not in parameters for identifier in identifiers):
                raise AnimationEditingError("Selection requires the complete displayed parameter pose.")
            previous_values = [float(values[index]) for index in range(len(identifiers))]
            part_ids, part_values = core.get_part_ids(pointer), core.get_part_opacities(pointer)
            previous_parts = [float(part_values[index]) for index in range(core.get_part_count(pointer))] if part_values else []
            try:
                for index, identifier in enumerate(identifiers):
                    values[index] = parameters[identifier]
                if part_ids and part_values:
                    for index in range(len(previous_parts)):
                        identifier = part_ids[index].decode("utf-8", errors="replace")
                        if identifier in parts:
                            part_values[index] = parts[identifier]
                snapshot = model.drawable_snapshot()
            finally:
                for index, value in enumerate(previous_values):
                    values[index] = value
                for index, value in enumerate(previous_parts):
                    part_values[index] = value
                dll.csmUpdateModel(pointer)
        else:
            snapshot = copy.deepcopy(self.mesh_data or {})
            snapshot["parameters"] = parameters
            for part in snapshot.get("parts", []):
                if part["id"] in parts:
                    part["opacity"] = parts[part["id"]]
        # The SDK renderer also suppresses drawables belonging to hidden parts.
        # Explicit zero overrides must remain hidden for picking and PSD export,
        # even with a sidecar or a Core build that reports raw drawable opacity.
        hidden = {key for key, value in parts.items() if value <= .001}
        for drawable in snapshot.get("drawables", []):
            if drawable.get("parent_part_id") in hidden:
                drawable["opacity"] = 0.0
        return snapshot

    @staticmethod
    def _write_texture(path: Path, data: bytes) -> None:
        temporary = path.with_name(path.name + ".lpkedit-tmp")
        temporary.write_bytes(data)
        try:
            for attempt in range(6):
                try:
                    temporary.replace(path)
                    return
                except PermissionError:
                    if os.name != "nt" or attempt == 5:
                        raise
                    time.sleep(.2)
        finally:
            if temporary.exists():
                temporary.unlink()

    def replace_texture(self, index: int, source: str | Path) -> bool:
        return self.replace_textures({index: source})

    def replace_textures(self, sources: Mapping[int, str | Path]) -> bool:
        replacements = {}
        for index, source in sources.items():
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(self.texture_paths):
                raise AnimationEditingError(f"Unknown texture index: {index}")
            with Image.open(source) as image:
                if image.size != self.texture_sizes[index]:
                    raise AnimationEditingError(f"Texture size must remain {self.texture_sizes[index][0]} × {self.texture_sizes[index][1]}.")
                buffer = io.BytesIO()
                image.convert("RGBA").save(buffer, format="PNG")
                data = buffer.getvalue()
            if data != self._texture_data[index]:
                replacements[index] = data
        if not replacements:
            return False
        def apply():
            for index, data in replacements.items():
                self._write_texture(self.texture_paths[index], data)
                self._texture_data[index] = data
        self._record(apply)
        return True

    def validate_texture_package(self, source: str | Path):
        """Validate a PSD/MOD preview package against this model before use."""
        info = resolve_live2d_source(Path(source))
        moc = self.root / _relative(self.original_document["FileReferences"]["Moc"])
        if info.moc3 is None or not info.moc3.is_file() or hashlib.sha256(info.moc3.read_bytes()).digest() != hashlib.sha256(moc.read_bytes()).digest():
            raise AnimationEditingError("Texture package belongs to a different MOC3 model.")
        if len(info.textures) != len(self.texture_paths):
            raise AnimationEditingError("Texture package must contain the same texture count.")
        expected = [path.relative_to(self.root).as_posix() for path in self.texture_paths]
        actual = [path.relative_to(info.root_dir).as_posix() for path in info.textures]
        if actual != expected:
            raise AnimationEditingError("Texture package must retain the model's texture order and paths.")
        for index, path in enumerate(info.textures):
            if self._image_size(path) != self.texture_sizes[index]:
                raise AnimationEditingError(f"Texture package size changed at index {index}.")
        return info

    def apply_texture_package(self, source: str | Path) -> bool:
        info = self.validate_texture_package(source)
        return self.replace_textures(dict(enumerate(info.textures)))

    def accept_texture_change(self, index: int) -> bool:
        path = self.texture_paths[index]
        data = path.read_bytes()
        if data == self._texture_data[index]:
            return False
        if self._image_size(path) != self.texture_sizes[index]:
            self._write_texture(path, self._texture_data[index])
            raise AnimationEditingError("External editor changed the texture dimensions; the last valid copy was restored.")
        self._record(lambda: self._texture_data.__setitem__(index, data))
        return True

    def restore_texture(self, index: int) -> None:
        """Restore the last accepted version after a failed/partial external save."""
        self._write_texture(self.texture_paths[index], self._texture_data[index])

    def local_artmesh_image(self, drawable: Mapping[str, Any], *, max_size=(480, 360)) -> Image.Image | None:
        """Extract the selected ArtMesh footprint, using its actual UV triangles."""
        index = int(drawable.get("texture_index", -1))
        if not 0 <= index < len(self.texture_paths):
            return None
        path = self.texture_paths[index]
        try:
            stamp = path.stat()
            file_stamp = (str(path), stamp.st_mtime_ns, stamp.st_size)
        except OSError:
            file_stamp = (str(path), None, None)
        data = self._texture_data[index]
        token = (*file_stamp, id(data))
        # The watcher accepts a complete image before replacing these bytes.
        # A half-written external PNG must never replace a valid local preview.
        cached = self._local_textures.get(index)
        if cached is None or cached[0] != token:
            with Image.open(io.BytesIO(data)) as texture:
                image = texture.convert("RGBA")
            self._local_textures[index] = (token, image)
        else:
            image = cached[1]
        self._local_textures.move_to_end(index)
        while len(self._local_textures) > 2:
            self._local_textures.popitem(last=False)
        width, height = image.size
        uvs = drawable.get("uvs", [])
        indices = drawable.get("indices", [])
        key = (index, token, tuple(tuple(uv) for uv in uvs), tuple(indices),
               tuple(max_size) if max_size is not None else None)
        if key in self._local_crops:
            self._local_crops.move_to_end(key)
            cached = self._local_crops[key]
            return cached.copy() if cached is not None else None
        points = [(float(u) * width, (1 - float(v)) * height) for u, v in uvs]
        triangles = [indices[offset:offset + 3] for offset in range(0, len(indices) - 2, 3)]
        triangles = [triangle for triangle in triangles if all(0 <= int(i) < len(points) for i in triangle)]
        vertices = [points[int(index)] for triangle in triangles for index in triangle]
        if not vertices:
            return None
        left = max(0, math.floor(min(point[0] for point in vertices)))
        top = max(0, math.floor(min(point[1] for point in vertices)))
        right = min(width, math.ceil(max(point[0] for point in vertices)) + 1)
        bottom = min(height, math.ceil(max(point[1] for point in vertices)) + 1)
        if right <= left or bottom <= top:
            return None
        mask = Image.new("L", (right - left, bottom - top))
        painter = ImageDraw.Draw(mask)
        for triangle in triangles:
            painter.polygon([(points[int(i)][0] - left, points[int(i)][1] - top) for i in triangle], fill=255)
        bounds = mask.getbbox()
        if bounds is None:
            return None
        from PIL import ImageChops
        result = image.crop((left, top, right, bottom))
        result.putalpha(ImageChops.multiply(result.getchannel("A"), mask))
        result = result.crop(bounds)
        if max_size is not None:
            result.thumbnail(max_size)
            # Cache only small previews. Full-resolution popup crops and large
            # custom sizes cannot accumulate hundreds of atlas-sized images.
            if result.width * result.height * 4 <= 2 * 1024 * 1024:
                self._local_crops[key] = result.copy()
                while len(self._local_crops) > 24:
                    self._local_crops.popitem(last=False)
        return result

    def write_inspector_metadata(self, parameters: dict[str, float]) -> Path:
        mesh = self.snapshot_mesh(parameters)
        sidecar = self.model_path.with_name(f"{self.model_path.stem}.drawables.json")
        self._write_json(sidecar, mesh)
        textures = [{"index": index, "relative_path": path.relative_to(self.root).as_posix(),
                     "width": size[0], "height": size[1]} for index, (path, size) in enumerate(zip(self.texture_paths, self.texture_sizes))]
        metadata = {"format": "LpkUnpacker.Live2DAtlasPSD", "version": 2, "mode": "atlas-artmesh",
                    "source_root": str(self.root), "source_model": str(self.model_path),
                    "canvas": mesh.get("canvas", {}), "textures": textures}
        path = self.root / "editor_artmesh.lpkpsd.json"
        self._write_json(path, metadata)
        return path

    def export_snapshot(self, output_dir: str | Path) -> Path:
        output = Path(output_dir).expanduser().resolve()
        if not output.is_relative_to(self.root):
            return Path(self.save_copy(output, include_projects=False, include_skins=False, mark_saved=False)["model_path"])
        if output.exists() or output.is_relative_to(self.source_root):
            raise AnimationEditingError("Create a snapshot in a new editor workspace directory.")
        # The animation exporter forbids a destination inside its input root.
        # Export independently first, then publish the immutable snapshot into
        # a new managed directory without consuming the editor's dirty state.
        with tempfile.TemporaryDirectory(prefix="lpk-editor-export-") as temporary:
            exported = self.save_copy(Path(temporary) / "model", include_projects=False, include_skins=False, mark_saved=False)
            output.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".lpk-snapshot-", dir=output.parent) as publishing:
                stage = Path(publishing) / "model"
                shutil.copytree(Path(exported["model_path"]).parent, stage)
                _rename_directory(stage, output)
        return output / "model.json"

    export_model_snapshot = export_snapshot

    def save_copy(self, output_dir: str | Path, *, include_projects: bool = True, include_skins: bool = True,
                  include_editor_metadata: bool = True, mark_saved: bool = True) -> dict[str, Any]:
        output = Path(output_dir).expanduser().resolve()
        if output.exists() or output.is_relative_to(self.source_root) or self.source_root.is_relative_to(output):
            raise AnimationEditingError("Save to a new folder outside the source package.")
        self.commit_parameter_preview()
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".lpk-live2d-copy-", dir=output.parent) as temporary:
            stage = Path(temporary) / "package"
            # Empty authored curves are a legitimate edit (deleting the last key).
            # The stricter MCP API rejects newly authored tracks with no keys.
            empty = {name for name in self.project.modified if not self.project.motions[name]["Curves"]}
            modified = self.project.modified.copy()
            try:
                self.project.modified -= empty
                result = self.project.save_copy(stage)
            finally:
                self.project.modified = modified
            document = copy.deepcopy(self.original_document)
            groups = document["FileReferences"].setdefault("Motions", {})
            exported = _read_json(Path(result["model_path"]))
            animation_paths = []
            for name in sorted(modified):
                group, index = self.project.bindings[name]
                new_item = exported["FileReferences"].get("Motions", {}).get(group, [])
                if name in empty:
                    relative = f"edited_motions/{hashlib.sha256(name.encode()).hexdigest()[:12]}.motion3.json"
                    self._write_json(stage / relative, self.project.motions[name])
                    item = {"File": relative, "FadeInTime": 0.0, "FadeOutTime": 0.0}
                else:
                    item = copy.deepcopy(new_item[index])
                animation_paths.append(str(output / item["File"]))
                originals = groups.setdefault(group, [])
                original_index = self.original_bindings.get((group, index))
                if original_index is None:
                    originals.append(item)
                else:
                    originals[original_index] = dict(originals[original_index], File=item["File"])
            for group, original_index in sorted(self._deleted_original_bindings, reverse=True):
                entries = groups.get(group, [])
                if original_index < len(entries):
                    entries.pop(original_index)
            self._write_json(Path(result["model_path"]), document)
            projects = self._projects.export(stage) if include_projects else {}
            if include_editor_metadata:
                state = {"format": "LpkUnpacker.Live2DEditor", "version": 2,
                         "source_path": str(self.source_path), "pose_parameters": self.parameter_overrides,
                         "preview_part_opacity": self.part_overrides, "warnings": self.warnings,
                         "authoring_source": None, "model": "model.json", "projects": projects}
                if include_skins:
                    state["skins"] = self._skins.export_registry(stage)
                else:
                    active = self._skins.get(self.active_skin_id)
                    state["skin_at_export"] = {"id": active["id"], "name": active["name"],
                                                "working_modified": self.skin_modified}
                self._write_json(stage / "model.drawables.json", self.snapshot_mesh(self.parameter_overrides))
                self._write_json(stage / EDITOR_MANIFEST, state)
            manifest_path = Path(result["manifest_path"])
            manifest = _read_json(manifest_path)
            manifest["animations"] = [self.project._summary(name) for name in sorted(modified)]
            manifest["source_path"] = str(self.source_path)
            self._write_json(manifest_path, manifest)
            old_prefix = str(stage)
            for key, value in result.items():
                if isinstance(value, str) and (value == old_prefix or value.startswith(old_prefix + os.sep)):
                    result[key] = str(output) + value[len(old_prefix):]
            result["animation_paths"] = animation_paths
            _rename_directory(stage, output)
        if mark_saved:
            self._saved_signature = self._signature()
        if include_editor_metadata:
            result["editor_manifest_path"] = str(output / EDITOR_MANIFEST)
        return result

    def close(self) -> None:
        self._core_model = None
        self._local_textures.clear()
        self._local_crops.clear()
        temporary = getattr(self, "_temporary", None)
        if temporary:
            temporary.cleanup()
            self._temporary = None
