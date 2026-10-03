import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from app.core.model import Live2DPackageError
from app.core.preview.session import prepare_preview_import


class DirectLive2DPreviewCopyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="direct-preview-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "model.moc3").write_bytes(b"fixture")
        (self.source / "motions").mkdir()
        (self.source / "motions" / "idle.json").write_text(json.dumps({
            "Version": 3, "Meta": {"Duration": 1, "CurveCount": 99},
            "Curves": [{"Target": "Parameter", "Id": "ParamAngleX", "Segments": [0, 0, 0, 1, 5]}],
        }))
        self.model = self.source / "model0.json"
        self.menu = {"Name": "Viewer menu", "Command": "start_mtn init#9:init;start_mtn Idle",
                     "Choices": [{"Text": "Next", "NextMtn": "Idle#1"}]}
        self.model.write_text(json.dumps({"Version": 3, "FileReferences": {
            "Moc": "model.moc3", "Motions": {"Idle": [self.menu, {"File": "motions/idle.json"}]}},
            "Options": {"ViewerOption": True}}))
        (self.source / "PSD-notes.txt").write_text("complete package side file")
        self.temp_root = self.root / "previews"

    def hashes(self):
        return {path.relative_to(self.source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.source.rglob("*") if path.is_file()}

    def test_direct_model_normalizes_copy_and_transfers_only_disposable_ownership(self):
        before = self.hashes()
        result = prepare_preview_import(self.model, self.temp_root)
        self.assertIsNotNone(result.temp_dir)
        self.assertTrue(result.temp_dir.is_relative_to(self.temp_root))
        self.assertEqual(result.package.model_json.parent, result.temp_dir)
        self.assertNotEqual(result.preview_model_json, result.package.model_json)
        full = json.loads(result.package.model_json.read_text())
        filtered = json.loads(result.preview_model_json.read_text())
        self.assertEqual(full["FileReferences"]["Motions"]["Idle"][0], self.menu)
        self.assertEqual(filtered["FileReferences"]["Motions"]["Idle"], [{"File": "motions/idle.json"}])
        self.assertTrue(full["Options"]["ViewerOption"])
        self.assertEqual((result.temp_dir / "PSD-notes.txt").read_text(), "complete package side file")
        motion = json.loads((result.temp_dir / "motions" / "idle.json").read_text())
        self.assertEqual(motion["Meta"]["CurveCount"], 1)
        self.assertEqual(self.hashes(), before)
        # The caller owns this returned copy and may release it independently.
        shutil.rmtree(result.temp_dir)
        self.assertEqual(self.hashes(), before)
        self.assertTrue(self.model.is_file())

    def test_normalization_failure_cleans_only_new_copy(self):
        self.temp_root.mkdir()
        keep = self.temp_root / "other-import.txt"
        keep.write_text("other import")
        before = self.hashes()
        with patch("app.core.preview.session.prepare_model_json_for_preview", side_effect=RuntimeError("normalize failed")):
            with self.assertRaisesRegex(RuntimeError, "normalize failed"):
                prepare_preview_import(self.model, self.temp_root)
        self.assertEqual(list(self.temp_root.iterdir()), [keep])
        self.assertEqual(self.hashes(), before)

    def test_temporary_root_inside_source_is_rejected_before_any_source_write(self):
        before = self.hashes()
        with self.assertRaisesRegex(Live2DPackageError, "outside"):
            prepare_preview_import(self.model, self.source / "temporary")
        self.assertFalse((self.source / "temporary").exists())
        self.assertEqual(self.hashes(), before)

    def test_real_escaping_motion_file_is_rejected_before_normalization(self):
        outside = self.root / "outside.json"
        outside.write_text((self.source / "motions" / "idle.json").read_text())
        data = json.loads(self.model.read_text())
        data["FileReferences"]["Motions"]["Idle"] = [{"File": "../outside.json"}]
        self.model.write_text(json.dumps(data))
        before = self.hashes()
        outside_bytes = outside.read_bytes()
        with self.assertRaises(Live2DPackageError):
            prepare_preview_import(self.model, self.temp_root)
        self.assertEqual(self.hashes(), before)
        self.assertEqual(outside.read_bytes(), outside_bytes)


if __name__ == "__main__":
    unittest.main()
