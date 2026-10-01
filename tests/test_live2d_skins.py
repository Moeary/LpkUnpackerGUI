from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.core.animation_editing import AnimationEditingError
from app.core.live2d_editor_session import EDITOR_MANIFEST, Live2DEditorSession
from app.core.live2d_skins import ORIGINAL_SKIN, _uv_signature
from tests.test_live2d_editor_session import make_model


def hashes(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def two_atlas_model(root, *, different_moc=False):
    path = make_model(root)
    Image.new("RGBA", (32, 32), "green").save(root / "second.png")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["FileReferences"]["Textures"].append("second.png")
    data["FileReferences"]["Motions"] = {"Opaque": [{"Command": "start_mtn Idle;change_model model8.json", "Name": "Keep"}]}
    data["HitAreas"] = [{"Id": "ArtMeshSwitch", "Name": "Unused"}]
    data["CustomViewerData"] = {"label": "Keep all opaque fields", "enabled": True}
    path.write_text(json.dumps(data), encoding="utf-8")
    if different_moc:
        moc = root / "model.moc3"
        moc.write_bytes(b"other invalid MOC fixture")
        inventory = root / "live2d_parameter_inventory.json"
        params = json.loads(inventory.read_text(encoding="utf-8"))
        params["moc_sha256"] = hashlib.sha256(moc.read_bytes()).hexdigest()
        inventory.write_text(json.dumps(params), encoding="utf-8")
    return path


class Live2DSkinTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lpk-skins-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.model = two_atlas_model(self.root / "source")
        self.session = Live2DEditorSession(self.model)
        self.addCleanup(self.session.close)
        self.original = tuple(path.read_bytes() for path in self.session.texture_paths)
        self.source_hashes = hashes(self.model.parent)

    def image(self, name, color, size=(32, 32)):
        path = self.root / name
        Image.new("RGBA", size, color).save(path)
        return path

    def mapped(self, name="Red", *, activate=False):
        return self.session.import_skin(self.image(name + ".png", "red"), name,
                                        mapping={0: self.root / (name + ".png")}, activate=activate)

    def test_open_browse_and_noop_apply_are_clean_and_lazy(self):
        self.assertFalse(self.session.dirty)
        self.assertEqual(self.session.active_skin_id, ORIGINAL_SKIN)
        self.assertFalse(self.session.skin_modified)
        self.assertEqual([item["id"] for item in self.session.list_skins()], [ORIGINAL_SKIN])
        self.assertFalse((self.session.root / "skins").exists())
        self.assertFalse((self.session.root / "psd").exists())
        self.assertFalse((self.session.root / "mods").exists())
        self.assertFalse(self.session.apply_skin(ORIGINAL_SKIN))
        self.assertFalse(self.session.dirty)
        self.assertFalse(self.session.can_undo)
        self.session.list_skins()[0]["name"] = "Caller must not mutate the registry"
        self.assertEqual(self.session.list_skins()[0]["name"], "Original")

    def test_same_moc_index_matching_and_explicit_cross_model_mapping(self):
        candidate = two_atlas_model(self.root / "candidate")
        Image.new("RGBA", (32, 32), "yellow").save(candidate.parent / "texture.png")
        before = hashes(candidate.parent)
        skin = self.session.import_skin(candidate, "Same MOC")
        self.assertEqual(skin["compatibility"]["mode"], "same_moc")
        self.assertEqual(self.session.active_skin_id, ORIGINAL_SKIN)
        self.assertEqual(tuple(self.session._texture_data), self.original)
        self.assertTrue(self.session.inspect_skin_source(candidate)["automatic_mapping"])
        other = two_atlas_model(self.root / "other", different_moc=True)
        previous = copy.deepcopy(self.session.list_skins())
        with self.assertRaisesRegex(AnimationEditingError, "UV compatibility"):
            self.session.import_skin(other, "Unproven")
        self.assertEqual(self.session.list_skins(), previous)
        cross = self.session.import_skin(other, "Artist confirmed", mapping={0: 1, 1: 0}, activate=True)
        self.assertEqual(cross["compatibility"]["mode"], "explicit_mapping")
        self.assertFalse(cross["compatibility"]["automatic"])
        self.assertEqual(self.session.texture_paths[0].read_bytes(), (other.parent / "second.png").read_bytes())
        self.assertEqual(before, hashes(candidate.parent))
        self.assertEqual(self.source_hashes, hashes(self.model.parent))

    def test_same_size_or_untrusted_mesh_sidecar_is_not_uv_evidence(self):
        other = two_atlas_model(self.root / "other", different_moc=True)
        # Both fixtures have identical dimensions and mesh sidecar UVs.
        self.assertEqual((other.parent / "drawables.json").read_bytes(), (self.model.parent / "drawables.json").read_bytes())
        inspection = self.session.inspect_skin_source(other)
        self.assertFalse(inspection["automatic_mapping"])
        self.assertEqual(inspection["compatibility"]["mode"], "mapping_required")
        with self.assertRaises(AnimationEditingError):
            self.session.import_skin(other, "Unsafe guess")
        with self.assertRaises(AnimationEditingError):
            self.session.import_skin(self.image("bare.png", "blue"), "Bare image")
        self.assertFalse(self.session.dirty)

    def test_texture_folder_candidates_require_mapping_and_model_folder_keeps_indices(self):
        folder = self.root / "images"
        folder.mkdir()
        Image.new("RGBA", (32, 32), "red").save(folder / "atlas-red.png")
        info = self.session.inspect_skin_source(folder)
        self.assertEqual(len(info["textures"]), 1)
        self.assertFalse(info["automatic_mapping"])
        with self.assertRaisesRegex(AnimationEditingError, "UV compatibility"):
            self.session.import_skin(folder, "Unconfirmed")
        skin = self.session.import_skin(folder, "Confirmed", mapping={1: 0}, activate=True)
        self.assertEqual(skin["compatibility"]["mode"], "explicit_mapping")
        self.assertEqual(self.session.texture_paths[1].read_bytes(), (folder / "atlas-red.png").read_bytes())
        candidate = two_atlas_model(self.root / "model-folder", different_moc=True)
        skin2 = self.session.import_skin(candidate.parent, "Cross folder", mapping={0: 1})
        self.assertEqual(skin2["mappings"][0]["source_index"], 1)
        self.assertEqual(self.session.skin_texture_paths(skin2["id"])[0].read_bytes(), (candidate.parent / "second.png").read_bytes())

    def test_native_uv_signature_requires_exact_ids_indices_and_assignments(self):
        snapshot = {"drawables": [{"id": "A", "texture_index": 0, "uvs": [[0, 0], [1, 1]],
                                    "indices": [0, 1, 0], "vertices": [[10, 20], [50, 60]]}]}
        baseline = _uv_signature(snapshot)
        moved = copy.deepcopy(snapshot)
        moved["drawables"][0]["vertices"][0] = [88, 99]
        self.assertEqual(baseline, _uv_signature(moved))
        for key, value in (("id", "B"), ("texture_index", 1), ("uvs", [[0, 0], [.5, 1]]), ("indices", [1, 0, 1])):
            changed = copy.deepcopy(snapshot)
            changed["drawables"][0][key] = value
            self.assertNotEqual(baseline, _uv_signature(changed))
        self.assertIsNone(_uv_signature({"drawables": []}))

    def test_import_apply_is_one_undo_and_retains_current_actions_pose_and_source(self):
        self.session.create_motion("NewMotion", 1)
        self.session.set_keyframes("NewMotion", "ParamAngleY", [{"time": 0, "value": 10}])
        self.session.set_parameter("ParamAngleY", 8)
        motions, pose = copy.deepcopy(self.session.project.motions), dict(self.session.parameter_overrides)
        count = len(self.session._undo)
        skin = self.mapped(activate=True)
        self.assertEqual(len(self.session._undo), count + 1)
        self.assertEqual(self.session.active_skin_id, skin["id"])
        self.assertFalse(self.session.skin_modified)
        self.assertEqual(self.session.project.motions, motions)
        self.assertEqual(self.session.parameter_overrides, pose)
        self.session.undo()
        self.assertEqual(self.session.active_skin_id, ORIGINAL_SKIN)
        self.assertEqual(tuple(self.session._texture_data), self.original)
        self.assertEqual(len(self.session.list_skins()), 1)
        self.session.redo()
        self.assertEqual(self.session.active_skin_id, skin["id"])
        self.assertEqual(self.session.project.motions, motions)
        self.assertEqual(self.source_hashes, hashes(self.model.parent))

    def test_switch_retains_uncollected_working_bytes_in_same_history_entry(self):
        skin = self.mapped(activate=True)
        self.session.replace_texture(1, self.image("work.png", "purple"))
        working = tuple(self.session._texture_data)
        self.assertTrue(self.session.skin_modified)
        count = len(self.session._undo)
        self.session.apply_skin(ORIGINAL_SKIN)
        self.assertEqual(len(self.session._undo), count + 1)
        draft = next(item for item in self.session.list_skins() if item["source_kind"] == "working")
        self.assertEqual(tuple(path.read_bytes() for path in self.session.skin_texture_paths(draft["id"])), working)
        self.assertEqual(tuple(self.session._texture_data), self.original)
        self.session.undo()
        self.assertEqual(self.session.active_skin_id, skin["id"])
        self.assertEqual(tuple(self.session._texture_data), working)
        self.assertTrue(self.session.skin_modified)
        self.assertFalse(any(item["source_kind"] == "working" for item in self.session.list_skins()))
        self.session.redo()
        self.session.apply_skin(draft["id"])
        self.assertEqual(tuple(self.session._texture_data), working)
        self.assertFalse(self.session.skin_modified)

    def test_capture_clone_rename_remove_undo_and_original_protection(self):
        self.session.replace_texture(0, self.image("work.png", "orange"))
        skin = self.session.capture_skin("Orange", source_kind="psd", metadata={"version_id": "v1"})
        self.assertEqual(self.session.active_skin_id, skin["id"])
        self.assertFalse(self.session.skin_modified)
        clone = self.session.clone_skin(skin["id"], "Orange copy")
        self.session.rename_skin(clone["id"], "Orange renamed")
        self.assertEqual(self.session.list_skins()[-1]["name"], "Orange renamed")
        self.session.undo()
        self.assertEqual(self.session.list_skins()[-1]["name"], "Orange copy")
        self.session.redo()
        self.session.remove_skin(skin["id"])
        self.assertEqual(self.session.active_skin_id, ORIGINAL_SKIN)
        self.session.undo()
        self.assertEqual(self.session.active_skin_id, skin["id"])
        self.session.redo()
        self.assertEqual(self.session.active_skin_id, ORIGINAL_SKIN)
        for operation in (lambda: self.session.rename_skin(ORIGINAL_SKIN, "Changed"),
                          lambda: self.session.remove_skin(ORIGINAL_SKIN),
                          lambda: self.session.clone_skin(clone["id"], "Original")):
            with self.assertRaises(AnimationEditingError):
                operation()

    def test_mapping_failure_and_second_write_failure_leave_bytes_registry_history_intact(self):
        previous = copy.deepcopy(self.session._snapshot_values())
        good = self.image("good.png", "red")
        bad = self.image("bad.png", "yellow", (16, 32))
        with self.assertRaises(AnimationEditingError):
            self.session.import_skin(None, "Bad", mapping={0: good, 1: bad}, activate=True)
        self.assertEqual(self.session._snapshot_values(), previous)
        self.assertFalse((self.session.root / "skins").exists())
        write = self.session._write_texture
        calls = []
        def fail_second(path, data):
            calls.append(path)
            if len(calls) == 2:
                raise PermissionError("locked atlas")
            return write(path, data)
        with patch.object(self.session, "_write_texture", side_effect=fail_second):
            with self.assertRaises(PermissionError):
                self.session.import_skin(None, "Failed transaction", mapping={0: good, 1: good}, activate=True)
        self.assertEqual(self.session._snapshot_values(), previous)
        self.assertEqual(tuple(path.read_bytes() for path in self.session.texture_paths), self.original)
        self.assertFalse(self.session.can_undo)
        self.assertFalse(self.session.dirty)

    def test_non_json_provenance_fails_atomically_before_it_can_corrupt_dirty_checks(self):
        for metadata in ({"bad": Path("external.psd")}, {"bad": float("nan")}, ["not an object"]):
            with self.assertRaises(AnimationEditingError):
                self.session.capture_skin("Invalid provenance", metadata=metadata)
            self.assertFalse(self.session.dirty)
            self.assertFalse(self.session.can_undo)
            self.assertEqual(len(self.session.list_skins()), 1)

    def test_save_move_reopen_preserves_registry_draft_original_and_excludes_orphans(self):
        red = self.mapped(activate=True)
        orphan = self.session.clone_skin(red["id"], "Removed")
        self.session.remove_skin(orphan["id"])
        self.session.replace_texture(1, self.image("draft.png", "pink"))
        working = tuple(self.session._texture_data)
        self.session.create_motion("CurrentAction", 1)
        result = self.session.save_copy(self.root / "saved")
        saved = Path(result["model_path"]).parent
        self.assertFalse((saved / "skins" / orphan["id"]).exists())
        state = json.loads((saved / EDITOR_MANIFEST).read_text(encoding="utf-8"))
        self.assertEqual(state["version"], 2)
        for entry in state["skins"]["entries"]:
            self.assertTrue(all(not Path(texture["path"]).is_absolute() for texture in entry["textures"]))
        moved = self.root / "moved"
        saved.rename(moved)
        self.session.close()
        reopened = Live2DEditorSession(moved / "model.json")
        self.addCleanup(reopened.close)
        self.assertFalse(reopened.dirty)
        self.assertEqual(reopened.active_skin_id, red["id"])
        self.assertTrue(reopened.skin_modified)
        self.assertEqual(tuple(reopened._texture_data), working)
        reopened.apply_skin(ORIGINAL_SKIN)
        self.assertEqual(tuple(reopened._texture_data), self.original)
        self.assertIn("CurrentAction[0]", reopened.project.motions)
        self.assertEqual(self.source_hashes, hashes(self.model.parent))

    def test_registry_escape_digest_and_moc_mismatch_are_rejected(self):
        result = self.session.save_copy(self.root / "registry")
        root = Path(result["model_path"]).parent
        manifest = root / EDITOR_MANIFEST
        baseline = json.loads(manifest.read_text(encoding="utf-8"))
        for mutate in (lambda state: state["skins"]["entries"][0]["textures"][0].update(path="../texture.png"),
                       lambda state: state["skins"]["entries"][0]["textures"][0].update(sha256="0" * 64),
                       lambda state: state["skins"].update(moc_sha256="0" * 64)):
            state = copy.deepcopy(baseline)
            mutate(state)
            manifest.write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaises(AnimationEditingError):
                Live2DEditorSession(root / "model.json")

    def test_psd_import_adds_skin_without_replacing_current_motion_or_metadata(self):
        self.session.create_motion("KeepAction", 1)
        snapshot = self.session.export_snapshot(self.root / "snapshot")
        document = json.loads(snapshot.read_text(encoding="utf-8"))
        Image.new("RGBA", (32, 32), "cyan").save(snapshot.parent / document["FileReferences"]["Textures"][0])
        sidecar = json.loads((snapshot.parent / EDITOR_MANIFEST).read_text(encoding="utf-8"))
        self.assertEqual(sidecar["skin_at_export"]["id"], ORIGINAL_SKIN)
        skin = self.session.import_skin(snapshot, "PSD version 1", source_kind="psd",
                                        metadata={"version_id": "v1", "skin_at_export": sidecar["skin_at_export"]}, activate=True)
        self.assertEqual(skin["source"]["metadata"]["version_id"], "v1")
        self.assertIn("KeepAction", self.session.project.motions)
        self.assertEqual(self.session.active_skin_id, skin["id"])
        self.assertEqual(self.source_hashes, hashes(self.model.parent))

    def test_plain_export_uses_current_motion_and_chosen_bytes_without_workspace_or_new_switch_commands(self):
        skin = self.mapped(activate=True)
        self.session.ensure_psd_project()
        self.session.ensure_mod_project()
        self.session.create_motion("LatestAction", 1)
        self.session.replace_texture(1, self.image("unsaved.png", "purple"))
        before, undo = self.session._signature(), len(self.session._undo)
        result = self.session.export_skin(ORIGINAL_SKIN, self.root / "plain")
        root = Path(result["output_dir"])
        document = json.loads(Path(result["model_path"]).read_text(encoding="utf-8"))
        self.assertIn("LatestAction", document["FileReferences"]["Motions"])
        self.assertEqual(document["CustomViewerData"], self.session.original_document["CustomViewerData"])
        self.assertEqual(document["FileReferences"]["Motions"]["Opaque"], self.session.original_document["FileReferences"]["Motions"]["Opaque"])
        self.assertNotIn("TapSwitchSkin", document["FileReferences"]["Motions"])
        self.assertEqual(tuple((root / ref).read_bytes() for ref in document["FileReferences"]["Textures"]), self.original)
        for folder in ("skins", "psd", "mods", EDITOR_MANIFEST, "skin_manifest.json"):
            self.assertFalse((root / folder).exists(), folder)
        current = self.session.export_skin(None, self.root / "current")
        self.assertEqual((Path(current["output_dir"]) / "second.png").read_bytes(), self.session.texture_paths[1].read_bytes())
        self.assertEqual(self.session.active_skin_id, skin["id"])
        self.assertEqual(self.session._signature(), before)
        self.assertEqual(len(self.session._undo), undo)
        with self.assertRaises(AnimationEditingError):
            self.session.export_skin(None, root)
        with self.assertRaises(AnimationEditingError):
            self.session.export_skin(None, self.model.parent / "must-not-write")

    def test_viewerex_adapter_current_actions_mapping_and_existing_trigger_conflicts(self):
        red = self.mapped(activate=True)
        self.session.create_motion("CurrentAction", 1)
        before = self.session._signature()
        output = self.session.export_viewerex([ORIGINAL_SKIN, red["id"]], self.root / "viewer", trigger_id="ArtMeshSwitch")
        root = Path(output["output_dir"])
        for index, expected in enumerate((self.original, tuple(self.session._texture_data))):
            document = json.loads((root / f"model{index}.json").read_text(encoding="utf-8"))
            self.assertIn("CurrentAction", document["FileReferences"]["Motions"])
            self.assertIn("TapSwitchSkin", document["FileReferences"]["Motions"])
            self.assertEqual(document["CustomViewerData"], self.session.original_document["CustomViewerData"])
            self.assertEqual(tuple((root / ref).read_bytes() for ref in document["FileReferences"]["Textures"]), expected)
            self.assertEqual((root / document["FileReferences"]["Moc"]).read_bytes(), (self.model.parent / "model.moc3").read_bytes())
        self.assertTrue(Path(output["manifest_path"]).is_file())
        self.assertTrue(Path(output["mapping_path"]).is_file())
        self.assertFalse((root / "skins").exists())
        self.assertFalse((root / EDITOR_MANIFEST).exists())
        self.assertEqual(self.session._signature(), before)
        self.assertEqual(self.source_hashes, hashes(self.model.parent))
        with self.assertRaisesRegex(Exception, "unused|trigger"):
            self.session.export_viewerex([ORIGINAL_SKIN, red["id"]], self.root / "no-trigger")
        self.assertFalse((self.root / "no-trigger").exists())
        self.session.original_document["HitAreas"][0]["Motion"] = "ExistingAction"
        with self.assertRaisesRegex(Exception, "already has an event"):
            self.session.export_viewerex([ORIGINAL_SKIN, red["id"]], self.root / "conflict", trigger_id="ArtMeshSwitch")
        self.assertFalse((self.root / "conflict").exists())

    def test_viewerex_reserved_motion_groups_are_not_overwritten(self):
        skin = self.mapped()
        self.session.create_motion("SwitchSkin", 1)
        before = self.session._signature()
        with self.assertRaisesRegex(AnimationEditingError, "existing motion group: SwitchSkin"):
            self.session.export_viewerex([ORIGINAL_SKIN, skin["id"]], self.root / "reserved", trigger_id="ArtMeshSwitch")
        self.assertFalse((self.root / "reserved").exists())
        self.assertEqual(before, self.session._signature())
        ordinary = self.session.export_skin(None, self.root / "ordinary-reserved")
        document = json.loads(Path(ordinary["model_path"]).read_text(encoding="utf-8"))
        self.assertIn("SwitchSkin", document["FileReferences"]["Motions"])
        self.assertNotIn("TapSwitchSkin", document["FileReferences"]["Motions"])


if __name__ == "__main__":
    unittest.main()
