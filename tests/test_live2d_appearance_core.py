from __future__ import annotations

import copy
import json
import pickle
import tempfile
import threading
import unittest
from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.core.animation_editing import AnimationEditingError, _motion_meta
from app.core.live2d_editor_session import (
    EDITOR_MANIFEST, Live2DEditorSession, prepare_readonly_preview_session,
)
from app.core.live2d_skins import ORIGINAL_SKIN
from app.core.live2d_editor_export import Live2DExportSnapshotRequest
from tests.test_live2d_skins import hashes, two_atlas_model


class _AppearanceFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="appearance-core-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = two_atlas_model(self.root / "source")
        self.session = Live2DEditorSession(self.source)
        self.addCleanup(self.session.close)
        self.original_hashes = hashes(self.source.parent)
        self.project_file = "psd/project.lpkpsd_project.json"
        self.candidate = two_atlas_model(self.root / "candidate")
        Image.new("RGBA", (32, 32), "red").save(self.candidate.parent / "texture.png")

    def import_version(self, version="repack:shared", name="PSD shared", **kwargs):
        return self.session.import_psd_skin(self.candidate, name,
            project_file=self.project_file, version=version, **kwargs)


class AppearanceCoreTests(_AppearanceFixture, unittest.TestCase):
    def test_same_version_reuses_before_io_and_clean_active_repeat_has_no_history(self):
        self.session.create_motion("Keep", 2)
        self.session.set_keyframes("Keep", "ParamAngleY", [{"time": 0, "value": 4}])
        skin = self.import_version()
        self.session._saved_signature = self.session._signature()
        before = self.session.skin_state_snapshot()
        history_count = len(self.session._undo)
        with patch.object(self.session._skins, "_source", side_effect=AssertionError("duplicate source I/O")):
            repeated = self.session.import_psd_skin(self.root / "missing" / "model.json", "Duplicate name",
                project_file=self.session.root / self.project_file, version="repack:shared")
        self.assertEqual(skin["id"], repeated["id"])
        self.assertEqual(len(self.session.list_skins()), 2)
        self.assertEqual(len(self.session._undo), history_count)
        self.assertIs(before, self.session.skin_state_snapshot())
        self.assertFalse(self.session.dirty)
        self.assertEqual(self.session.keyframes("Keep", "ParamAngleY")[0]["value"], 4)
        self.assertEqual(self.original_hashes, hashes(self.source.parent))

    def test_reapply_existing_from_other_skin_is_one_reversible_switch(self):
        skin = self.import_version()
        self.session.apply_skin(ORIGINAL_SKIN)
        self.session.set_parameter("ParamAngleY", 7)
        parameters = dict(self.session.parameter_overrides)
        count = len(self.session._undo)
        repeated = self.session.import_psd_skin(None, "Ignored name", project_file=self.project_file,
                                               version="repack:shared")
        self.assertEqual(skin["id"], repeated["id"])
        self.assertEqual(len(self.session._undo), count + 1)
        self.assertEqual(dict(self.session.parameter_overrides), parameters)
        self.session.undo()
        self.assertEqual(self.session.active_skin_id, ORIGINAL_SKIN)
        self.assertEqual(dict(self.session.parameter_overrides), parameters)
        self.session.redo()
        self.assertEqual(self.session.active_skin_id, skin["id"])

    def test_kind_token_and_project_identity_are_exact_without_name_or_pixel_guessing(self):
        repack = self.import_version()
        composite = self.import_version("composite:shared", "Composite shared")
        self.assertNotEqual(repack["id"], composite["id"])
        found = self.session.find_psd_skin(self.project_file.replace("/", "\\"), "shared", version_kind="repack")
        self.assertEqual(found["id"], repack["id"])
        self.assertEqual(self.session.get_skin_origin(repack["id"]), {
            "kind": "psd", "project": self.project_file, "version_kind": "repack", "version": "shared"})
        self.assertIsNone(self.session.find_psd_skin("psd/other/project.lpkpsd_project.json", "repack:shared"))
        self.assertIsNone(self.session.find_psd_skin(self.project_file, "shared"))
        self.assertIsNone(self.session.find_psd_skin(self.project_file, "repack:shared", version_kind="composite"))
        self.assertEqual(self.session.find_skin_origin({"kind": "psd", "project": self.project_file,
                                                       "version": "repack:shared"})["id"], repack["id"])
        unknown = self.session.capture_skin("Unknown same bytes", source_kind="psd", metadata={"version": "repack:unknown"})
        self.assertIsNone(self.session.get_skin_origin(unknown["id"]))
        self.assertIsNone(self.session.find_psd_skin(self.project_file, "repack:unknown"))
        with self.assertRaises(AnimationEditingError):
            self.session.import_psd_skin(self.candidate, "Escaped", project_file="../external.json", version="repack:shared")

    def test_clone_delete_and_undo_redo_allow_origin_to_be_registered_again(self):
        first = self.import_version()
        clone = self.session.clone_skin(first["id"], "Explicit variant")
        self.assertNotEqual(clone["id"], first["id"])
        self.assertEqual(self.session.get_skin_origin(clone["id"]), self.session.get_skin_origin(first["id"]))
        self.assertEqual(self.session.find_psd_skin(self.project_file, "repack:shared")["id"], first["id"])
        self.session.remove_skin(first["id"])
        self.assertIsNone(self.session.find_psd_skin(self.project_file, "repack:shared"))
        self.assertEqual(self.session.get_skin_origin(clone["id"])["version"], "shared")
        second = self.import_version()
        self.assertNotEqual(second["id"], first["id"])
        self.session.undo()
        self.assertIsNone(self.session.find_psd_skin(self.project_file, "repack:shared"))
        self.session.undo()
        self.assertEqual(self.session.find_psd_skin(self.project_file, "repack:shared")["id"], first["id"])
        self.session.redo()
        self.assertIsNone(self.session.find_psd_skin(self.project_file, "repack:shared"))
        self.session.redo()
        self.assertEqual(self.session.find_psd_skin(self.project_file, "repack:shared")["id"], second["id"])

    def test_cloned_psd_origin_backlink_survives_primary_delete_save_and_reopen(self):
        first = self.import_version()
        clone = self.session.clone_skin(first["id"], "Variant")
        second_clone = self.session.clone_skin(clone["id"], "Variant copy")
        self.session.remove_skin(first["id"])
        self.assertEqual(self.session.get_skin_origin(second_clone["id"]), self.session.get_skin_origin(clone["id"]))
        saved = self.session.save_copy(self.root / "only_variants")
        reopened = Live2DEditorSession(saved["model_path"])
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get_skin_origin(second_clone["id"])["version_kind"], "repack")
        self.assertIsNone(reopened.find_psd_skin(self.project_file, "repack:shared"))

    def test_exact_legacy_metadata_save_move_reopen_survives_missing_preview_source(self):
        skin = self.import_version()
        exported = self.session.save_copy(self.root / "saved")
        saved = Path(exported["model_path"]).parent
        state = json.loads((saved / EDITOR_MANIFEST).read_text(encoding="utf-8"))
        legacy = next(item for item in state["skins"]["entries"] if item["id"] == skin["id"])
        legacy.pop("origin")
        legacy["source"]["metadata"].pop("origin")
        (saved / EDITOR_MANIFEST).write_text(json.dumps(state), encoding="utf-8")
        moved = self.root / "moved"
        saved.rename(moved)
        self.candidate.parent.rename(self.root / "retired_candidate")
        self.session.close()
        reopened = Live2DEditorSession(moved / "model.json")
        self.addCleanup(reopened.close)
        found = reopened.find_psd_skin(self.project_file, "repack:shared")
        self.assertEqual(found["id"], skin["id"])
        self.assertEqual(reopened.get_skin_origin(skin["id"])["version_kind"], "repack")
        self.assertFalse(reopened.dirty)
        self.assertFalse(reopened.apply_skin(found["id"]))
        self.assertFalse(reopened.can_undo)
        self.assertEqual(self.original_hashes, hashes(self.source.parent))

    def test_snapshot_reuses_object_and_revision_without_copy_or_digest_polling(self):
        snapshot = self.session.skin_state_snapshot()
        with patch("app.core.live2d_skins.copy.deepcopy", side_effect=AssertionError("poll copied registry")), \
             patch.object(self.session._skins, "bytes", side_effect=AssertionError("poll compared atlases")):
            for _ in range(100):
                self.assertIs(snapshot, self.session.skin_state_snapshot())
                self.assertFalse(self.session.skin_modified)
                self.assertFalse(self.session.dirty)
        self.session.set_parameter("ParamAngleY", 5)
        self.session.create_motion("Pose independent", 2)
        self.assertIs(snapshot, self.session.skin_state_snapshot())
        self.assertEqual(snapshot["revision"], self.session.skin_state_snapshot()["revision"])

    def test_snapshot_invalidates_texture_registry_history_watcher_and_replacement(self):
        previous = self.session.skin_state_snapshot()
        self.session.replace_texture(0, self.candidate.parent / "texture.png")
        changed = self.session.skin_state_snapshot()
        self.assertGreater(changed["revision"], previous["revision"])
        self.assertTrue(changed["modified"])
        self.session.undo()
        undone = self.session.skin_state_snapshot()
        self.assertGreater(undone["revision"], changed["revision"])
        self.assertFalse(undone["modified"])
        self.session.redo()
        self.assertGreater(self.session.skin_state_snapshot()["revision"], undone["revision"])
        skin = self.session.capture_skin("Captured")
        captured = self.session.skin_state_snapshot()
        self.assertFalse(captured["modified"])
        self.session.rename_skin(skin["id"], "Renamed")
        renamed = self.session.skin_state_snapshot()
        self.assertGreater(renamed["revision"], captured["revision"])
        self.session._skins.get(skin["id"])["source"]["metadata"]["direct"] = True
        direct = self.session.skin_state_snapshot()
        self.assertGreater(direct["revision"], renamed["revision"])
        self.assertTrue(direct["entries"][-1]["source"]["metadata"]["direct"])
        self.session._texture_data = list(self.session._skins.bytes(ORIGINAL_SKIN))
        replaced = self.session.skin_state_snapshot()
        self.assertGreater(replaced["revision"], direct["revision"])
        self.assertTrue(replaced["modified"])
        self.session._skins.state = copy.deepcopy(self.session._skins.state)
        self.assertGreater(self.session.skin_state_snapshot()["revision"], replaced["revision"])
        Image.new("RGBA", (32, 32), "cyan").save(self.session.texture_paths[0])
        previous = self.session.skin_state_snapshot()
        self.assertTrue(self.session.accept_texture_change(0))
        accepted = self.session.skin_state_snapshot()
        self.assertGreater(accepted["revision"], previous["revision"])
        self.session.texture_paths[0].write_bytes(b"incomplete external save")
        with self.assertRaises(OSError):
            self.session.accept_texture_change(0)
        self.session.restore_texture(0)
        self.assertEqual(self.session.texture_paths[0].read_bytes(), self.session._texture_data[0])
        self.assertIs(accepted, self.session.skin_state_snapshot())

    def test_registry_operations_after_undo_do_not_update_detached_old_entries(self):
        skin = self.import_version()
        self.session.rename_skin(skin["id"], "Temporary name")
        self.session.undo()
        self.session.rename_skin(skin["id"], "Name after undo")
        self.assertEqual(self.session.find_psd_skin(self.project_file, "repack:shared")["name"], "Name after undo")
        self.session._skins.state = copy.deepcopy(self.session._skins.state)
        self.session.rename_skin(skin["id"], "Name after direct replacement")
        self.assertEqual(self.session.find_psd_skin(self.project_file, "repack:shared")["name"], "Name after direct replacement")


class DetachedExportTests(_AppearanceFixture, unittest.TestCase):
    def test_child_payload_is_pickleable_detached_and_has_equivalent_output(self):
        self.session.create_motion("Child", 2)
        self.session.set_keyframes("Child", "ParamAngleY", [{"time": 0, "value": 3}])
        self.session.preview_parameter("ParamAngleY", 5)
        request = self.session.capture_export_snapshot()
        self.addCleanup(request.close)
        payload = request.to_worker_payload()
        self.assertNotIn("_lease", payload)
        restored = Live2DExportSnapshotRequest.from_worker_payload(pickle.loads(pickle.dumps(payload)))
        self.assertEqual(restored._parameters, {"ParamAngleY": 5})
        self.session.close()
        direct = request.write(self.root / "direct_child_contract")
        child = restored.write(self.root / "restored_child_contract")
        self.assertEqual(hashes(direct.parent), hashes(child.parent))
        self.assertTrue(restored._project.source_root.exists())
        restored.close()  # The child cannot dispose its parent's workspace.
        self.assertTrue(restored._project.source_root.exists())
        request.close()
        self.assertFalse(restored._project.source_root.exists())

    def test_capture_is_lightweight_and_pending_pose_does_not_commit_or_use_live_core(self):
        self.session.preview_parameter("ParamAngleY", 11)
        count = len(self.session._undo)
        dirty = self.session.dirty
        source = {"id": "capture", "nested": {"value": 1}}
        with patch.object(self.session, "snapshot_mesh", side_effect=AssertionError("capture used live Core")), \
             patch.object(self.session, "commit_parameter_preview", side_effect=AssertionError("capture committed pose")), \
             patch.object(Path, "read_bytes", side_effect=AssertionError("capture read atlas/MOC files")):
            request = self.session.capture_export_snapshot(skin_source=source)
        self.addCleanup(request.close)
        source["nested"]["value"] = 2
        self.assertEqual(request.skin_source["nested"]["value"], 1)
        with self.assertRaises(FrozenInstanceError):
            request.output_dir = self.root / "changed"
        self.assertEqual(len(self.session._undo), count)
        self.assertEqual(self.session.dirty, dirty)
        self.assertTrue(self.session.parameter_preview_pending)
        output = request.write(self.root / "pending")
        state = json.loads((output.parent / EDITOR_MANIFEST).read_text(encoding="utf-8"))
        self.assertEqual(state["pose_parameters"], {"ParamAngleY": 11})
        self.assertEqual(len(self.session._undo), count)
        self.assertTrue(self.session.parameter_preview_pending)

    def test_worker_uses_captured_textures_motions_and_assets_after_session_close(self):
        self.session.create_motion("CapturedMotion", 2)
        self.session.set_keyframes("CapturedMotion", "ParamAngleY", [{"time": 0, "value": 6}])
        captured_textures = tuple(self.session._texture_data)
        request = self.session.capture_export_snapshot()
        workspace = self.session.root
        self.session.replace_texture(0, self.candidate.parent / "texture.png")
        self.session.set_keyframes("CapturedMotion", "ParamAngleY", [{"time": 0, "value": 19}])
        self.session.close()
        self.assertTrue(workspace.is_dir())
        with ThreadPoolExecutor(max_workers=1) as pool:
            output = pool.submit(request.write, self.root / "captured").result(timeout=30)
        reopened = Live2DEditorSession(output)
        self.addCleanup(reopened.close)
        self.assertEqual(tuple(reopened._texture_data), captured_textures)
        self.assertEqual(reopened.keyframes("CapturedMotion[0]", "ParamAngleY")[0]["value"], 6)
        request.close()
        self.assertFalse(workspace.exists())
        self.assertEqual(self.original_hashes, hashes(self.source.parent))

    def test_output_matches_existing_snapshot_for_edits_empty_tracks_and_opaque_metadata(self):
        self.session.create_motion("Move", 2)
        self.session.set_keyframes("Move", "ParamAngleY", [{"time": 0, "value": 4}, {"time": 2, "value": 8}])
        self.session.create_motion("Empty authored", 1)
        self.session.set_parameter("ParamAngleY", 9)
        self.session.set_part_opacity("PartFace", .2)
        self.session.replace_texture(0, self.candidate.parent / "texture.png")
        request = self.session.capture_export_snapshot()
        self.addCleanup(request.close)
        old = self.session.export_snapshot(self.root / "old")
        new = request.write(self.root / "new")
        self.assertEqual(hashes(old.parent), hashes(new.parent))
        self.assertEqual(self.original_hashes, hashes(self.source.parent))

    def test_equivalence_for_original_modified_new_deleted_motions_viewer_fields_and_pending_pose(self):
        model = two_atlas_model(self.root / "rich_source")
        motion = {"Version": 3, "Meta": {"Duration": 2, "Loop": True, "Fps": 30},
                  "Curves": [{"Target": "Parameter", "Id": "ParamAngleY", "Segments": [0, 0, 0, 2, 2]}]}
        _motion_meta(motion)
        for filename in ("existing.motion3.json", "deleted.motion3.json"):
            (model.parent / filename).write_text(json.dumps(motion), encoding="utf-8")
        document = json.loads(model.read_text(encoding="utf-8"))
        document["FileReferences"]["Motions"]["Idle"] = [
            {"Name": "ViewerEX command", "Command": "change_model dress;start_mtn Idle"},
            {"File": "existing.motion3.json", "Name": "Authored title", "FadeInTime": .7, "FadeOutTime": .3,
             "Command": "keep_displayed_command", "CustomViewerField": {"enabled": True}},
            {"File": "deleted.motion3.json", "Name": "Remove this", "FadeInTime": .5},
        ]
        model.write_text(json.dumps(document), encoding="utf-8")
        before_hashes = hashes(model.parent)
        session = Live2DEditorSession(model)
        self.addCleanup(session.close)
        session.set_keyframes("Idle[0]", "ParamAngleY", [{"time": 0, "value": -4}, {"time": 2, "value": 5}])
        session.delete_motion("Idle[1]")
        session.create_motion("New", 1)
        session.set_keyframes("New", "ParamAngleY", [{"time": 0, "value": 12}])
        session.create_motion("Empty", 1)
        session.replace_texture(0, self.candidate.parent / "texture.png")
        yellow = self.root / "yellow.png"
        Image.new("RGBA", (32, 32), "yellow").save(yellow)
        session.replace_texture(1, yellow)
        session.preview_parameter("ParamAngleY", 14)
        request = session.capture_export_snapshot()
        self.addCleanup(request.close)
        self.assertTrue(session.parameter_preview_pending)
        old = session.export_snapshot(self.root / "rich_old")
        new = request.write(self.root / "rich_new")
        self.assertEqual(hashes(old.parent), hashes(new.parent))
        result = json.loads(new.read_text(encoding="utf-8"))
        self.assertEqual(result["FileReferences"]["Motions"]["Idle"][0], document["FileReferences"]["Motions"]["Idle"][0])
        modified_item = result["FileReferences"]["Motions"]["Idle"][1]
        self.assertEqual(modified_item["CustomViewerField"], {"enabled": True})
        self.assertEqual(modified_item["Command"], "keep_displayed_command")
        self.assertEqual(modified_item["FadeInTime"], .7)
        self.assertEqual(len(result["FileReferences"]["Motions"]["Idle"]), 2)
        self.assertEqual(json.loads((new.parent / EDITOR_MANIFEST).read_text())["pose_parameters"], {"ParamAngleY": 14})
        self.assertEqual(before_hashes, hashes(model.parent))

    def test_worker_core_is_fresh_and_not_live_session_core(self):
        main_thread = threading.get_ident()
        called = []
        class WorkerModel:
            def drawable_snapshot(inner, parameters):
                called.append((threading.get_ident(), dict(parameters)))
                return {"parameters": dict(parameters), "drawables": []}
        self.session.set_parameter("ParamAngleY", 8)
        request = self.session.capture_export_snapshot()
        self.addCleanup(request.close)
        # Patch the class, not just load_moc: constructing CubismCore needs the
        # optional Core DLL, which clean CI checkouts do not have.
        with patch.object(self.session, "snapshot_mesh", side_effect=AssertionError("worker touched live Core")), \
             patch("app.core.cubism_core.CubismCore") as worker_core:
            worker_core.return_value.load_moc.return_value = WorkerModel()
            with ThreadPoolExecutor(max_workers=1) as pool:
                output = pool.submit(request.write, self.root / "worker_core").result(timeout=30)
        self.assertNotEqual(called[0][0], main_thread)
        self.assertEqual(called[0][1], {"ParamAngleY": 8})
        self.assertEqual(json.loads((output.parent / "model.drawables.json").read_text())["parameters"], {"ParamAngleY": 8})

    def test_snapshot_output_refuses_source_existing_and_request_release(self):
        request = self.session.capture_export_snapshot(self.root / "destination")
        output = request.write()
        with self.assertRaises(AnimationEditingError):
            request.write(output.parent)
        with self.assertRaises(AnimationEditingError):
            request.write(self.source.parent / "inside")
        request.close()
        with self.assertRaises(AnimationEditingError):
            request.write(self.root / "released")
        self.assertEqual(self.original_hashes, hashes(self.source.parent))

    def test_internal_stage_output_and_publish_failure_are_isolated(self):
        self.session.create_motion("Export pending", 2)
        self.session.preview_parameter("ParamAngleY", 13)
        before = self.session._snapshot()
        count = len(self.session._undo)
        request = self.session.capture_export_snapshot()
        self.addCleanup(request.close)
        target = self.session.root / "psd" / ".psd-staging" / "test" / "snapshot"
        output = request.write(target)
        self.assertEqual(output.parent, target)
        failed = self.root / "failed_publish"
        with patch("app.core.live2d_editor_export._rename_directory", side_effect=PermissionError("publish denied")):
            with self.assertRaises(PermissionError):
                request.write(failed)
        self.assertFalse(failed.exists())
        self.assertEqual(self.session._snapshot(), before)
        self.assertEqual(len(self.session._undo), count)
        self.assertTrue(self.session.parameter_preview_pending)
        self.assertEqual(self.original_hashes, hashes(self.source.parent))

    def test_closing_request_during_write_preserves_worker_asset_lease_until_it_finishes(self):
        request = self.session.capture_export_snapshot()
        workspace = self.session.root
        started, resume = threading.Event(), threading.Event()
        original_write = type(request)._write
        def blocked_write(captured, output):
            started.set()
            if not resume.wait(10):
                raise TimeoutError("worker did not resume")
            return original_write(captured, output)
        with patch.object(type(request), "_write", blocked_write):
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(request.write, self.root / "leased_write")
                self.assertTrue(started.wait(10))
                self.session.close()
                request.close()
                self.assertTrue(workspace.exists())
                resume.set()
                output = result.result(timeout=30)
        self.assertTrue(output.is_file())
        self.assertFalse(workspace.exists())

    def test_readonly_preparation_is_fresh_cpu_owned_and_cancellation_releases_candidate(self):
        source_hashes = hashes(self.candidate.parent)
        with ThreadPoolExecutor(max_workers=1) as pool:
            prepared = pool.submit(prepare_readonly_preview_session, self.candidate).result(timeout=30)
        self.addCleanup(prepared.close)
        self.assertNotEqual(prepared.root, self.session.root)
        self.assertEqual(source_hashes, hashes(self.candidate.parent))
        event = threading.Event()
        event.set()
        with self.assertRaises(CancelledError):
            prepare_readonly_preview_session(self.candidate, cancel_event=event)
        class LateCancellation:
            count = 0
            def is_set(inner):
                inner.count += 1
                return inner.count > 1
        with patch("app.core.live2d_editor_session.Live2DEditorSession", wraps=Live2DEditorSession) as constructor:
            with self.assertRaises(CancelledError):
                prepare_readonly_preview_session(self.candidate, cancel_event=LateCancellation())
        self.assertEqual(constructor.call_count, 1)


if __name__ == "__main__":
    unittest.main()
