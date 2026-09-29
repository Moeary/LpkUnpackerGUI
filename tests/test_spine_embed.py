import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core import spine_embed


class SpineEmbedTests(unittest.TestCase):
    def test_cmake_failure_includes_readable_bounded_output(self):
        command = [
            sys.executable,
            "-c",
            "print('compiler diagnostic', flush=True); raise SystemExit(7)",
        ]
        with self.assertRaisesRegex(RuntimeError, r"(?s)CMake build failed.*compiler diagnostic"):
            spine_embed._run_cmake(command, phase="build")

    def test_cmake_cancel_terminates_process_and_uses_hidden_window(self):
        command = [
            sys.executable,
            "-c",
            "import time; print('started', flush=True); time.sleep(30)",
        ]
        calls = 0

        def cancel() -> bool:
            nonlocal calls
            calls += 1
            return calls >= 2

        original_popen = subprocess.Popen
        popen_kwargs: dict[str, object] = {}

        def start(*args, **kwargs):
            popen_kwargs.update(kwargs)
            return original_popen(*args, **kwargs)

        with patch.object(spine_embed.subprocess, "Popen", side_effect=start):
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                spine_embed._run_cmake(command, phase="build", cancel=cancel)

        if sys.platform.startswith("win"):
            self.assertEqual(
                popen_kwargs.get("creationflags"),
                getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

    def test_build_cleans_its_verified_temp_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            output = root / "output"
            build = root / "build"
            build.mkdir()
            commands: list[list[str]] = []

            def fake_cmake(command, *, phase, progress=None, cancel=None):
                commands.append(command)
                if phase == "build":
                    (build / "spine_bridge.dll").write_bytes(b"bridge")

            with patch.object(spine_embed, "_find_spine_root", return_value=source), patch.object(
                spine_embed,
                "_safe_build_directory",
                return_value=(root, build),
            ), patch.object(spine_embed, "_run_cmake", side_effect=fake_cmake):
                installed = spine_embed.build_native_runtime(
                    source,
                    output,
                    {"runtimeFamily": "3.8"},
                )

            self.assertEqual(installed, output / "spine_bridge.dll")
            self.assertTrue(installed.is_file())
            self.assertFalse(build.exists())
            self.assertEqual(len(commands), 2)


if __name__ == "__main__":
    unittest.main()
