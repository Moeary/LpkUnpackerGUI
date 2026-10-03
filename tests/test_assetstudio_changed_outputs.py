from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from app.core.assetstudio_cli import AssetStudioCLI


class AssetStudioChangedOutputTests(unittest.TestCase):
    def test_repeat_export_includes_overwritten_files_but_not_old_models(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir()
            updated = output / "hero.json"
            untouched = output / "old.json"
            updated.write_text("old", encoding="utf-8")
            untouched.write_text("unchanged", encoding="utf-8")

            def export(*args, **kwargs):
                updated.write_text("new skeleton bytes", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            cli = AssetStudioCLI(root / "AssetStudioModCLI.exe")
            with patch("app.core.assetstudio_cli.subprocess.run", side_effect=export):
                result = cli.export_spine(root / "model.bundle", output)
            self.assertEqual(result.exported_files, [updated.resolve()])
            self.assertEqual(untouched.read_text(encoding="utf-8"), "unchanged")


if __name__ == "__main__":
    unittest.main()
