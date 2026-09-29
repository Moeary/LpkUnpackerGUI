"""Regression tests for the optional native-tool installer."""

from __future__ import annotations

import hashlib
import io
import json
import types
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.core import toolchain_download as download
from app.core import toolchain
from app.core.settings_manager import SettingsManager
from app.core.toolchain_manifest import (
    TOOLCHAIN_MANIFEST,
    ASSETSTUDIO_MANIFEST,
    CUBISM_CORE_MANIFEST,
    SPINE_NATIVE_MANIFEST,
    register_tool_package_manifest,
)


class _Response:
    def __init__(self, payload: bytes):
        self._stream = io.BytesIO(payload)
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


def _zip_payload(*members: tuple[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members:
            archive.writestr(name, data)
    return buffer.getvalue()


class ToolchainManifestTests(unittest.TestCase):
    def test_built_in_manifests_are_pinned_and_self_consistent(self):
        for manifest in TOOLCHAIN_MANIFEST.values():
            if manifest.artifact_sha256:
                self.assertEqual(len(manifest.artifact_sha256), 64, manifest.package_id)
                int(manifest.artifact_sha256, 16)
            if manifest.entrypoint:
                self.assertIn(
                    manifest.entrypoint.casefold(),
                    {item.casefold() for item in manifest.expected_files},
                    manifest.package_id,
                )
        for manifest in TOOLCHAIN_MANIFEST.values():
            if manifest.source_commit:
                self.assertIn(manifest.source_commit, manifest.artifact_url)
                self.assertIn("/zip/" + manifest.source_commit, manifest.artifact_url)

    def test_spine_native_download_requires_the_builder(self):
        with self.assertRaises(download.ToolchainBuildRequiredError):
            download.download_tool_package(SPINE_NATIVE_MANIFEST.package_id)

    def test_cubism_core_requires_official_or_exact_local_source(self):
        with self.assertRaises(download.ToolchainDownloadError) as context:
            download.download_tool_package(CUBISM_CORE_MANIFEST.package_id)
        self.assertIn("live2d.com", str(context.exception))
        self.assertIn("proprietary", str(context.exception).lower())


class ToolchainInstallerTests(unittest.TestCase):
    def setUp(self):
        self._registered: list[str] = []

    def tearDown(self):
        for package_id in self._registered:
            TOOLCHAIN_MANIFEST.pop(package_id, None)

    def _manifest(self, package_id: str, payload: bytes, **kwargs):
        manifest = replace(
            ASSETSTUDIO_MANIFEST,
            package_id=package_id,
            install_dir=package_id,
            artifact_url=f"https://example.invalid/{package_id}.zip",
            artifact_sha256=hashlib.sha256(payload).hexdigest(),
            artifact_size=len(payload),
            artifact_filename=f"{package_id}.zip",
            **kwargs,
        )
        register_tool_package_manifest(manifest)
        self._registered.append(package_id)
        return manifest

    def test_download_extracts_and_reuses_verified_install(self):
        payload = _zip_payload(
            ("AssetStudioModCLI_net472_win32_64/AssetStudioModCLI.exe", b"MZ"),
        )
        manifest = self._manifest(
            "fixture_assetstudio",
            payload,
            expected_files=("AssetStudioModCLI_net472_win32_64/AssetStudioModCLI.exe",),
            entrypoint="AssetStudioModCLI_net472_win32_64/AssetStudioModCLI.exe",
        )
        progress = []
        with tempfile.TemporaryDirectory() as temporary, patch(
            "app.core.toolchain_download.urlopen", return_value=_Response(payload)
        ) as mocked:
            first = download.download_tool_package(
                manifest.package_id,
                temporary,
                progress=progress.append,
            )
            second = download.download_tool_package(manifest.package_id, temporary)
            self.assertEqual(first.install_dir, second.install_dir)
            self.assertTrue(first.entrypoint.is_file())
            self.assertTrue(first.manifest_path.is_file())
            self.assertTrue(any(item.phase == "installed" for item in progress))
            mocked.assert_called_once()

    def test_existing_directory_is_preserved_and_uses_sibling(self):
        payload = _zip_payload(
            ("AssetStudioModCLI_net472_win32_64/AssetStudioModCLI.exe", b"MZ"),
        )
        manifest = self._manifest(
            "fixture_collision",
            payload,
            expected_files=("AssetStudioModCLI_net472_win32_64/AssetStudioModCLI.exe",),
            entrypoint="AssetStudioModCLI_net472_win32_64/AssetStudioModCLI.exe",
        )
        with tempfile.TemporaryDirectory() as temporary, patch(
            "app.core.toolchain_download.urlopen", return_value=_Response(payload)
        ):
            occupied = Path(temporary) / manifest.install_dir
            occupied.mkdir()
            sentinel = occupied / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            result = download.download_tool_package(manifest.package_id, temporary)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertEqual(result.install_dir.name, f"{manifest.install_dir}-2")

    def test_checksum_failure_leaves_no_partial_file(self):
        payload = b"wrong archive"
        manifest = replace(
            self._manifest("fixture_bad_hash", payload),
            artifact_sha256="0" * 64,
        )
        TOOLCHAIN_MANIFEST[manifest.package_id] = manifest
        with tempfile.TemporaryDirectory() as temporary, patch(
            "app.core.toolchain_download.urlopen", return_value=_Response(payload)
        ):
            with self.assertRaises(download.ToolchainIntegrityError):
                download.download_tool_package(manifest.package_id, temporary)
            self.assertEqual(list(Path(temporary).rglob("*.part")), [])
            self.assertFalse((Path(temporary) / manifest.install_dir).exists())

    def test_archive_path_traversal_is_rejected(self):
        payload = _zip_payload(("../outside.txt", b"do not write"))
        manifest = self._manifest(
            "fixture_traversal",
            payload,
            expected_files=("expected.exe",),
            entrypoint="expected.exe",
        )
        with tempfile.TemporaryDirectory() as temporary:
            with patch(
                "app.core.toolchain_download.urlopen", return_value=_Response(payload)
            ):
                with self.assertRaises(download.ToolchainInstallError):
                    download.download_tool_package(manifest.package_id, temporary)
            self.assertFalse((Path(temporary).parent / "outside.txt").exists())

    def test_explicit_cubism_core_source_is_copied_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "Live2DCubismCore.dll"
            source.write_bytes(b"core-dll")
            root = Path(temporary) / "tools"
            first = download.extract_cubism_core_from_live2d_py(root, source=source)
            second = download.extract_cubism_core_from_live2d_py(root, source=source)
            self.assertEqual(first.install_dir, second.install_dir)
            self.assertEqual(first.entrypoint.read_bytes(), b"core-dll")
            self.assertEqual(source.read_bytes(), b"core-dll")

    def test_native_path_setting_is_persisted(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings_file = Path(temporary) / "settings.json"
            manager = SettingsManager(settings_file=str(settings_file))
            manager.set_spine_native_runtime_path(r"D:\Tools\spine_native")
            reloaded = SettingsManager(settings_file=str(settings_file))
            self.assertEqual(
                reloaded.get_spine_native_runtime_path(),
                r"D:\Tools\spine_native",
            )

    def test_native_runtime_root_discovery_prefers_common_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            bridge = project / "runtime" / "tools" / "spine_native" / "3.8.75"
            bridge.mkdir(parents=True)
            (bridge / "spine_bridge.dll").write_bytes(b"bridge")
            (bridge / "spine_native.json").write_text(
                json.dumps({"runtimeFamily": "3.8"}), encoding="utf-8"
            )
            with patch.object(toolchain, "PROJECT_ROOT", project):
                found = toolchain.find_spine_native_runtime_root()
            self.assertEqual(found, (project / "runtime" / "tools" / "spine_native").resolve())

    def test_spine_builder_adapter_passes_runtime_family_and_keeps_source(self):
        payload = _zip_payload(
            (
                "spine-runtimes-c0699e23/LICENSE",
                b"Spine Runtimes License Agreement",
            ),
            ("spine-runtimes-c0699e23/spine-cpp/spine-cpp/README.txt", b"official source"),
        )
        captured = {}

        def fake_download(manifest, destination, *, progress=None, cancel=None):
            archive = Path(destination) / manifest.artifact_filename
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(payload)
            return archive

        def fake_build_native_runtime(
            *, source_dir, output_dir, manifest, progress=None, cancel=None
        ):
            captured["manifest"] = dict(manifest)
            captured["source_dir"] = Path(source_dir)
            library = Path(output_dir) / "spine_bridge.dll"
            library.write_bytes(b"native bridge")
            if progress:
                progress("fake compiler complete")
            return library

        fake_module = types.SimpleNamespace(build_native_runtime=fake_build_native_runtime)
        with tempfile.TemporaryDirectory() as temporary, patch(
            "app.core.toolchain_download.download_pinned_archive", side_effect=fake_download
        ), patch(
            "app.core.toolchain_download.importlib.import_module", return_value=fake_module
        ):
            result = download.build_spine_native_runtime(install_root=temporary)

            self.assertTrue(result.entrypoint.is_file())
            self.assertEqual(captured["manifest"]["runtimeFamily"], "3.8")
            self.assertTrue(
                (
                    captured["source_dir"]
                    / "spine-runtimes-c0699e23"
                    / "spine-cpp"
                    / "spine-cpp"
                    / "README.txt"
                ).is_file()
            )
            self.assertTrue((result.install_dir / result.archive_path.name).is_file())
            self.assertEqual(
                (result.install_dir / "LICENSE").read_bytes(),
                b"Spine Runtimes License Agreement",
            )
            marker = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["source_commit"], SPINE_NATIVE_MANIFEST.source_commit)
            self.assertEqual(marker["runtimeFamily"], "3.8")
            self.assertEqual(marker["license_file"], "LICENSE")

            result_40 = download.build_spine_native_runtime(
                "spine_native_4_0", install_root=temporary
            )
            self.assertTrue(result_40.entrypoint.is_file())
            self.assertEqual(captured["manifest"]["runtimeFamily"], "4.0")


if __name__ == "__main__":
    unittest.main()
