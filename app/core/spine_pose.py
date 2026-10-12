"""Bone-mode geometry for the native Spine preview.

The native bridge reports each bone's animated local pose (before
constraints) and final world matrix.  This module turns preview gestures
into new local values the same way the Spine editor's rotate and translate
tools do, and picks the bone under a point.  It is pure Python so it can be
tested without a compiled bridge.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

BONE_IK = 1
BONE_IK_TARGET = 2
BONE_CONSTRAINED = 4
BONE_ACTIVE = 8
BONE_OVERRIDDEN = 16


@dataclass(frozen=True)
class SpineBone:
    index: int
    name: str
    parent: int
    flags: int
    length: float
    x: float
    y: float
    rotation: float
    scale_x: float
    scale_y: float
    shear_x: float
    shear_y: float
    a: float
    b: float
    c: float
    d: float
    world_x: float
    world_y: float

    @property
    def origin(self) -> tuple[float, float]:
        return self.world_x, self.world_y

    @property
    def tip(self) -> tuple[float, float]:
        return self.world_x + self.a * self.length, self.world_y + self.c * self.length

    @property
    def world_rotation(self) -> float:
        return math.degrees(math.atan2(self.c, self.a))

    @property
    def is_ik(self) -> bool:
        return bool(self.flags & BONE_IK)

    @property
    def is_ik_target(self) -> bool:
        return bool(self.flags & BONE_IK_TARGET)

    @property
    def is_constrained(self) -> bool:
        return bool(self.flags & BONE_CONSTRAINED)

    @property
    def active(self) -> bool:
        return bool(self.flags & BONE_ACTIVE)

    def world_to_local(self, x: float, y: float) -> tuple[float, float]:
        """Spine's ``Bone::worldToLocal``: a world point in this bone's space."""
        determinant = self.a * self.d - self.b * self.c
        if abs(determinant) < 1e-12:
            return x - self.world_x, y - self.world_y
        dx, dy = x - self.world_x, y - self.world_y
        return (dx * self.d - dy * self.b) / determinant, (dy * self.a - dx * self.c) / determinant


@dataclass(frozen=True)
class SpineIk:
    name: str
    target: int
    bones: tuple[int, ...]
    mix: float
    bend_direction: int


def _angle(origin: tuple[float, float], point: tuple[float, float]) -> float:
    return math.degrees(math.atan2(point[1] - origin[1], point[0] - origin[0]))


def _wrap(degrees: float) -> float:
    return (degrees + 180.0) % 360.0 - 180.0


def parent_of(bone: SpineBone, bones: Sequence[SpineBone]) -> SpineBone | None:
    return bones[bone.parent] if 0 <= bone.parent < len(bones) else None


def rotate_bone(bone: SpineBone, parent: SpineBone | None, press: tuple[float, float],
                current: tuple[float, float]) -> float:
    """New local rotation after dragging from ``press`` to ``current``.

    Like the editor's rotate tool the bone turns by the angle the cursor
    swept around the bone's origin, so grabbing anywhere does not snap it.
    A mirrored parent (negative determinant) turns local rotation the other
    way round in world space.
    """
    delta = _wrap(_angle(bone.origin, current) - _angle(bone.origin, press))
    if parent is not None and parent.a * parent.d - parent.b * parent.c < 0:
        delta = -delta
    return bone.rotation + delta


def translate_bone(bone: SpineBone, parent: SpineBone | None, press: tuple[float, float],
                   current: tuple[float, float]) -> tuple[float, float]:
    """New local position after dragging from ``press`` to ``current``."""
    if parent is None:
        return bone.x + current[0] - press[0], bone.y + current[1] - press[1]
    start = parent.world_to_local(*press)
    end = parent.world_to_local(*current)
    return bone.x + end[0] - start[0], bone.y + end[1] - start[1]


def _segment_distance(point: tuple[float, float], start: tuple[float, float], end: tuple[float, float]) -> float:
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = dx * dx + dy * dy
    t = 0.0 if length <= 1e-12 else max(0.0, min(1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length))
    return math.hypot(point[0] - (start[0] + t * dx), point[1] - (start[1] + t * dy))


def pick_bone(bones: Iterable[SpineBone], point: tuple[float, float],
              to_view: Callable[[float, float], tuple[float, float]], tolerance: float = 8.0) -> SpineBone | None:
    """The bone drawn under a view-space ``point`` (pixels), or None.

    Distances are measured on screen so the tolerance does not change with
    zoom.  Joints and IK targets are small handles and win over the bone
    bodies they overlap; among bodies the nearest wins, deeper bones first
    on ties because they are drawn on top.
    """
    best: tuple[tuple[int, float, int], SpineBone] | None = None
    for bone in bones:
        if not bone.active:
            continue
        origin = to_view(*bone.origin)
        joint = math.hypot(point[0] - origin[0], point[1] - origin[1])
        handle = tolerance * (1.6 if bone.is_ik_target else 1.0)
        if joint <= handle:
            key = (0, joint, -bone.index)
        elif bone.length > 0:
            distance = _segment_distance(point, origin, to_view(*bone.tip))
            if distance > tolerance:
                continue
            key = (1, distance, -bone.index)
        else:
            continue
        if best is None or key < best[0]:
            best = key, bone
    return best[1] if best else None


def animation_offsets(setup: dict, values: dict[str, float]) -> dict[str, float]:
    """Convert local pose values into Spine key offsets from the setup pose."""
    result = {}
    if "rotation" in values:
        result["angle"] = float(values["rotation"]) - float(setup.get("rotation", 0.0))
    if "x" in values:
        result["x"] = float(values["x"]) - float(setup.get("x", 0.0))
    if "y" in values:
        result["y"] = float(values["y"]) - float(setup.get("y", 0.0))
    return result


__all__ = [
    "BONE_ACTIVE", "BONE_CONSTRAINED", "BONE_IK", "BONE_IK_TARGET", "BONE_OVERRIDDEN",
    "SpineBone", "SpineIk", "animation_offsets", "parent_of", "pick_bone", "rotate_bone", "translate_bone",
]
