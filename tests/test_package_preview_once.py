import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.core.preview.session import prepare_package_preview_import


class PackagePreviewOnceTests(unittest.TestCase):
    def test_live2d_fallback_reuses_single_extraction(self):
        with tempfile.TemporaryDirectory() as root:
            def extract(sources, destination, mode, **kwargs):
                self.assertNotIn("spine_conversion", kwargs)
                model = Path(destination) / "model0.json"
                model.write_text(json.dumps({"Version": 3, "FileReferences": {"Moc": "model.moc3"}}), encoding="utf-8")
                return SimpleNamespace(has_failures=False)
            with patch("app.core.preview.session.run_extraction_batch", side_effect=extract) as run:
                result = prepare_package_preview_import("sample.lpk", root)
            self.assertEqual(run.call_count, 1)
            self.assertTrue(result.preview_model_json.is_file())
            self.assertTrue(result.temp_dir.is_dir())
            self.assertEqual(result.package.model_json.parent, result.temp_dir)

    def test_cancel_removes_only_disposable_workspace(self):
        with tempfile.TemporaryDirectory() as root:
            keep = Path(root) / "keep.txt"
            keep.write_text("unrelated", encoding="utf-8")
            with patch("app.core.preview.session.run_extraction_batch", return_value=SimpleNamespace(has_failures=False)):
                with self.assertRaisesRegex(RuntimeError, "cancelled"):
                    prepare_package_preview_import("sample.lpk", root, should_continue=lambda: False)
            self.assertEqual(list(Path(root).iterdir()), [keep])


if __name__ == "__main__":
    unittest.main()
