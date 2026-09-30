"""Identify Cubism / ViewerEX asset fields without interpreting commands.

ViewerEX extends motion entries with Command, PostCommand, Code, Text,
Language and NextMtn. These are opaque runtime metadata, even when they
contain punctuation or resemble a relative path. File/Sound and the model
resource fields retain their normal file meaning at any nested level.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any


_ASSET_FIELDS = frozenset({
    "Moc", "Textures", "Physics", "PhysicsV2", "Pose", "DisplayInfo", "UserData",
    "File", "Sound",
})


def iter_live2d_asset_references(value: Any, key: str = "") -> Iterator[str]:
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from iter_live2d_asset_references(child, child_key)
    elif isinstance(value, list):
        for child in value:
            yield from iter_live2d_asset_references(child, key)
    elif isinstance(value, str) and key in _ASSET_FIELDS:
        yield value
