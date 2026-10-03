"""Detached, worker-owned Live2D model snapshot export.

Only static asset paths are leased from the editor workspace. Texture bytes,
animation state and pose values belong to the request; no live session, Qt
object, renderer or Cubism model crosses the thread boundary.
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
import threading
import weakref
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from app.core.animation_editing import (
    AnimationEditingError, AnimationEditingProject, INVENTORY_FORMAT,
    PROJECT_FORMAT, _MANIFEST, _asset, _motion_meta, _relative, _rename_directory,
)


class WorkspaceAssetLifetime:
    """Keep the static workspace alive until its owner and requests release it."""

    def __init__(self, temporary):
        self._temporary = temporary
        self._lock = threading.Lock()
        self._owner_open = True
        self._leases = 0

    def acquire(self):
        with self._lock:
            if self._temporary is None:
                raise AnimationEditingError("The snapshot source workspace has been closed.")
            self._leases += 1
        return _WorkspaceAssetLease(self)

    def _release(self):
        with self._lock:
            self._leases -= 1
            temporary = self._retired_temporary()
        if temporary is not None:
            temporary.cleanup()

    def _retired_temporary(self):
        if not self._owner_open and not self._leases:
            temporary, self._temporary = self._temporary, None
            return temporary
        return None

    def close(self):
        with self._lock:
            self._owner_open = False
            temporary = self._retired_temporary()
        if temporary is not None:
            temporary.cleanup()


class _WorkspaceAssetLease:
    def __init__(self, owner):
        self._owner = owner
        self._finalizer = weakref.finalize(self, owner._release)

    def borrow(self):
        if not self._finalizer.alive:
            raise AnimationEditingError("The snapshot request has been closed.")
        return self._owner.acquire()

    def close(self):
        self._finalizer()


class _WorkerAssetLease:
    """A child borrows paths held by its parent's request until process exit."""

    def borrow(self):
        return self

    def close(self):
        pass


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class Live2DExportSnapshotRequest:
    """A captured state with an asset lease; consume it only in CPU workers.

    Nested private state and ``skin_source`` are read-only by contract. A write
    clones its plain animation project before generating output. ``close`` may
    release a cancelled request, while an in-progress write holds its own lease
    until filesystem work finishes. The output is always a new model package.
    """

    _project: AnimationEditingProject = field(repr=False, compare=False)
    _original_document: dict = field(repr=False, compare=False)
    _original_bindings: dict = field(repr=False, compare=False)
    _deleted_original_bindings: frozenset = field(repr=False, compare=False)
    _textures: tuple[tuple[str, bytes], ...] = field(repr=False, compare=False)
    _parameters: dict = field(repr=False, compare=False)
    _parts: dict = field(repr=False, compare=False)
    _mesh_metadata: dict = field(repr=False, compare=False)
    _warnings: tuple[str, ...]
    _source_path: Path
    _skin_at_export: dict = field(repr=False, compare=False)
    _lease: _WorkspaceAssetLease = field(repr=False, compare=False)
    skin_source: dict[str, Any] = field(default_factory=dict, compare=False)
    output_dir: Path | None = None
    _drawable_visibility: dict = field(default_factory=dict, repr=False, compare=False)
    _drawable_opacity: dict = field(default_factory=dict, repr=False, compare=False)

    def close(self):
        self._lease.close()

    def to_worker_payload(self) -> dict:
        """Return private, pickleable capture data without live objects or leases.

        Serialize this in the task thread. The parent must keep this request
        open until the child has actually exited, including cancellation.
        Assets are captured bytes or immutable paths protected by that lease.
        This is a trusted internal protocol, never an imported project file.
        """
        if isinstance(self._lease, _WorkspaceAssetLease) and not self._lease._finalizer.alive:
            raise AnimationEditingError("The snapshot request has been closed.")
        return {item.name: getattr(self, item.name) for item in fields(self) if item.name != "_lease"}

    @classmethod
    def from_worker_payload(cls, payload: dict) -> Live2DExportSnapshotRequest:
        """Restore internal capture data in a CPU child; parent owns asset life."""
        expected = {item.name for item in fields(cls) if item.name != "_lease"}
        if not isinstance(payload, dict) or set(payload) != expected:
            raise AnimationEditingError("Invalid internal snapshot payload.")
        return cls(**payload, _lease=_WorkerAssetLease())

    def write(self, output_dir=None) -> Path:
        destination = self.output_dir if output_dir is None else output_dir
        if destination is None:
            raise AnimationEditingError("Choose a new snapshot output directory.")
        output = Path(destination).expanduser().resolve()
        source_root = self._source_path.parent
        if output.exists() or output.is_relative_to(source_root) or source_root.is_relative_to(output):
            raise AnimationEditingError("Create a snapshot in a new directory outside the source package.")
        lease = self._lease.borrow()
        try:
            return self._write(output)
        finally:
            lease.close()

    def _write(self, output: Path) -> Path:
        project = copy.deepcopy(self._project)
        textures = dict(self._textures)
        references = dict(project.references)
        for relative, source in references.items():
            if _asset(project.source_root, relative) != source:
                raise AnimationEditingError(f"Asset changed/escaped since snapshot capture: {relative}")

        document = copy.deepcopy(self._original_document)
        groups = document["FileReferences"].setdefault("Motions", {})
        generated = {}
        for name in sorted(project.modified):
            relative = f"edited_motions/{hashlib.sha256(name.encode()).hexdigest()[:12]}.motion3.json"
            if relative.casefold() in {value.casefold() for value in references}:
                raise AnimationEditingError(f"Generated motion would overwrite a source asset: {relative}")
            motion = copy.deepcopy(project.motions[name])
            if motion["Curves"]:
                _motion_meta(motion)
            generated[relative] = motion
            group, index = project.bindings[name]
            original_index = self._original_bindings.get((group, index))
            if original_index is None:
                item = {"File": relative, "FadeInTime": 0.0, "FadeOutTime": 0.0}
                if motion["Curves"]:
                    editable_items = project.document["FileReferences"].get("Motions", {}).get(group, [])
                    if index < len(editable_items):
                        item = dict(editable_items[index], File=relative)
                groups.setdefault(group, []).append(item)
            else:
                original_items = groups.setdefault(group, [])
                original_items[original_index] = dict(original_items[original_index], File=relative)
        for group, index in sorted(self._deleted_original_bindings, reverse=True):
            items = groups.get(group, [])
            if index < len(items):
                items.pop(index)

        moc = _asset(project.source_root, document["FileReferences"]["Moc"])
        generated["model.json"] = document
        generated["live2d_parameter_inventory.json"] = {
            "format": INVENTORY_FORMAT, "version": 1,
            "moc_sha256": hashlib.sha256(moc.read_bytes()).hexdigest(), "parameters": project.parameters,
        }
        manifest = {
            "format": PROJECT_FORMAT, "version": 1, "kind": "live2d",
            "source_path": str(self._source_path), "model_path": "model.json", "skeleton_path": None,
            "animations": [project._summary(name) for name in sorted(project.modified)],
            "warnings": project.warnings, "value_semantics": project.inspect()["value_semantics"],
            "parameter_inventory": "live2d_parameter_inventory.json",
            "sources": [{"path": relative, "sha256": hashlib.sha256(
                textures[relative] if relative in textures else path.read_bytes()).hexdigest()}
                        for relative, path in references.items()],
        }
        generated[_MANIFEST] = manifest
        allowed = {project.model_path.relative_to(project.source_root).as_posix().casefold(),
                   project.source_path.relative_to(project.source_root).as_posix().casefold()}
        for relative in generated:
            if relative.casefold() in {value.casefold() for value in references} and relative.casefold() not in allowed:
                raise AnimationEditingError(f"Generated output collides with referenced asset: {relative}")

        # Core is fresh and used exclusively in this worker. The plain sidecar
        # fallback matches editor export when an optional Core is unavailable.
        mesh = copy.deepcopy(self._mesh_metadata)
        try:
            from app.core.cubism_core import CubismCore
            model = CubismCore().load_moc(moc)
            mesh = model.drawable_snapshot(self._parameters)
        except Exception:
            pass
        generated["model.drawables.json"] = mesh
        generated["lpk_live2d_editor.json"] = {
            "format": "LpkUnpacker.Live2DEditor", "version": 2,
            "source_path": str(self._source_path), "pose_parameters": copy.deepcopy(self._parameters),
            "preview_part_opacity": copy.deepcopy(self._parts), "warnings": list(self._warnings),
            "authoring_source": None, "model": "model.json", "projects": {},
            "skin_at_export": copy.deepcopy(self._skin_at_export),
        }
        if self._drawable_visibility:
            generated["lpk_live2d_editor.json"]["preview_drawable_visibility"] = dict(self._drawable_visibility)
        if self._drawable_opacity:
            generated["lpk_live2d_editor.json"]["preview_drawable_opacity"] = dict(self._drawable_opacity)
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".lpk-live2d-snapshot-", dir=output.parent) as temporary:
            stage = Path(temporary) / "package"
            stage.mkdir()
            for relative, source in references.items():
                target = stage / _relative(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                if relative in textures:
                    target.write_bytes(textures[relative])
                elif relative not in generated:
                    shutil.copy2(source, target)
            for relative, data in generated.items():
                _write_json(stage / _relative(relative), data)
            _rename_directory(stage, output)
        return output / "model.json"
