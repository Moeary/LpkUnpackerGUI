"""Private, atomic recovery checkpoints; never overwrite a source or a user save.

Each editor run has its own UUID directory. Immutable blobs are deduplicated
within it, so three generations do not triple the PSD/texture disk footprint.
Only complete manifests are discoverable; interrupted staging is ignored.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from app.core.live2d_editor_project import _ProjectCopier, copy_project_tree

FORMAT = "LpkUnpacker.Recovery"


def _json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _inside(root, relative):
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise ValueError("Recovery path escapes its package")
    return path


def _tree_signature(path):
    return {str(p.relative_to(path)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in path.rglob("*") if p.is_file()}


@dataclass
class RecoveryCapture:
    kind: str
    source: str
    request: object
    lease: object = None
    skins: object = None
    skin_blobs: object = None
    selections: object = None
    workspace: Path | None = None
    projects: object = None

    def close(self):
        if self.kind == "live2d":
            self.request.close()
        elif self.lease:
            self.lease.close()

    def write(self, target):
        if self.kind == "spine":
            return Path(self.request.save_copy(target)["model_path"])
        model = self.request.write(target)
        manifest = target / "lpk_live2d_editor.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["skins"] = self.skins
        data["named_selections"] = self.selections
        for relative, blob in self.skin_blobs.items():
            path = _inside(target, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
        for kind, (relative, project_data) in self.projects.items():
            source_tree = self.workspace / kind
            before = _tree_signature(source_tree)
            copy_project_tree(source_tree, target / kind, source_workspace=self.workspace, target_workspace=target)
            if before != _tree_signature(source_tree):
                raise RuntimeError("Project assets changed while preparing recovery; retry on the next interval.")
            # Capture pending form data without marking the real subproject saved.
            destination = _inside(target, relative)
            copier = _ProjectCopier(self.workspace, target, target / kind)
            portable = copier._portable_json(project_data, (self.workspace / relative).parent, destination.parent)
            _json(destination, portable)
            data["projects"][kind] = {"file": relative}
        _json(manifest, data)
        return model


def capture_recovery(session, kind, *, project_overrides=None):
    """Capture mutable state on the GUI thread; workers receive no live Core/Qt."""
    if kind == "spine":
        clone = copy.copy(session)
        clone.project = copy.deepcopy(session.project)
        clone._textures = dict(session._textures)
        # This private export reads only the leased workspace. A user's source
        # can be an ancestor of runtime/recovery; no original file is written.
        clone.original_root = session.workspace
        return RecoveryCapture(kind, str(session.original_source), clone, lease=session._asset_lifetime.acquire())
    request = session.capture_export_snapshot()
    try:
        skins = copy.deepcopy(session._skins.state)
        blobs = {}
        for entry in skins["entries"]:
            for texture, blob in zip(entry["textures"], session._skins.bytes(entry["id"])):
                blobs[texture["path"]] = blob
        projects = {}
        for name, relative in session._projects.files.items():
            project = session._projects.get(name)
            projects[name] = (relative, copy.deepcopy((project_overrides or {}).get(name, project.data)))
        return RecoveryCapture(kind, str(session.source_path), request, skins=skins, skin_blobs=blobs,
                               selections=session.named_selections(), workspace=session.root, projects=projects)
    except Exception:
        request.close()
        raise


class RecoveryStore:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def directory(self, identity):
        if not re.fullmatch(r"[0-9a-f]{32}", identity):
            raise ValueError("Invalid recovery identity")
        return _inside(self.root, identity)

    def prepare(self, identity, capture):
        """Write blobs in the worker. Publish the manifest only on GUI acceptance."""
        directory = self.directory(identity)
        blobs = directory / "blobs"
        blobs.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix=".stage-", dir=directory) as temporary:
                package = Path(temporary) / "package"
                model = capture.write(package)
                files = {}
                for path in package.rglob("*"):
                    if not path.is_file():
                        continue
                    if path.is_symlink() or not path.resolve().is_relative_to(package.resolve()):
                        raise ValueError("Recovery packages cannot contain external links")
                    data = path.read_bytes()
                    digest = hashlib.sha256(data).hexdigest()
                    blob = blobs / digest
                    if not blob.exists():
                        pending = Path(temporary) / digest
                        pending.write_bytes(data)
                        os.replace(pending, blob)
                    files[path.relative_to(package).as_posix()] = digest
                return {"format": FORMAT, "version": 1, "id": identity, "kind": capture.kind,
                        "source": capture.source, "created": datetime.now(timezone.utc).isoformat(),
                        "model": model.relative_to(package).as_posix(), "files": files}
        finally:
            capture.close()

    def publish(self, record, keep=3):
        directory = self.directory(record["id"])
        name = "checkpoint-" + uuid.uuid4().hex + ".json"
        temporary = directory / ("." + name)
        _json(temporary, record)
        os.replace(temporary, directory / name)
        records = sorted(directory.glob("checkpoint-*.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
        for path in records[max(1, min(10, int(keep))) :]:
            path.unlink()
        used = {digest for path in records[:max(1, min(10, int(keep)))]
                for digest in json.loads(path.read_text(encoding="utf-8"))["files"].values()}
        # This UUID is owned by one writer. Never collect another session's blobs.
        for path in (directory / "blobs").iterdir():
            if re.fullmatch(r"[0-9a-f]{64}", path.name) and path.name not in used and not path.is_symlink():
                path.unlink()
        return record

    def records(self, kind=None):
        records = []
        if not self.root.exists():
            return records
        for directory in self.root.iterdir():
            if not re.fullmatch(r"[0-9a-f]{32}", directory.name) or directory.is_symlink():
                continue
            if (directory / "dismissed").exists():
                continue
            for path in directory.glob("checkpoint-*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if data.get("format") != FORMAT or data.get("version") != 1 or data.get("id") != directory.name:
                        continue
                    datetime.fromisoformat(data["created"])
                    if not isinstance(data["files"], dict) or not isinstance(data["source"], str):
                        continue
                    _inside(directory, data["model"])
                    if kind is None or data.get("kind") == kind:
                        records.append(data)
                except (OSError, ValueError, TypeError, KeyError, AttributeError):
                    continue
        return sorted(records, key=lambda item: item.get("created", ""), reverse=True)

    def restore(self, record, destination):
        directory = self.directory(record["id"])
        destination = Path(destination).resolve()
        destination.mkdir(parents=True, exist_ok=False)
        for relative, digest in record["files"].items():
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("Invalid recovery blob")
            blob = _inside(directory / "blobs", digest)
            data = blob.read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("Recovery data is damaged; choose an older checkpoint")
            target = _inside(destination, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        model = _inside(destination, record["model"])
        if not model.is_file():
            raise ValueError("Recovery model is missing")
        return model

    def dismiss(self, identity):
        directory = self.directory(identity)
        if directory.is_dir():
            (directory / "dismissed").touch()
