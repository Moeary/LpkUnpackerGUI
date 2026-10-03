"""Edit runtime animations in memory and export an independent model package.

Cubism motion segments follow the official specification:
https://github.com/Live2D/CubismSpecs/blob/master/FileFormats/motion3.json.md
Spine timelines follow the versioned official runtime readers:
https://github.com/EsotericSoftware/spine-runtimes/tree/4.0/spine-cpp

Spine translation and rotation are offsets from setup pose, while scale is a
multiplier. Only linear and stepped bone/parameter keys are authored. Existing
unrelated curves, attachments and constraint timelines are preserved.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping, Sequence


class AnimationEditingError(ValueError):
    """Invalid input, unsupported data or unsafe output location."""


INVENTORY_FORMAT = "LpkUnpacker.Live2DParameterInventory"
PROJECT_FORMAT = "LpkUnpacker.AnimationEditingProject"
_MANIFEST = "lpk_animation_project.json"


def _rename_directory(source: Path, output: Path) -> None:
    """Publish a new package, tolerating brief Windows antivirus locks.

    A target that appeared meanwhile is never replaced. All other failures
    remain visible, including a persistent PermissionError after one second.
    """
    for attempt in range(6):
        if output.exists():
            raise AnimationEditingError("Output appeared during export; refusing to overwrite it.")
        try:
            source.rename(output)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 5 or output.exists():
                raise
            time.sleep(0.2)


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnimationEditingError(f"{label} must be a finite number.")
    try:
        result = float(value)
    except OverflowError as exc:
        raise AnimationEditingError(f"{label} exceeds the numeric range.") from exc
    if not math.isfinite(result) or abs(result) > 3.4028234663852886e38:
        raise AnimationEditingError(f"{label} must be a finite number.")
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON constant: {value}")


def _validate_finite_json(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise AnimationEditingError("JSON contains a non-finite numeric value.")
    if isinstance(value, dict):
        for child in value.values():
            _validate_finite_json(child)
    elif isinstance(value, list):
        for child in value:
            _validate_finite_json(child)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=_reject_constant)
    except (OSError, UnicodeError, ValueError) as exc:
        raise AnimationEditingError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AnimationEditingError(f"JSON root must be an object: {path}")
    _validate_finite_json(data)
    return data


def _relative(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnimationEditingError("Asset reference must be a nonempty relative path.")
    value = value.replace("\\", "/")
    posix, windows = PurePosixPath(value), PureWindowsPath(value)
    if (posix.is_absolute() or windows.drive or windows.root or ".." in posix.parts
            or any(":" in part or part.endswith((" ", ".")) for part in posix.parts)
            or any(ord(c) < 32 or c in '*?"<>|' for c in value)
            or any(part.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL",
                    *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}
                   for part in posix.parts)):
        raise AnimationEditingError(f"Unsafe asset reference: {value!r}")
    result = posix.as_posix()
    if result == ".":
        raise AnimationEditingError(f"Unsafe asset reference: {value!r}")
    return result


def _asset(root: Path, relative: Any) -> Path:
    rel = _relative(relative)
    result = (root / rel).resolve()
    if not result.is_relative_to(root.resolve()) or not result.is_file():
        raise AnimationEditingError(f"Missing or escaping asset reference: {relative!r}")
    return result


def _name(value: Any) -> str:
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or len(value) > 128 or any(ord(c) < 32 for c in value)
            or any(c in value for c in '/\\:*?"<>|') or value in {".", ".."}
            or value.endswith((".", " "))):
        raise AnimationEditingError("Animation name must be a safe nonempty name (128 characters maximum).")
    return value


def _read_native_parameters(moc_path: Path) -> list[dict[str, Any]]:
    # Do not hand tiny/non-MOC fixtures to native code.
    with moc_path.open("rb") as stream:
        if stream.read(4) != b"MOC3":
            raise AnimationEditingError("Not a MOC3 binary; native parameter inspection is unavailable.")
    from app.core.cubism_core import CubismCore
    core = CubismCore()
    model = core.load_moc(moc_path)
    dll, pointer = core.dll, model.model_pointer
    count = int(dll.csmGetParameterCount(pointer))
    ids = dll.csmGetParameterIds(pointer)
    mins = dll.csmGetParameterMinimumValues(pointer)
    maxs = dll.csmGetParameterMaximumValues(pointer)
    defaults = dll.csmGetParameterDefaultValues(pointer)
    return [{"id": ids[i].decode("utf-8"), "min": float(mins[i]),
             "max": float(maxs[i]), "default": float(defaults[i])} for i in range(count)]


def _parameters(moc: Path, inventory_path: Path | None, display_info: Path | None
                ) -> tuple[list[dict[str, Any]], str, list[str]]:
    sidecar = None
    if inventory_path:
        sidecar = _read_json(inventory_path)
        if (sidecar.get("format") != INVENTORY_FORMAT or sidecar.get("version") != 1
                or sidecar.get("moc_sha256") != hashlib.sha256(moc.read_bytes()).hexdigest()):
            raise AnimationEditingError("Parameter inventory must have the supported format and matching moc_sha256.")
    warnings: list[str] = []
    try:
        parameters = _read_native_parameters(moc)
        source = "native"
    except Exception as exc:
        if sidecar is None:
            raise AnimationEditingError(f"Cannot inspect Live2D parameter ranges: {exc}") from exc
        parameters = sidecar.get("parameters")
        source = "sidecar"
        warnings.append(f"Native parameter inventory unavailable; using matching-hash sidecar: {exc}")
    if not isinstance(parameters, list) or not parameters:
        raise AnimationEditingError("Model has no usable parameter inventory.")
    names: dict[str, str] = {}
    if display_info:
        for item in _read_json(display_info).get("Parameters", []):
            if isinstance(item, dict):
                names[str(item.get("Id"))] = str(item.get("Name") or item.get("Id"))
    if sidecar:
        for item in sidecar.get("parameters", []):
            if isinstance(item, dict) and item.get("name"):
                names[str(item.get("id"))] = str(item["name"])
    result, seen = [], set()
    for item in parameters:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise AnimationEditingError("Invalid parameter inventory item.")
        parameter_id = item["id"]
        if parameter_id in seen:
            raise AnimationEditingError(f"Duplicate parameter ID: {parameter_id}")
        low, high, default = (_number(item.get(key), f"{parameter_id}.{key}")
                              for key in ("min", "max", "default"))
        if not low <= default <= high:
            raise AnimationEditingError(f"Invalid parameter range/default: {parameter_id}")
        result.append({"id": parameter_id, "min": low, "max": high,
                       "default": default, "name": names.get(parameter_id, parameter_id)})
        seen.add(parameter_id)
    return result, source, warnings


def _segment_frames(segments: Any) -> tuple[list[dict[str, Any]], int, int, bool]:
    if not isinstance(segments, list) or len(segments) < 2:
        raise AnimationEditingError("Cubism curve has invalid Segments.")
    first_time, first_value = _number(segments[0], "time"), _number(segments[1], "value")
    if first_time < 0:
        raise AnimationEditingError("Keyframe time must be nonnegative.")
    frames = [{"time": first_time, "value": first_value, "interpolation": "linear"}]
    cursor, count, points, editable = 2, 0, 1, True
    while cursor < len(segments):
        kind = segments[cursor]
        if isinstance(kind, bool) or kind not in (0, 1, 2, 3):
            raise AnimationEditingError(f"Unknown Cubism segment type: {kind!r}")
        size = 6 if kind == 1 else 2
        values = segments[cursor + 1:cursor + 1 + size]
        if len(values) != size:
            raise AnimationEditingError("Truncated Cubism segment.")
        values = [_number(v, "segment coordinate") for v in values]
        time, value = values[-2:]
        if time <= frames[-1]["time"]:
            raise AnimationEditingError("Cubism keyframe times must increase strictly.")
        if kind == 1 and not frames[-1]["time"] <= values[0] <= values[2] <= time:
            raise AnimationEditingError("Cubism Bezier control-point times are invalid.")
        frames[-1]["interpolation"] = {0: "linear", 1: "bezier", 2: "stepped", 3: "inverse_stepped"}[kind]
        frames.append({"time": time, "value": value, "interpolation": "linear"})
        count += 1
        points += 3 if kind == 1 else 1
        editable = editable and kind in (0, 2)
        cursor += size + 1
    return frames, count, points, editable


def _motion_meta(data: dict[str, Any]) -> None:
    if data.get("Version") != 3 or not isinstance(data.get("Meta"), dict):
        raise AnimationEditingError("Only Cubism Version 3 motion3 JSON is supported.")
    duration = _number(data["Meta"].get("Duration"), "Duration")
    fps = _number(data["Meta"].get("Fps"), "Fps")
    if duration < 0 or fps <= 0 or not isinstance(data.get("Curves"), list):
        raise AnimationEditingError("Invalid Cubism duration/Fps/Curves.")
    segment_count = point_count = 0
    seen = set()
    for curve in data["Curves"]:
        if not isinstance(curve, dict) or curve.get("Target") not in {"Parameter", "Model", "PartOpacity"}:
            raise AnimationEditingError("Unsupported Cubism curve target.")
        if not isinstance(curve.get("Id"), str) or not curve["Id"]:
            raise AnimationEditingError("Cubism curve ID is missing.")
        key = curve["Target"], curve["Id"]
        if key in seen:
            raise AnimationEditingError("Duplicate Cubism curve target/ID.")
        seen.add(key)
        frames, segments, points, _ = _segment_frames(curve.get("Segments"))
        if frames[-1]["time"] > duration:
            raise AnimationEditingError("Cubism keyframes exceed motion duration.")
        segment_count += segments
        point_count += points
    events = data.get("UserData", [])
    if not isinstance(events, list):
        raise AnimationEditingError("Cubism UserData must be a list.")
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("Value"), str):
            raise AnimationEditingError("Invalid Cubism UserData.")
        if not 0 <= _number(event.get("Time"), "UserData.Time") <= duration:
            raise AnimationEditingError("Cubism UserData time is outside motion duration.")
    data["Meta"].update(CurveCount=len(data["Curves"]), TotalSegmentCount=segment_count,
                        TotalPointCount=point_count, UserDataCount=len(events),
                        TotalUserDataSize=sum(len(e["Value"].encode("utf-8")) for e in events))


def _duration(data: Any) -> float:
    if isinstance(data, dict):
        own = _number(data["time"], "time") if "time" in data else 0.0
        if own < 0:
            raise AnimationEditingError("Keyframe time must be nonnegative.")
        return max([own] + [_duration(v) for k, v in data.items() if k != "time"])
    if isinstance(data, list):
        return max([0.0] + [_duration(v) for v in data])
    return 0.0


@dataclass
class AnimationEditingProject:
    source_path: Path
    model_path: Path
    source_root: Path
    kind: str
    version: str
    document: dict[str, Any]
    parameters: list[dict[str, Any]] = field(default_factory=list)
    inventory_source: str | None = None
    references: dict[str, Path] = field(default_factory=dict)
    motions: dict[str, dict[str, Any]] = field(default_factory=dict)
    bindings: dict[str, tuple[str, int]] = field(default_factory=dict)
    wrapper: dict[str, Any] | None = None
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)
    modified: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)

    def _animation(self, name: str) -> dict[str, Any]:
        animations = self.motions if self.kind == "live2d" else self.document.get("animations", {})
        if name not in animations:
            raise AnimationEditingError(f"Unknown animation: {name!r}")
        return animations[name]

    def _summary(self, name: str) -> dict[str, Any]:
        animation = self._animation(name)
        settings = self.settings.get(name, {})
        timelines = []
        if self.kind == "live2d":
            for curve in animation["Curves"]:
                frames, _, _, editable = _segment_frames(curve["Segments"])
                timelines.append({"target": curve["Id"], "channel": "value",
                                  "keyframe_count": len(frames),
                                  "editable": curve["Target"] == "Parameter" and editable})
            duration = animation["Meta"]["Duration"]
        else:
            for bone, channels in animation.get("bones", {}).items():
                for channel, frames in channels.items():
                    timelines.append({"target": bone, "channel": channel,
                                      "keyframe_count": len(frames),
                                      "editable": channel in {"translate", "rotate", "scale"}
                                      and all(f.get("curve") in (None, "stepped") for f in frames)})
            duration = settings.get("duration", _duration(animation))
        result = {"name": name, "duration": duration,
                  "loop": settings.get("loop", animation.get("Meta", {}).get("Loop", False)),
                  "modified": name in self.modified, "timelines": timelines}
        if name in self.bindings:
            result["group"], result["index"] = self.bindings[name]
        return result

    def inspect(self) -> dict[str, Any]:
        names = self.motions if self.kind == "live2d" else self.document.get("animations", {})
        return copy.deepcopy({"kind": self.kind, "source_path": str(self.source_path),
                              "model_path": str(self.model_path), "version": self.version,
                              "parameters": self.parameters, "inventory_source": self.inventory_source,
                              "bones": self.document.get("bones", []),
                              "slots": self.document.get("slots", []),
                              "ik": self.document.get("ik", []),
                              "transform": self.document.get("transform", []),
                              "animations": [self._summary(name) for name in names],
                              "supported_timelines": ["value"] if self.kind == "live2d"
                              else ["translate", "rotate", "scale"],
                              "value_semantics": "absolute parameter values" if self.kind == "live2d"
                              else "translate/rotate offsets from setup pose; scale multipliers",
                              "referenced_assets": list(self.references), "warnings": self.warnings})

    def create_animation(self, name: str, *, duration: float, loop: bool = False,
                         fps: float = 30) -> dict[str, Any]:
        name = _name(name)
        duration, fps = _number(duration, "duration"), _number(fps, "fps")
        if duration <= 0 or fps <= 0 or not isinstance(loop, bool):
            raise AnimationEditingError("duration and fps must be positive; loop must be boolean.")
        if self.kind == "live2d":
            groups = self.document["FileReferences"].get("Motions", {})
            if name in self.motions or name in groups:
                raise AnimationEditingError(f"Animation/group already exists: {name}")
            self.motions[name] = {"Version": 3, "Meta": {"Duration": duration, "Fps": fps,
                                     "Loop": loop, "AreBeziersRestricted": True}, "Curves": []}
            _motion_meta(self.motions[name])
            self.bindings[name] = name, 0
        else:
            animations = self.document.setdefault("animations", {})
            if name in animations:
                raise AnimationEditingError(f"Animation already exists: {name}")
            animations[name] = {"bones": {}}
        self.settings[name] = {"duration": duration, "fps": fps, "loop": loop}
        self.modified.add(name)
        return self._summary(name)

    def clone_animation(self, source_name: str, new_name: str) -> dict[str, Any]:
        original = copy.deepcopy(self._animation(source_name))
        summary = self._summary(source_name)
        self.create_animation(new_name, duration=max(float(summary["duration"]), 1 / 30),
                              loop=bool(summary["loop"]))
        if self.kind == "live2d":
            self.motions[new_name] = original
            self.settings[new_name]["duration"] = original["Meta"]["Duration"]
            self.settings[new_name]["fps"] = original["Meta"]["Fps"]
        else:
            self.document["animations"][new_name] = original
        return self._summary(new_name)

    def get_keyframes(self, animation_name: str, target: str, channel: str) -> dict[str, Any]:
        animation = self._animation(animation_name)
        if self.kind == "live2d":
            if channel != "value" or target not in {p["id"] for p in self.parameters}:
                raise AnimationEditingError(f"Unknown parameter/channel: {target}/{channel}")
            curve = next((c for c in animation["Curves"]
                          if c["Target"] == "Parameter" and c["Id"] == target), None)
            frames, editable = [], True
            if curve:
                frames, _, _, editable = _segment_frames(curve["Segments"])
        else:
            if target not in {b["name"] for b in self.document["bones"]} or channel not in {"translate", "rotate", "scale"}:
                raise AnimationEditingError(f"Unknown bone/channel: {target}/{channel}")
            frames = copy.deepcopy(animation.get("bones", {}).get(target, {}).get(channel, []))
            editable = all(f.get("curve") in (None, "stepped") for f in frames)
            for frame in frames:
                frame.setdefault("time", 0.0)
                curve = frame.pop("curve", None)
                frame["interpolation"] = "stepped" if curve == "stepped" else "linear" if curve is None else "bezier"
                if curve is not None and curve != "stepped":
                    frame["curve"] = curve
                if channel == "rotate":
                    frame["angle"] = frame.pop("value", 0.0) if self.version.startswith("4.0.") else frame.get("angle", 0.0)
                else:
                    frame.setdefault("x", 1.0 if channel == "scale" else 0.0)
                    frame.setdefault("y", 1.0 if channel == "scale" else 0.0)
        result = {"animation_name": animation_name, "target": target, "channel": channel,
                  "keyframes": frames, "editable": editable}
        if not editable:
            result["reason"] = "Existing Bezier/inverse-stepped timeline requires explicit replace."
        return result

    def set_keyframes(self, animation_name: str, target: str, channel: str,
                      keyframes: Sequence[Mapping[str, Any]], *, mode: str = "replace") -> dict[str, Any]:
        existing = self.get_keyframes(animation_name, target, channel)
        if mode not in {"replace", "merge"}:
            raise AnimationEditingError("mode must be replace or merge.")
        if not isinstance(keyframes, (list, tuple)) or not keyframes:
            raise AnimationEditingError("keyframes must be a nonempty list.")
        duration = float(self._summary(animation_name)["duration"])
        fields = ("value",) if self.kind == "live2d" else ("angle",) if channel == "rotate" else ("x", "y")
        parameter = next((p for p in self.parameters if p["id"] == target), None)
        frames, last = [], -1.0
        for raw in keyframes:
            if not isinstance(raw, Mapping) or set(raw) - {"time", "interpolation", *fields}:
                raise AnimationEditingError("Unknown keyframe fields or invalid keyframe object.")
            time = _number(raw.get("time"), "time")
            if time < 0 or time <= last or time > duration:
                raise AnimationEditingError("Keyframe times must increase strictly within [0, duration].")
            interpolation = raw.get("interpolation", "linear")
            if interpolation not in {"linear", "stepped"}:
                raise AnimationEditingError("Only linear and stepped interpolation is supported.")
            frame = {"time": time, "interpolation": interpolation}
            for field_name in fields:
                frame[field_name] = _number(raw.get(field_name), field_name)
            if parameter and not parameter["min"] <= frame["value"] <= parameter["max"]:
                raise AnimationEditingError(f"Value outside parameter range [{parameter['min']}, {parameter['max']}]: {target}")
            frames.append(frame)
            last = time
        if mode == "merge":
            if not existing["editable"]:
                raise AnimationEditingError(existing["reason"])
            by_time = {f["time"]: f for f in existing["keyframes"]}
            by_time.update({f["time"]: f for f in frames})
            frames = [by_time[t] for t in sorted(by_time)]
        # Spine duration is defined by the last key, so author an explicit hold.
        if self.kind == "spine" and frames[-1]["time"] < duration:
            hold = dict(frames[-1], time=duration, interpolation="linear")
            frames.append(hold)
        animation = self._animation(animation_name)
        if self.kind == "live2d":
            segments = [frames[0]["time"], frames[0]["value"]]
            for previous, frame in zip(frames, frames[1:]):
                segments.extend([2 if previous["interpolation"] == "stepped" else 0,
                                 frame["time"], frame["value"]])
            curves = animation["Curves"]
            new_curve = {"Target": "Parameter", "Id": target, "Segments": segments}
            index = next((i for i, c in enumerate(curves) if c["Target"] == "Parameter" and c["Id"] == target), None)
            if index is None:
                curves.append(new_curve)
            else:
                new_curve = dict(curves[index], Segments=segments)
                curves[index] = new_curve
            _motion_meta(animation)
        else:
            encoded = []
            for frame in frames:
                output = {k: v for k, v in frame.items() if k != "interpolation"}
                if frame["interpolation"] == "stepped":
                    output["curve"] = "stepped"
                if channel == "rotate" and self.version.startswith("4.0."):
                    output["value"] = output.pop("angle")
                encoded.append(output)
            animation.setdefault("bones", {}).setdefault(target, {})[channel] = encoded
        self.modified.add(animation_name)
        return self.get_keyframes(animation_name, target, channel)

    def save_copy(self, output_dir: str | os.PathLike[str]) -> dict[str, Any]:
        output = Path(output_dir).expanduser().resolve()
        if output.exists() or output.is_relative_to(self.source_root):
            raise AnimationEditingError("Output must be a new directory outside the source package; existing paths are never overwritten.")
        if any(not self._summary(name)["timelines"] for name in self.modified):
            raise AnimationEditingError("An edited/new animation has no keyframes.")
        output.parent.mkdir(parents=True, exist_ok=True)
        # Validate the complete copy plan before writing the first asset.
        references = dict(self.references)
        for rel, path in references.items():
            if _asset(self.source_root, rel) != path:
                raise AnimationEditingError(f"Asset changed/escaped since inspection: {rel}")
        document, wrapper = copy.deepcopy(self.document), copy.deepcopy(self.wrapper)
        generated: dict[str, Any] = {}
        animation_paths = []
        if self.kind == "live2d":
            model_rel = _relative(self.model_path.relative_to(self.source_root).as_posix())
            for name in sorted(self.modified):
                digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
                rel = f"edited_motions/{digest}.motion3.json"
                if rel.casefold() in {p.casefold() for p in references}:
                    raise AnimationEditingError(f"Generated motion would overwrite a source asset: {rel}")
                data = copy.deepcopy(self.motions[name])
                _motion_meta(data)
                generated[rel] = data
                group, index = self.bindings[name]
                groups = document["FileReferences"].setdefault("Motions", {})
                items = groups.setdefault(group, [])
                if index == len(items):
                    items.append({"File": rel, "FadeInTime": 0.0, "FadeOutTime": 0.0})
                else:
                    items[index]["File"] = rel
                animation_paths.append(rel)
            generated[model_rel] = document
            # Include the inspected inventory for reopening without a native DLL.
            moc = _asset(self.source_root, document["FileReferences"]["Moc"])
            generated["live2d_parameter_inventory.json"] = {
                "format": INVENTORY_FORMAT, "version": 1,
                "moc_sha256": hashlib.sha256(moc.read_bytes()).hexdigest(), "parameters": self.parameters}
        else:
            skeleton_rel = _relative(self.model_path.relative_to(self.source_root).as_posix())
            if not skeleton_rel.lower().endswith(".json"):
                skeleton_rel = str(PurePosixPath(skeleton_rel).with_suffix(".json"))
            generated[skeleton_rel] = document
            animation_paths = [skeleton_rel]
            if wrapper is not None:
                wrapper["skeleton"] = skeleton_rel
                motions = wrapper.setdefault("motions", {})
                for name in sorted(self.modified):
                    if name not in motions:
                        motions[name] = [{"file": name, "wrap_mode": 1 if self.settings.get(name, {}).get("loop") else 0}]
                model_rel = _relative(self.source_path.relative_to(self.source_root).as_posix())
                generated[model_rel] = wrapper
            else:
                model_rel = skeleton_rel
        if _MANIFEST.casefold() in {p.casefold() for p in references}:
            raise AnimationEditingError("Source package collides with the editing manifest filename.")
        manifest = {"format": PROJECT_FORMAT, "version": 1, "kind": self.kind,
                    "source_path": str(self.source_path), "model_path": model_rel,
                    "skeleton_path": skeleton_rel if self.kind == "spine" else None,
                    "animations": [self._summary(n) for n in sorted(self.modified)],
                    "warnings": self.warnings, "value_semantics": self.inspect()["value_semantics"],
                    "parameter_inventory": "live2d_parameter_inventory.json" if self.kind == "live2d" else None,
                    "sources": [{"path": rel, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                                for rel, path in references.items()]}
        generated[_MANIFEST] = manifest
        allowed_replacements = {self.model_path.relative_to(self.source_root).as_posix().casefold(),
                                self.source_path.relative_to(self.source_root).as_posix().casefold()}
        folded_references = {p.casefold(): p for p in references}
        for rel in generated:
            if rel.casefold() in folded_references and rel.casefold() not in allowed_replacements:
                raise AnimationEditingError(f"Generated output collides with referenced asset: {rel}")
        with tempfile.TemporaryDirectory(prefix=".lpk-animation-", dir=output.parent) as temporary:
            stage = Path(temporary) / "package"
            stage.mkdir()
            for rel, source in references.items():
                target = stage / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            for rel, data in generated.items():
                target = stage / _relative(rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
            if output.exists():
                raise AnimationEditingError("Output appeared during export; refusing to overwrite it.")
            _rename_directory(stage, output)
        return {"output_dir": str(output), "model_path": str(output / model_rel),
                "skeleton_path": str(output / skeleton_rel) if self.kind == "spine" else None,
                "manifest_path": str(output / _MANIFEST),
                "parameter_inventory_path": str(output / "live2d_parameter_inventory.json") if self.kind == "live2d" else None,
                "animation_paths": [str(output / rel) for rel in animation_paths], "warnings": list(self.warnings)}


def _add_reference(project: AnimationEditingProject, relative: Any) -> Path:
    rel = _relative(relative)
    path = _asset(project.source_root, rel)
    folded = {p.casefold(): p for p in project.references}
    if rel.casefold() in folded and folded[rel.casefold()] != rel:
        raise AnimationEditingError(f"Case-colliding asset references: {rel}")
    project.references[rel] = path
    return path


def _live2d_references(project: AnimationEditingProject, value: Any, key: str = "") -> None:
    from app.core.live2d_references import iter_live2d_asset_references

    for relative in iter_live2d_asset_references(value, key):
        _add_reference(project, relative)


def _spine_assets(project: AnimationEditingProject) -> None:
    from app.core.spine_atlas import parse_atlas
    wrapper = project.wrapper
    atlas_paths = []
    if wrapper:
        for item in wrapper.get("atlases", []):
            if not isinstance(item, dict):
                raise AnimationEditingError("Invalid Spine atlas reference.")
            atlas_paths.append(_add_reference(project, item.get("atlas")))
            for texture in item.get("textures", []):
                _add_reference(project, texture)
        def sounds(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "sound" and child:
                        _add_reference(project, child)
                    else:
                        sounds(child)
            elif isinstance(value, list):
                for child in value:
                    sounds(child)
        sounds(wrapper.get("motions", {}))
    else:
        from app.core.spine_preview import load_spine_asset
        atlas_paths = list(load_spine_asset(project.model_path).atlas_paths)
        for atlas in atlas_paths:
            _add_reference(project, atlas.relative_to(project.source_root).as_posix())
    for atlas in atlas_paths:
        try:
            parsed = parse_atlas(atlas)
        except Exception as exc:
            raise AnimationEditingError(f"Cannot inspect atlas {atlas}: {exc}") from exc
        for page in parsed.pages:
            rel = (atlas.parent.relative_to(project.source_root) / _relative(page.name)).as_posix()
            _add_reference(project, rel)
    events = project.document.get("events", {})
    audio_root = project.document.get("skeleton", {}).get("audio", "")
    for event in events.values():
        if isinstance(event, dict) and event.get("audio"):
            _add_reference(project, str(PurePosixPath(audio_root) / _relative(event["audio"])))
    def has_renderable_attachments(value: Any) -> bool:
        if isinstance(value, dict):
            if "type" in value and value["type"] in {"region", "mesh", "linkedmesh", "skinnedmesh"}:
                return True
            if ("width" in value and "height" in value) or ("uvs" in value and "vertices" in value):
                return True
            return any(has_renderable_attachments(child) for child in value.values())
        return isinstance(value, list) and any(has_renderable_attachments(child) for child in value)
    if not atlas_paths and has_renderable_attachments(project.document.get("skins", [])):
        raise AnimationEditingError("Renderable Spine attachments require a texture atlas; no atlas was found.")


def _validate_spine_timelines(document: dict[str, Any], version: str) -> None:
    bone_names = {b["name"] for b in document["bones"]}
    slots = document.get("slots", [])
    if not isinstance(slots, list):
        raise AnimationEditingError("Spine slots must be a list.")
    slot_names = set()
    for slot in slots:
        if (not isinstance(slot, dict) or not isinstance(slot.get("name"), str)
                or slot["name"] in slot_names or slot.get("bone") not in bone_names):
            raise AnimationEditingError("Invalid Spine slot name/bone reference.")
        slot_names.add(slot["name"])
    allowed_channels = {"translate", "rotate", "scale", "shear"}
    if version.startswith("4.0."):
        allowed_channels.update({"translatex", "translatey", "scalex", "scaley", "shearx", "sheary"})
    for name, animation in document.get("animations", {}).items():
        if not isinstance(animation, dict):
            raise AnimationEditingError(f"Invalid Spine animation: {name}")
        bones = animation.get("bones", {})
        if not isinstance(bones, dict):
            raise AnimationEditingError(f"Invalid bone timelines in animation: {name}")
        for bone, channels in bones.items():
            if bone not in bone_names or not isinstance(channels, dict):
                raise AnimationEditingError(f"Unknown bone/invalid timeline: {bone}")
            for channel, frames in channels.items():
                if channel not in allowed_channels or not isinstance(frames, list) or not frames:
                    raise AnimationEditingError(f"Unsupported/empty Spine bone timeline: {bone}/{channel}")
                last = -1.0
                for frame in frames:
                    if not isinstance(frame, dict):
                        raise AnimationEditingError("Spine keyframe must be an object.")
                    time = _number(frame.get("time", 0), "time")
                    if time < 0 or time <= last:
                        raise AnimationEditingError("Spine keyframe times must increase strictly and be nonnegative.")
                    last = time
                    for key in ("x", "y", "angle", "value"):
                        if key in frame:
                            _number(frame[key], f"{bone}.{key}")
                    curve = frame.get("curve")
                    if curve not in (None, "stepped"):
                        # Official 3.8 exports encode c1 in `curve`, with
                        # optional c2/c3/c4 on the frame. 4.0 uses arrays.
                        if version.startswith("3.8.") and isinstance(curve, (int, float)):
                            _number(curve, "Bezier c1")
                            for key, default in (("c2", 0), ("c3", 1), ("c4", 1)):
                                _number(frame.get(key, default), f"Bezier {key}")
                        else:
                            if not isinstance(curve, list) or len(curve) not in ({4, 8} if version.startswith("4.0.") else {4}):
                                raise AnimationEditingError("Unsupported Spine interpolation curve.")
                            for coordinate in curve:
                                _number(coordinate, "Bezier control coordinate")
        slot_timelines = animation.get("slots", {})
        if not isinstance(slot_timelines, dict) or any(slot not in slot_names for slot in slot_timelines):
            raise AnimationEditingError("Unknown slot/invalid slot animation timeline.")


@dataclass(frozen=True)
class _ValidatedCubismMotion:
    path: Path
    data: dict[str, Any]
    metadata_changed: bool


def _read_validated_cubism_motion(path: Path) -> _ValidatedCubismMotion:
    """Read and normalize one motion using the shared strict validator."""
    data = _read_json(path)
    original_meta = dict(data.get("Meta", {})) if isinstance(data.get("Meta"), dict) else None
    _motion_meta(data)
    return _ValidatedCubismMotion(path.resolve(), data, original_meta != data["Meta"])


def _create_live2d_project(source: Path, data: dict[str, Any], *,
                           parameter_inventory_path: str | os.PathLike[str] | None = None,
                           validated_motions: Mapping[str, _ValidatedCubismMotion] | None = None
                           ) -> AnimationEditingProject:
    """Construct from an isolated model and motions already read by the editor.

    Public callers never supply this cache. Asset boundaries, the model header,
    and parameter inventory are still checked here; cache entries come from
    the same validator that is used by the public factory.
    """
    if data.get("Version") != 3 or data.get("Type", 0) != 0:
        raise AnimationEditingError("Only Cubism Version 3 Live2D model settings are supported.")
    project = AnimationEditingProject(source, source, source.parent, "live2d", "3", data)
    _add_reference(project, source.name)
    _live2d_references(project, data["FileReferences"])
    moc = _asset(source.parent, data["FileReferences"]["Moc"])
    display = data["FileReferences"].get("DisplayInfo")
    inventory = Path(parameter_inventory_path).expanduser().resolve() if parameter_inventory_path else None
    if inventory is None and (source.parent / "live2d_parameter_inventory.json").is_file():
        inventory = source.parent / "live2d_parameter_inventory.json"
    project.parameters, project.inventory_source, project.warnings = _parameters(
        moc, inventory, _asset(source.parent, display) if display else None)
    groups = data["FileReferences"].get("Motions", {})
    if not isinstance(groups, dict):
        raise AnimationEditingError("Live2D Motions must be an object.")
    cache = dict(validated_motions or {})
    used_paths = set()
    for group, entries in groups.items():
        if not isinstance(entries, list):
            raise AnimationEditingError("Live2D motion group must be a list.")
        for index, item in enumerate(entries):
            if not isinstance(item, dict):
                raise AnimationEditingError("Invalid Live2D motion entry.")
            path = _asset(source.parent, item.get("File"))
            relative = path.relative_to(source.parent).as_posix()
            validated = cache.get(relative)
            if validated is None or validated.path != path:
                validated = _read_validated_cubism_motion(path)
                cache[relative] = validated
            # Different Viewer entries may share a file. Each editable motion
            # must own its curves so editing one entry cannot change another.
            motion = copy.deepcopy(validated.data) if path in used_paths else validated.data
            used_paths.add(path)
            name = f"{group}[{index}]"
            project.motions[name], project.bindings[name] = motion, (group, index)
    return project


def create_animation_project(model_path: str | os.PathLike[str], *,
                             parameter_inventory_path: str | os.PathLike[str] | None = None
                             ) -> AnimationEditingProject:
    source = Path(model_path).expanduser().resolve()
    if not source.is_file():
        raise AnimationEditingError(f"Select one existing model JSON or skeleton file: {source}")
    data = _read_json(source) if source.suffix.lower() == ".json" else {}
    if isinstance(data.get("FileReferences"), dict) and data["FileReferences"].get("Moc"):
        return _create_live2d_project(source, data, parameter_inventory_path=parameter_inventory_path)
    wrapper = data if isinstance(data.get("skeleton"), str) else None
    skeleton = _asset(source.parent, data["skeleton"]) if wrapper else source
    root = source.parent
    warnings = []
    if skeleton.suffix.lower() == ".json":
        document = _read_json(skeleton)
    else:
        if not skeleton.name.lower().endswith((".skel", ".skel.bytes")):
            raise AnimationEditingError("Unsupported model format; choose Cubism model3 JSON or Spine JSON/skel.")
        from app.core.spine_preview import read_spine_version
        from app.core.spine_converter import discover_native_converter, _run_native_converter
        version = read_spine_version(skeleton)
        if not re.fullmatch(r"(?:3\.8|4\.0)\.\d+", version or ""):
            raise AnimationEditingError(f"Unsupported Spine version: {version!r}; only 3.8 and 4.0 are editable.")
        converter = discover_native_converter()
        if converter is None:
            raise AnimationEditingError("Bundled native Spine converter is unavailable.")
        with tempfile.TemporaryDirectory(prefix="lpk-animation-skel-") as temporary:
            converted = Path(temporary) / "skeleton.json"
            _, _, diagnostic, code = _run_native_converter(converter, skeleton, converted, version, False)
            if code != 0:
                raise AnimationEditingError(f"Spine binary-to-JSON conversion failed: {diagnostic}")
            document = _read_json(converted)
            if document.get("skeleton", {}).get("spine") != version:
                raise AnimationEditingError("Native conversion unexpectedly changed the Spine version.")
        warnings.append(f"Binary skeleton converted to same-version JSON ({version}); original binary preserved.")
    metadata = document.get("skeleton")
    version = metadata.get("spine") if isinstance(metadata, dict) else None
    if not re.fullmatch(r"(?:3\.8|4\.0)\.\d+", version or ""):
        raise AnimationEditingError(f"Unsupported Spine version: {version!r}; only 3.8 and 4.0 are editable.")
    bones = document.get("bones")
    if not isinstance(bones, list) or not bones:
        raise AnimationEditingError("Spine skeleton has no bones.")
    seen = set()
    for bone in bones:
        if not isinstance(bone, dict) or not isinstance(bone.get("name"), str) or not bone["name"]:
            raise AnimationEditingError("Invalid Spine bone.")
        if bone["name"] in seen or (bone.get("parent") is not None and bone["parent"] not in seen):
            raise AnimationEditingError("Duplicate bone name or missing/out-of-order parent.")
        for key in ("x", "y", "rotation", "scaleX", "scaleY", "length", "shearX", "shearY"):
            if key in bone:
                _number(bone[key], f"{bone['name']}.{key}")
        seen.add(bone["name"])
    if not isinstance(document.get("animations", {}), dict):
        raise AnimationEditingError("Spine animations must be an object.")
    _validate_spine_timelines(document, version)
    project = AnimationEditingProject(source, skeleton, root, "spine", version, document,
                                     wrapper=wrapper, warnings=warnings)
    _add_reference(project, source.relative_to(root).as_posix())
    _add_reference(project, skeleton.relative_to(root).as_posix())
    _spine_assets(project)
    for name, animation in document.get("animations", {}).items():
        if not isinstance(animation, dict):
            raise AnimationEditingError(f"Invalid Spine animation: {name}")
        project.settings[name] = {"duration": _duration(animation), "loop": False, "fps": 30}
    return project


def inspect_animation_model(model_path: str | os.PathLike[str], *,
                            parameter_inventory_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    return create_animation_project(model_path, parameter_inventory_path=parameter_inventory_path).inspect()


__all__ = ["AnimationEditingError", "AnimationEditingProject", "create_animation_project",
           "inspect_animation_model", "INVENTORY_FORMAT", "PROJECT_FORMAT"]
