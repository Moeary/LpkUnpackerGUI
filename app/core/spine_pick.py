"""Find the atlas region drawn under a point of a rendered Spine frame.

The native bridge returns plain triangles grouped by atlas page, without slot
or attachment names.  Every vertex still carries its atlas UV, so the topmost
triangle under a skeleton-space point tells which atlas pixel is visible
there, and the atlas region containing that pixel names the part.  Nothing
here touches OpenGL, which keeps the picking rules testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence

import numpy as np

# Below this alpha (0-255) a texel counts as empty, so a click on the
# transparent margin of a part falls through to the part underneath.
ALPHA_THRESHOLD = 8
_EPSILON = 1e-6


@dataclass(frozen=True)
class PickRegion:
    region_id: str
    name: str
    page: int
    x: int
    y: int
    width: int
    height: int

    def contains(self, px: int, py: int) -> bool:
        return self.x <= px < self.x + self.width and self.y <= py < self.y + self.height


@dataclass(frozen=True)
class PickResult:
    region_id: str
    name: str
    page: int
    pixel: tuple[int, int]


def regions_by_page(regions: Iterable[PickRegion]) -> dict[int, list[PickRegion]]:
    result: dict[int, list[PickRegion]] = {}
    for region in regions:
        result.setdefault(region.page, []).append(region)
    return result


def pick_region(
    vertices: np.ndarray,
    batches: Sequence[tuple[int, int, int]],
    point: tuple[float, float],
    page_sizes: Mapping[int, tuple[int, int]],
    regions: Mapping[int, Sequence[PickRegion]],
    alpha_at: Callable[[int, int, int], int] | None = None,
    alpha_threshold: int = ALPHA_THRESHOLD,
) -> PickResult | None:
    """Return the topmost visible atlas region at ``point`` (skeleton space).

    ``vertices`` is the bridge's ``(N, 8)`` float array (x, y, u, v, r, g, b,
    a) of contiguous triangles; ``batches`` lists ``(vertex_offset,
    vertex_count, page)`` in draw order.  Spine-cpp flips V for OpenGL, so a
    UV maps to page pixel ``(u * width, (1 - v) * height)``.  ``alpha_at``
    returns the texel alpha (0-255) of ``(page, x, y)``; when given, clicks on
    transparent texels pass through to what is drawn below.
    """

    x, y = float(point[0]), float(point[1])
    for vertex_offset, vertex_count, page in reversed(list(batches)):
        size = page_sizes.get(int(page))
        page_regions = regions.get(int(page))
        if not size or not page_regions:
            continue
        count = int(vertex_count) - int(vertex_count) % 3
        if count <= 0:
            continue
        triangles = vertices[int(vertex_offset):int(vertex_offset) + count].reshape(-1, 3, 8)
        for index in _covering_triangles(triangles, x, y):
            triangle = triangles[index]
            weights = _barycentric(triangle, x, y)
            if weights is None:
                continue
            u = float(np.dot(weights, triangle[:, 2]))
            v = float(np.dot(weights, triangle[:, 3]))
            width, height = size
            px = min(max(int(u * width), 0), width - 1)
            py = min(max(int((1.0 - v) * height), 0), height - 1)
            region = next((item for item in page_regions if item.contains(px, py)), None)
            if region is None:
                continue
            if alpha_at is not None and alpha_at(int(page), px, py) < alpha_threshold:
                continue
            return PickResult(region.region_id, region.name, int(page), (px, py))
    return None


def _covering_triangles(triangles: np.ndarray, x: float, y: float) -> list[int]:
    """Indices of visible triangles containing the point, topmost first."""

    a, b, c = triangles[:, 0, :2], triangles[:, 1, :2], triangles[:, 2, :2]
    cross = lambda p, q, r: (q[:, 0] - p[:, 0]) * (r[:, 1] - p[:, 1]) - (q[:, 1] - p[:, 1]) * (r[:, 0] - p[:, 0])
    point = np.array([[x, y]], dtype=np.float64)
    area = cross(a, b, c)
    d0, d1, d2 = cross(b, c, point), cross(c, a, point), cross(a, b, point)
    # Accept either winding; reject degenerate and fully transparent triangles.
    same_sign = ((d0 >= -_EPSILON) & (d1 >= -_EPSILON) & (d2 >= -_EPSILON)) | (
        (d0 <= _EPSILON) & (d1 <= _EPSILON) & (d2 <= _EPSILON))
    visible = triangles[:, :, 7].max(axis=1) > 0.004
    hits = np.flatnonzero(same_sign & (np.abs(area) > _EPSILON) & visible)
    return [int(index) for index in hits[::-1]]


def _barycentric(triangle: np.ndarray, x: float, y: float) -> np.ndarray | None:
    (x0, y0), (x1, y1), (x2, y2) = triangle[:, :2].astype(np.float64)
    denominator = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
    if abs(denominator) <= _EPSILON:
        return None
    w0 = ((y1 - y2) * (x - x2) + (x2 - x1) * (y - y2)) / denominator
    w1 = ((y2 - y0) * (x - x2) + (x0 - x2) * (y - y2)) / denominator
    return np.array([w0, w1, 1.0 - w0 - w1])


__all__ = ["ALPHA_THRESHOLD", "PickRegion", "PickResult", "pick_region", "regions_by_page"]
