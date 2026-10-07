from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from app.core.live2d_preview_native import validate_preview_runtime
from scripts.build_nuitka import ROOT, build_nuitka_args
from tests import test_build_nuitka


class Live2DPreviewRuntimeTests(unittest.TestCase):
    def test_manifest_rejects_wrong_version_hash_and_escaping_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            test_build_nuitka.BuildNuitkaTests._write_required_native_inputs(root)
            runtime = root / "runtime/tools/live2d_native/opacity-v1"
            metadata_path = runtime / "live2d_native.json"
            original = json.loads(metadata_path.read_text(encoding="utf-8"))
            validate_preview_runtime(runtime)
            for key, value in (("upstream_version", "0.8.0"), ("drawable_opacity_api_version", 2),
                               ("python_abi", "cp39"), ("architecture", "win32")):
                metadata_path.write_text(json.dumps(dict(original, **{key: value})), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "version/API/ABI"):
                    validate_preview_runtime(runtime)
            metadata_path.write_text(json.dumps(original), encoding="utf-8")
            (runtime / "_v3cpp.pyd").write_bytes(b"changed")
            with self.assertRaisesRegex(RuntimeError, "SHA256 mismatch"):
                validate_preview_runtime(runtime)
            (runtime / "_v3cpp.pyd").write_bytes(b"extension")
            original["files"]["../escape.dll"] = "0" * 64
            metadata_path.write_text(json.dumps(original), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "escaping"):
                validate_preview_runtime(runtime)

    def test_release_build_refuses_an_incompatible_python_wrapper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            test_build_nuitka.BuildNuitkaTests._write_required_native_inputs(root)
            with patch("scripts.build_nuitka.ROOT", root), \
                 patch("scripts.build_nuitka.importlib.metadata.version", return_value="0.8.0"):
                with self.assertRaisesRegex(RuntimeError, "pinned live2d-py 0.7.0"):
                    build_nuitka_args("mingw", require_native=True)

    def test_nuitka_dll_configuration_resolves_extension_and_core_to_expected_dist_paths(self):
        from nuitka.plugins.standard.DllFilesPlugin import NuitkaPluginDllFiles
        from nuitka.utils.ModuleNames import ModuleName
        from nuitka.utils.Yaml import parseYaml
        from nuitka.Tracing import general
        config = parseYaml(logger=general, data=(ROOT / "scripts/live2d-native.nuitka-package.config.yml").read_bytes(),
                           error_message="Invalid Live2D native package config")
        self.assertEqual(config[0]["module-name"], "app.core.live2d_preview_native")
        with tempfile.TemporaryDirectory() as temporary:
            source_root = Path(temporary) / "source"
            test_build_nuitka.BuildNuitkaTests._write_required_native_inputs(source_root)
            class Probe:
                evaluateCondition = staticmethod(lambda **_kwargs: True)
                evaluateExpressionOrConstant = staticmethod(lambda *, expression, **_kwargs: expression)
                locateModule = staticmethod(lambda _name: str(source_root / "app/core/live2d_preview_native.py"))
                makeDllEntryPoint = staticmethod(lambda **kwargs: kwargs)
                _yieldDllsFromDirectory = NuitkaPluginDllFiles._yieldDllsFromDirectory
            entry = config[0]["dlls"][0]
            result = list(NuitkaPluginDllFiles._handleDllConfigFromFilenames(
                Probe(), entry["from_filenames"], ModuleName(config[0]["module-name"]), entry["dest_path"]))
            self.assertEqual({Path(item["source_path"]).name for item in result}, {"_v3cpp.pyd", "LpkPreviewCubismCore.dll"})
            self.assertEqual({Path(item["dest_path"]).as_posix() for item in result}, {
                "tools/live2d_native/opacity-v1/_v3cpp.pyd", "tools/live2d_native/opacity-v1/LpkPreviewCubismCore.dll"})
            target = Path(temporary) / "dist/tools/live2d_native/opacity-v1"
            shutil.copytree(source_root / "runtime/tools/live2d_native/opacity-v1", target)
            validate_preview_runtime(target)

    def test_fresh_process_fallback_reports_missing_capability_without_replacing_installed_wrapper(self):
        code = ("import json; from app.core.live2d_preview_native import load_preview_runtime,preview_native_status; "
                "runtime=load_preview_runtime(); print('STATUS='+json.dumps(preview_native_status())); "
                "assert not hasattr(runtime.Model, 'SetDrawableOpacityOverrides')")
        env = dict(os.environ, LPK_DISABLE_DRAWABLE_OPACITY="1")
        completed = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True,
                                   timeout=30, check=True, encoding="utf-8")
        line = next(line for line in completed.stdout.splitlines() if line.startswith("STATUS="))
        result = json.loads(line.removeprefix("STATUS="))
        self.assertFalse(result["supported"])
        self.assertIn("explicitly disabled", result["reason"])


if __name__ == "__main__":
    unittest.main()
