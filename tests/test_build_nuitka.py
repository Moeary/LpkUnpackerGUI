from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.build_nuitka import ROOT, build_nuitka_args, run_msvc_build


class BuildNuitkaTests(unittest.TestCase):
    def test_build_allows_required_nuitka_helper_downloads(self):
        args = build_nuitka_args("msvc")

        self.assertIn("--assume-yes-for-downloads", args)

    @staticmethod
    def _write_required_native_inputs(root: Path, *, include_notices: bool = True) -> None:
        files = {
            root / "runtime/tools/SpineSkeletonDataConverter/lpk_spine_converter.dll": b"converter",
            root / "third_party/wang606_spine_converter/LICENSE": b"license",
            root / "third_party/wang606_spine_converter/SOURCE_METADATA.json": b"{}",
            root / "runtime/tools/spine_native/3.8.75/spine_bridge.dll": b"bridge-375",
            root / "runtime/tools/spine_native/3.8.75/spine_native.json": b"{}",
            root / "runtime/tools/spine_native/3.8.75/LICENSE": b"license-375",
            root / "runtime/tools/spine_native/4.0/spine_bridge.dll": b"bridge-40",
            root / "runtime/tools/spine_native/4.0/spine_native.json": b"{}",
            root / "runtime/tools/spine_native/4.0/LICENSE": b"license-40",
        }
        if include_notices:
            files[root / "third_party/wang606_spine_converter/THIRD_PARTY_NOTICES.md"] = b"notices"
        for path, payload in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)

    def test_required_native_build_fails_with_explicit_missing_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("scripts.build_nuitka.ROOT", Path(temporary)):
                with self.assertRaises(FileNotFoundError) as context:
                    build_nuitka_args("msvc", require_native=True)
            self.assertIn("converter DLL", str(context.exception))
            self.assertIn("Spine 3.8.75 bridge", str(context.exception))

    def test_required_packaging_args_cover_three_dlls_and_licenses(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_required_native_inputs(root)
            with patch("scripts.build_nuitka.ROOT", root):
                args = build_nuitka_args("msvc", require_native=True)
            joined = "\n".join(args)
            for relative in (
                "tools/SpineSkeletonDataConverter/lpk_spine_converter.dll",
                "tools/spine_native/3.8.75/spine_bridge.dll",
                "tools/spine_native/4.0/spine_bridge.dll",
                "tools/SpineSkeletonDataConverter/LICENSE",
                "tools/spine_native/3.8.75/LICENSE",
                "tools/spine_native/4.0/LICENSE",
            ):
                self.assertIn(relative, joined)
            self.assertIn("THIRD_PARTY_NOTICES.md", joined)

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
