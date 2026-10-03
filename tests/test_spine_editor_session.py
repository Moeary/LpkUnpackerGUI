from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.animation_editing import AnimationEditingError, _rename_directory
from app.core.editor_session import (
    SpineEditorSession, decode_bone_track, encode_bone_track,
    decode_draw_order,
)


class SpineEditorSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.sessions = []

    def tearDown(self):
        for session in self.sessions:
            session.close()
        self.temporary.cleanup()

    def model(self, version="3.8.75", **extra):
        from PIL import Image
        image = Image.new("RGBA", (8, 8), (255, 100, 0, 255))
        image.save(self.source / "page.png")
        (self.source / "model.atlas").write_text("page.png\nsize: 8,8\nformat: RGBA8888\nfilter: Linear,Linear\nrepeat: none\nbody\n  rotate: false\n  xy: 0,0\n  size: 4,4\n  orig: 4,4\n  offset: 0,0\n  index: -1\n", encoding="utf-8")
        document = {"skeleton": {"spine": version, "x": 0, "y": 0, "width": 20, "height": 20},
                    "bones": [{"name": "root"}, {"name": "hip", "parent": "root", "x": 10, "y": 20}],
                    "slots": [{"name": "back", "bone": "hip", "attachment": "body"},
                              {"name": "middle", "bone": "hip", "attachment": "body"},
                              {"name": "front", "bone": "hip", "attachment": "body"}],
                    "skins": [{"name": "default", "attachments": {name: {"body": {"width": 4, "height": 4}} for name in ("back", "middle", "front")}}],
                    "animations": {"Idle": {"bones": {"hip": {"rotate": [{"time": 0, "value" if version.startswith("4.0") else "angle": 0}, {"time": 2, "value" if version.startswith("4.0") else "angle": 0}]}}}}}
        document.update(extra)
        model = self.source / "model.json"
        model.write_text(json.dumps(document), encoding="utf-8")
        return model

    def open(self, model):
        session = SpineEditorSession.open(model)
        self.sessions.append(session)
        return session

    def hashes(self):
        return {path.relative_to(self.source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.source.rglob("*") if path.is_file()}

    def test_edit_undo_save_and_reopen_are_independent_and_preserve_other_data(self):
        unrelated = {"deform": {"default": {"front": {"body": [{"time": 0, "vertices": [1, 2]}]}}},
                     "ik": {"leg": [{"time": 0, "mix": 0.5}]}, "events": [{"time": 0.5, "name": "tap"}]}
        model = self.model(animations={"Idle": unrelated})
        before = self.hashes()
        session = self.open(model)
        session.set_bone_transform("hip", {"y": 25})
        session.set_bone_track("Idle", "hip", "translate", [{"time": 0, "x": 0, "y": 0}, {"time": 0.5, "x": 0, "y": -10}])
        self.assertTrue(session.dirty)
        session.undo()
        self.assertNotIn("bones", session.document["animations"]["Idle"])
        session.redo()
        result = session.save_copy(self.root / "export")
        self.assertFalse(session.dirty)
        reopened = self.open(result["model_path"])
        self.assertEqual(reopened.bone("hip")["y"], 25)
        self.assertEqual(reopened.get_bone_track("Idle", "hip", "translate")["keyframes"][-1]["y"], -10)
        for key, value in unrelated.items():
            self.assertEqual(reopened.document["animations"]["Idle"][key], value)
        self.assertEqual(self.hashes(), before)

    def test_spine38_numeric_curve_and_spine40_multi_value_and_flat_curve_roundtrip(self):
        cases = [
            ("3.8.75", [{"time": 0, "x": 0, "y": 2, "curve": 0.25, "c2": 0.1, "c3": 0.75, "c4": 0.9}, {"time": 2, "x": 10, "y": 4}]),
            ("4.0.37", [{"time": 0, "x": 0, "y": 2, "curve": [0.5, 1, 1.5, 9, 0.5, 5, 1.5, 6]}, {"time": 2, "x": 10, "y": 2}]),
        ]
        for version, raw in cases:
            frames = decode_bone_track(raw, "translate", version)
            encoded = encode_bone_track(frames, "translate", version, 2)
            self.assertEqual(encoded, raw)
            frames[0]["bezier"]["x"][1] = 0
            if version.startswith("3.8"):
                frames[0]["bezier"]["y"][1] = 0
            output = encode_bone_track(frames, "translate", version, 2)
            self.assertNotEqual(output, raw)
            self.assertEqual(raw, cases[0 if version.startswith("3.8") else 1][1])

    def test_invalid_bezier_is_transactional_and_last_key_can_be_deleted(self):
        session = self.open(self.model())
        old = copy.deepcopy(session.document)
        with self.assertRaises(AnimationEditingError):
            session.set_bone_track("Idle", "hip", "rotate", [{"time": 0, "angle": 0, "interpolation": "bezier", "bezier": {"angle": [0.8, 0, 0.2, 1]}}, {"time": 1, "angle": 10}])
        self.assertEqual(session.document, old)
        self.assertFalse(session.can_undo)
        session.set_bone_track("Idle", "hip", "rotate", [])
        self.assertNotIn("hip", session.document["animations"]["Idle"]["bones"])
        session.save_copy(self.root / "empty_track")
        session.undo()
        self.assertEqual(session.document, old)
        self.assertTrue(session.dirty)

    def test_setup_slot_reorder_preserves_existing_draw_order_semantics(self):
        frames = [{"time": 0, "offsets": [{"slot": "back", "offset": 2}]}, {"time": 1}]
        model = self.model(animations={"Idle": {"draworder": frames}})
        session = self.open(model)
        orders = [session.draw_order_at("Idle", seconds) for seconds in (0, 1)]
        session.set_draw_order(["front", "back", "middle"])
        self.assertEqual([session.draw_order_at("Idle", seconds) for seconds in (0, 1)], orders)
        session.undo()
        self.assertEqual(session.document["animations"]["Idle"]["draworder"], frames)
        session.redo()
        session.set_draw_order(["middle", "back", "front"], animation="Idle", seconds=0.5)
        self.assertEqual(session.draw_order_at("Idle", 0.5), ["middle", "back", "front"])
        session.save_copy(self.root / "ordered")

    def test_slot_only_animation_setup_visibility_and_delete_key_save(self):
        session = self.open(self.model())
        session.create_animation("SlotOnly", 2)
        session.set_slot_visible("front", False, animation="SlotOnly", seconds=0.5)
        self.assertIsNone(session.attachment_at("front", "SlotOnly", 1))
        session.set_slot_visible("front", True, animation="SlotOnly", seconds=1)
        self.assertEqual(session.attachment_at("front", "SlotOnly", 1), "body")
        session.delete_slot_key("front", "SlotOnly", 1)
        result = session.save_copy(self.root / "slots")
        reopened = self.open(result["model_path"])
        self.assertIsNone(reopened.attachment_at("front", "SlotOnly", 1.5))
        self.assertEqual(reopened.duration("SlotOnly"), 2)
        session.set_slot_visible("back", False)
        self.assertIsNone(session.slot("back").get("attachment"))

    def test_original_assets_can_disappear_after_session_has_copied_them(self):
        session = self.open(self.model())
        # Simulate disposal of a PreviewPage extraction cache.
        for path in self.source.iterdir():
            path.unlink()
        self.assertTrue(session.prepare_preview().asset.has_skeleton)
        self.assertTrue(Path(session.save_copy(self.root / "after_cache_disposal")["model_path"]).is_file())

    def test_atlas_replacement_changes_only_the_region_and_undo_restores_source(self):
        from PIL import Image
        model = self.model()
        before = self.hashes()
        session = self.open(model)
        item = session.atlas_regions()[0]
        replacement = self.root / "replacement.png"
        Image.new("RGBA", (4, 4), (20, 200, 40, 255)).save(replacement)
        session.replace_atlas_region(item["atlas"], item["id"], replacement)
        self.assertEqual(session.region_image(item["atlas"], item["id"]).getpixel((0, 0)), (20, 200, 40, 255))
        result = session.save_copy(self.root / "replaced")
        with Image.open(Path(result["output_dir"]) / "page.png") as image:
            self.assertEqual(image.getpixel((0, 0)), (20, 200, 40, 255))
            self.assertEqual(image.getpixel((7, 7)), (255, 100, 0, 255))
        session.undo()
        self.assertEqual(session.region_image(item["atlas"], item["id"]).getpixel((0, 0)), (255, 100, 0, 255))
        self.assertEqual(self.hashes(), before)

    def test_export_does_not_overwrite_source_or_existing_directory(self):
        session = self.open(self.model())
        for target in (self.source, self.source / "child", self.root):
            with self.subTest(target=target), self.assertRaises(AnimationEditingError):
                session.save_copy(target)

    def test_native_preview_applies_edited_keys_and_hidden_slot(self):
        from app.core.spine_native import find_native_library, SpineNativeModel
        for version, family in (("3.8.75", "3.8"), ("4.0.37", "4.0")):
            library = find_native_library(None, family)
            if library is None:
                continue
            session = self.open(self.model(version))
            session.create_animation("Editor", 1)
            session.set_bone_track("Editor", "hip", "translate", [
                {"time": 0, "x": 0, "y": 0, "interpolation": "bezier", "bezier": {"x": [0.25, 0, 0.75, 1], "y": [0.25, 0, 0.75, 1]}},
                {"time": 1, "x": 0, "y": -10}])
            session.set_slot_visible("front", False, animation="Editor", seconds=0.5)
            asset = session.prepare_preview().asset
            native = SpineNativeModel(library, asset.skeleton_path, asset.atlas_paths[0], "json")
            try:
                native.set_animation("Editor", False)
                native.set_paused(True)
                native.set_time(0)
                start = native.render_into()
                start_count = start[1]
                y = start[0][0].y
                native.set_time(1)
                end = native.render_into()
                self.assertLess(end[1], start_count)
                self.assertAlmostEqual(end[0][0].y - y, -10, places=3)
            finally:
                native.close()

    def test_publish_retries_transient_permission_without_overwriting(self):
        source, target = self.root / "stage", self.root / "target"
        source.mkdir()
        rename = Path.rename
        attempts = []
        def transient(path, output):
            attempts.append(path)
            if len(attempts) < 3:
                raise PermissionError("temporary Windows file scanner lock")
            return rename(path, output)
        with patch("app.core.animation_editing.os.name", "nt"), patch("app.core.animation_editing.time.sleep"), patch.object(Path, "rename", transient):
            _rename_directory(source, target)
        self.assertEqual(len(attempts), 3)
        self.assertTrue(target.is_dir())
        other = self.root / "other"
        other.mkdir()
        with self.assertRaises(AnimationEditingError):
            _rename_directory(other, target)
        self.assertTrue(other.is_dir())


if __name__ == "__main__":
    unittest.main()
