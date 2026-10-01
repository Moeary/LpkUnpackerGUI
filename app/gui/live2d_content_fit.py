"""One-shot visible-content fitting in the native FBO's normalized coordinates.

The FBO alpha is read before the preview background is composited. Its bounds
therefore describe rendered pixels rather than hidden geometry or HitAreas.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


def alpha_content_bounds(rgba) -> tuple[float, float, float, float] | None:
    """Return exclusive normalized edges, with rows in OpenGL's bottom-up order."""
    pixels = np.asarray(rgba)
    if pixels.ndim != 3 or pixels.shape[2] != 4 or min(pixels.shape[:2]) < 1:
        raise ValueError("A nonempty RGBA framebuffer is required")
    alpha = pixels[:, :, 3]
    columns = np.flatnonzero(np.any(alpha > 0, axis=0))
    if columns.size == 0:
        return None
    rows = np.flatnonzero(np.any(alpha > 0, axis=1))
    height, width = alpha.shape
    return (float(columns[0]) / width, float(rows[0]) / height,
            float(columns[-1] + 1) / width, float(rows[-1] + 1) / height)


@dataclass(frozen=True)
class ContentFitTransform:
    scale: float
    offset_x: float
    offset_y: float  # shader convention: +y up
    range_limited: bool


def content_fit_transform(bounds, viewport, source_aspect: float, fill: float = .96) -> ContentFitTransform:
    """Center the entire sampled silhouette inside the existing preview ranges.

    ``fill`` is the occupied fraction of the limiting viewport dimension, not
    padding on each edge. Scale and pan use the same limits as the display UI.
    """
    left, bottom, right, top = map(float, bounds)
    width, height = map(float, viewport)
    source_aspect, fill = float(source_aspect), float(fill)
    if (not all(math.isfinite(value) for value in (left, bottom, right, top, width, height, source_aspect, fill))
            or width <= 0 or height <= 0 or source_aspect <= 0 or not 0 < fill <= 1
            or not 0 <= left < right <= 1 or not 0 <= bottom < top <= 1):
        raise ValueError("Finite positive viewport/aspect and normalized content bounds are required")
    viewport_aspect = width / height
    sx, sy = ((viewport_aspect / source_aspect, 1.) if viewport_aspect >= source_aspect
              else (1., source_aspect / viewport_aspect))
    desired_scale = min(fill * sx / (right - left), fill * sy / (top - bottom))
    center_x, center_y = (left + right) / 2 - .5, (bottom + top) / 2 - .5
    # Limit the scale before computing pan so an off-center silhouette still
    # stays centered and complete; independently clipping the pan would crop it.
    pan_limits = [1 / abs(center) for center in (center_x, center_y) if abs(center) > 1e-12]
    scale = min(desired_scale, 4., *pan_limits)
    return ContentFitTransform(scale, -center_x * scale, -center_y * scale,
                               scale < desired_scale - 1e-9)
