from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.core.toolchain_manifest import SPINE_NATIVE_MANIFEST
from scripts.build_native import _find_verified_archive


class NativeBuildCacheTests(unittest.TestCase):
    def test_reuses_verified_archive_beside_existing_install(self) -> None:
        payload = b"verified pinned source archive"
        digest = hashlib.sha256(payload).hexdigest()
        manifest = replace(
            SPINE_NATIVE_MANIFEST,
            artifact_filename="spine-runtimes-test.zip",
            artifact_sha256=digest,
            artifact_size=len(payload),
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "3.8.75"
            output.mkdir()
            archive = output / manifest.artifact_filename
            archive.write_bytes(payload)
            with patch("scripts.build_native.download_pinned_archive") as download:
                found = _find_verified_archive(manifest, output, Path(temporary) / "cache")
            self.assertEqual(found, archive.resolve())
            download.assert_not_called()

    def test_rejects_wrong_archive_beside_existing_install(self) -> None:
        payload = b"wrong archive"
        manifest = replace(
            SPINE_NATIVE_MANIFEST,
            artifact_filename="spine-runtimes-test.zip",
            artifact_sha256="0" * 64,
            artifact_size=len(payload) + 1,
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "3.8.75"
            output.mkdir()
            (output / manifest.artifact_filename).write_bytes(payload)
            with self.assertRaises(RuntimeError) as context:
                _find_verified_archive(manifest, output, Path(temporary) / "cache")
            self.assertIn("wrong size or SHA-256", str(context.exception))


if __name__ == "__main__":
    unittest.main()
