"""Portable PSD/MOD attachments owned by an isolated Live2D editor session."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any
import uuid

from app.core.animation_editing import AnimationEditingError, _relative
from app.core.live2d_references import iter_live2d_asset_references


_RUNTIME_FIELDS = {
    "live2d_dir", "base_model_json", "psd", "metadata", "source_psd", "source_psds",
    "source_textures", "textures_dir", "output_paths", "texture_outputs", "directory",
    "current_dir", "source_root", "source_model", "workspace_path", "model_json",
    "textures", "source_texture", "generated_output_dir", "generated_model_json_paths",
    "baseline_path", "last_export_path",
}


class _ProjectCopier:
    def __init__(self, source_workspace: Path, target_workspace: Path, dependency_root: Path):
        self.source_workspace = source_workspace
        self.target_workspace = target_workspace
        self.dependency_root = dependency_root
        self.imported: dict[Path, Path] = {}

    def _relocate(self, value: str, source_parent: Path, target_parent: Path) -> str:
        source = Path(value)
        source = (source if source.is_absolute() else source_parent / source).resolve()
        if source.is_relative_to(self.source_workspace):
            target = self.target_workspace / source.relative_to(self.source_workspace)
        else:
            target = None
            for root, copied in sorted(self.imported.items(), key=lambda item: len(item[0].parts), reverse=True):
                if source == root or (root.is_dir() and source.is_relative_to(root)):
                    target = copied / source.relative_to(root)
                    break
            if target is None:
                if not source.exists():
                    raise AnimationEditingError(f"Missing external project dependency: {source}")
                identity = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:12]
                target = self.dependency_root / ".project_assets" / f"{identity}_{source.name}"
                self.imported[source] = target
                if source.is_dir():
                    self.copy_tree(source, target)
                else:
                    self.copy_file(source, target)
        return Path(os.path.relpath(target, target_parent)).as_posix()

    def _copy_external_baseline(self, data: dict, metadata_parent: Path) -> None:
        root_value = str(data.get("source_root") or "")
        if not root_value:
            return
        root = Path(root_value)
        root = (root if root.is_absolute() else metadata_parent / root).resolve()
        if root.is_relative_to(self.source_workspace) or root in self.imported:
            return
        model_value = data.get("source_model") or (data.get("source_model_summary") or {}).get("path")
        if not model_value:
            raise AnimationEditingError("An external PSD baseline requires its source model JSON.")
        model = Path(str(model_value))
        model = (model if model.is_absolute() else metadata_parent / model).resolve()
        if not model.is_relative_to(root) or not model.is_file():
            raise AnimationEditingError(f"Missing or escaping PSD baseline model: {model}")
        identity = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
        target = self.dependency_root / ".project_assets" / f"{identity}_{root.name}"
        self.imported[root] = target
        target.mkdir(parents=True, exist_ok=False)
        self.copy_file(model, target / model.relative_to(root))
        document = json.loads(model.read_text(encoding="utf-8-sig"))
        refs = document.get("FileReferences", {})
        required = {refs.get("Moc"), *refs.get("Textures", [])}
        files = set(iter_live2d_asset_references(refs))
        files.update({"live2d_parameter_inventory.json", f"{model.stem}.drawables.json", "drawables.json"})
        for value in files:
            relative = _relative(value)
            source = (root / relative).resolve()
            if not source.is_relative_to(root):
                raise AnimationEditingError(f"Escaping PSD baseline resource: {value}")
            if source.is_file():
                self.copy_file(source, target / relative)
            elif value in required:
                raise AnimationEditingError(f"Missing PSD baseline resource: {source}")

    def _portable_json(self, value: Any, source_parent: Path, target_parent: Path,
                       *, key: str = "", parents: tuple[str, ...] = (), metadata: bool = False):
        if isinstance(value, dict):
            return {child_key: self._portable_json(child, source_parent, target_parent,
                    key=child_key, parents=parents + (key,), metadata=metadata)
                    for child_key, child in value.items()}
        if isinstance(value, list):
            return [self._portable_json(child, source_parent, target_parent,
                    key=key, parents=parents, metadata=metadata) for child in value]
        runtime = key in _RUNTIME_FIELDS or "texture_outputs" in parents
        runtime |= metadata and key == "source_path"
        runtime |= metadata and key == "path" and any(
            parent in {"source_model_summary", "source_moc_summary"} for parent in parents)
        if isinstance(value, str) and value and runtime:
            return self._relocate(value, source_parent, target_parent)
        return value

    def copy_file(self, source: Path, copied: Path) -> None:
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, copied)
        if source.suffix.lower() != ".json":
            return
        try:
            data = json.loads(source.read_text(encoding="utf-8-sig"))
        except (UnicodeError, ValueError):
            return
        if not isinstance(data, dict) or isinstance(data.get("FileReferences"), dict):
            # Immutable model snapshots must retain their original byte digest.
            return
        format_name = str(data.get("format") or "")
        metadata = bool(data.get("source_model_summary") or data.get("source_root"))
        is_project = format_name in {"LpkUnpacker.Live2DPSDProject", "LpkUnpacker.Live2DViewerModProject"}
        if not (metadata or is_project):
            return
        if metadata:
            self._copy_external_baseline(data, source.parent)
        portable = self._portable_json(data, source.parent, copied.parent, metadata=metadata)
        if portable != data:
            copied.write_text(json.dumps(portable, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def copy_tree(self, source: Path, target: Path) -> None:
        if target.exists() or target.is_relative_to(source):
            raise AnimationEditingError("Copy a project to a new independent workspace directory.")
        files = []
        for path in source.rglob("*"):
            if not path.resolve().is_relative_to(source):
                raise AnimationEditingError(f"Escaping project resource: {path}")
            if path.is_file():
                files.append(path)
        target.mkdir(parents=True)
        for path in files:
            self.copy_file(path, target / path.relative_to(source))


def copy_project_tree(source: Path, target: Path, *, source_workspace: Path | None = None,
                      target_workspace: Path | None = None) -> None:
    source, target = source.resolve(), target.resolve()
    source_workspace = (source_workspace or source).resolve()
    target_workspace = (target_workspace or target).resolve()
    _ProjectCopier(source_workspace, target_workspace, target).copy_tree(source, target)


class Live2DEditorProjects:
    """Use the established PSD/MOD schemas, with relative registry entries."""

    def __init__(self, session):
        self.session = session
        self.root = session.root
        self.files: dict[str, str] = {}
        self._known: dict[str, str] = {}

    @staticmethod
    def _fingerprint(project):
        return json.dumps(project.data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _remember(self, kind):
        self._known[kind] = self._fingerprint(self.get(kind))

    @staticmethod
    def _api(kind):
        from app.core import psd_project, live2dviewer_mod_project
        return psd_project if kind == "psd" else live2dviewer_mod_project

    def get(self, kind):
        relative = self.files.get(kind)
        return self._api(kind).load_project(self.root / relative) if relative else None

    def restore(self, source_root: Path, state: dict) -> None:
        for kind, entry in state.get("projects", {}).items():
            if kind not in {"psd", "mods"}:
                continue
            value = entry.get("file") if isinstance(entry, dict) else entry
            relative = _relative(value)
            if Path(relative).parts[0] != kind:
                raise AnimationEditingError(f"The {kind} project must be inside its managed directory: {value}")
            path = (source_root / relative).resolve()
            if not path.is_relative_to(source_root) or not path.is_file():
                raise AnimationEditingError(f"Missing or escaping {kind} project: {value}")
            target = self.root / kind
            copy_project_tree(source_root / kind, target, source_workspace=source_root, target_workspace=self.root)
            self.files[kind] = relative
            self._remember(kind)

    def ensure(self, kind):
        project = self.get(kind)
        if project:
            return project
        # A separate snapshot prevents the legacy importer's whole-model
        # copy from recursively including its own psd/mods destination.
        with tempfile.TemporaryDirectory(prefix="lpk-editor-snapshot-") as temporary:
            model = self.session.export_snapshot(Path(temporary) / "model")
            api = self._api(kind)
            create = api.create_project_from_source if kind == "psd" else api.create_project_from_base_source
            project = create(model, project_name=kind, output_root=self.root)
        self.files[kind] = project.project_file.relative_to(self.root).as_posix()
        self._remember(kind)
        self.session.mark_project_changed()
        return self.get(kind)

    def attach(self, kind, source):
        api = self._api(kind)
        project = api.load_project(source)
        if project.project_dir.is_relative_to(self.root / kind):
            return self.bind(kind, project)
        target = self.root / kind
        if target.exists():
            target = target / ("imported_" + uuid.uuid4().hex[:10])
        # Keep imported dependency files within the managed tree, so all
        # subsequent save/reopen operations copy them with that tree.
        with tempfile.TemporaryDirectory(prefix=".lpk-attach-", dir=self.root) as temporary:
            stage = Path(temporary) / "project"
            copy_project_tree(project.project_dir, stage)
            target.parent.mkdir(parents=True, exist_ok=True)
            stage.rename(target)
        self.files[kind] = (target / project.project_file.name).relative_to(self.root).as_posix()
        self._remember(kind)
        self.session.mark_project_changed()
        return self.get(kind)

    def bind(self, kind, project):
        if project is None:
            return self.detach(kind)
        path = project.project_file.resolve()
        if not path.is_relative_to(self.root / kind):
            return self.attach(kind, path)
        self._api(kind).save_project(project.project_dir, project.data)
        relative = path.relative_to(self.root).as_posix()
        changed = self.files.get(kind) != relative or self._known.get(kind) != self._fingerprint(self._api(kind).load_project(path))
        self.files[kind] = relative
        self._remember(kind)
        if changed:
            self.session.mark_project_changed()
        return self.get(kind)

    def detach(self, kind):
        if self.files.pop(kind, None) is not None:
            self._known.pop(kind, None)
            self.session.mark_project_changed()

    def export(self, destination: Path) -> dict:
        result = {}
        copied = set()
        for kind, relative in self.files.items():
            # Copy each managed tree once, including imported versions/history.
            first = Path(relative).parts[0]
            if first not in copied:
                copy_project_tree(self.root / first, destination / first,
                                  source_workspace=self.root, target_workspace=destination)
                copied.add(first)
            result[kind] = {"file": relative}
        return result
