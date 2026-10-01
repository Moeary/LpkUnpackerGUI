from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.core.animation_editing import AnimationEditingError, INVENTORY_FORMAT, _motion_meta, create_animation_project
from app.core.live2d_editor_session import Live2DEditorSession, decode_curve, encode_curve


def make_model(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    moc = root / "model.moc3"
    moc.write_bytes(b"editor test, deliberately not a native MOC")
    Image.new("RGBA", (32, 32), (30, 60, 90, 255)).save(root / "texture.png")
    (root / "live2d_parameter_inventory.json").write_text(json.dumps({
        "format": INVENTORY_FORMAT, "version": 1,
        "moc_sha256": hashlib.sha256(moc.read_bytes()).hexdigest(),
        "parameters": [{"id": "ParamAngleY", "name": "垂直角度", "min": -30, "max": 30, "default": 0}],
    }), encoding="utf-8")
    (root / "drawables.json").write_text(json.dumps({
        "canvas": {"width": 100, "height": 100},
        "parts": [{"id": "PartFace", "index": 0, "opacity": .7}],
        "drawables": [{"id": "ArtMeshFace", "texture_index": 0, "vertices": [[10, 10], [90, 10], [90, 90]],
                       "uvs": [[0, 1], [1, 1], [1, 0]], "indices": [0, 1, 2],
                       "parent_part_id": "PartFace", "parent_part_index": 0}],
    }), encoding="utf-8")
    model = root / "test.model3.json"
    model.write_text(json.dumps({"Version": 3, "FileReferences": {"Moc": moc.name, "Textures": ["texture.png"]}}), encoding="utf-8")
    return model


class Live2DEditorSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="live2d-editor-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.model = make_model(self.root / "source")
        self.session = Live2DEditorSession(self.model)
        self.addCleanup(self.session.close)

    def hashes(self):
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.model.parent.iterdir() if path.is_file()}

    def test_parameter_gesture_is_pending_dirty_and_commits_one_small_history_entry(self):
        original = self.hashes()
        with patch.object(self.session, "_snapshot", side_effect=AssertionError("pose copied the whole project")):
            for value in (-20, -10, 5, 15):
                self.session.preview_parameter("ParamAngleY", value)
                self.assertTrue(self.session.dirty)
                self.assertTrue(self.session.can_undo)
                self.assertEqual(self.session.pose_at(None, 0, overrides=True)["ParamAngleY"], value)
                self.assertEqual(len(self.session._undo), 0)
            self.assertTrue(self.session.commit_parameter_preview())
            self.assertEqual(len(self.session._undo), 1)
            self.assertEqual(set(self.session._undo[0]), {"_history_kind", "parameters", "parts"})
            self.assertFalse(self.session.parameter_preview_pending)
            self.assertTrue(self.session.undo())
            self.assertFalse(self.session.dirty)
            self.assertTrue(self.session.redo())
            self.assertEqual(self.session.parameter_overrides["ParamAngleY"], 15)
        self.assertEqual(original, self.hashes())

    def test_pose_digests_reuse_assets_and_invalid_or_reverted_preview_has_no_history(self):
        self.session.set_parameter("ParamAngleY", 5)
        self.session._saved_signature = self.session._signature()
        with patch("app.core.live2d_editor_session.json.dumps", wraps=json.dumps) as serialize:
            self.session.preview_parameter("ParamAngleY", 10)
            self.session.commit_parameter_preview()
            self.assertTrue(self.session.dirty)
            payload = serialize.call_args_list[-1].args[0]
            self.assertEqual(payload, [{"ParamAngleY": 10}, {}])
            self.assertEqual(serialize.call_count, 1)
        count = len(self.session._undo)
        self.session.preview_parameter("ParamAngleY", 12)
        self.session.preview_parameter("ParamAngleY", 10)
        self.assertFalse(self.session.parameter_preview_pending)
        self.assertFalse(self.session.commit_parameter_preview())
        self.assertEqual(count, len(self.session._undo))
        with self.assertRaises(AnimationEditingError):
            self.session.preview_parameter("ParamAngleY", 31)
        self.assertFalse(self.session.parameter_preview_pending)

    def test_pose_motion_skin_and_part_history_interoperate_without_mutable_aliases(self):
        self.session.preview_parameter("ParamAngleY", -12)
        self.session.create_motion("Move", 2)  # flushes the staged pose first
        self.assertEqual(len(self.session._undo), 2)
        self.session.set_keyframes("Move", "ParamAngleY", [{"time": 0, "value": -10}])
        skin = self.session.capture_skin("Pose skin")
        self.session.set_part_opacity("PartFace", .25)
        self.assertEqual(len(self.session._undo), 5)
        self.assertTrue(self.session.undo())
        self.assertEqual(self.session.part_overrides, {})
        self.assertTrue(self.session.undo())
        self.assertNotIn(skin["id"], [item["id"] for item in self.session.list_skins()])
        self.assertTrue(self.session.undo())
        self.assertEqual(self.session.keyframes("Move", "ParamAngleY"), [])
        self.assertTrue(self.session.undo())
        self.assertEqual(self.session.project.motions, {})
        self.assertTrue(self.session.undo())
        self.assertFalse(self.session.dirty)
        for _ in range(5):
            self.assertTrue(self.session.redo())
        self.assertEqual(self.session.parameter_overrides, {"ParamAngleY": -12})
        self.assertEqual(self.session.part_overrides, {"PartFace": .25})
        self.assertEqual(self.session.keyframes("Move", "ParamAngleY")[0]["value"], -10)
        self.assertEqual(self.session.active_skin_id, skin["id"])
        result = self.session.save_copy(self.root / "interleaved")
        self.assertFalse(self.session.dirty)
        reopened = Live2DEditorSession(result["model_path"])
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.parameter_overrides, self.session.parameter_overrides)
        self.assertEqual(reopened.part_overrides, self.session.part_overrides)

    def test_saving_pending_parameter_pose_flushes_final_value_once(self):
        for value in (1, 8, -14):
            self.session.preview_parameter("ParamAngleY", value)
        result = self.session.save_copy(self.root / "pending-pose")
        self.assertEqual(len(self.session._undo), 1)
        self.assertFalse(self.session.dirty)
        reopened = Live2DEditorSession(result["model_path"])
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.parameter_overrides, {"ParamAngleY": -14})
        self.assertTrue(self.session.undo())
        self.assertTrue(self.session.dirty)
        self.assertTrue(self.session.redo())
        self.assertFalse(self.session.dirty)

    def test_exact_selection_pose_preserves_out_of_range_values_and_restores_core_arrays(self):
        from types import SimpleNamespace
        values, parts = [2.0], [.7]
        dll = SimpleNamespace(csmGetParameterIds=lambda _: [b"ParamAngleY"],
                              csmGetParameterValues=lambda _: values,
                              csmGetParameterCount=lambda _: 1,
                              csmUpdateModel=lambda _: None)
        core = SimpleNamespace(dll=dll, get_part_ids=lambda _: [b"PartFace"],
                               get_part_opacities=lambda _: parts, get_part_count=lambda _: 1)
        def snapshot():
            return {"parameters": {"ParamAngleY": values[0]}, "parts": [{"id": "PartFace", "opacity": parts[0]}],
                    "drawables": [{"id": "ArtMeshFace", "opacity": 1, "parent_part_id": "PartFace", "masks": [0]}]}
        self.session._core_model = SimpleNamespace(core=core, model_pointer=1, drawable_snapshot=snapshot)
        result = self.session.exact_pose_mesh({"ParamAngleY": 38}, {"PartFace": 0})
        self.assertEqual(result["parameters"], {"ParamAngleY": 38})
        self.assertEqual(result["drawables"][0]["opacity"], 0)
        self.assertEqual(result["drawables"][0]["masks"], [0])
        self.assertEqual(values, [2])
        self.assertEqual(parts, [.7])
        self.session.exact_pose_mesh({"ParamAngleY": 35}, {"PartFace": .5})
        self.assertEqual(snapshot()["parts"][0]["opacity"], .7)
        with self.assertRaises(AnimationEditingError):
            self.session.exact_pose_mesh({}, {"PartFace": 0})
        with patch.object(self.session._core_model, "drawable_snapshot", side_effect=RuntimeError("failed core read")):
            with self.assertRaises(RuntimeError):
                self.session.exact_pose_mesh({"ParamAngleY": 40}, {"PartFace": 0})
        self.assertEqual(values, [2])
        self.assertEqual(parts, [.7])

    def test_bezier_and_inverse_stepped_roundtrip_including_flat_handles(self):
        parameter = self.session.parameters[0]
        segments = [0, 2, 1, .2, 8, .7, -5, 1, 2, 3, 2, 4]
        frames = decode_curve(segments)
        self.assertEqual(frames[0]["interpolation"], "bezier")
        self.assertEqual(frames[0]["bezier_value_scale"], {"value": 1})
        self.assertEqual(frames[1]["interpolation"], "inverse_stepped")
        self.assertEqual(encode_curve(frames, 2, parameter), segments)
        self.session.create_motion("Curve", 2)
        self.session.set_keyframes("Curve", "ParamAngleY", frames)
        output = self.session.save_copy(self.root / "copy")
        reopened = Live2DEditorSession(output["model_path"])
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.project.motions["Curve[0]"]["Curves"][0]["Segments"], segments)
        self.assertFalse(reopened.dirty)

    def test_empty_track_and_history_do_not_touch_source(self):
        before = self.hashes()
        self.session.create_motion("Move", 2)
        self.session.set_keyframes("Move", "ParamAngleY", [{"time": 0, "value": 10}])
        self.session.set_keyframes("Move", "ParamAngleY", [])
        self.assertEqual(self.session.keyframes("Move", "ParamAngleY"), [])
        self.session.undo()
        self.assertEqual(self.session.keyframes("Move", "ParamAngleY")[0]["value"], 10)
        self.session.redo()
        result = self.session.save_copy(self.root / "empty-copy")
        reopened = Live2DEditorSession(result["model_path"])
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.keyframes("Move[0]", "ParamAngleY"), [])
        self.assertEqual(before, self.hashes())
        self.assertFalse(self.session.dirty)
        self.assertFalse(list(Path(result["output_dir"]).rglob("*.cmo3")))

    def test_texture_history_save_reopen_and_partial_save_restore(self):
        before = self.hashes()
        baseline = self.session.texture_paths[0].read_bytes()
        replacement = self.root / "replacement.png"
        Image.new("RGBA", (32, 32), (240, 20, 70, 255)).save(replacement)
        self.session.replace_texture(0, replacement)
        edited = self.session.texture_paths[0].read_bytes()
        self.session.undo()
        self.assertEqual(self.session.texture_paths[0].read_bytes(), baseline)
        self.session.redo()
        self.assertEqual(self.session.texture_paths[0].read_bytes(), edited)
        result = self.session.save_copy(self.root / "texture-copy")
        reopened = Live2DEditorSession(result["model_path"])
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.texture_paths[0].read_bytes(), edited)
        self.session.texture_paths[0].write_bytes(b"partially written image")
        with self.assertRaises(OSError):
            self.session.accept_texture_change(0)
        self.session.restore_texture(0)
        self.assertEqual(self.session.texture_paths[0].read_bytes(), edited)
        self.assertEqual(before, self.hashes())

    def test_time_sampling_is_clean_and_invalid_keyframes_leave_state_intact(self):
        self.session.pose_at(None, .8)
        self.assertFalse(self.session.dirty)
        self.session.create_motion("Move", 2)
        self.session.set_keyframes("Move", "ParamAngleY", [{"time": 0, "value": -30}, {"time": 2, "value": 30}])
        self.assertEqual(self.session.pose_at("Move", 1)["ParamAngleY"], 0)
        previous = copy.deepcopy(self.session.project.motions)
        for frames in ([{"time": 0, "value": 31}], [{"time": 0, "value": 0}, {"time": 0, "value": 1}]):
            with self.assertRaises(AnimationEditingError):
                self.session.set_keyframes("Move", "ParamAngleY", frames)
        self.assertEqual(self.session.project.motions, previous)
        with self.assertRaises(AnimationEditingError):
            self.session.save_copy(self.model.parent / "unsafe")

    def test_existing_commands_and_original_binding_indices_survive(self):
        data = json.loads(self.model.read_text())
        (self.model.parent / "motion.json").write_text(json.dumps({"Version": 3, "Meta": {"Duration": 2, "Fps": 30, "Loop": False},
                            "Curves": [{"Target": "Parameter", "Id": "ParamAngleY", "Segments": [0, 0, 0, 2, 1]}]}), encoding="utf-8")
        command = {"Name": "Menu", "Text": "original command",
                   "Choices": [{"Text": "Main", "NextMtn": "SwitchSkin:0"},
                               {"Text": "Skin", "NextMtn": "SwitchSkin:1"}]}
        switch_commands = [{"Name": "0", "Command": "change_model model0.json"},
                           {"Name": "1", "Command": "change_model model1.json"}]
        data["FileReferences"]["Motions"] = {
            "Idle": [command, {"File": "motion.json", "FadeInTime": .2}],
            "SwitchSkin": switch_commands,
        }
        self.model.write_text(json.dumps(data), encoding="utf-8")
        other = Live2DEditorSession(self.model)
        self.addCleanup(other.close)
        other.set_keyframes("Idle[0]", "ParamAngleY", [{"time": 0, "value": -10}])
        result = other.save_copy(self.root / "commands-copy")
        saved = json.loads(Path(result["model_path"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["FileReferences"]["Motions"]["Idle"][0], command)
        self.assertTrue(saved["FileReferences"]["Motions"]["Idle"][1]["File"].startswith("edited_motions/"))
        self.assertEqual(saved["FileReferences"]["Motions"]["SwitchSkin"], switch_commands)
        reopened = Live2DEditorSession(result["model_path"])
        self.addCleanup(reopened.close)
        copied_again = reopened.save_copy(self.root / "commands-reopened-copy")
        self.assertEqual(json.loads(Path(copied_again["model_path"]).read_text(encoding="utf-8"))["FileReferences"]["Motions"],
                         saved["FileReferences"]["Motions"])
        self.assertFalse(any("SwitchSkin:" in relative for relative in reopened.project.references))

    def test_virtual_skin_menu_does_not_allow_unsafe_asset_paths(self):
        document = json.loads(self.model.read_text(encoding="utf-8"))
        for key, value in (("File", "../outside.json"), ("Sound", "../outside.wav"),
                           ("File", "D:/outside.json"), ("File", "SwitchSkin:0")):
            with self.subTest(key=key, value=value):
                document["FileReferences"]["Motions"] = {"Menu": [{key: value}]}
                self.model.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(AnimationEditingError):
                    Live2DEditorSession(self.model)

    def test_viewer_commands_and_inline_physics_are_opaque_metadata(self):
        document = json.loads(self.model.read_text(encoding="utf-8"))
        entry = {"Command": "start_mtn init#9:init;start_mtn Idle;start_mtn Idle#1",
                 "PostCommand": "parameters lock drag $drag 0", "Text": "Press [~]",
                 "Choices": [{"NextMtn": "touch#9:enable", "Language": "ja"}]}
        document["FileReferences"]["Motions"] = {"Menu": [entry]}
        document["FileReferences"]["PhysicsV2"] = {"Name": "inline:physics", "Code": "param x;y"}
        self.model.write_text(json.dumps(document), encoding="utf-8")
        other = Live2DEditorSession(self.model)
        self.addCleanup(other.close)
        result = other.save_copy(self.root / "viewer-copy")
        saved = json.loads(Path(result["model_path"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["FileReferences"]["Motions"]["Menu"], [entry])
        self.assertEqual(saved["FileReferences"]["PhysicsV2"], document["FileReferences"]["PhysicsV2"])
        self.assertTrue(all("start_mtn" not in value for value in other.project.references))

    def test_locked_texture_does_not_consume_undo_or_redo_history(self):
        replacement = self.root / "replacement.png"
        Image.new("RGBA", (32, 32), (255, 0, 40, 255)).save(replacement)
        baseline = self.session.texture_paths[0].read_bytes()
        self.session.replace_texture(0, replacement)
        edited = self.session.texture_paths[0].read_bytes()
        with patch.object(self.session, "_write_texture", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.session.undo()
        self.assertTrue(self.session.can_undo)
        self.assertFalse(self.session.can_redo)
        self.assertEqual(self.session.texture_paths[0].read_bytes(), edited)
        self.assertEqual(self.session._texture_data[0], edited)
        self.session.undo()
        self.assertEqual(self.session.texture_paths[0].read_bytes(), baseline)
        with patch.object(self.session, "_write_texture", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.session.redo()
        self.assertTrue(self.session.can_redo)
        self.assertFalse(self.session.can_undo)
        self.assertEqual(self.session._texture_data[0], baseline)
        self.session.redo()
        self.assertEqual(self.session.texture_paths[0].read_bytes(), edited)

    def test_atomic_texture_publish_retries_transient_windows_lock(self):
        replacement = self.root / "replacement.png"
        Image.new("RGBA", (32, 32), (30, 255, 40, 255)).save(replacement)
        original = Path.replace
        attempts = []
        def intermittent(path, target):
            attempts.append(str(target))
            if len(attempts) <= 2:
                raise PermissionError("transient Windows file lock")
            return original(path, target)
        with patch("app.core.live2d_editor_session.os.name", "nt"), patch("app.core.live2d_editor_session.time.sleep"), patch.object(Path, "replace", intermittent):
            self.assertTrue(self.session.replace_texture(0, replacement))
        self.assertEqual(len(attempts), 3)

    def _shared_motion_model(self):
        motion = {"Version": 3, "Meta": {"Duration": 2, "Fps": 30, "Loop": False},
                  "Curves": [{"Target": "Parameter", "Id": "ParamAngleY", "Segments": [0, 0, 0, 2, 1]}]}
        _motion_meta(motion)
        motion_path = self.model.parent / "motion.json"
        motion_path.write_text(json.dumps(motion, separators=(",", ":")), encoding="utf-8")
        document = json.loads(self.model.read_text())
        document["FileReferences"]["Motions"] = {"Idle": [
            {"Name": "Menu", "Command": "start_mtn Idle#1"},
            {"File": "motion.json", "Name": "First", "PostCommand": "parameters lock drag 0"},
            {"Name": "Switch", "Command": "change_model model0.json"},
            {"File": "motion.json", "Name": "Shared", "FadeInTime": .3},
            {"File": "motion.json", "Name": "Last"},
        ]}
        self.model.write_text(json.dumps(document), encoding="utf-8")
        return document, motion_path

    def test_open_validates_shared_motion_once_without_rewriting_or_sharing_edits(self):
        _, path = self._shared_motion_model()
        raw = path.read_bytes()
        from app.core import animation_editing
        import shutil
        with patch("app.core.animation_editing._motion_meta", wraps=animation_editing._motion_meta) as validate, patch("app.core.live2d_editor_session.shutil.copy2", wraps=shutil.copy2) as copy_asset:
            other = Live2DEditorSession(self.model)
            self.addCleanup(other.close)
        self.assertEqual(validate.call_count, 1)
        self.assertEqual((other.root / "motion.json").read_bytes(), raw)
        copied = [str(call.args[0]) for call in copy_asset.call_args_list]
        self.assertEqual(copied.count(str(path.resolve())), 1)
        self.assertEqual(copied.count(str((self.model.parent / "model.moc3").resolve())), 1)
        other.set_keyframes("Idle[0]", "ParamAngleY", [{"time": 0, "value": -20}])
        self.assertEqual(other.keyframes("Idle[1]", "ParamAngleY")[0]["value"], 0)
        self.assertEqual(path.read_bytes(), raw)
        # The public factory still validates unknown callers' file data, and
        # its two bindings must also own independent editable curves.
        native = create_animation_project(other.model_path)
        native.set_keyframes("Idle[0]", "ParamAngleY", "value", [{"time": 0, "value": 10}])
        self.assertEqual(native.get_keyframes("Idle[1]", "ParamAngleY", "value")["keyframes"][0]["value"], 0)
        invalid = json.loads(raw)
        invalid["Curves"][0]["Segments"] = [0, 0, 4, 2, 1]
        (other.root / "motion.json").write_text(json.dumps(invalid))
        with self.assertRaises(AnimationEditingError):
            create_animation_project(other.model_path)

    def test_dirty_cache_tracks_nested_edits_attribute_replacement_and_repeated_polling(self):
        self.session.create_motion("Move", 2)
        self.session.set_keyframes("Move", "ParamAngleY", [{"time": 0, "value": 0}, {"time": 2, "value": 1}])
        self.session.save_copy(self.root / "cache_baseline")
        with patch("app.core.live2d_editor_session.json.dumps", side_effect=AssertionError("unchanged dirty poll recomputed its digest")):
            for _ in range(20):
                self.assertFalse(self.session.dirty)
        segments = self.session.project.motions["Move"]["Curves"][0]["Segments"]
        baseline_segments = list(segments)
        segments[1] = 10
        self.assertTrue(self.session.dirty)
        segments[1] = baseline_segments[1]
        self.assertFalse(self.session.dirty)
        segments[2:] = [2, 2, 1]
        self.assertTrue(self.session.dirty)
        segments[2:] = baseline_segments[2:]
        self.assertFalse(self.session.dirty)
        metadata = self.session.project.motions["Move"]["Meta"]
        metadata.update(Loop=False)
        self.assertTrue(self.session.dirty)
        metadata.update(Loop=True)
        self.assertFalse(self.session.dirty)
        curves = self.session.project.motions["Move"]["Curves"]
        curves.append({"Target": "PartOpacity", "Id": "Face", "Segments": [0, 1]})
        self.assertTrue(self.session.dirty)
        curves.pop()
        self.assertFalse(self.session.dirty)
        self.session.parameter_overrides = {"ParamAngleY": 10}
        self.assertTrue(self.session.dirty)
        self.session.parameter_overrides = {}
        self.assertFalse(self.session.dirty)
        self.session.project.document = copy.deepcopy(self.session.project.document)
        self.assertFalse(self.session.dirty)
        self.session.project.document["Groups"] = [{"Target": "Parameter", "Name": "Test", "Ids": []}]
        self.assertTrue(self.session.dirty)

    def test_each_edit_kind_undo_redo_and_save_reset_dirty(self):
        replacement = self.root / "dirty_texture.png"
        Image.new("RGBA", (32, 32), "red").save(replacement)
        actions = [lambda: self.session.create_motion("DirtyMotion", 2),
                   lambda: self.session.set_keyframes("DirtyMotion", "ParamAngleY", [{"time": 0, "value": 3}]),
                   lambda: self.session.set_parameter("ParamAngleY", 5),
                   lambda: self.session.set_part_opacity("PartFace", .5),
                   lambda: self.session.replace_texture(0, replacement)]
        for index, action in enumerate(actions):
            with self.subTest(index=index):
                action()
                self.assertTrue(self.session.dirty)
                self.assertTrue(self.session.undo())
                self.assertFalse(self.session.dirty)
                self.assertTrue(self.session.redo())
                self.assertTrue(self.session.dirty)
                self.session.save_copy(self.root / f"dirty_saved_{index}")
                self.assertFalse(self.session.dirty)
        self.session.ensure_psd_project()
        self.assertTrue(self.session.dirty)
        self.session.save_copy(self.root / "dirty_psd_saved")
        self.assertFalse(self.session.dirty)
        self.session.ensure_mod_project()
        self.assertTrue(self.session.dirty)
        self.session.save_copy(self.root / "dirty_mod_saved")
        self.assertFalse(self.session.dirty)
        self.session.mark_project_changed()
        self.assertTrue(self.session.dirty)

    def test_delete_shared_motion_preserves_commands_reindexes_bindings_and_reopens(self):
        document, path = self._shared_motion_model()
        before = self.hashes()
        other = Live2DEditorSession(self.model)
        self.addCleanup(other.close)
        self.assertEqual(len(other.motion_catalog()), 5)
        command = other.motion_catalog()[0]
        self.assertFalse(command["editable"])
        self.assertEqual(command["kind"], "command")
        with self.assertRaises(AnimationEditingError):
            other.delete_motion(command["name"])
        self.assertFalse(other.can_undo)
        self.assertTrue(other.delete_motion("Idle[0]"))
        self.assertEqual(other.original_bindings, {("Idle", 0): 3, ("Idle", 1): 4})
        self.assertEqual(other.project.bindings["Idle[1]"], ("Idle", 0))
        self.assertTrue(other.dirty)
        other.undo()
        self.assertEqual(other.original_bindings, {("Idle", 0): 1, ("Idle", 1): 3, ("Idle", 2): 4})
        self.assertFalse(other.dirty)
        other.redo()
        other.set_keyframes("Idle[1]", "ParamAngleY", [{"time": 0, "value": -20}])
        other.delete_motion("Idle[2]")
        saved = other.save_copy(self.root / "deleted-copy")
        output = json.loads(Path(saved["model_path"]).read_text())
        entries = output["FileReferences"]["Motions"]["Idle"]
        self.assertEqual(entries[:2], [document["FileReferences"]["Motions"]["Idle"][0], document["FileReferences"]["Motions"]["Idle"][2]])
        self.assertEqual(entries[2]["Name"], "Shared")
        self.assertEqual(entries[2]["FadeInTime"], .3)
        self.assertEqual(len(entries), 3)
        self.assertTrue(path.is_file())
        self.assertTrue((Path(saved["model_path"]).parent / "motion.json").is_file())
        reopened = Live2DEditorSession(saved["model_path"])
        self.addCleanup(reopened.close)
        self.assertEqual(list(reopened.project.motions), ["Idle[0]"])
        self.assertEqual(reopened.keyframes("Idle[0]", "ParamAngleY")[0]["value"], -20)
        self.assertEqual(len(reopened.motion_catalog()), 3)
        self.assertEqual(before, self.hashes())
        self.assertFalse(other.dirty)
        self.assertFalse(reopened.dirty)

    def test_delete_new_motion_roundtrip_and_unknown_name_do_not_consume_history(self):
        self.session.create_motion("Temporary", 2)
        self.session.clone_motion("Temporary", "Kept")
        self.session.delete_motion("Temporary")
        self.session.undo()
        self.assertIn("Temporary", self.session.project.motions)
        self.session.redo()
        self.assertNotIn("Temporary", self.session.project.motions)
        previous = self.session._snapshot()
        undo_count = len(self.session._undo)
        with self.assertRaises(AnimationEditingError):
            self.session.delete_motion("unknown")
        self.assertEqual(self.session._snapshot(), previous)
        self.assertEqual(len(self.session._undo), undo_count)
        saved = self.session.save_copy(self.root / "new-deleted-copy")
        reopened = Live2DEditorSession(saved["model_path"])
        self.addCleanup(reopened.close)
        self.assertEqual(list(reopened.project.motions), ["Kept[0]"])


if __name__ == "__main__":
    unittest.main()
