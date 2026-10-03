from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.live2d_editor_session import Live2DEditorSession
from app.core.psd_project import (
    PsdSnapshotRequest, commit_pose_export_stage, create_project_from_source,
    discard_pose_export_stage, finalize_pose_export_stage, load_project,
    resolve_project_path, stage_pose_export,
)
from app.core.psd_reconstructor import reconstruct_live2d_psd, repack_atlas_png_from_psd
from tests import test_selection_psd as fixture_module
from tests import test_live2d_editor_session as session_fixture


class PsdExportStagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="psd-stage-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = self.root / "source"
        source.mkdir()
        self.fixture = fixture_module.SelectedPosePsdTests()
        self.model, self.textures, self.mesh = self.fixture.make_source(source, shared=True)
        (source / "drawables.json").write_text(json.dumps(self.mesh), encoding="utf-8")
        self.project = create_project_from_source(self.model, "Stage", output_root=self.root / "projects")
        self.source_hashes = self.hashes(source)

    @staticmethod
    def hashes(root):
        return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in root.rglob("*") if path.is_file() and ".psd-staging" not in path.parts}

    def stage(self, name="pose", mode="mesh"):
        stage = stage_pose_export(
            self.project, name, 0, {}, PsdSnapshotRequest(str(self.project.base_model_json)),
            scheme_id=name, selection={"drawable_ids": ["Head"]})
        self.addCleanup(discard_pose_export_stage, stage)
        result = reconstruct_live2d_psd(
            stage.source_model, stage.scheme_dir, mode=mode, output_name=name,
            mesh_data=self.mesh, selected_drawable_ids=["Head"] if mode == "mesh" else None,
            resource_limits={"mesh_max_dimension": 0})
        finalize_pose_export_stage(stage, result)
        return stage, result

    def publish_prior(self):
        stage, _result = self.stage("prior")
        self.project = commit_pose_export_stage(self.project, stage)

    def test_published_metadata_reopens_and_noop_preserves_each_atlas_byte(self):
        stage, result = self.stage()
        baseline = {path.name: path.read_bytes() for path in stage.scheme_dir.rglob("*.png")}
        project_data = copy.deepcopy(self.project.data)
        registry = self.project.project_file.read_bytes()
        self.assertEqual(self.project.data, project_data)
        self.assertEqual(self.project.project_file.read_bytes(), registry)
        published = commit_pose_export_stage(self.project, stage)
        reopened = load_project(published.project_file)
        scheme = reopened.data["pose_schemes"][-1]
        metadata = resolve_project_path(reopened, scheme["metadata"])
        psd = resolve_project_path(reopened, scheme["psd"])
        self.assertNotIn(".psd-staging", metadata.read_text(encoding="utf-8"))
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        self.assertTrue(Path(payload["source_model"]).is_file())
        self.assertTrue(Path(payload["source_root"]).is_dir())
        self.assertEqual(baseline, {path.name: path.read_bytes() for path in stage.final_scheme_dir.rglob("*.png")})
        packed = repack_atlas_png_from_psd(psd, self.root / "reopened-repack", metadata_path=metadata)
        for index, original in enumerate(self.textures):
            self.assertEqual(original.read_bytes(), packed.texture_outputs[index].read_bytes())
        self.assertEqual(self.source_hashes, self.hashes(self.model.parent))

    def test_registry_replace_failure_preserves_prior_history_and_restores_owned_stage(self):
        self.publish_prior()
        before = self.hashes(self.project.project_dir)
        stage, _result = self.stage("new")
        with patch("app.core.psd_project.os.replace", side_effect=OSError("registry fault")):
            with self.assertRaisesRegex(OSError, "registry fault"):
                commit_pose_export_stage(self.project, stage)
        self.assertFalse(stage.final_snapshot_dir.exists())
        self.assertFalse(stage.final_scheme_dir.exists())
        self.assertTrue(stage.snapshot_dir.is_dir())
        self.assertTrue(stage.scheme_dir.is_dir())
        self.assertEqual(before, self.hashes(self.project.project_dir))
        self.assertEqual(self.source_hashes, self.hashes(self.model.parent))
        self.assertFalse(list(self.project.project_dir.glob(".psd-registry-*.tmp")))

    def test_second_directory_publish_failure_restores_first_and_keeps_registry(self):
        self.publish_prior()
        before = self.hashes(self.project.project_dir)
        stage, _result = self.stage("new")
        rename = os.rename

        def fail_second(source, destination):
            if Path(source) == stage.scheme_dir:
                raise OSError("directory fault")
            return rename(source, destination)

        with patch("app.core.psd_project.os.rename", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "directory fault"):
                commit_pose_export_stage(self.project, stage)
        self.assertTrue(stage.snapshot_dir.is_dir())
        self.assertFalse(stage.final_snapshot_dir.exists())
        self.assertFalse(stage.final_scheme_dir.exists())
        self.assertEqual(before, self.hashes(self.project.project_dir))
        self.assertEqual(self.source_hashes, self.hashes(self.model.parent))

    def test_source_snapshot_copies_references_without_unrelated_skin_or_psd_history(self):
        model_dir = self.project.base_model_json.parent
        for directory in ("skins", "psd", "snapshots", "repacks"):
            (model_dir / directory).mkdir()
            (model_dir / directory / "unrelated.dat").write_bytes(b"keep out")
        with patch("app.core.psd_project._copy_workspace", side_effect=AssertionError("whole workspace copy")):
            stage = stage_pose_export(self.project, "minimal", 0, {}, str(self.project.base_model_json), scheme_id="minimal")
        self.addCleanup(discard_pose_export_stage, stage)
        self.assertFalse(list(stage.snapshot_dir.rglob("unrelated.dat")))
        self.assertTrue((stage.snapshot_dir / "drawables.json").is_file())
        self.assertEqual(len(list(stage.snapshot_dir.glob("atlas_*.png"))), 2)

    def test_real_detached_request_writes_directly_into_stage_after_session_close(self):
        model = session_fixture.make_model(self.root / "detached-source")
        session = Live2DEditorSession(model)
        session.preview_parameter("ParamAngleY", 12)
        before = copy.deepcopy(session.project.modified), copy.deepcopy(session._undo), session.parameter_preview_pending
        request = session.capture_export_snapshot(skin_source={"id": session.active_skin_id, "name": "captured"})
        self.addCleanup(request.close)
        self.assertEqual(before, (session.project.modified, session._undo, session.parameter_preview_pending))
        session.close()
        with patch("app.core.psd_project._copy_psd_source", side_effect=AssertionError("duplicate source copy")):
            stage = stage_pose_export(
                self.project, "detached", 0, {"ParamAngleY": 12},
                PsdSnapshotRequest(export_request=request, skin_source=request.skin_source), scheme_id="detached")
        self.addCleanup(discard_pose_export_stage, stage)
        self.assertEqual(stage.source_model.parent, stage.snapshot_dir)
        self.assertEqual(stage.scheme["skin_source"], request.skin_source)
        self.assertEqual((model.parent / "texture.png").read_bytes(), (stage.snapshot_dir / "texture.png").read_bytes())
        self.assertFalse(self.project.data["pose_schemes"])

    def test_real_detached_request_publishes_from_editor_owned_psd_stage_without_live_changes(self):
        model = session_fixture.make_model(self.root / "managed-source")
        source_hashes = self.hashes(model.parent)
        session = Live2DEditorSession(model)
        self.addCleanup(session.close)
        project = session.ensure_psd_project()
        session.preview_parameter("ParamAngleY", 12)
        before = copy.deepcopy((session.project.modified, session._undo, session._redo,
                                session.parameter_overrides, session._parameter_preview))
        request = session.capture_export_snapshot()
        self.addCleanup(request.close)
        stage = stage_pose_export(
            project, "managed", 0, {"ParamAngleY": 12},
            PsdSnapshotRequest(export_request=request), scheme_id="managed")
        self.addCleanup(discard_pose_export_stage, stage)
        self.assertTrue(stage.snapshot_dir.is_relative_to(session.root))
        result = reconstruct_live2d_psd(
            stage.source_model, stage.scheme_dir, mode="mesh", output_name="managed",
            parameter_values={"ParamAngleY": 12}, mesh_data=copy.deepcopy(session.mesh_data),
            selected_drawable_ids=["ArtMeshFace"], resource_limits={"mesh_max_dimension": 0})
        finalize_pose_export_stage(stage, result)
        published = commit_pose_export_stage(project, stage)
        reopened = load_project(published.project_file)
        scheme = reopened.data["pose_schemes"][-1]
        packed = repack_atlas_png_from_psd(
            resolve_project_path(reopened, scheme["psd"]), self.root / "managed-noop",
            metadata_path=resolve_project_path(reopened, scheme["metadata"]))
        self.assertEqual((model.parent / "texture.png").read_bytes(), packed.texture_outputs[0].read_bytes())
        self.assertEqual(before, (session.project.modified, session._undo, session._redo,
                                  session.parameter_overrides, session._parameter_preview))
        self.assertEqual(source_hashes, self.hashes(model.parent))


if __name__ == "__main__":
    unittest.main()
