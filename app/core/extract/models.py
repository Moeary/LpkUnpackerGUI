from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class ExtractSourceType(str, Enum):
    LPK = "LPK"
    WPK = "WPK"
    UNITY = "UNITY"
    FOLDER = "FOLDER"
    UNKNOWN = "UNKNOWN"


class ExtractMode(str, Enum):
    FULL = "full"
    TEXTURES = "textures"
    LIVE2D = "live2d"


@dataclass(frozen=True)
class ExtractInput:
    path: Path
    source_type: ExtractSourceType


@dataclass
class ExtractItemResult:
    source: Path
    source_type: ExtractSourceType
    success: bool
    output_dir: Path | None = None
    exported_count: int = 0
    skipped_count: int = 0
    message: str = ""
    error: str | None = None
    children: list["ExtractItemResult"] = field(default_factory=list)


@dataclass
class ExtractBatchResult:
    output_dir: Path
    items: list[ExtractItemResult] = field(default_factory=list)

    @property
    def total_count(self) -> int:
        return len(self.items)

    @property
    def success_count(self) -> int:
        return sum(1 for item in self.items if item.success)

    @property
    def failure_count(self) -> int:
        return sum(1 for item in self.items if not item.success)

    @property
    def exported_count(self) -> int:
        return sum(item.exported_count for item in self.items)

    @property
    def skipped_count(self) -> int:
        return sum(item.skipped_count for item in self.items)

    @property
    def failed_items(self) -> list[ExtractItemResult]:
        failed: list[ExtractItemResult] = []
        for item in self.items:
            if not item.success:
                failed.append(item)
            failed.extend(child for child in item.children if not child.success)
        return failed

    @property
    def has_failures(self) -> bool:
        return self.failure_count > 0
