import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.animation_editing import (
    AnimationEditingError,
    INVENTORY_FORMAT,
    create_animation_project,
    inspect_animation_model,
)


class AnimationEditingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="animation-editing-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()

    def write_json(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def live2d(self, *, motion=None, refs=None):
        moc = self.source / "test.moc3"
        moc.write_bytes(b"not a native MOC fixture")
        (self.source / "page.png").write_bytes(b"texture fixture")
        inventory = self.source / "inventory.json"
        self.write_json(inventory, {
            "format": INVENTORY_FORMAT, "version": 1,
            "moc_sha256": hashlib.sha256(moc.read_bytes()).hexdigest(),
            "parameters": [{"id": "ParamKnee", "name": "膝盖", "min": -1,
                            "max": 1, "default": 0}],
        })
        references = {"Moc": moc.name, "Textures": ["page.png"]}
        if motion is not None:
            self.write_json(self.source / "idle.motion3.json", motion)
            references["Motions"] = {"Idle": [{"File": "idle.motion3.json", "FadeInTime": 0.1}]}
        references.update(refs or {})
        model = self.source / "test.model3.json"
        self.write_json(model, {"Version": 3, "FileReferences": references})
        return model, inventory

    def spine(self, version="3.8.75", *, animations=None, bones=None, extra=None):
        data = {
            "skeleton": {"spine": version, "hash": "fixture"},
            "bones": bones or [{"name": "root"}, {"name": "hip", "parent": "root", "x": 10, "y": 20}],
            "slots": [], "skins": [], "animations": animations or {},
        }
        data.update(extra or {})
        model = self.source / "skeleton.json"
        self.write_json(model, data)
        return model

    def motion(self, segments=None):
        return {"Version": 3, "Meta": {"Duration": 2, "Fps": 30, "Loop": False,
                                       "CurveCount": 999, "TotalSegmentCount": 999,
                                       "TotalPointCount": 999},
                "Curves": [{"Target": "Parameter", "Id": "ParamKnee",
                            "Segments": segments or [0, 0, 0, 2, 0]}],
                "UserData": [{"Time": 1, "Value": "膝"}]}

    def hashes(self):
        return {path.relative_to(self.source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.source.rglob("*") if path.is_file()}

    def test_live2d_range_inventory_and_independent_package(self):
        model, inventory = self.live2d()
        before = self.hashes()
        project = create_animation_project(model, parameter_inventory_path=inventory)
        inspection = project.inspect()
        self.assertEqual(inspection["parameters"][0]["name"], "膝盖")
        self.assertEqual(inspection["inventory_source"], "sidecar")
        project.create_animation("Crouch", duration=2)
        project.set_keyframes("Crouch", "ParamKnee", "value", [
            {"time": 0, "value": 0, "interpolation": "stepped"},
            {"time": 1, "value": 1}, {"time": 2, "value": 0},
        ])
        result = project.save_copy(self.root / "out")
        saved_model = json.loads(Path(result["model_path"]).read_text(encoding="utf-8"))
        rel = saved_model["FileReferences"]["Motions"]["Crouch"][0]["File"]
        motion = json.loads((Path(result["output_dir"]) / rel).read_text(encoding="utf-8"))
        self.assertEqual(motion["Curves"][0]["Segments"], [0, 0, 2, 1, 1, 0, 2, 0])
        self.assertEqual(motion["Meta"]["CurveCount"], 1)
        self.assertEqual(motion["Meta"]["TotalSegmentCount"], 2)
        self.assertEqual(motion["Meta"]["TotalPointCount"], 3)
        self.assertEqual(self.hashes(), before)
        self.assertEqual((Path(result["output_dir"]) / "page.png").read_bytes(), b"texture fixture")
        reopened = create_animation_project(result["model_path"])
        self.assertEqual(reopened.get_keyframes("Crouch[0]", "ParamKnee", "value")["keyframes"][1]["value"], 1)
        project.save_copy(self.root / "out2")
        self.assertEqual(project.source_path, model)

    def test_live2d_existing_bezier_preserved_and_meta_corrected(self):
        original = self.motion([0, 0, 1, 0.25, 0.5, 0.75, 1, 1, 0.5, 0, 2, 0])
        original["Curves"].append({"Target": "Model", "Id": "EyeBlink", "Segments": [0, 1, 0, 2, 1]})
        model, inventory = self.live2d(motion=original)
        project = create_animation_project(model, parameter_inventory_path=inventory)
        self.assertEqual(project.motions["Idle[0]"]["Meta"]["TotalSegmentCount"], 3)
        self.assertEqual(project.motions["Idle[0]"]["Meta"]["TotalPointCount"], 7)
        self.assertEqual(project.motions["Idle[0]"]["Meta"]["TotalUserDataSize"], 3)
        self.assertFalse(project.get_keyframes("Idle[0]", "ParamKnee", "value")["editable"])
        with self.assertRaises(AnimationEditingError):
            project.set_keyframes("Idle[0]", "ParamKnee", "value", [{"time": 1, "value": 0}], mode="merge")
        project.clone_animation("Idle[0]", "Edited")
        project.set_keyframes("Edited", "ParamKnee", "value", [{"time": 0, "value": 0}, {"time": 2, "value": 1}])
        result = project.save_copy(self.root / "copy")
        self.assertEqual(json.loads((Path(result["output_dir"]) / "idle.motion3.json").read_text(encoding="utf-8")), original)
        edited = json.loads(Path(result["animation_paths"][0]).read_text(encoding="utf-8"))
        self.assertEqual(edited["Curves"][1], original["Curves"][1])
        self.assertEqual(edited["UserData"], original["UserData"])

    def test_native_inventory_is_authoritative(self):
        model, inventory = self.live2d()
        with patch("app.core.animation_editing._read_native_parameters", return_value=[
            {"id": "ParamKnee", "min": -0.5, "max": 0.5, "default": 0},
        ]):
            project = create_animation_project(model, parameter_inventory_path=inventory)
        self.assertEqual(project.inventory_source, "native")
        project.create_animation("Move", duration=1)
        with self.assertRaises(AnimationEditingError):
            project.set_keyframes("Move", "ParamKnee", "value", [{"time": 0, "value": 1}])

    def test_inventory_hash_mismatch_is_rejected(self):
        model, inventory = self.live2d()
        payload = json.loads(inventory.read_text(encoding="utf-8"))
        payload["moc_sha256"] = "0" * 64
        self.write_json(inventory, payload)
        with self.assertRaises(AnimationEditingError):
            create_animation_project(model, parameter_inventory_path=inventory)

    def test_missing_inventory_does_not_invent_parameters(self):
        model, _ = self.live2d()
        with self.assertRaises(AnimationEditingError):
            create_animation_project(model)

    def test_editing_unknown_parameter_and_channel_is_rejected(self):
        model, inventory = self.live2d()
        project = create_animation_project(model, parameter_inventory_path=inventory)
        project.create_animation("Move", duration=1)
        for target, channel in [("ParamMissing", "value"), ("ParamKnee", "translate")]:
            with self.subTest(target=target, channel=channel), self.assertRaises(AnimationEditingError):
                project.set_keyframes("Move", target, channel, [{"time": 0, "value": 0}])

    def test_live2d_path_references_are_checked_before_native_read(self):
        for unsafe in ["../outside.json", "D:/outside.json", "\\\\server\\outside.json", "NUL.png", "a\x01.png"]:
            with self.subTest(path=unsafe):
                model, inventory = self.live2d(refs={"Physics": unsafe})
                with self.assertRaises(AnimationEditingError):
                    create_animation_project(model, parameter_inventory_path=inventory)

    def test_motion_segment_validation(self):
        for segments in [[0, 0, 99, 1, 1], [0, 0, 1, 1], [1, 0, 0, 0, 1],
                         [-1, 0, 0, 1, 1], [0, 0, 1, 1.5, 0, 1, 1, 2, 0]]:
            with self.subTest(segments=segments):
                model, inventory = self.live2d(motion=self.motion(segments))
                with self.assertRaises(AnimationEditingError):
                    create_animation_project(model, parameter_inventory_path=inventory)

    def test_spine_38_and_40_rotation_fields_and_setup_semantics(self):
        for version, rotation_key in [("3.8.75", "angle"), ("4.0.37", "value")]:
            with self.subTest(version=version):
                model = self.spine(version)
                before = self.hashes()
                project = create_animation_project(model)
                project.create_animation("Crouch", duration=2)
                project.set_keyframes("Crouch", "hip", "translate", [
                    {"time": 0, "x": 0, "y": 0}, {"time": 1, "x": 0, "y": -10},
                ])
                project.set_keyframes("Crouch", "hip", "rotate", [{"time": 0, "angle": -12}])
                project.set_keyframes("Crouch", "hip", "scale", [{"time": 0, "x": 1, "y": 0.9}])
                result = project.save_copy(self.root / version)
                saved = json.loads(Path(result["skeleton_path"]).read_text(encoding="utf-8"))
                self.assertEqual(saved["bones"][1]["y"], 20)
                timeline = saved["animations"]["Crouch"]["bones"]["hip"]
                self.assertEqual(timeline["translate"][1]["y"], -10)
                self.assertEqual(timeline["translate"][-1]["time"], 2)
                self.assertEqual(timeline["rotate"][0][rotation_key], -12)
                self.assertNotIn("angle" if rotation_key == "value" else "value", timeline["rotate"][0])
                self.assertEqual(timeline["scale"][0]["y"], 0.9)
                self.assertEqual(self.hashes(), before)
                self.assertEqual(inspect_animation_model(result["skeleton_path"])["animations"][0]["duration"], 2)

    def test_spine_existing_slots_constraints_and_other_curves_survive(self):
        animation = {"bones": {"hip": {"rotate": [{"time": 0, "angle": 0, "curve": [0.25, 0, 0.75, 1]},
                                                       {"time": 2, "angle": 10}]}},
                     "slots": {"body": {"attachment": [{"time": 0, "name": "body"}]}},
                     "ik": {"leg": [{"time": 0, "mix": 1}]}}
        model = self.spine(animations={"Idle": animation}, extra={
            "slots": [{"name": "body", "bone": "hip"}],
            "ik": [{"name": "leg", "bones": ["hip"], "target": "root"}],
        })
        project = create_animation_project(model)
        self.assertFalse(project.get_keyframes("Idle", "hip", "rotate")["editable"])
        project.clone_animation("Idle", "Crouch")
        project.set_keyframes("Crouch", "hip", "translate", [{"time": 0, "x": 0, "y": -10}])
        result = project.save_copy(self.root / "copy")
        saved = json.loads(Path(result["skeleton_path"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["animations"]["Idle"], animation)
        self.assertEqual(saved["animations"]["Crouch"]["slots"], animation["slots"])
        self.assertEqual(saved["animations"]["Crouch"]["ik"], animation["ik"])
        self.assertEqual(saved["animations"]["Crouch"]["bones"]["hip"]["rotate"], animation["bones"]["hip"]["rotate"])

    def test_merge_replaces_conflicting_times_and_keeps_other_keys(self):
        project = create_animation_project(self.spine())
        project.create_animation("Move", duration=3)
        project.set_keyframes("Move", "hip", "rotate", [{"time": 0, "angle": 0}, {"time": 2, "angle": 10}])
        result = project.set_keyframes("Move", "hip", "rotate", [{"time": 1, "angle": 4}, {"time": 2, "angle": 8}], mode="merge")
        self.assertEqual([(f["time"], f["angle"]) for f in result["keyframes"]], [(0, 0), (1, 4), (2, 8), (3, 10)])

    def test_spine_38_native_numeric_bezier_preserved(self):
        timeline = [{"x": -1.48, "y": -0.02, "curve": 0.332, "c2": 0.4, "c3": 0.689},
                    {"time": 2, "x": 4.63, "y": 0.06}]
        project = create_animation_project(self.spine(animations={
            "Idle": {"bones": {"hip": {"translate": timeline}}},
        }))
        frames = project.get_keyframes("Idle", "hip", "translate")
        self.assertFalse(frames["editable"])
        self.assertEqual(frames["keyframes"][0]["curve"], 0.332)
        with self.assertRaises(AnimationEditingError):
            project.set_keyframes("Idle", "hip", "translate", [{"time": 1, "x": 1, "y": 0}], mode="merge")
        project.clone_animation("Idle", "Copy")
        project.set_keyframes("Copy", "hip", "rotate", [{"time": 0, "angle": 1}])
        result = project.save_copy(self.root / "copy")
        data = json.loads(Path(result["skeleton_path"]).read_text(encoding="utf-8"))
        self.assertEqual(data["animations"]["Copy"]["bones"]["hip"]["translate"], timeline)

    def test_invalid_keyframes_do_not_change_project(self):
        project = create_animation_project(self.spine())
        project.create_animation("Move", duration=2)
        before = copy.deepcopy(project.document)
        cases = [[], [{"time": -1, "angle": 0}], [{"time": 3, "angle": 0}],
                 [{"time": 0, "angle": float("nan")}], [{"time": 0, "angle": True}],
                 [{"time": 1, "angle": 0}, {"time": 0, "angle": 0}],
                 [{"time": 0, "angle": 0}, {"time": 0, "angle": 1}],
                 [{"time": 0, "angle": 0, "interpolation": "bezier"}],
                 [{"time": 0, "angle": 0, "unexpected": 1}],
                 [{"time": 0, "angle": 10 ** 1000}], [{"time": 0, "angle": 1e100}]]
        for frames in cases:
            with self.subTest(frames=str(frames)[:100]), self.assertRaises(AnimationEditingError):
                project.set_keyframes("Move", "hip", "rotate", frames)
            self.assertEqual(project.document, before)

    def test_spine_missing_bone_or_unsupported_channel_rejected(self):
        project = create_animation_project(self.spine())
        project.create_animation("Move", duration=2)
        for target, channel in [("missing", "rotate"), ("hip", "shear"), ("hip", "value")]:
            with self.subTest(target=target, channel=channel), self.assertRaises(AnimationEditingError):
                project.get_keyframes("Move", target, channel)

    def test_unsafe_and_duplicate_animation_names_rejected(self):
        project = create_animation_project(self.spine())
        for name in ["", "../escape", "a/b", "a\\b", "a:", "a\x00", "ends.", " spaced "]:
            with self.subTest(name=name), self.assertRaises(AnimationEditingError):
                project.create_animation(name, duration=1)
        project.create_animation("Move", duration=1)
        with self.assertRaises(AnimationEditingError):
            project.create_animation("Move", duration=1)

    def test_output_existing_source_and_empty_motion_rejected(self):
        project = create_animation_project(self.spine())
        project.create_animation("Move", duration=1)
        with self.assertRaises(AnimationEditingError):
            project.save_copy(self.root / "empty-motion")
        self.assertFalse((self.root / "empty-motion").exists())
        project.set_keyframes("Move", "hip", "rotate", [{"time": 0, "angle": 0}])
        for output in [self.source, self.source / "child", self.root]:
            with self.subTest(output=output), self.assertRaises(AnimationEditingError):
                project.save_copy(output)
        self.assertFalse((self.source / "child").exists())

    def test_missing_asset_at_save_leaves_no_output_package(self):
        model, inventory = self.live2d()
        project = create_animation_project(model, parameter_inventory_path=inventory)
        (self.source / "page.png").unlink()
        with self.assertRaises(AnimationEditingError):
            project.save_copy(self.root / "out")
        self.assertFalse((self.root / "out").exists())

    def test_atlas_and_page_copy_and_traversal_rejection(self):
        model = self.spine()
        (self.source / "page.png").write_bytes(b"page fixture")
        atlas = self.source / "skeleton.atlas"
        atlas.write_text("page.png\nsize:1,1\nfilter:Linear,Linear\nregion\n  bounds:0,0,1,1\n", encoding="utf-8")
        project = create_animation_project(model)
        result = project.save_copy(self.root / "copy")
        self.assertEqual((Path(result["output_dir"]) / "page.png").read_bytes(), b"page fixture")
        atlas.write_text("../outside.png\nsize:1,1\nregion\n  bounds:0,0,1,1\n", encoding="utf-8")
        with self.assertRaises(AnimationEditingError):
            create_animation_project(model)

    def test_source_unknown_bone_unsorted_frames_and_versions_rejected(self):
        for animations in [
            {"Bad": {"bones": {"missing": {"rotate": [{"time": 0, "angle": 0}]}}}},
            {"Bad": {"bones": {"hip": {"rotate": [{"time": 1, "angle": 0}, {"time": 0, "angle": 0}]}}}},
            {"Bad": {"bones": {"hip": {"unknown": [{"time": 0, "value": 0}]}}}},
        ]:
            with self.subTest(animations=animations), self.assertRaises(AnimationEditingError):
                create_animation_project(self.spine(animations=animations))
        for version in ["4.2.1", "3.7.0", "unknown"]:
            with self.subTest(version=version), self.assertRaises(AnimationEditingError):
                create_animation_project(self.spine(version))

    def test_nonfinite_json_rejected(self):
        model = self.spine()
        for value in ["NaN", "Infinity", "1e999"]:
            model.write_text('{"skeleton":{"spine":"3.8.75"},"bones":[{"name":"root","x":' + value + '}]}', encoding="utf-8")
            with self.subTest(value=value), self.assertRaises(AnimationEditingError):
                create_animation_project(model)

    def test_binary_conversion_same_version_independent_json_and_wrapper(self):
        binary = self.source / "model.skel"
        binary.write_bytes(b"4.0.37 binary fixture")
        wrapper = self.source / "model0.json"
        self.write_json(wrapper, {"skeleton": binary.name, "atlases": [], "motions": {}})
        def convert(converter, source, output, version, remove_curve):
            self.assertEqual(source, binary)
            self.assertEqual(version, "4.0.37")
            self.assertFalse(remove_curve)
            self.write_json(output, {"skeleton": {"spine": "4.0.37"}, "bones": [{"name": "root"}],
                                     "animations": {}, "slots": [], "skins": []})
            return [], "", "", 0
        before = self.hashes()
        with patch("app.core.spine_converter.discover_native_converter", return_value=self.root / "fixture.dll"), \
                patch("app.core.spine_converter._run_native_converter", side_effect=convert):
            project = create_animation_project(wrapper)
        project.create_animation("Move", duration=1)
        project.set_keyframes("Move", "root", "rotate", [{"time": 0, "angle": 5}])
        result = project.save_copy(self.root / "copy")
        saved_wrapper = json.loads(Path(result["model_path"]).read_text(encoding="utf-8"))
        self.assertEqual(saved_wrapper["skeleton"], "model.json")
        self.assertEqual(saved_wrapper["motions"]["Move"][0]["file"], "Move")
        self.assertEqual((Path(result["output_dir"]) / "model.skel").read_bytes(), binary.read_bytes())
        self.assertEqual(self.hashes(), before)

    def test_generated_json_collision_rejected(self):
        model, inventory = self.live2d(refs={"Physics": "lpk_animation_project.json"})
        self.write_json(self.source / "lpk_animation_project.json", {"Version": 3})
        project = create_animation_project(model, parameter_inventory_path=inventory)
        with self.assertRaises(AnimationEditingError):
            project.save_copy(self.root / "copy")
        self.assertFalse((self.root / "copy").exists())


if __name__ == "__main__":
    unittest.main()
