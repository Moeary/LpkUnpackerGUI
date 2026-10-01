"""Immutable atlas skins for a Live2D editor workspace.

The registry contains relative asset paths, not rendering commands.  Applying a
skin changes only the working atlas bytes; the editor owns motions and pose.
ViewerEX commands are generated exclusively by the existing MOD service.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Mapping

from PIL import Image

from app.core.animation_editing import AnimationEditingError, _read_json, _relative, _rename_directory


SKINS_FORMAT = "LpkUnpacker.Live2DSkins"
SKINS_VERSION = 1
ORIGINAL_SKIN = "original"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tga"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _metadata(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AnimationEditingError("Skin provenance metadata must be a JSON object.")
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise AnimationEditingError("Skin provenance metadata must contain finite JSON values.") from exc


def _asset(root: Path, reference: str) -> Path:
    path = (root / _relative(reference)).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise AnimationEditingError(f"Missing or escaping skin asset: {reference}")
    return path


def _uv_signature(snapshot: dict) -> str | None:
    drawables = snapshot.get("drawables", [])
    if not drawables:
        return None
    # Deformed vertex positions and current opacity are deliberately excluded.
    # IDs, atlas assignments, UV coordinates and triangulation all must agree.
    values = [{key: item.get(key) for key in ("id", "texture_index", "uvs", "indices")}
              for item in sorted(drawables, key=lambda item: str(item.get("id", "")))]
    return _sha(json.dumps(values, sort_keys=True, allow_nan=False, separators=(",", ":")).encode())


def _native_uv_signature(path: Path) -> str | None:
    # A user-authored mesh sidecar is not evidence about the compiled MOC.
    # CubismCore.load_moc performs the SDK's native consistency check first.
    if path.read_bytes()[:4] != b"MOC3":
        return None
    try:
        from app.core.cubism_core import CubismCore
        model = CubismCore().load_moc(path)
        return _uv_signature(model.drawable_snapshot())
    except Exception:
        return None


class Live2DSkins:
    def __init__(self, session):
        self.session = session
        self.root = session.root
        moc = _asset(self.root, session.original_document["FileReferences"]["Moc"])
        self.moc_sha256 = _sha(moc.read_bytes())
        self._uv_cache: dict[str, str | None] = {}
        self._source_cache: dict[tuple, Path] = {}
        self._blobs: dict[str, tuple[bytes, ...]] = {}
        original = self._entry(ORIGINAL_SKIN, "Original", tuple(session._texture_data),
                               source_kind="original", source=str(session.source_path),
                               metadata={}, compatibility={"mode": "original", "automatic": True,
                                                            "moc_sha256": self.moc_sha256}, mappings=[])
        self.state = {"format": SKINS_FORMAT, "version": SKINS_VERSION,
                      "moc_sha256": self.moc_sha256, "active_id": ORIGINAL_SKIN, "entries": [original]}
        # No directory or duplicate atlas is created merely by opening a model.
        self._blobs[ORIGINAL_SKIN] = tuple(session._texture_data)

    def _entry(self, skin_id, name, blobs, *, source_kind, source, metadata, compatibility, mappings):
        return {"id": skin_id, "name": name, "is_original": skin_id == ORIGINAL_SKIN,
                "source_kind": source_kind, "source": {"kind": source_kind, "path": source,
                                                         "metadata": copy.deepcopy(metadata)},
                "compatibility": copy.deepcopy(compatibility), "mappings": copy.deepcopy(mappings),
                "textures": [{"index": index, "path": f"skins/{skin_id}/atlas_{index}.png",
                              "sha256": _sha(data), "width": size[0], "height": size[1]}
                             for index, (data, size) in enumerate(zip(blobs, self.session.texture_sizes))]}

    def get(self, skin_id):
        entry = next((item for item in self.state["entries"] if item["id"] == skin_id), None)
        if entry is None:
            raise AnimationEditingError(f"Unknown skin: {skin_id}")
        return entry

    @property
    def active_id(self):
        return self.state["active_id"]

    @property
    def modified(self):
        return tuple(self.session._texture_data) != self.bytes(self.active_id)

    def list(self):
        result = copy.deepcopy(self.state["entries"])
        for item in result:
            item["texture_paths"] = [str(self.root / texture["path"]) for texture in item["textures"]]
            item["active"] = item["id"] == self.active_id
        return result

    def _name(self, name, *, exclude=None):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80 or any(ord(c) < 32 for c in name):
            raise AnimationEditingError("Skin name must contain 1–80 printable characters.")
        name = name.strip()
        if any(item["id"] != exclude and item["name"].casefold() == name.casefold() for item in self.state["entries"]):
            raise AnimationEditingError(f"Skin name already exists: {name}")
        return name

    def _unique_name(self, base):
        names = {item["name"].casefold() for item in self.state["entries"]}
        name, index = base, 2
        while name.casefold() in names:
            name = f"{base} {index}"
            index += 1
        return name

    def bytes(self, skin_id):
        entry = self.get(skin_id)
        if skin_id in self._blobs:
            return self._blobs[skin_id]
        blobs = []
        for texture in entry["textures"]:
            path = _asset(self.root, texture["path"])
            data = path.read_bytes()
            self._validate_image(data, texture["index"])
            if _sha(data) != texture["sha256"]:
                raise AnimationEditingError(f"Saved skin texture was modified: {texture['path']}")
            blobs.append(data)
        self._blobs[skin_id] = tuple(blobs)
        return self._blobs[skin_id]

    def _materialize(self, skin_id, destination):
        entry = self.get(skin_id)
        for texture, data in zip(entry["textures"], self.bytes(skin_id)):
            path = destination / texture["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                if _sha(path.read_bytes()) != texture["sha256"]:
                    raise AnimationEditingError(f"Immutable skin texture changed: {path}")
            else:
                path.write_bytes(data)

    def paths(self, skin_id):
        self._materialize(skin_id, self.root)
        return [self.root / texture["path"] for texture in self.get(skin_id)["textures"]]

    def export_registry(self, destination):
        # Removed/undone assets may still back undo entries.  Only live registry
        # entries are shipped, never the entire managed skins directory.
        for entry in self.state["entries"]:
            self._materialize(entry["id"], destination)
        return copy.deepcopy(self.state)

    def restore(self, source_root, state):
        if state.get("format") != SKINS_FORMAT or state.get("version") != SKINS_VERSION:
            raise AnimationEditingError("Unsupported Live2D skin registry.")
        if state.get("moc_sha256") != self.moc_sha256:
            raise AnimationEditingError("The skin registry belongs to another MOC3 model.")
        entries = state.get("entries")
        if not isinstance(entries, list) or not entries:
            raise AnimationEditingError("Skin registry is empty.")
        seen, names = set(), set()
        for entry in entries:
            skin_id, name = entry.get("id"), entry.get("name")
            if (not isinstance(skin_id, str) or not skin_id or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in skin_id)
                    or skin_id in seen or not isinstance(name, str) or not name.strip() or name.casefold() in names):
                raise AnimationEditingError("Invalid or duplicate skin identity.")
            seen.add(skin_id)
            names.add(name.casefold())
            if bool(entry.get("is_original")) != (skin_id == ORIGINAL_SKIN):
                raise AnimationEditingError("Original skin identity cannot be changed.")
            textures = entry.get("textures", [])
            if len(textures) != len(self.session.texture_paths):
                raise AnimationEditingError("Saved skin has a different atlas count.")
            for index, texture in enumerate(textures):
                relative = _relative(texture.get("path"))
                if texture.get("index") != index or Path(relative).parts[:2] != ("skins", skin_id):
                    raise AnimationEditingError("Saved skin asset must remain inside its own directory.")
                source = _asset(source_root, relative)
                data = source.read_bytes()
                self._validate_image(data, index)
                if _sha(data) != texture.get("sha256"):
                    raise AnimationEditingError(f"Saved skin texture digest changed: {relative}")
                target = self.root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        if ORIGINAL_SKIN not in seen or state.get("active_id") not in seen:
            raise AnimationEditingError("Saved skin registry has no original or active skin.")
        self.state = copy.deepcopy(state)
        self._blobs.clear()

    def _validate_image(self, data, index):
        try:
            with Image.open(io.BytesIO(data)) as image:
                size = image.size
                image.verify()
        except (OSError, ValueError) as exc:
            raise AnimationEditingError(f"Invalid image for atlas {index}: {exc}") from exc
        if size != self.session.texture_sizes[index]:
            raise AnimationEditingError(f"Atlas {index} must remain {self.session.texture_sizes[index][0]} × {self.session.texture_sizes[index][1]}.")

    def _add(self, name, blobs, *, source_kind, source="", metadata=None, compatibility=None, mappings=None):
        if not isinstance(source_kind, str) or not source_kind:
            raise AnimationEditingError("Skin source kind must be a nonempty string.")
        skin_id = "skin_" + uuid.uuid4().hex
        entry = self._entry(skin_id, name, blobs, source_kind=source_kind, source=source,
                            metadata=_metadata(metadata), compatibility=compatibility or {"mode": "current", "automatic": True},
                            mappings=mappings or [])
        self._blobs[skin_id] = tuple(blobs)
        self.state["entries"].append(entry)
        # These are owned immutable files, retained while undo can refer to them.
        self._materialize(skin_id, self.root)
        return entry

    def _retain_working_edit(self):
        if self.modified:
            self._add(self._unique_name("Working edit"), tuple(self.session._texture_data), source_kind="working",
                      metadata={"previous_skin_id": self.active_id})

    def _apply(self, skin_id, *, preserve=True):
        blobs = self.bytes(skin_id)
        if preserve:
            self._retain_working_edit()
        for index, (data, previous) in enumerate(zip(blobs, self.session._texture_data)):
            if data != previous:
                self.session._write_texture(self.session.texture_paths[index], data)
                self.session._texture_data[index] = data
        self.state["active_id"] = skin_id

    def apply(self, skin_id):
        self.get(skin_id)
        if self.active_id == skin_id and not self.modified:
            return False
        self.session._record(lambda: self._apply(skin_id))
        return True

    def capture(self, name, *, source_kind="current", metadata=None):
        name = self._name(name)
        def action():
            entry = self._add(name, tuple(self.session._texture_data), source_kind=source_kind, metadata=metadata)
            self.state["active_id"] = entry["id"]
            return copy.deepcopy(entry)
        return self.session._record(action)

    def clone(self, skin_id, name):
        name, source = self._name(name), self.get(skin_id)
        blobs = self.bytes(skin_id)
        return self.session._record(lambda: copy.deepcopy(self._add(name, blobs, source_kind="clone",
                                   metadata={"source_skin_id": skin_id}, compatibility=source["compatibility"],
                                   mappings=source.get("mappings", []))))

    def rename(self, skin_id, name):
        entry = self.get(skin_id)
        if skin_id == ORIGINAL_SKIN:
            raise AnimationEditingError("The original skin is read-only.")
        name = self._name(name, exclude=skin_id)
        self.session._record(lambda: entry.update(name=name))
        return copy.deepcopy(entry)

    def remove(self, skin_id):
        self.get(skin_id)
        if skin_id == ORIGINAL_SKIN:
            raise AnimationEditingError("The original skin is read-only.")
        def action():
            if self.active_id == skin_id:
                self._apply(ORIGINAL_SKIN)
            self.state["entries"] = [item for item in self.state["entries"] if item["id"] != skin_id]
        self.session._record(action)
        return True

    def _model_source(self, source):
        from app.core.psd_reconstructor import resolve_live2d_source
        source = Path(source).resolve()
        if source.is_file() and source.suffix.lower() not in {".json", ".moc3"}:
            from app.core.model.importer import prepare_live2d_source_import
            stat = source.stat()
            token = (str(source), stat.st_size, stat.st_mtime_ns)
            prepared = self._source_cache.get(token)
            if prepared is None:
                # Reuse the package importer and its safety/preflight checks.
                # All temporary package ownership remains under this session;
                # no prepared model is ever added to the saved skin registry.
                imported = prepare_live2d_source_import(source, temp_root=self.root / ".skin_imports")
                prepared = imported.package.model_json
                self._source_cache[token] = prepared
            source = prepared
        info = resolve_live2d_source(source)
        document = _read_json(info.model_json)
        if document.get("Version") != 3 or not isinstance(document.get("FileReferences"), dict):
            raise AnimationEditingError("Choose a Cubism Version 3 model JSON.")
        references = document["FileReferences"]
        moc = _asset(info.model_json.parent, references.get("Moc"))
        textures = [_asset(info.model_json.parent, reference) for reference in references.get("Textures", [])]
        return info.model_json, moc, textures

    def _source(self, path):
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
            return None, None, [path]
        if path.is_dir():
            from app.core.psd_reconstructor import PsdReconstructionError, resolve_live2d_source
            try:
                resolve_live2d_source(path)
            except PsdReconstructionError:
                images = sorted((item.resolve() for item in path.rglob("*")
                                 if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES), key=str)
                if not images:
                    raise AnimationEditingError("The source contains no Live2D model or atlas images.")
                return None, None, images
        return self._model_source(path)

    def inspect_source(self, source):
        """Describe candidate atlases; dimensions alone never enable auto-match."""
        path = Path(source).expanduser().resolve()
        model, moc, textures = self._source(path)
        evidence = self._compatibility(moc) if moc else None
        return {"source": str(path), "model_path": str(model) if model else None,
                "textures": [{"index": index, "path": str(texture), "size": list(self.session._image_size(texture))}
                             for index, texture in enumerate(textures)],
                "compatibility": evidence or {"mode": "mapping_required", "automatic": False},
                "automatic_mapping": bool(evidence and len(textures) == len(self.session.texture_paths))}

    def _compatibility(self, moc):
        digest = _sha(moc.read_bytes())
        if digest == self.moc_sha256:
            return {"mode": "same_moc", "automatic": True, "moc_sha256": digest}
        if self.moc_sha256 not in self._uv_cache:
            model = self.session._core_model
            self._uv_cache[self.moc_sha256] = _uv_signature(model.drawable_snapshot()) if model else None
        if digest not in self._uv_cache:
            self._uv_cache[digest] = _native_uv_signature(moc)
        signature = self._uv_cache[self.moc_sha256]
        if signature and signature == self._uv_cache[digest]:
            return {"mode": "same_uv", "automatic": True, "moc_sha256": digest, "uv_sha256": signature}
        return None

    def import_skin(self, source, name=None, *, mapping=None, source_kind="model", metadata=None, activate=False):
        source_path = Path(source).expanduser().resolve() if source is not None else None
        textures, moc, model = [], None, None
        if source_path is not None:
            model, moc, textures = self._source(source_path)
        name = self._name(name or (source_path.stem if source_path else "Imported skin"))
        evidence = self._compatibility(moc) if moc and mapping is None else None
        if mapping is None:
            if not evidence:
                raise AnimationEditingError("Atlas UV compatibility is unproven; provide an explicit target-index to image mapping.")
            if len(textures) != len(self.session.texture_paths):
                raise AnimationEditingError("Automatically matched models must have the same atlas count.")
            mapping = dict(enumerate(textures))
        else:
            if not isinstance(mapping, Mapping) or not mapping:
                raise AnimationEditingError("Provide at least one explicit atlas mapping.")
            evidence = {"mode": "explicit_mapping", "automatic": False,
                        "source_moc_sha256": _sha(moc.read_bytes()) if moc else None,
                        "target_moc_sha256": self.moc_sha256}
        blobs = list(self.session._texture_data)
        mappings = []
        for target_index, source_image in mapping.items():
            if isinstance(target_index, bool) or not isinstance(target_index, int) or not 0 <= target_index < len(blobs):
                raise AnimationEditingError(f"Unknown target atlas index: {target_index}")
            source_index = None
            if isinstance(source_image, int) and not isinstance(source_image, bool):
                source_index = source_image
                if not 0 <= source_index < len(textures):
                    raise AnimationEditingError(f"Unknown source atlas index: {source_index}")
                image_path = textures[source_index]
            else:
                image_path = Path(source_image).expanduser().resolve()
                if image_path in textures:
                    source_index = textures.index(image_path)
            data = image_path.read_bytes()
            self._validate_image(data, target_index)
            # Store canonical PNG bytes regardless of the selected image format.
            if image_path.suffix.lower() != ".png":
                with Image.open(io.BytesIO(data)) as image:
                    buffer = io.BytesIO()
                    image.convert("RGBA").save(buffer, format="PNG")
                    data = buffer.getvalue()
            blobs[target_index] = data
            mappings.append({"target_index": target_index, "source_index": source_index,
                             "source_path": str(image_path), "source_sha256": _sha(image_path.read_bytes())})
        # Validate everything before publishing registry/assets or applying bytes.
        # Provenance is JSON-only, never an operational external dependency.
        metadata = _metadata(metadata)
        def action():
            if activate:
                self._retain_working_edit()
            entry = self._add(name, blobs, source_kind=source_kind, source=str(source_path or ""), metadata=metadata,
                              compatibility=evidence, mappings=mappings)
            if activate:
                self._apply(entry["id"], preserve=False)
            return copy.deepcopy(entry)
        return self.session._record(action)

    def _new_output(self, output):
        output = Path(output).expanduser().resolve()
        if output.exists() or output.is_relative_to(self.session.source_root) or self.session.source_root.is_relative_to(output):
            raise AnimationEditingError("Export to a new folder outside the source package.")
        return output

    def export_skin(self, skin_id, output):
        output = self._new_output(output)
        current = skin_id is None or skin_id == "current"
        blobs = tuple(self.session._texture_data) if current else self.bytes(skin_id)
        name = "Current working textures" if current else self.get(skin_id)["name"]
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".lpk-skin-export-", dir=output.parent) as temporary:
            stage = Path(temporary) / "model"
            result = self.session.save_copy(stage, include_projects=False, include_skins=False,
                                            include_editor_metadata=False, mark_saved=False)
            document = _read_json(Path(result["model_path"]))
            for reference, data in zip(document["FileReferences"]["Textures"], blobs):
                _asset(stage, reference).write_bytes(data)
            _rename_directory(stage, output)
        return {"output_dir": str(output), "model_path": str(output / "model.json"), "skin_id": skin_id,
                "skin_name": name, "current_working_textures": current}

    def export_viewerex(self, skin_ids, output, *, trigger_id=None):
        from app.core import live2dviewer_mod_project as mod
        skin_ids = list(skin_ids)
        if not skin_ids or len(set(skin_ids)) != len(skin_ids):
            raise AnimationEditingError("Select one or more distinct skins.")
        output = self._new_output(output)
        for skin_id in skin_ids:
            if skin_id not in (None, "current"):
                self.get(skin_id)
        names = ["Current working textures" if skin_id in (None, "current") else self.get(skin_id)["name"]
                 for skin_id in skin_ids]
        normalized = [mod.sanitize_skin_name(name).casefold() for name in names]
        if len(set(normalized)) != len(normalized):
            raise AnimationEditingError("ViewerEX skin names collide after filename normalization.")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".lpk-viewerex-export-", dir=output.parent) as temporary:
            root = Path(temporary)
            base = self.export_skin(skin_ids[0], root / "base")
            try:
                mod.validate_switch_motion_groups(_read_json(Path(base["model_path"])))
            except mod.Live2DViewerModProjectError as exc:
                raise AnimationEditingError(str(exc)) from exc
            project = mod.create_project_from_base_source(base["model_path"], project_name="ViewerEX skins", output_root=root / "projects")
            data = copy.deepcopy(project.data)
            data["models"][0]["skin_name"] = names[0]
            mod.save_project(project.project_dir, data)
            project = mod.load_project(project.project_file)
            for skin_id, name in zip(skin_ids[1:], names[1:]):
                source = root / ("skin_" + uuid.uuid4().hex)
                source.mkdir()
                blobs = tuple(self.session._texture_data) if skin_id in (None, "current") else self.bytes(skin_id)
                for index, blob in enumerate(blobs):
                    (source / f"atlas_{index}.png").write_bytes(blob)
                project = mod.add_model_to_project(project, source, skin_name=name)
                model_id = project.models[-1]["id"]
                imported = {path.name: path for path in mod.model_texture_paths(project, model_id)}
                project = mod.update_model_mappings(project, model_id,
                                                    [(index, imported[f"atlas_{index}.png"]) for index in range(len(blobs))])
            data = copy.deepcopy(project.data)
            data["selected_artmesh_id"] = str(trigger_id or "")
            mod.save_project(project.project_dir, data)
            project = mod.generate_live2dviewer_mod(mod.load_project(project.project_file))
            generated = project.project_dir / project.data["generated_output_dir"]
            stage = root / "output"
            shutil.copytree(generated, stage)
            _rename_directory(stage, output)
        return {"output_dir": str(output), "model_path": str(output / "model0.json"),
                "manifest_path": str(output / mod.SKIN_MANIFEST_FILE),
                "mapping_path": str(output / mod.MAPPING_TABLE_FILE), "skin_ids": skin_ids,
                "trigger_id": trigger_id, "skin_count": len(skin_ids)}
