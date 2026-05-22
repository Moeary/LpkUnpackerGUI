from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Live2DPackage:
    root_dir: Path
    model_json: Path
    version: int | None = None
    moc_path: Path | None = None
    texture_paths: list[Path] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.model_json.stem
