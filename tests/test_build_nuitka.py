from __future__ import annotations

import subprocess
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.build_nuitka import ROOT, build_nuitka_args, remove_intermediate_build_dirs, run_msvc_build


class BuildNuitkaTests(unittest.TestCase):
    def test_packaging_keeps_mcp_http_dynamic_imports_and_docstrings(self):
        args = build_nuitka_args("msvc")
        self.assertIn("--include-package=mcp", args)
        self.assertIn("--include-package=uvicorn", args)
        self.assertNotIn("--python-flag=no_docstrings", args)

    def test_build_allows_required_nuitka_helper_downloads(self):
        args = build_nuitka_args("msvc")

        self.assertIn("--assume-yes-for-downloads", args)

    def test_release_is_onefile_and_keeps_dist_for_verification(self):
        args = build_nuitka_args("msvc")

        self.assertIn("--onefile", args)
        self.assertNotIn("--standalone", args)
        self.assertNotIn("--remove-output", args)

    def test_dynamically_imported_live2d_runtime_is_bundled(self):
        args = build_nuitka_args("msvc")

        self.assertIn("--include-package=live2d.v3", args)
        self.assertIn("--include-package-data=live2d", args)

    def test_assetstudio_exe_and_dlls_are_bundled_raw(self):
        # --include-data-dir silently skips .exe/.dll files.
        args = build_nuitka_args("msvc")

        self.assertIn("--include-raw-dir=./app/tools/AssetStudioCLI=tools/AssetStudioCLI", args)
        self.assertFalse(any(arg.startswith("--include-data-dir=./app/tools/AssetStudioCLI") for arg in args))

    def test_intermediate_cleanup_keeps_onefile_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            build = Path(temporary)
            for name in ("main.build", "main.onefile-build", "main.dist"):
                (build / name).mkdir()
                (build / name / "file").write_text("x", encoding="utf-8")

            remove_intermediate_build_dirs(build)

            self.assertEqual(sorted(path.name for path in build.iterdir()), ["main.dist"])

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
        live2d_root = root / "runtime/tools/live2d_native/opacity-v1"
        live2d_files = {
            "_v3cpp.pyd": b"extension", "LpkPreviewCubismCore.dll": b"core",
            "SOURCE_METADATA.json": b"{}", "THIRD_PARTY_NOTICES.md": b"notices",
            "LICENSE.live2d-py": b"mit", "LICENSE.CubismFramework.md": b"framework",
            "LICENSE.CubismCore.md": b"core", "FrameworkShaders/FragShaderSrc.frag": b"shader",
        }
        for name, data in live2d_files.items():
            target = live2d_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        metadata = {"format": "LpkUnpacker.Live2DPreviewRuntime", "version": 1, "drawable_opacity_api_version": 1,
                    "upstream_version": "0.7.0", "sdk_version": "5-r.4.1", "python_abi": "cp310-abi3",
                    "architecture": "win_amd64", "files": {name: hashlib.sha256(data).hexdigest()
                                                           for name, data in live2d_files.items()}}
        (live2d_root / "live2d_native.json").write_text(json.dumps(metadata), encoding="utf-8")

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
            self.assertIn("--include-distribution-metadata=live2d-py", args)
            self.assertIn("--include-module=app.core.psd_worker", args)
            self.assertIn("live2d-native.nuitka-package.config.yml", joined)
            self.assertIn("tools/live2d_native/opacity-v1/live2d_native.json", joined)
            self.assertNotIn("--include-data-file=" + str(root / "runtime/tools/live2d_native/opacity-v1/_v3cpp.pyd"), joined)

    def test_packaging_includes_bridge_source_under_app_native(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_required_native_inputs(root)
            bridge_source = root / "app" / "native" / "spine_bridge"
            bridge_source.mkdir(parents=True)
            (bridge_source / "CMakeLists.txt").write_text("project(test)", encoding="utf-8")

            with patch("scripts.build_nuitka.ROOT", root):
                args = build_nuitka_args("msvc", require_native=True)

            bridge_arg = f"--include-data-dir={bridge_source}=app/native/spine_bridge"
            self.assertIn(bridge_arg, args)
            self.assertNotIn("=native/spine_bridge", "\n".join(args))

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
