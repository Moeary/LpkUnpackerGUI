import os
import re
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from app.paths import BUNDLE_ROOT, PROJECT_ROOT


ASSETSTUDIO_EXE_NAME = "AssetStudioModCLI.exe"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tga"}
LIVE2D_EXTENSIONS = {".model3.json", ".moc3", ".motion3.json", ".physics3.json", ".png"}
# AssetStudio has no built-in Spine mode.  Unity Spine packages are stored as
# TextAsset skeleton/atlas files plus Texture2D/Sprite pages, so extraction
# asks for those two verified asset kinds and lets the Spine resolver validate
# their contents afterwards.
SPINE_EXPORT_EXTENSIONS = {
    ".json",
    ".skel",
    ".bytes",
    ".atlas",
    ".txt",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".bmp",
}
UNITYFS_SIGNATURE = b"UnityFS\x00"


class AssetStudioCLIError(RuntimeError):
    pass


def validate_unityfs_bundle(input_path: str | Path) -> str | None:
    """Reject a truncated UnityFS file before AssetStudio can export garbage.

    AssetStudioModCLI reports a short bundle as a warning and exits with code
    zero.  Its Live2D exporter may then write a model/MOC from the bytes it was
    able to read, which is indistinguishable from a successful export to the
    caller.  The UnityFS file-size field is part of the bundle header and is a
    reliable preflight check; no source bytes are changed.

    Non-UnityFS inputs are left to AssetStudio (for example serialized
    ``.assets`` files).  A directory is also left to AssetStudio because it can
    contain several files and companion resource streams.  A trailing-byte
    condition is returned as a warning because wrappers may append transport
    data; it is not treated as a truncated bundle.
    """

    path = Path(input_path)
    if not path.is_file():
        return None

    try:
        with path.open("rb") as stream:
            header = stream.read(512)
            stream.seek(0, os.SEEK_END)
            actual_size = stream.tell()
    except OSError as exc:
        raise AssetStudioCLIError(f"Unable to inspect Unity source {path}: {exc}") from exc

    if not header.startswith(UNITYFS_SIGNATURE):
        return None

    try:
        file_size_offset = _unityfs_file_size_offset(header)
        declared_size = struct.unpack_from(">Q", header, file_size_offset)[0]
    except (ValueError, struct.error) as exc:
        raise AssetStudioCLIError(f"Invalid UnityFS header: {path}: {exc}") from exc

    # A file with extra trailing bytes can be a valid wrapper/transport
    # artifact.  It is safe to leave that case to AssetStudio; only a short
    # file proves that bytes required by the UnityFS header are missing.
    if declared_size <= actual_size:
        if declared_size < actual_size:
            return (
                "UnityFS source has "
                f"{actual_size - declared_size:,} trailing byte(s) beyond its "
                "declared bundle size; AssetStudio will inspect the wrapper."
            )
        return None

    state = "truncated"
    difference = declared_size - actual_size

    stream_names = sorted(
        {
            match.group(1).decode("utf-8", errors="replace")
            for match in re.finditer(rb"([A-Za-z0-9_.-]+\.resS)\x00", header)
        }
    )
    companion_hint = ""
    if stream_names:
        companion_hint = (
            " Bundle metadata references "
            + ", ".join(stream_names)
            + "; obtain the complete UnityFS bundle/resource stream."
        )
    raise AssetStudioCLIError(
        "UnityFS source is "
        f"{state}: header declares {declared_size:,} bytes, "
        f"but the file contains {actual_size:,} bytes "
        f"({difference:,} byte difference).{companion_hint}"
    )


def _unityfs_file_size_offset(header: bytes) -> int:
    """Return the big-endian file-size field offset in a UnityFS header."""

    if not header.startswith(UNITYFS_SIGNATURE):
        raise ValueError("not a UnityFS header")

    offset = len(UNITYFS_SIGNATURE)
    if len(header) < offset + 4:
        raise ValueError("missing format version")
    offset += 4  # format version
    for label in ("Unity version", "Unity revision"):
        end = header.find(b"\x00", offset)
        if end < 0:
            raise ValueError(f"missing {label}")
        offset = end + 1
    if len(header) < offset + 8:
        raise ValueError("missing file size")
    return offset


@dataclass
class AssetStudioResult:
    command: list[str]
    output_dir: Path
    exported_files: list[Path]
    stdout: str
    stderr: str
    returncode: int

    @property
    def exported_count(self) -> int:
        return len(self.exported_files)


class AssetStudioCLI:
    def __init__(self, executable: Optional[str | Path] = None):
        self.executable = Path(executable).resolve() if executable else self.find_executable()

    @staticmethod
    def find_executable() -> Path:
        env_path = os.environ.get("LPK_ASSETSTUDIO_CLI")
        candidates = []
        try:
            from app.core.settings_manager import SettingsManager

            configured_path = SettingsManager().get_assetstudio_cli_path()
            if configured_path:
                candidates.append(Path(configured_path))
        except Exception:
            pass
        if env_path:
            candidates.append(Path(env_path))

        # A copy placed beside the EXE wins over the one bundled in the build.
        for root in dict.fromkeys((PROJECT_ROOT, BUNDLE_ROOT)):
            candidates.extend(
                [
                    root / "tools" / "AssetStudioCLI" / ASSETSTUDIO_EXE_NAME,
                    root / "app" / "tools" / "AssetStudioCLI" / ASSETSTUDIO_EXE_NAME,
                ]
            )

        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()

        searched = "\n".join(str(path) for path in candidates)
        raise AssetStudioCLIError(
            f"AssetStudio CLI not found. Set LPK_ASSETSTUDIO_CLI or place it at:\n{searched}"
        )

    def export_textures(
        self,
        input_path: str | Path,
        output_dir: str | Path,
        include_sprites: bool = True,
        filter_text: str = "",
        max_export_tasks: Optional[int] = None,
    ) -> AssetStudioResult:
        asset_types = "tex2d,sprite" if include_sprites else "tex2d"
        args = [
            str(self.executable),
            str(Path(input_path)),
            "-o",
            str(Path(output_dir)),
            "-t",
            asset_types,
            "--image-format",
            "png",
            "-g",
            "container",
            "-r",
            "--decompress-to-disk",
        ]
        if filter_text:
            args.extend(["--filter-by-text", filter_text])
        if max_export_tasks:
            args.extend(["--max-export-tasks", str(max_export_tasks)])

        return self._run(args, output_dir, IMAGE_EXTENSIONS)

    def export_live2d(
        self,
        input_path: str | Path,
        output_dir: str | Path,
        search_by_filename: bool = True,
        filter_name: str = "",
    ) -> AssetStudioResult:
        args = [
            str(self.executable),
            str(Path(input_path)),
            "-m",
            "live2d",
            "-o",
            str(Path(output_dir)),
            "-r",
            "--decompress-to-disk",
        ]
        if search_by_filename:
            args.append("--l2d-search-by-filename")
        if filter_name:
            args.extend(["--filter-by-name", filter_name])

        return self._run(args, output_dir, LIVE2D_EXTENSIONS)

    def export_spine(
        self,
        input_path: str | Path,
        output_dir: str | Path,
        filter_text: str = "",
    ) -> AssetStudioResult:
        """Export Unity TextAsset/texture inputs for a Spine resolver.

        AssetStudioModCLI does not parse Spine itself.  This deliberately
        requests only the documented ``textAsset``, ``tex2d`` and ``sprite``
        types; :mod:`app.core.extract.unity` recognizes a Spine skeleton or
        atlas from the resulting bytes before reporting success.  Arbitrary
        Unity assets are never presented as Spine merely because this export
        command exited successfully.
        """

        args = [
            str(self.executable),
            str(Path(input_path)),
            "-m",
            "export",
            "-o",
            str(Path(output_dir)),
            "-t",
            "textAsset,tex2d,sprite",
            "-g",
            "container",
            "-r",
            "--image-format",
            "png",
            "--decompress-to-disk",
        ]
        if filter_text:
            args.extend(["--filter-by-text", filter_text])
        return self._run(args, output_dir, SPINE_EXPORT_EXTENSIONS)

    def info(self, input_path: str | Path) -> AssetStudioResult:
        args = [str(self.executable), str(Path(input_path)), "-m", "info", "--load-all"]
        return self._run(args, self.executable.parent, set())

    def _run(
        self,
        args: list[str],
        output_dir: str | Path,
        extensions: Iterable[str],
    ) -> AssetStudioResult:
        output_path = Path(output_dir).resolve()
        output_path.mkdir(parents=True, exist_ok=True)
        before = _snapshot_files(output_path)

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        completed = subprocess.run(
            args,
            cwd=str(self.executable.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )

        after = _snapshot_files(output_path)
        exported_files = sorted(
            path for path, stamp in after.items() if before.get(path) != stamp
        )
        ext_set = {ext.lower() for ext in extensions}
        if ext_set:
            exported_files = [
                path
                for path in exported_files
                if path.suffix.lower() in ext_set or path.name.lower().endswith(tuple(ext_set))
            ]

        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip()
            raise AssetStudioCLIError(message or f"AssetStudio CLI exited with {completed.returncode}")

        return AssetStudioResult(
            command=args,
            output_dir=output_path,
            exported_files=exported_files,
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )


def _snapshot_files(root: Path) -> dict[Path, tuple[int, int]]:
    if not root.exists():
        return {}
    result = {}
    for path in root.rglob("*"):
        if path.is_file():
            stat = path.stat()
            result[path.resolve()] = (stat.st_size, stat.st_mtime_ns)
    return result
