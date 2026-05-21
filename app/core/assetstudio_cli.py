import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from app.paths import PROJECT_ROOT


ASSETSTUDIO_EXE_NAME = "AssetStudioModCLI.exe"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tga"}
LIVE2D_EXTENSIONS = {".model3.json", ".moc3", ".motion3.json", ".physics3.json", ".png"}


class AssetStudioCLIError(RuntimeError):
    pass


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
        if env_path:
            candidates.append(Path(env_path))

        candidates.extend(
            [
                PROJECT_ROOT / "tools" / "AssetStudioCLI" / ASSETSTUDIO_EXE_NAME,
                PROJECT_ROOT / "app" / "tools" / "AssetStudioCLI" / ASSETSTUDIO_EXE_NAME,
            ]
        )

        if getattr(__import__("sys"), "frozen", False):
            import sys

            exe_root = Path(sys.executable).resolve().parent
            candidates.insert(0, exe_root / "tools" / "AssetStudioCLI" / ASSETSTUDIO_EXE_NAME)

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
        exported_files = sorted(after - before)
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


def _snapshot_files(root: Path) -> set[Path]:
    if not root.exists():
        return set()
    return {path.resolve() for path in root.rglob("*") if path.is_file()}
