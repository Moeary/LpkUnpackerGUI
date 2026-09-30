from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.core.animation_editing import AnimationEditingError, INVENTORY_FORMAT
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
        for key, value in (("NextMtn", "SwitchSkin:../model0.json"), ("NextMtn", "Other:0"),
                           ("NextMtn", "../outside.json"), ("File", "SwitchSkin:0")):
            with self.subTest(key=key, value=value):
                document["FileReferences"]["Motions"] = {"Menu": [{key: value}]}
                self.model.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(AnimationEditingError):
                    Live2DEditorSession(self.model)

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


if __name__ == "__main__":
    unittest.main()
