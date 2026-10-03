"""Independent Spine editor session, history and version-aware track codecs.

This deliberately leaves the conservative animation MCP authoring API alone.
Only the selected timeline is re-encoded; deform/constraint/event data and all
other source fields remain in the document. Original resources are copied once
before the GUI accepts a session, so preview cache disposal cannot break it.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

from app.core.animation_editing import (
    AnimationEditingError, AnimationEditingProject, _duration, _number, _rename_directory,
    create_animation_project,
)


SPINE_EDITOR_MANIFEST = "lpk_spine_editor.json"
BONE_FIELDS = {
    "rotate": ["angle"], "translate": ["x", "y"], "scale": ["x", "y"],
    "shear": ["x", "y"], "translatex": ["value"], "translatey": ["value"],
    "scalex": ["value"], "scaley": ["value"], "shearx": ["value"], "sheary": ["value"],
}


def decode_draw_order(frame: dict, setup: list[str]) -> list[str]:
    """Decode Spine's sparse offsets against the *original* setup slot indices."""
    order: list[str | None] = [None] * len(setup)
    unchanged = []
    cursor = 0
    for offset in frame.get("offsets", []):
        if not isinstance(offset, dict) or offset.get("slot") not in setup:
            raise AnimationEditingError("Draw order references an unknown slot.")
        index = setup.index(offset["slot"])
        delta = offset.get("offset", 0)
        if isinstance(delta, bool) or not isinstance(delta, int) or index < cursor:
            raise AnimationEditingError("Draw order offsets must follow setup slot order.")
        while cursor < index:
            unchanged.append(setup[cursor])
            cursor += 1
        target = cursor + delta
        if not 0 <= target < len(setup) or order[target] is not None:
            raise AnimationEditingError("Draw order offset is outside the slot list or overlaps another slot.")
        order[target] = setup[cursor]
        cursor += 1
    unchanged.extend(setup[cursor:])
    for index in range(len(order) - 1, -1, -1):
        if order[index] is None:
            order[index] = unchanged.pop()
    return list(order)


def encode_draw_order(order: list[str], setup: list[str]) -> list[dict]:
    if len(order) != len(setup) or set(order) != set(setup):
        raise AnimationEditingError("Draw order must contain every slot exactly once.")
    # Encoding every setup slot avoids sparse-offset ambiguity and keeps the
    # runtime's required original-index ordering regardless of the new order.
    return [{"slot": slot, "offset": order.index(slot) - index} for index, slot in enumerate(setup)]


def _controls(values: Any) -> list[float]:
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        raise AnimationEditingError("Bezier control points must contain four coordinates.")
    result = [_number(value, "Bezier control") for value in values]
    if not 0 <= result[0] <= result[2] <= 1:
        raise AnimationEditingError("Bezier time controls must satisfy 0 <= cx1 <= cx2 <= 1.")
    return result


def decode_bone_track(raw: list[dict], channel: str, version: str) -> list[dict]:
    """Normalize 3.8 shared curves and 4.0 absolute per-value curves."""
    fields = BONE_FIELDS[channel]
    frames = copy.deepcopy(raw)
    for frame in frames:
        frame.setdefault("time", 0.0)
        if channel == "rotate":
            frame["angle"] = frame.pop("value", 0.0) if version.startswith("4.0.") else frame.get("angle", 0.0)
        for field in fields:
            frame.setdefault(field, 1.0 if channel.startswith("scale") else 0.0)
    for index, frame in enumerate(frames):
        curve = frame.pop("curve", None)
        numeric_controls = [curve, frame.pop("c2", 0), frame.pop("c3", 1), frame.pop("c4", 1)]
        frame["interpolation"] = "linear" if curve is None else "stepped" if curve == "stepped" else "bezier"
        if frame["interpolation"] != "bezier" or index + 1 == len(frames):
            continue
        next_frame = frames[index + 1]
        if version.startswith("3.8."):
            controls = list(curve) if isinstance(curve, list) else numeric_controls
            frame["bezier"] = {field: list(controls) for field in fields}
            frame["bezier_shared"] = True
            continue
        if not isinstance(curve, list) or len(curve) < len(fields) * 4:
            raise AnimationEditingError("Spine 4.0 curve is missing per-value control points.")
        dt = float(next_frame["time"]) - float(frame["time"])
        bezier, scales = {}, {}
        for field_index, field in enumerate(fields):
            x1, y1, x2, y2 = map(float, curve[field_index * 4:field_index * 4 + 4])
            start, end = float(frame[field]), float(next_frame[field])
            dy = end - start
            scale = dy if abs(dy) > 1e-12 else 1.0
            bezier[field] = [(x1 - frame["time"]) / dt, (y1 - start) / scale,
                             (x2 - frame["time"]) / dt, (y2 - start) / scale]
            if abs(dy) <= 1e-12:
                scales[field] = scale
        frame["bezier"] = bezier
        if scales:
            frame["bezier_value_scale"] = scales
    return frames


def encode_bone_track(frames: list[dict], channel: str, version: str, duration: float) -> list[dict]:
    fields = BONE_FIELDS[channel]
    result = []
    last = -1.0
    for index, raw in enumerate(frames):
        frame = copy.deepcopy(raw)
        seconds = _number(frame.get("time", 0), "time")
        if not last < seconds <= duration or seconds < 0:
            raise AnimationEditingError("Keyframe times must increase within the animation duration.")
        last = seconds
        frame["time"] = seconds
        for field in fields:
            frame[field] = _number(frame.get(field, 1 if channel.startswith("scale") else 0), field)
        interpolation = frame.pop("interpolation", "linear")
        bezier = frame.pop("bezier", {})
        scales = frame.pop("bezier_value_scale", {})
        frame.pop("bezier_shared", None)
        for key in ("curve", "c2", "c3", "c4"):
            frame.pop(key, None)
        if interpolation == "stepped":
            frame["curve"] = "stepped"
        elif interpolation == "bezier" and index + 1 < len(frames):
            next_frame = frames[index + 1]
            if version.startswith("3.8."):
                controls = [_controls(bezier.get(field, [0.25, 0.25, 0.75, 0.75])) for field in fields]
                if any(control != controls[0] for control in controls[1:]):
                    raise AnimationEditingError("Spine 3.8 shares one interpolation curve across both values.")
                frame["curve"], frame["c2"], frame["c3"], frame["c4"] = controls[0]
            else:
                absolute = []
                dt = _number(next_frame.get("time"), "next time") - seconds
                for field in fields:
                    x1, y1, x2, y2 = _controls(bezier.get(field, [0.25, 0.25, 0.75, 0.75]))
                    start, end = frame[field], _number(next_frame.get(field, 1 if channel.startswith("scale") else 0), field)
                    scale = scales.get(field, end - start) if isinstance(scales, dict) else scales
                    scale = _number(scale, "Bezier value scale")
                    absolute.extend([seconds + x1 * dt, start + y1 * scale,
                                     seconds + x2 * dt, start + y2 * scale])
                frame["curve"] = absolute
        elif interpolation not in {"linear", "bezier"}:
            raise AnimationEditingError(f"Unsupported Spine interpolation: {interpolation}")
        if channel == "rotate" and version.startswith("4.0."):
            frame["value"] = frame.pop("angle")
        result.append(frame)
    return result


class SpineEditorSession:
    """A source-independent document and bounded undo/redo history."""

    def __init__(self, project: AnimationEditingProject, original_source: Path, temporary):
        self.project = project
        self.original_source = original_source
        self.original_root = original_source.parent
        self._temporary = temporary
        self.workspace = Path(temporary.name)
        self.preview_root = self.workspace / "preview"
        self._textures: dict[str, bytes] = {}
        self._undo: list[dict] = []
        self._redo: list[dict] = []
        self._saved = self._signature()
        self.last_saved_path: str | None = None
        self._closed = False
        self._atlas_cache = None

    @classmethod
    def open(cls, source: str | Path) -> "SpineEditorSession":
        original = Path(source).expanduser().resolve()
        project = create_animation_project(original)
        if project.kind != "spine":
            raise AnimationEditingError("Select a Spine skeleton or model wrapper.")
        temporary = tempfile.TemporaryDirectory(prefix="lpk-spine-editor-")
        try:
            root = Path(temporary.name) / "source"
            root.mkdir()
            old_root = project.source_root
            source_rel = project.source_path.relative_to(old_root)
            model_rel = project.model_path.relative_to(old_root)
            for relative, asset in project.references.items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(asset, target)
            project.source_root = root
            project.source_path = root / source_rel
            project.model_path = root / model_rel
            project.references = {relative: root / relative for relative in project.references}
            saved = original.parent / SPINE_EDITOR_MANIFEST
            if saved.is_file():
                data = json.loads(saved.read_text(encoding="utf-8"))
                if data.get("format") == "LpkUnpacker.SpineEditor" and data.get("version") == 1:
                    for name, settings in data.get("animations", {}).items():
                        if name in project.document.get("animations", {}) and isinstance(settings, dict):
                            duration = _number(settings.get("duration", _duration(project.document["animations"][name])), "duration")
                            if duration < _duration(project.document["animations"][name]):
                                raise AnimationEditingError("Saved animation duration is shorter than its keys.")
                            project.settings[name] = {"duration": duration, "fps": 30, "loop": bool(settings.get("loop", False))}
            return cls(project, original, temporary)
        except Exception:
            temporary.cleanup()
            raise

    @property
    def document(self) -> dict:
        return self.project.document

    @property
    def animation_names(self) -> list[str]:
        return list(self.document.get("animations", {}))

    @property
    def skin_names(self) -> list[str]:
        skins = self.document.get("skins", {})
        return [str(skin.get("name", "default")) for skin in skins if isinstance(skin, dict)] if isinstance(skins, list) else list(skins)

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def dirty(self) -> bool:
        return self._signature() != self._saved

    def _state(self) -> dict:
        return copy.deepcopy({"document": self.document, "settings": self.project.settings,
                              "wrapper": self.project.wrapper, "modified": self.project.modified,
                              "textures": self._textures})

    def _restore(self, state: dict):
        self.project.document = copy.deepcopy(state["document"])
        self.project.settings = copy.deepcopy(state["settings"])
        self.project.wrapper = copy.deepcopy(state["wrapper"])
        self.project.modified = set(state["modified"])
        self._textures = dict(state["textures"])

    def _signature(self) -> bytes:
        payload = {"document": self.document, "settings": self.project.settings, "wrapper": self.project.wrapper,
                   "textures": {name: hashlib.sha256(data).hexdigest() for name, data in self._textures.items()}}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).digest()

    def _change(self, operation: Callable[[], Any]):
        before = self._state()
        try:
            result = operation()
            self._signature()  # validate the complete edited state before committing history
        except Exception:
            self._restore(before)
            raise
        current = self._state()
        if before != current:
            self._undo.append(before)
            self._undo = self._undo[-60:]
            self._redo.clear()
        return result

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self._state())
        self._restore(self._undo.pop())
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self._state())
        self._restore(self._redo.pop())
        return True

    def duration(self, name: str) -> float:
        return max(0.001, float(self.project.settings.get(name, {}).get("duration", _duration(self.project._animation(name)))))

    def set_duration(self, name: str, duration: float):
        duration = _number(duration, "duration")
        animation = self.project._animation(name)
        if duration <= 0 or duration < _duration(animation):
            raise AnimationEditingError("Animation duration must be positive and include every existing key.")
        self._change(lambda: self.project.settings.setdefault(name, {}).update(duration=duration))

    def create_animation(self, name: str, duration: float = 2.0):
        return self._change(lambda: self.project.create_animation(name, duration=duration))

    def clone_animation(self, source: str, name: str):
        return self._change(lambda: self.project.clone_animation(source, name))

    def delete_animation(self, name: str):
        self.project._animation(name)
        def operation():
            del self.document["animations"][name]
            self.project.settings.pop(name, None)
            self.project.modified.discard(name)
            if self.project.wrapper:
                self.project.wrapper.get("motions", {}).pop(name, None)
        self._change(operation)

    def bone(self, name: str) -> dict:
        bone = next((item for item in self.document["bones"] if item["name"] == name), None)
        if bone is None:
            raise AnimationEditingError(f"Unknown bone: {name}")
        return copy.deepcopy(bone)

    def set_bone_transform(self, name: str, values: dict[str, float]):
        allowed = {"x", "y", "rotation", "scaleX", "scaleY", "shearX", "shearY", "length"}
        if set(values) - allowed:
            raise AnimationEditingError("Unknown bone transform field.")
        numbers = {key: _number(value, key) for key, value in values.items()}
        self.bone(name)
        self._change(lambda: next(item for item in self.document["bones"] if item["name"] == name).update(numbers))

    def get_bone_track(self, animation: str, bone: str, channel: str) -> dict:
        self.bone(bone)
        if channel not in BONE_FIELDS or (channel in {"translatex", "translatey", "scalex", "scaley", "shearx", "sheary"}
                                           and not self.project.version.startswith("4.0.")):
            raise AnimationEditingError(f"Unknown bone channel: {channel}")
        raw = self.project._animation(animation).get("bones", {}).get(bone, {}).get(channel, [])
        return {"keyframes": decode_bone_track(raw, channel, self.project.version), "fields": BONE_FIELDS[channel], "editable": True}

    def set_bone_track(self, animation: str, bone: str, channel: str, frames: list[dict]):
        self.get_bone_track(animation, bone, channel)
        encoded = encode_bone_track(frames, channel, self.project.version, self.duration(animation))
        def operation():
            data = self.project._animation(animation)
            if encoded:
                data.setdefault("bones", {}).setdefault(bone, {})[channel] = encoded
            else:
                channels = data.get("bones", {}).get(bone, {})
                channels.pop(channel, None)
                if not channels:
                    data.get("bones", {}).pop(bone, None)
            self.project.modified.add(animation)
        self._change(operation)

    def slot(self, name: str) -> dict:
        slot = next((item for item in self.document.get("slots", []) if item["name"] == name), None)
        if slot is None:
            raise AnimationEditingError(f"Unknown slot: {name}")
        return copy.deepcopy(slot)

    def attachments(self, slot: str, skin: str | None = None) -> list[str]:
        self.slot(slot)
        raw = self.document.get("skins", {})
        skins = [(item.get("name"), item.get("attachments", {})) for item in raw] if isinstance(raw, list) else list(raw.items())
        names = []
        for name, attachments in skins:
            if skin is not None and name not in {skin, "default"}:
                continue
            for attachment in attachments.get(slot, {}):
                if attachment not in names:
                    names.append(attachment)
        return names

    def attachment_at(self, slot: str, animation: str | None, seconds: float) -> str | None:
        attachment = self.slot(slot).get("attachment")
        if animation:
            frames = self.project._animation(animation).get("slots", {}).get(slot, {}).get("attachment", [])
            for frame in frames:
                if float(frame.get("time", 0)) <= seconds + 1e-7:
                    attachment = frame.get("name")
        return attachment

    def set_slot_attachment(self, slot: str, attachment: str | None, *, animation: str | None = None, seconds: float = 0):
        self.slot(slot)
        if attachment is not None and attachment not in self.attachments(slot):
            raise AnimationEditingError(f"Unknown attachment: {slot}/{attachment}")
        if animation:
            seconds = _number(seconds, "time")
            if not 0 <= seconds <= self.duration(animation):
                raise AnimationEditingError("Attachment key time is outside the animation.")
        def operation():
            if not animation:
                target = next(item for item in self.document["slots"] if item["name"] == slot)
                target.pop("attachment", None) if attachment is None else target.update(attachment=attachment)
                return
            target = self.project._animation(animation).setdefault("slots", {}).setdefault(slot, {}).setdefault("attachment", [])
            frames = [frame for frame in target if abs(float(frame.get("time", 0)) - seconds) > 1e-7]
            frames.append({"time": seconds, "name": attachment})
            target[:] = sorted(frames, key=lambda frame: frame.get("time", 0))
            self.project.modified.add(animation)
        self._change(operation)

    def set_slot_visible(self, slot: str, visible: bool, *, attachment: str | None = None,
                         animation: str | None = None, seconds: float = 0):
        candidate = attachment or self.slot(slot).get("attachment") or next(iter(self.attachments(slot)), None)
        self.set_slot_attachment(slot, candidate if visible else None, animation=animation, seconds=seconds)

    def draw_order_at(self, animation: str | None, seconds: float) -> list[str]:
        setup = [slot["name"] for slot in self.document.get("slots", [])]
        order = list(setup)
        if animation:
            data = self.project._animation(animation)
            for frame in data.get("drawOrder", data.get("draworder", [])):
                if float(frame.get("time", 0)) <= seconds + 1e-7:
                    order = decode_draw_order(frame, setup)
        return order

    def set_draw_order(self, order: list[str], *, animation: str | None = None, seconds: float = 0):
        setup = [slot["name"] for slot in self.document.get("slots", [])]
        offsets = encode_draw_order(order, setup)
        if animation and not 0 <= _number(seconds, "time") <= self.duration(animation):
            raise AnimationEditingError("Draw-order key time is outside the animation.")
        def operation():
            if animation:
                data = self.project._animation(animation)
                key = "draworder" if "draworder" in data and "drawOrder" not in data else "drawOrder"
                frames = [frame for frame in data.get(key, []) if abs(float(frame.get("time", 0)) - seconds) > 1e-7]
                frames.append({"time": seconds, "offsets": offsets})
                data[key] = sorted(frames, key=lambda frame: frame.get("time", 0))
                self.project.modified.add(animation)
                return
            for data in self.document.get("animations", {}).values():
                for key in ("drawOrder", "draworder"):
                    for frame in data.get(key, []):
                        preserved = decode_draw_order(frame, setup)
                        frame["offsets"] = encode_draw_order(preserved, order)
            slots = {slot["name"]: slot for slot in self.document.get("slots", [])}
            self.document["slots"] = [slots[name] for name in order]
        self._change(operation)

    def delete_slot_key(self, slot: str, animation: str, seconds: float):
        def operation():
            channels = self.project._animation(animation).get("slots", {}).get(slot, {})
            frames = [frame for frame in channels.get("attachment", []) if abs(float(frame.get("time", 0)) - seconds) > 1e-6]
            if frames:
                channels["attachment"] = frames
            else:
                channels.pop("attachment", None)
            if not channels:
                self.project._animation(animation).get("slots", {}).pop(slot, None)
            self.project.modified.add(animation)
        self._change(operation)

    def atlas_regions(self) -> list[dict]:
        from app.core.spine_atlas import parse_atlas
        if self._atlas_cache is None:
            self._atlas_cache = []
            for relative, path in self.project.references.items():
                if path.name.lower().endswith((".atlas", ".atlas.txt", ".atlas.bytes")):
                    atlas = parse_atlas(path)
                    for region in atlas.regions:
                        self._atlas_cache.append({"atlas": relative, "id": region.region_id, "name": region.name,
                                                  "size": region.original_size, "region": region, "parsed": atlas})
        return list(self._atlas_cache)

    def _region(self, atlas: str, region_id: str) -> dict:
        item = next((item for item in self.atlas_regions() if item["atlas"] == atlas and item["id"] == region_id), None)
        if item is None:
            raise AnimationEditingError("Unknown atlas region.")
        return item

    def _page_image(self, item: dict):
        from PIL import Image
        atlas, region = item["parsed"], item["region"]
        page = atlas.pages[region.page_index]
        path = (atlas.atlas_path.parent / page.name).resolve()
        relative = path.relative_to(self.project.source_root).as_posix()
        source = io.BytesIO(self._textures[relative]) if relative in self._textures else path
        with Image.open(source) as image:
            result = image.convert("RGBA")
        return result, page, relative

    def region_image(self, atlas: str, region_id: str):
        from PIL import Image
        from app.core.spine_atlas import _rotate_from_page, _unpremultiply
        item = self._region(atlas, region_id)
        image, page, _relative = self._page_image(item)
        region = item["region"]
        width, height = region.physical_size
        packed = image.crop((region.x, region.y, region.x + width, region.y + height))
        if page.pma:
            packed = _unpremultiply(packed)
        cropped = _rotate_from_page(packed, region.rotation)
        result = Image.new("RGBA", region.original_size)
        result.paste(cropped, (region.offset_x, region.offset_top))
        return result

    def replace_atlas_region(self, atlas: str, region_id: str, source: str | Path):
        from PIL import Image
        from app.core.spine_atlas import _premultiply, _rotate_to_page
        item = self._region(atlas, region_id)
        region = item["region"]
        with Image.open(source) as replacement:
            replacement = replacement.convert("RGBA")
        if replacement.size != region.original_size:
            raise AnimationEditingError(f"Replacement size must match the untrimmed region: {region.original_size}.")
        box = (region.offset_x, region.offset_top, region.offset_x + region.logical_size[0], region.offset_top + region.logical_size[1])
        alpha = replacement.getchannel("A")
        alpha.paste(0, box)
        if alpha.getbbox():
            raise AnimationEditingError("Replacement pixels exceed the atlas trim bounds; resize/repack is required.")
        cropped = _rotate_to_page(replacement.crop(box), region.rotation)
        image, page, relative = self._page_image(item)
        if page.pma:
            cropped = _premultiply(cropped)
        image.paste(cropped, (region.x, region.y))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        self._change(lambda: self._textures.update({relative: buffer.getvalue()}))

    def prepare_preview(self):
        """Write an edited render copy, preserving source and export inputs."""
        from app.core.spine_preview import SpinePreviewPlan, discover_spine_runtime, load_spine_asset
        if not self.preview_root.exists():
            self.preview_root.mkdir()
            for relative, source in self.project.references.items():
                target = self.preview_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        relative = self.project.model_path.relative_to(self.project.source_root).with_suffix(".json")
        target = self.preview_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.document, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        for relative, source in self.project.references.items():
            if relative in self._textures:
                (self.preview_root / relative).write_bytes(self._textures[relative])
            elif source.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                # Undoing a texture replacement restores the copied source.
                shutil.copy2(source, self.preview_root / relative)
        asset = load_spine_asset(target)
        runtime = None
        error = None
        try:
            runtime = discover_spine_runtime(requested_family=asset.family)
        except Exception as exc:
            error = str(exc)
        return SpinePreviewPlan("native" if runtime else "atlas", asset, runtime, reason=error or "", runtime_error=error)

    def save_copy(self, output: str | Path) -> dict:
        output = Path(output).expanduser().resolve()
        if output.exists() or output.is_relative_to(self.original_root):
            raise AnimationEditingError("Export to a new directory outside the original source package.")
        candidate = copy.copy(self.project)
        candidate.document = copy.deepcopy(self.document)
        candidate.wrapper = copy.deepcopy(self.project.wrapper)
        candidate.settings = copy.deepcopy(self.project.settings)
        # The original MCP refuses empty newly authored tracks. A GUI session
        # can intentionally delete a last key, edit a slot-only animation, or
        # change only the setup pose. These remain valid runtime documents.
        candidate.modified = {name for name in self.project.modified if name in self.animation_names and candidate._summary(name)["timelines"]}
        if candidate.wrapper is not None:
            motions = candidate.wrapper.setdefault("motions", {})
            for name in self.animation_names:
                motions.setdefault(name, [{"file": name, "wrap_mode": 0}])
        metadata = {"format": "LpkUnpacker.SpineEditor", "version": 1,
                    "animations": self.project.settings, "original_source": str(self.original_source)}
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".lpk-spine-export-", dir=output.parent) as stage_dir:
            package = Path(stage_dir) / "package"
            result = candidate.save_copy(package)
            for relative, data in self._textures.items():
                (package / relative).write_bytes(data)
            (package / SPINE_EDITOR_MANIFEST).write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if output.exists():
                raise AnimationEditingError("Output appeared during export; refusing to overwrite it.")
            for key in ("model_path", "skeleton_path", "manifest_path"):
                result[key] = str(output / Path(result[key]).relative_to(package)) if result.get(key) else None
            result["animation_paths"] = [str(output / Path(path).relative_to(package)) for path in result["animation_paths"]]
            result["output_dir"] = str(output)
            _rename_directory(package, output)
        self.last_saved_path = result["model_path"]
        self._saved = self._signature()
        return result

    def close(self):
        if not self._closed:
            self._closed = True
            self._temporary.cleanup()


__all__ = ["SpineEditorSession", "BONE_FIELDS", "decode_bone_track", "encode_bone_track",
           "decode_draw_order", "encode_draw_order", "SPINE_EDITOR_MANIFEST"]
