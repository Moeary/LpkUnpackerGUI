"""Read-only hit testing in Cubism's canvas pixel coordinates.

The SDK MVP is distinct from the final preview contain/zoom transform. Keeping
both explicit also makes selections independent of the window's DPI.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import numpy as np
from PIL import Image, ImageDraw


def barycentric(point, triangle):
    if len(triangle) != 3:
        return None
    (ax, ay), (bx, by), (cx, cy) = triangle
    determinant = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    if not math.isfinite(determinant) or abs(determinant) < 1e-10:
        return None
    x, y = point
    a = ((by - cy) * (x - cx) + (cx - bx) * (y - cy)) / determinant
    b = ((cy - ay) * (x - cx) + (ax - cx) * (y - cy)) / determinant
    c = 1 - a - b
    return (a, b, c) if min(a, b, c) >= -1e-7 else None


def triangle_indices(drawable):
    vertices = drawable.get("vertices", [])
    indices = drawable.get("indices", [])
    for offset in range(0, len(indices) - 2, 3):
        triplet = indices[offset:offset + 3]
        if all(isinstance(i, int) and 0 <= i < len(vertices) for i in triplet):
            triangle = [vertices[i] for i in triplet]
            if barycentric(triangle[0], triangle) is not None:
                yield triplet


def intersect_convex(subject, clip):
    """Clip a triangle against a convex selection polygon, either winding."""
    polygon = [tuple(p) for p in subject]
    area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(clip, clip[1:] + clip[:1]))
    sign = 1 if area >= 0 else -1
    for a, b in zip(clip, clip[1:] + clip[:1]):
        if not polygon:
            break
        def distance(p):
            return sign * ((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]))
        result = []
        previous = polygon[-1]
        dp = distance(previous)
        for current in polygon:
            dc = distance(current)
            if (dp >= -1e-7) != (dc >= -1e-7):
                fraction = dp / (dp - dc)
                result.append((previous[0] + fraction * (current[0] - previous[0]),
                               previous[1] + fraction * (current[1] - previous[1])))
            if dc >= -1e-7:
                result.append(current)
            previous, dp = current, dc
        polygon = result
    return polygon


@dataclass(frozen=True)
class PreviewTransform:
    width: float
    height: float
    fbo_width: float
    fbo_height: float
    scale: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0  # already converted to the shader's +y-up convention
    rotation: float = 0.0

    def _contain(self):
        viewport = max(1, self.width) / max(1, self.height)
        canvas = max(1, self.fbo_width) / max(1, self.fbo_height)
        return (viewport / canvas, 1.0) if viewport >= canvas else (1.0, canvas / viewport)

    def window_to_clip(self, point):
        sx, sy = self._contain()
        x = ((point[0] / max(1, self.width) - .5) * sx - self.offset_x) / self.scale
        y = ((.5 - point[1] / max(1, self.height)) * sy - self.offset_y) / self.scale
        cosine, sine = math.cos(math.radians(self.rotation)), math.sin(math.radians(self.rotation))
        return (2 * (cosine * x + sine * y), 2 * (-sine * x + cosine * y))

    def clip_to_window(self, point):
        x, y = point[0] / 2, point[1] / 2
        cosine, sine = math.cos(math.radians(self.rotation)), math.sin(math.radians(self.rotation))
        x, y = cosine * x - sine * y, sine * x + cosine * y
        sx, sy = self._contain()
        return (((x * self.scale + self.offset_x) / sx + .5) * self.width,
                (.5 - (y * self.scale + self.offset_y) / sy) * self.height)


class ModelCoordinates:
    """Compose the preview shader with the native column-major SDK MVP."""
    def __init__(self, preview: PreviewTransform, mvp, canvas: Mapping):
        self.preview, self.canvas = preview, canvas
        self.matrix = np.asarray(mvp, dtype=float).reshape((4, 4), order="F")
        if not np.isfinite(self.matrix).all():
            raise ValueError("Invalid native MVP")
        self.inverse = np.linalg.inv(self.matrix)
        self.ppu = float(canvas.get("pixels_per_unit", 0))
        if not math.isfinite(self.ppu) or self.ppu <= 0:
            raise ValueError("Cubism canvas pixels_per_unit is required for selection")

    def window_to_canvas(self, point):
        x, y = self.preview.window_to_clip(point)
        vector = self.inverse @ np.array([x, y, 0., 1.])
        vector /= vector[3]
        return (float(self.canvas["origin_x"] + vector[0] * self.ppu),
                float(self.canvas["origin_y"] - vector[1] * self.ppu))

    def canvas_to_window(self, point):
        core = [(point[0] - self.canvas["origin_x"]) / self.ppu,
                (self.canvas["origin_y"] - point[1]) / self.ppu, 0., 1.]
        clip = self.matrix @ np.array(core)
        return self.preview.clip_to_window((clip[0] / clip[3], clip[1] / clip[3]))

    def window_rect(self, first, last):
        x0, x1 = sorted((first[0], last[0]))
        y0, y1 = sorted((first[1], last[1]))
        polygon = [self.window_to_canvas(p) for p in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        xs, ys = zip(*polygon)
        return {"coordinate_space": "canvas-pixels-y-down", "x": min(xs), "y": min(ys),
                "width": max(xs) - min(xs), "height": max(ys) - min(ys),
                "polygon": [list(p) for p in polygon]}


class SelectionScene:
    """A full current-pose snapshot with texture-aware overlap candidates."""
    def __init__(self, snapshot: Mapping, texture_paths=(), *, alpha_cache=None):
        self.snapshot = snapshot
        self.drawables = snapshot.get("drawables", [])
        self.parts = {str(p["id"]): float(p.get("opacity", 1)) for p in snapshot.get("parts", [])}
        self.texture_paths = dict(enumerate(texture_paths)) if not isinstance(texture_paths, Mapping) else texture_paths
        self._alpha_cache = alpha_cache if alpha_cache is not None else {}
        self._alpha_arrays = {}

    def _alpha(self, texture_index):
        path = self.texture_paths.get(texture_index)
        if not path:
            return None
        path = Path(path)
        try:
            stat = path.stat()
            key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
            if key not in self._alpha_cache:
                # Replacements use the same filename; the file fingerprint is
                # part of the key so a skin change never keeps old transparency.
                for old in list(self._alpha_cache):
                    if old[0] == key[0]:
                        del self._alpha_cache[old]
                with Image.open(path) as image:
                    self._alpha_cache[key] = image.convert("RGBA").getchannel("A")
            return self._alpha_cache[key]
        except (OSError, ValueError):
            return None

    def _visible(self, drawable):
        return (drawable.get("visible", True) and float(drawable.get("opacity", 1)) > 0
                and self.parts.get(str(drawable.get("parent_part_id")), 1) > 0)

    def _point_alpha(self, drawable, point):
        alpha = self._alpha(int(drawable.get("texture_index", 0)))
        vertices, uvs = drawable.get("vertices", []), drawable.get("uvs", [])
        for indices in triangle_indices(drawable):
            weights = barycentric(point, [vertices[i] for i in indices])
            if weights is None:
                continue
            if alpha is None or max(indices) >= len(uvs):
                return 1.0
            u = sum(w * uvs[i][0] for w, i in zip(weights, indices))
            v = sum(w * uvs[i][1] for w, i in zip(weights, indices))
            x = max(0, min(alpha.width - 1, int(u * alpha.width)))
            y = max(0, min(alpha.height - 1, int((1 - v) * alpha.height)))
            value = alpha.getpixel((x, y)) / 255
            if value > 0:
                return value
        return 0.0

    def _mask_alpha(self, drawable, point):
        masks = drawable.get("masks", [])
        if not masks:
            return 1.0
        alpha = max((self._point_alpha(self.drawables[i], point) for i in masks
                     if isinstance(i, int) and 0 <= i < len(self.drawables)), default=0)
        return 1 - alpha if drawable.get("inverted_mask") else alpha

    def _mask_alpha_many(self, drawable, points):
        """Evaluate clipping at visible own-texel positions in bounded batches."""
        result = np.zeros(len(points), dtype=float)
        for source_index in drawable.get("masks", []):
            if not isinstance(source_index, int) or not 0 <= source_index < len(self.drawables):
                continue
            source = self.drawables[source_index]
            alpha = self._alpha(int(source.get("texture_index", 0)))
            if alpha is not None and id(alpha) not in self._alpha_arrays:
                self._alpha_arrays[id(alpha)] = np.asarray(alpha)
            vertices, uvs = source.get("vertices", []), source.get("uvs", [])
            for indices in triangle_indices(source):
                triangle = [vertices[i] for i in indices]
                (ax, ay), (bx, by), (cx, cy) = triangle
                min_x, max_x = min(ax, bx, cx), max(ax, bx, cx)
                min_y, max_y = min(ay, by, cy), max(ay, by, cy)
                candidate = np.flatnonzero((points[:, 0] >= min_x) & (points[:, 0] <= max_x)
                                           & (points[:, 1] >= min_y) & (points[:, 1] <= max_y))
                if not len(candidate):
                    continue
                x, y = points[candidate].T
                determinant = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
                a = ((by - cy) * (x - cx) + (cx - bx) * (y - cy)) / determinant
                b = ((cy - ay) * (x - cx) + (ax - cx) * (y - cy)) / determinant
                c = 1 - a - b
                inside = (a >= -1e-7) & (b >= -1e-7) & (c >= -1e-7)
                candidate = candidate[inside]
                if not len(candidate):
                    continue
                if alpha is None or max(indices) >= len(uvs):
                    result[candidate] = 1
                else:
                    weights = np.column_stack((a[inside], b[inside], c[inside]))
                    uv = weights @ np.asarray([uvs[i] for i in indices])
                    tex_x = np.clip((uv[:, 0] * alpha.width).astype(int), 0, alpha.width - 1)
                    tex_y = np.clip(((1 - uv[:, 1]) * alpha.height).astype(int), 0, alpha.height - 1)
                    value = self._alpha_arrays[id(alpha)][tex_y, tex_x] / 255
                    result[candidate] = np.maximum(result[candidate], value)
        return 1 - result if drawable.get("inverted_mask") else result

    def _untextured_region_visible(self, drawable, triangle, polygon):
        if not drawable.get("masks"):
            return True
        # With no texture available, inspect actual canvas pixels rather than
        # treating an entire mask's bounding box as opaque. This fallback is
        # used by sidecars/tests; real models use the own-texture texels below.
        x0, x1 = math.floor(min(p[0] for p in polygon)), math.ceil(max(p[0] for p in polygon))
        y0, y1 = math.floor(min(p[1] for p in polygon)), math.ceil(max(p[1] for p in polygon))
        for y in range(y0, max(y0 + 1, y1)):
            points = np.column_stack((np.arange(x0, max(x0 + 1, x1)) + .5, np.full(max(1, x1 - x0), y + .5)))
            area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(polygon, polygon[1:] + polygon[:1]))
            sign = 1 if area >= 0 else -1
            points = np.asarray([p for p in points if barycentric(p, triangle) is not None
                                 and all(sign * ((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])) >= -1e-7
                                         for a, b in zip(polygon, polygon[1:] + polygon[:1]))])
            if len(points) and np.any(self._mask_alpha_many(drawable, points) > .01):
                return True
        return False

    @staticmethod
    def _order(item):
        index, drawable = item
        return (drawable.get("render_order", drawable.get("draw_order", 0)),
                drawable.get("draw_order", 0), index)

    def hit_point(self, point):
        return [str(d.get("id") or d.get("drawable_id"))
                for _, d in sorted(enumerate(self.drawables), key=self._order, reverse=True)
                if self._visible(d) and self._point_alpha(d, point) * self._mask_alpha(d, point) > 0.01]

    def _region_alpha(self, drawable, triangle, indices, polygon):
        alpha = self._alpha(int(drawable.get("texture_index", 0)))
        uvs = drawable.get("uvs", [])
        if alpha is None or max(indices) >= len(uvs):
            return self._untextured_region_visible(drawable, triangle, polygon)
        uv_polygon = []
        for point in polygon:
            weights = barycentric(point, triangle)
            if weights is None:
                continue
            uv_polygon.append((sum(w * uvs[i][0] for w, i in zip(weights, indices)) * alpha.width,
                               (1 - sum(w * uvs[i][1] for w, i in zip(weights, indices))) * alpha.height))
        if len(uv_polygon) < 3:
            return False
        x0 = max(0, math.floor(min(p[0] for p in uv_polygon)))
        y0 = max(0, math.floor(min(p[1] for p in uv_polygon)))
        x1 = min(alpha.width, math.ceil(max(p[0] for p in uv_polygon)) + 1)
        y1 = min(alpha.height, math.ceil(max(p[1] for p in uv_polygon)) + 1)
        if x1 <= x0 or y1 <= y0:
            return False
        mask = Image.new("L", (x1 - x0, y1 - y0))
        ImageDraw.Draw(mask).polygon([(x - x0, y - y0) for x, y in uv_polygon], fill=255)
        own_visible = (np.asarray(alpha.crop((x0, y0, x1, y1))) > 2) & (np.asarray(mask) > 0)
        if not drawable.get("masks"):
            return bool(np.any(own_visible))
        rows, columns = np.nonzero(own_visible)
        if not len(rows):
            return False
        uv_triangle = np.asarray([uvs[i] for i in indices])
        (ax, ay), (bx, by), (cx, cy) = uv_triangle
        determinant = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(determinant) < 1e-10:
            return self._untextured_region_visible(drawable, triangle, polygon)
        for start in range(0, len(rows), 4096):
            u = (columns[start:start + 4096] + x0 + .5) / alpha.width
            v = 1 - (rows[start:start + 4096] + y0 + .5) / alpha.height
            a = ((by - cy) * (u - cx) + (cx - bx) * (v - cy)) / determinant
            b = ((cy - ay) * (u - cx) + (ax - cx) * (v - cy)) / determinant
            points = np.column_stack((a, b, 1 - a - b)) @ np.asarray(triangle)
            if np.any(self._mask_alpha_many(drawable, points) > .01):
                return True
        return False

    def hit_region(self, region):
        polygon = region.get("polygon")
        if not polygon:
            x, y, w, h = (float(region[k]) for k in ("x", "y", "width", "height"))
            polygon = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
        result = []
        for _, drawable in sorted(enumerate(self.drawables), key=self._order, reverse=True):
            if not self._visible(drawable):
                continue
            for indices in triangle_indices(drawable):
                triangle = [drawable["vertices"][i] for i in indices]
                overlap = intersect_convex(triangle, polygon)
                if len(overlap) >= 3 and self._region_alpha(drawable, triangle, indices, overlap):
                    # Keep whole ArtMeshes, including partially intersecting
                    # ones. Masks remain in the full snapshot for PSD export.
                    result.append(str(drawable.get("id") or drawable.get("drawable_id")))
                    break
        return result
