from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from app.core.animation_editing import AnimationEditingError
from app.core import psd_project, live2dviewer_mod_project as mod_project
from app.core.live2d_editor_session import Live2DEditorSession, EDITOR_MANIFEST
from app.core.psd_reconstructor import reconstruct_live2d_psd, repack_atlas_png_from_psd, PsdReconstructionError
from tests.test_live2d_editor_session import make_model


class Live2DEditorProjectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="editor-project-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.model = make_model(self.root / "source")
        self.session = Live2DEditorSession(self.model)
        self.addCleanup(self.session.close)

    def test_snapshot_inside_workspace_keeps_dirty_and_contains_current_motion(self):
        self.session.create_motion("NewPose", 2)
        self.session.set_keyframes("NewPose", "ParamAngleY", [{"time": 0, "value": -10}])
        snapshot = self.session.export_snapshot(self.session.root / "psd" / "snapshots" / "pose")
        self.assertTrue(self.session.dirty)
        self.assertEqual(snapshot.name, "model.json")
        data = json.loads(snapshot.read_text(encoding="utf-8"))
        self.assertTrue((snapshot.parent / data["FileReferences"]["Motions"]["NewPose"][0]["File"]).is_file())
        self.assertEqual(json.loads((snapshot.parent / EDITOR_MANIFEST).read_text(encoding="utf-8"))["projects"], {})
        with self.assertRaises(AnimationEditingError):
            self.session.export_snapshot(snapshot.parent)

    def test_external_psd_baselines_survive_move_cleanup_and_actual_repack(self):
        legacy = psd_project.create_project_from_source(self.model, "Legacy", output_root=self.root / "legacy")
        exported = reconstruct_live2d_psd(self.model, self.root / "external_export", mode="mesh")
        original_meta = json.loads(exported.metadata_path.read_text(encoding="utf-8"))
        legacy = psd_project.record_psd_export(legacy, exported.psd_path, exported.metadata_path,
                                             "mesh", exported.layer_count)
        self.session.create_motion("Retained", 2)
        self.session.set_keyframes("Retained", "ParamAngleY", [{"time": 0, "value": 12}])
        attached = self.session.attach_psd_project(legacy.project_file)
        entry = attached.data["psd_exports"][0]
        meta_path = psd_project.resolve_project_path(attached, entry["metadata"])
        attached_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(attached_meta["source_model_summary"]["sha256"], original_meta["source_model_summary"]["sha256"])
        self.assertFalse(Path(attached_meta["source_model_summary"]["path"]).is_absolute())
        output = self.session.save_copy(self.root / "saved")
        moved = self.root / "moved"
        Path(output["model_path"]).parent.rename(moved)
        self.session.close()
        (self.root / "source").rename(self.root / "retired_source")
        (self.root / "external_export").rename(self.root / "retired_export")
        (self.root / "legacy").rename(self.root / "retired_legacy")
        reopened = Live2DEditorSession(moved / "model.json")
        self.addCleanup(reopened.close)
        attached = reopened.psd_project
        entry = attached.data["psd_exports"][0]
        meta_path = psd_project.resolve_project_path(attached, entry["metadata"])
        result = repack_atlas_png_from_psd(psd_project.resolve_project_path(attached, entry["psd"]),
                                          self.root / "repack_after_move", metadata_path=meta_path)
        with Image.open(result.output_paths[0]) as actual, Image.open(reopened.texture_paths[0]) as baseline:
            self.assertEqual(actual.convert("RGBA").tobytes(), baseline.convert("RGBA").tobytes())
        self.assertEqual(reopened.keyframes("Retained[0]", "ParamAngleY")[0]["value"], 12)
        # Relocation must retain baseline validation, including digest and overwrite protection.
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        source_texture = (meta_path.parent / metadata["source_root"] / "texture.png").resolve()
        with self.assertRaisesRegex(PsdReconstructionError, "overwrite"):
            repack_atlas_png_from_psd(psd_project.resolve_project_path(attached, entry["psd"]),
                                      source_texture.parent, metadata_path=meta_path)
        Image.new("RGBA", (32, 32), "red").save(source_texture)
        with self.assertRaisesRegex(PsdReconstructionError, "digest changed"):
            repack_atlas_png_from_psd(psd_project.resolve_project_path(attached, entry["psd"]),
                                      self.root / "reject_corrupt", metadata_path=meta_path)

    def test_imported_registry_keeps_all_existing_managed_history(self):
        original = self.session.ensure_psd_project()
        history = original.project_dir / "old-history.bin"
        history.write_bytes(b"prior project history")
        legacy = psd_project.create_project_from_source(self.model, "Imported", output_root=self.root / "legacy")
        attached = self.session.attach_psd_project(legacy.project_file)
        registered = attached.project_file.relative_to(self.session.root).as_posix()
        self.assertTrue(registered.startswith("psd/imported_"))
        result = self.session.save_copy(self.root / "whole_tree")
        self.session.close()
        reopened = Live2DEditorSession(result["model_path"])
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.psd_project.project_file.relative_to(reopened.root).as_posix(), registered)
        self.assertEqual((reopened.root / "psd" / "old-history.bin").read_bytes(), b"prior project history")

    def test_external_mod_workspace_is_copied_and_can_build_after_move(self):
        legacy = mod_project.create_empty_project("Legacy MOD", output_root=self.root / "legacy_mod")
        legacy.data["models"] = [{"id": "model-001", "skin_name": "Original",
            "workspace_path": str(self.model.parent), "model_json": str(self.model),
            "textures": [str(self.model.parent / "texture.png")], "texture_mappings": []}]
        legacy.data["base_model_json"] = str(self.model)
        mod_project.save_project(legacy.project_dir, legacy.data)
        self.session.attach_mod_project(legacy.project_file)
        result = self.session.save_copy(self.root / "mod_whole")
        self.session.close()
        (self.root / "source").rename(self.root / "retired_source")
        reopened = Live2DEditorSession(result["model_path"])
        self.addCleanup(reopened.close)
        project = mod_project.generate_live2dviewer_mod(reopened.mod_project)
        self.assertEqual(len(project.models), 1)
        self.assertTrue((project.project_dir / project.data["generated_model_json_paths"][0]).is_file())

    def test_noop_bind_is_clean_and_legacy_disk_changes_are_detected(self):
        project = self.session.ensure_psd_project()
        self.session.save_copy(self.root / "clean")
        self.session.flush_projects(psd_project=project)
        self.assertFalse(self.session.dirty)
        project.data["ui_state"]["selected_tool"] = "psd"
        psd_project.save_project(project.project_dir, project.data)
        self.session.bind_psd_project(project)
        self.assertTrue(self.session.dirty)
        self.session.detach_psd_project()
        self.assertIsNone(self.session.psd_project)
        self.assertTrue(project.project_dir.is_dir())

    def test_missing_external_dependency_fails_without_leaving_bound_partial_project(self):
        legacy = psd_project.create_project_from_source(self.model, "Legacy", output_root=self.root / "legacy")
        legacy.data["psd_exports"] = [{"psd": str(self.root / "missing.psd"), "metadata": ""}]
        psd_project.save_project(legacy.project_dir, legacy.data)
        with self.assertRaisesRegex(AnimationEditingError, "Missing external"):
            self.session.attach_psd_project(legacy.project_file)
        self.assertIsNone(self.session.psd_project)
        self.assertFalse((self.session.root / "psd").exists())

    def test_manifest_cannot_escape_managed_project_tree(self):
        (self.model.parent / EDITOR_MANIFEST).write_text(json.dumps({
            "format": "LpkUnpacker.Live2DEditor", "version": 2,
            "projects": {"psd": {"file": "../outside/project.lpkpsd_project.json"}}}), encoding="utf-8")
        with self.assertRaises(AnimationEditingError):
            Live2DEditorSession(self.model)


class Live2DEditorTextureTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="editor-textures-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.model = make_model(self.root / "source")
        data = json.loads(self.model.read_text(encoding="utf-8"))
        data["FileReferences"]["Textures"].append("second.png")
        self.model.write_text(json.dumps(data), encoding="utf-8")
        Image.new("RGBA", (16, 16), (40, 70, 20, 255)).save(self.model.parent / "second.png")
        self.session = Live2DEditorSession(self.model)
        self.addCleanup(self.session.close)

    def test_second_texture_write_failure_rolls_back_first_and_history(self):
        first, second = self.root / "first.png", self.root / "second.png"
        Image.new("RGBA", (32, 32), "red").save(first)
        Image.new("RGBA", (16, 16), "blue").save(second)
        before = [p.read_bytes() for p in self.session.texture_paths]
        write = self.session._write_texture
        calls = []
        def fail_second(path, data):
            calls.append(path)
            if len(calls) == 2:
                raise PermissionError("second texture locked")
            write(path, data)
        with patch.object(self.session, "_write_texture", side_effect=fail_second):
            with self.assertRaises(PermissionError):
                self.session.replace_textures({0: first, 1: second})
        self.assertEqual([p.read_bytes() for p in self.session.texture_paths], before)
        self.assertEqual(self.session._texture_data, before)
        self.assertFalse(self.session.can_undo)
        self.assertFalse(self.session.dirty)

    def test_package_identity_order_and_dimensions_are_checked_before_writes(self):
        package = self.session.export_snapshot(self.root / "package")
        baseline = [p.read_bytes() for p in self.session.texture_paths]
        moc = package.parent / "model.moc3"
        original_moc = moc.read_bytes()
        moc.write_bytes(b"another model")
        with self.assertRaisesRegex(AnimationEditingError, "different MOC3"):
            self.session.apply_texture_package(package)
        moc.write_bytes(original_moc)
        data = json.loads(package.read_text(encoding="utf-8"))
        data["FileReferences"]["Textures"].reverse()
        package.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(AnimationEditingError, "order"):
            self.session.validate_texture_package(package)
        data["FileReferences"]["Textures"].reverse()
        package.write_text(json.dumps(data), encoding="utf-8")
        Image.new("RGBA", (15, 16), "red").save(package.parent / "second.png")
        with self.assertRaisesRegex(AnimationEditingError, "size changed"):
            self.session.apply_texture_package(package)
        self.assertEqual([p.read_bytes() for p in self.session.texture_paths], baseline)
        self.assertFalse(self.session.dirty)


if __name__ == "__main__":
    unittest.main()
