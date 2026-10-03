"""Editor-only drawable appearance, independent of Cubism Part opacity."""
from __future__ import annotations

import math
from collections.abc import Mapping

from app.core.animation_editing import AnimationEditingError


def opacity_multiplier(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AnimationEditingError("ArtMesh preview opacity must be between 0 and 1.") from exc
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise AnimationEditingError("ArtMesh preview opacity must be between 0 and 1.")
    return number


def effective_drawable_opacities(visibility: Mapping, opacity: Mapping) -> dict[str, float]:
    result = {str(key): opacity_multiplier(value) for key, value in opacity.items()}
    result.update({str(key): 0.0 for key, visible in visibility.items() if not visible})
    return {key: value for key, value in result.items() if value != 1.0}


def apply_drawable_opacities(snapshot: dict, overrides: Mapping) -> dict:
    """Apply colour-pass multipliers to an already detached mesh snapshot.

    Intrinsic pose opacity remains available to mask consumers. A hidden mask
    drawable must still clip other meshes exactly as before preview hiding.
    """
    values = {str(key): opacity_multiplier(value) for key, value in overrides.items()}
    for drawable in snapshot.get("drawables", []):
        intrinsic = float(drawable.get("source_pose_opacity", drawable.get("opacity", 1.0)))
        multiplier = values.get(str(drawable.get("id", "")), 1.0)
        drawable["source_pose_opacity"] = intrinsic
        drawable["preview_opacity_multiplier"] = multiplier
        drawable["opacity"] = intrinsic * multiplier
    return snapshot
