from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from app.core.editor_recovery import RecoveryStore, capture_recovery
from app.core.editor_session import SpineEditorSession
from app.core.live2d_editor_session import Live2DEditorSession
from tests.test_live2d_editor_session import make_model


class EditorRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = RecoveryStore(self.root / "recovery")
        self.identity = uuid.uuid4().hex

    def live2d(self):
        session = Live2DEditorSession(make_model(self.root / "source"))
        self.addCleanup(session.close)
        return session

    def test_live2d_pending_pose_skins_named_selections_and_subprojects_survive_close(self):
        session = self.live2d()
        session.ensure_psd_project()
        session.ensure_mod_project()
        session.capture_skin("Test skin")
        session.save_named_selection("Face", ["ArtMeshFace"])
        session.preview_parameter("ParamAngleY", 13)
        history = len(session._undo)
        override = copy.deepcopy(session.psd_project.data)
        override["ui_state"] = {"recovery_test": "pending field"}
        capture = capture_recovery(session, "live2d", project_overrides={"psd": override})
        self.assertEqual(len(session._undo), history)
        self.assertTrue(session.parameter_preview_pending)
        self.assertTrue(session.dirty)
        session.close()
        record = self.store.prepare(self.identity, capture)
        self.assertEqual(self.store.records(), [])  # Interrupted before publication.
        self.store.publish(record)
        model = self.store.restore(record, self.root / "restored")
        reopened = Live2DEditorSession(model)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.parameter_overrides["ParamAngleY"], 13)
        self.assertEqual(reopened.named_selections(), {"Face": ["ArtMeshFace"]})
        self.assertIn("Test skin", [s["name"] for s in reopened.list_skins()])
        self.assertEqual(reopened.psd_project.data["ui_state"]["recovery_test"], "pending field")
        self.assertIsNotNone(reopened.mod_project)

    def test_spine_capture_is_detached_and_keeps_assets_alive(self):
        source = self.root / "spine.json"
        source.write_text(json.dumps({"skeleton": {"spine": "3.8.75"}, "bones": [{"name": "root"}],
                                      "slots": [], "skins": [], "animations": {}}))
        session = SpineEditorSession.open(source)
        self.addCleanup(session.close)
        session.set_bone_transform("root", {"x": 42})
        capture = capture_recovery(session, "spine")
        session.set_bone_transform("root", {"x": 99})
        self.assertTrue(session.dirty)
        session.close()
        record = self.store.prepare(self.identity, capture)
        self.store.publish(record)
        reopened = SpineEditorSession.open(self.store.restore(record, self.root / "restored"))
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.bone("root")["x"], 42)

    def test_retention_dedup_integrity_and_path_validation(self):
        session = self.live2d()
        for value in (1, 2, 3, 4):
            session.set_parameter("ParamAngleY", value)
            record = self.store.prepare(self.identity, capture_recovery(session, "live2d"))
            self.store.publish(record, keep=2)
        records = self.store.records("live2d")
        self.assertEqual(len(records), 2)
        used = {digest for record in records for digest in record["files"].values()}
        self.assertEqual(len(list((self.store.directory(self.identity) / "blobs").iterdir())), len(used))
        bad = copy.deepcopy(records[0])
        digest = next(iter(bad["files"].values()))
        bad["files"] = {"../escape": digest}
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.store.restore(bad, self.root / "bad")
        blob = self.store.directory(self.identity) / "blobs" / digest
        blob.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "damaged"):
            self.store.restore(records[0], self.root / "corrupt")
        self.store.dismiss(self.identity)
        self.assertEqual(self.store.records(), [])

    def test_malformed_manifest_is_ignored(self):
        directory = self.store.directory(self.identity)
        directory.mkdir(parents=True)
        (directory / "checkpoint-invalid.json").write_text("[]")
        self.assertEqual(self.store.records(), [])
