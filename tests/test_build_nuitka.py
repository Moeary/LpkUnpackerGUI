from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.build_nuitka import ROOT, run_msvc_build


class BuildNuitkaTests(unittest.TestCase):
    def test_msvc_build_uses_batch_file_without_escaped_vsdevcmd_quotes(self):
        captured: dict[str, object] = {}

        def fake_call(command, *, cwd, stdin):
            captured["command"] = command
            captured["cwd"] = cwd
            captured["stdin"] = stdin
            captured["batch"] = Path(command[-1]).read_text(encoding="utf-8")
            return 0

        vsdevcmd = Path(
            r"C:\Program Files\Microsoft Visual Studio\18\Enterprise"
            r"\Common7\Tools\VsDevCmd.bat"
        )
        nuitka_args = [
            r"C:\Python 3.10\python.exe",
            "-m",
            "nuitka",
            "app/main.py",
        ]

        with patch("scripts.build_nuitka.subprocess.call", side_effect=fake_call):
            result = run_msvc_build(vsdevcmd, nuitka_args)

        self.assertEqual(result, 0)
        self.assertEqual(
            captured["command"][:3],
            ["cmd.exe", "/d", "/c"],
        )
        self.assertEqual(captured["cwd"], ROOT)
        self.assertIs(captured["stdin"], subprocess.DEVNULL)
        self.assertIn(f'call "{vsdevcmd}"', captured["batch"])
        self.assertNotIn(r"\"C:\Program Files", captured["batch"])
        self.assertIn(
            subprocess.list2cmdline(nuitka_args),
            captured["batch"],
        )


if __name__ == "__main__":
    unittest.main()
