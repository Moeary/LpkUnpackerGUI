"""Static ArtMesh/atlas inspection for PSD exports.

The inspector deliberately consumes the exported ``.lpkpsd.json`` rather than
trying to run the Live2D renderer.  It is therefore useful when a PSD was
created from a pose snapshot, while still making the atlas UV footprint easy
to audit before editing or repacking.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidgetItem,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    ComboBox as QComboBox, LineEdit as QLineEdit, ListWidget as QListWidget,
    PushButton as QPushButton, TextEdit as QTextEdit,
)

from app.i18n import get_i18n, tr


def _as_points(value: Any) -> list[tuple[float, float]]:
    if not isinstance(value, (list, tuple)):
        return []
    if value and all(isinstance(item, (int, float)) for item in value):
        if len(value) % 2:
            return []
        return [
            (float(value[index]), float(value[index + 1]))
            for index in range(0, len(value), 2)
        ]
    points: list[tuple[float, float]] = []
    for item in value:
        if isinstance(item, Mapping):
            try:
                points.append((float(item.get("x", 0)), float(item.get("y", 0))))
            except (TypeError, ValueError):
                return []
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            try:
                points.append((float(item[0]), float(item[1])))
            except (TypeError, ValueError):
                return []
        else:
            return []
    return points


def _as_indices(value: Any) -> list[int]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[int] = []
    for item in value:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


def _finite_points(points: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    return [
        (float(x), float(y))
        for x, y in points
        if math.isfinite(float(x)) and math.isfinite(float(y))
    ]


def _point_in_triangle(
    point: tuple[float, float],
    triangle: list[tuple[float, float]],
) -> bool:
    if len(triangle) != 3:
        return False
    px, py = point
    (ax, ay), (bx, by), (cx, cy) = triangle
    abx, aby = bx - ax, by - ay
    bcx, bcy = cx - bx, cy - by
    cax, cay = ax - cx, ay - cy
    apx, apy = px - ax, py - ay
    bpx, bpy = px - bx, py - by
    cpx, cpy = px - cx, py - cy
    cross_a = abx * apy - aby * apx
    cross_b = bcx * bpy - bcy * bpx
    cross_c = cax * cpy - cay * cpx
    epsilon = 1e-7
    return (
        (cross_a >= -epsilon and cross_b >= -epsilon and cross_c >= -epsilon)
        or (cross_a <= epsilon and cross_b <= epsilon and cross_c <= epsilon)
    )


def _uv_to_pixels(
    uvs: list[tuple[float, float]],
    size: tuple[int, int],
) -> list[tuple[float, float]]:
    width, height = size
    if not uvs:
        return []
    normalized = max(abs(value) for point in uvs for value in point) <= 2.0
    if normalized:
        return [(u * width, (1.0 - v) * height) for u, v in uvs]
    return list(uvs)


def _triangles(
    points: list[tuple[float, float]],
    indices: list[int],
) -> list[list[tuple[float, float]]]:
    result: list[list[tuple[float, float]]] = []
    for offset in range(0, len(indices) - 2, 3):
        triangle_indices = indices[offset : offset + 3]
        if max(triangle_indices, default=-1) >= len(points):
            continue
        triangle = [points[index] for index in triangle_indices]
        if len(_finite_points(triangle)) == 3:
            result.append(triangle)
    return result


@dataclass
class ArtMeshEntry:
    """A drawable entry normalized from PSD metadata and optional sidecar."""

    name: str
    drawable_id: str
    texture_index: int
    vertices: list[tuple[float, float]] = field(default_factory=list)
    uvs: list[tuple[float, float]] = field(default_factory=list)
    indices: list[int] = field(default_factory=list)
    bbox: tuple[int, int, int, int] | None = None
    source_index: int = -1
    render_order: float = 0.0
    draw_order: float = 0.0
    visible: bool = True
    opacity: float = 1.0
    layer_id: int = 0
    unit_group_id: int = 0
    binding_path: str = ""
    coordinate_scale: float = 1.0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_pose_geometry(self) -> bool:
        return bool(self.vertices and self.indices)

    @property
    def has_atlas_geometry(self) -> bool:
        return bool(self.uvs and self.indices)

    def pose_triangles(self) -> list[list[tuple[float, float]]]:
        scale = self.coordinate_scale if math.isfinite(self.coordinate_scale) else 1.0
        return _triangles([(x * scale, y * scale) for x, y in self.vertices], self.indices)

    def atlas_triangles(self, size: tuple[int, int]) -> list[list[tuple[float, float]]]:
        return _triangles(_uv_to_pixels(self.uvs, size), self.indices)


class _MeshCanvas(QWidget):
    """Paint a static pose or atlas and convert clicks to source coordinates."""

    hitRequested = Signal(float, float)

    def __init__(self, mode: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.mode = mode
        self.entries: list[ArtMeshEntry] = []
        self.selected_index = -1
        self.shared_indices: set[int] = set()
        self.source_size = (1, 1)
        self.texture_index = 0
        self.pixmap = QPixmap()
        self.setMinimumSize(320, 240)
        self.setMouseTracking(True)

    def set_scene(
        self,
        entries: list[ArtMeshEntry],
        source_size: tuple[int, int],
        selected_index: int,
        *,
        texture_index: int = 0,
        pixmap: QPixmap | None = None,
        shared_indices: set[int] | None = None,
    ) -> None:
        self.entries = entries
        self.source_size = (
            max(1, int(source_size[0])),
            max(1, int(source_size[1])),
        )
        self.selected_index = int(selected_index)
        self.texture_index = int(texture_index)
        self.pixmap = pixmap or QPixmap()
        self.shared_indices = set(shared_indices or set())
        self.update()

    def _transform(self) -> tuple[float, float, float]:
        width, height = self.source_size
        available_width = max(1, self.width() - 24)
        available_height = max(1, self.height() - 24)
        scale = min(available_width / width, available_height / height)
        scale = max(0.01, scale)
        offset_x = (self.width() - width * scale) / 2.0
        offset_y = (self.height() - height * scale) / 2.0
        return scale, offset_x, offset_y

    def source_to_view(self, point: tuple[float, float]) -> QPointF:
        scale, offset_x, offset_y = self._transform()
        return QPointF(offset_x + point[0] * scale, offset_y + point[1] * scale)

    def view_to_source(self, point: QPointF) -> tuple[float, float]:
        scale, offset_x, offset_y = self._transform()
        return ((point.x() - offset_x) / scale, (point.y() - offset_y) / scale)

    def _entry_triangles(self, entry: ArtMeshEntry):
        if self.mode == "pose":
            return entry.pose_triangles()
        if entry.texture_index != self.texture_index:
            return []
        return entry.atlas_triangles(self.source_size)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#20252d"))
        scale, offset_x, offset_y = self._transform()
        width, height = self.source_size
        target = QRectF(offset_x, offset_y, width * scale, height * scale)
        if not self.pixmap.isNull():
            painter.drawPixmap(target, self.pixmap, QRectF(self.pixmap.rect()))
        elif self.mode == "atlas":
            painter.setPen(QPen(QColor("#697483"), 1))
            painter.drawText(target, Qt.AlignmentFlag.AlignCenter, tr("psd.inspector.texture_missing"))
        else:
            painter.setPen(QPen(QColor("#697483"), 1))
            painter.drawRect(target)
            painter.setPen(QPen(QColor("#9aa6b5"), 1))
            painter.drawText(target, Qt.AlignmentFlag.AlignCenter, tr("psd.inspector.static_hint"))

        for index, entry in enumerate(self.entries):
            triangles = self._entry_triangles(entry)
            if not triangles:
                continue
            selected = index == self.selected_index
            shared = index in self.shared_indices
            if selected:
                color = QColor(255, 218, 70, 210)
                width_px = 3
            elif shared:
                color = QColor(255, 125, 54, 190)
                width_px = 2
            else:
                hue = (index * 47) % 360
                color = QColor.fromHsv(hue, 180, 235, 170)
                width_px = 1
            pen = QPen(color, width_px)
            if shared and not selected:
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            for triangle in triangles:
                polygon = QPolygonF([self.source_to_view(point) for point in triangle])
                painter.drawPolygon(polygon)
        painter.end()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            x, y = self.view_to_source(event.position())
            self.hitRequested.emit(float(x), float(y))
        super().mousePressEvent(event)


class ArtMeshInspector(QWidget):
    """Static ArtMesh list, pose hit-testing and atlas UV inspection widget."""

    selectionChanged = Signal(object)
    metadataChanged = Signal(str)

    def __init__(
        self,
        metadata_path: str | Path | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.metadata_path: Path | None = None
        self.metadata: dict[str, Any] = {}
        self.entries: list[ArtMeshEntry] = []
        self.shared_regions: list[dict[str, Any]] = []
        self.texture_paths: dict[int, Path] = {}
        self.texture_sizes: dict[int, tuple[int, int]] = {}
        self.pose_size: tuple[int, int] = (1, 1)
        self.pose_pixmap = QPixmap()
        self.pose_preview_available = False
        self.sidecar_canvas_size: tuple[int, int] = (1, 1)
        self.selected_index = -1
        self._loading = False
        self.i18n = get_i18n()
        self._build_ui()
        self.retranslate_ui()
        self.i18n.languageChanged.connect(self.retranslate_ui)
        if metadata_path:
            self.load_metadata(metadata_path)

    def _build_ui(self) -> None:
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(12, 12, 12, 12)
        self.main_layout.setSpacing(8)

        header = QHBoxLayout()
        self.metadata_edit = QLineEdit(self)
        self.metadata_edit.setReadOnly(True)
        self.metadata_edit.setPlaceholderText(tr("psd.inspector.metadata_placeholder"))
        self.open_button = QPushButton(self)
        self.open_button.clicked.connect(self._choose_metadata)
        self.refresh_button = QPushButton(self)
        self.refresh_button.clicked.connect(self.refresh)
        header.addWidget(self.metadata_edit, 1)
        header.addWidget(self.open_button)
        header.addWidget(self.refresh_button)
        self.main_layout.addLayout(header)

        self.mode_label = QLabel(self)
        self.mode_label.setWordWrap(True)
        self.main_layout.addWidget(self.mode_label)

        self.texture_row = QHBoxLayout()
        self.texture_label = QLabel(self)
        self.texture_combo = QComboBox(self)
        self.texture_combo.currentIndexChanged.connect(self._texture_changed)
        self.texture_row.addWidget(self.texture_label)
        self.texture_row.addWidget(self.texture_combo, 1)
        self.main_layout.addLayout(self.texture_row)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.entry_list = QListWidget(self.splitter)
        self.entry_list.currentRowChanged.connect(self._entry_row_changed)
        right = QSplitter(Qt.Orientation.Vertical, self.splitter)
        pose_frame = QFrame(right)
        pose_layout = QVBoxLayout(pose_frame)
        pose_layout.setContentsMargins(0, 0, 0, 0)
        self.pose_title_label = QLabel(pose_frame)
        pose_layout.addWidget(self.pose_title_label)
        self.pose_canvas = _MeshCanvas("pose", pose_frame)
        self.pose_canvas.hitRequested.connect(self._pose_hit)
        pose_layout.addWidget(self.pose_canvas, 1)
        atlas_frame = QFrame(right)
        atlas_layout = QVBoxLayout(atlas_frame)
        atlas_layout.setContentsMargins(0, 0, 0, 0)
        self.atlas_title_label = QLabel(atlas_frame)
        atlas_layout.addWidget(self.atlas_title_label)
        self.atlas_canvas = _MeshCanvas("atlas", atlas_frame)
        self.atlas_canvas.hitRequested.connect(self._atlas_hit)
        atlas_layout.addWidget(self.atlas_canvas, 1)
        right.addWidget(pose_frame)
        right.addWidget(atlas_frame)
        self.splitter.addWidget(self.entry_list)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 4)
        self.main_layout.addWidget(self.splitter, 1)

        self.shared_label = QLabel(self)
        self.shared_label.setWordWrap(True)
        self.main_layout.addWidget(self.shared_label)
        self.details = QTextEdit(self)
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(120)
        self.main_layout.addWidget(self.details)
        self.status_label = QLabel(self)
        self.status_label.setWordWrap(True)
        self.main_layout.addWidget(self.status_label)

    def retranslate_ui(self, *_args) -> None:
        self.open_button.setText(tr("psd.inspector.open_metadata"))
        self.refresh_button.setText(tr("psd.inspector.refresh"))
        self.texture_label.setText(tr("psd.inspector.texture"))
        self.pose_title_label.setText(tr("psd.inspector.pose"))
        self.atlas_title_label.setText(tr("psd.inspector.atlas"))
        self.metadata_edit.setPlaceholderText(tr("psd.inspector.metadata_placeholder"))
        if self.metadata_path is None:
            self.status_label.setText(tr("psd.inspector.choose_first"))
        self._update_status()

    def _choose_metadata(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("psd.inspector.choose_metadata"),
            str(self.metadata_path.parent) if self.metadata_path else "",
            "Live2D PSD metadata (*.lpkpsd.json);;JSON (*.json)",
        )
        if path:
            self.load_metadata(path)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8-sig") as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError("metadata root must be an object")
        return value

    def load_metadata(self, metadata_path: str | Path) -> bool:
        path = Path(metadata_path).resolve()
        try:
            metadata = self._read_json(path)
            self._loading = True
            previous_id = self.entries[self.selected_index].drawable_id if 0 <= self.selected_index < len(self.entries) else ""
            self.metadata_path = path
            self.metadata = metadata
            self.metadata_edit.setText(str(path))
            self.shared_regions = [
                dict(item)
                for item in (metadata.get("shared_regions") or [])
                if isinstance(item, Mapping)
            ]
            self.sidecar_canvas_size = (1, 1)
            sidecar = self._load_drawables_sidecar(metadata, path)
            self.entries = self._build_entries(metadata, sidecar)
            self._load_textures(metadata, path)
            self._load_pose_preview(metadata, path)
            self._populate_texture_combo()
            self._populate_entry_list()
            selected = next(
                (index for index, entry in enumerate(self.entries) if entry.drawable_id == previous_id),
                0 if self.entries else -1,
            )
            self.selected_index = -1
            self._loading = False
            if selected >= 0:
                self.select_entry(selected)
            else:
                self._update_views()
            self._update_status()
            self.metadataChanged.emit(str(path))
            return True
        except Exception as exc:
            self._loading = False
            self.entries = []
            self.selected_index = -1
            self.entry_list.clear()
            self.details.clear()
            self.status_label.setText(f"Metadata unavailable: {exc}")
            self.metadataChanged.emit("")
            return False

    def refresh(self) -> bool:
        if self.metadata_path is None:
            self.status_label.setText(tr("psd.inspector.choose_first"))
            return False
        return self.load_metadata(self.metadata_path)

    def _load_drawables_sidecar(
        self,
        metadata: Mapping[str, Any],
        metadata_path: Path,
    ) -> dict[str, dict[str, Any]]:
        candidates: list[Path] = []
        source_model = str(metadata.get("source_model") or "")
        if source_model:
            model_path = Path(source_model)
            if not model_path.is_absolute():
                model_path = metadata_path.parent / model_path
            candidates.extend(
                [
                    model_path.with_name(f"{model_path.stem}.drawables.json"),
                    model_path.with_name("drawables.json"),
                ]
            )
        if source_model:
            model_name = Path(source_model).name
            candidates.extend(
                [
                    metadata_path.parent / f"{Path(model_name).stem}.drawables.json",
                    metadata_path.parent / "drawables.json",
                ]
            )
        candidates.extend(
            [
                metadata_path.with_name(f"{metadata_path.stem}.drawables.json"),
                metadata_path.parent / "drawables.json",
            ]
        )
        for candidate in candidates:
            try:
                candidate = candidate.resolve()
                if not candidate.is_file():
                    continue
                value = self._read_json(candidate)
                drawables = value.get("drawables") if isinstance(value, Mapping) else value
                if not isinstance(drawables, list):
                    continue
                canvas = value.get("canvas") if isinstance(value, Mapping) else None
                if isinstance(canvas, Mapping):
                    try:
                        self.sidecar_canvas_size = (
                            max(1, int(canvas.get("width", 1) or 1)),
                            max(1, int(canvas.get("height", 1) or 1)),
                        )
                    except (TypeError, ValueError):
                        self.sidecar_canvas_size = (1, 1)
                result: dict[str, dict[str, Any]] = {}
                for index, item in enumerate(drawables):
                    if not isinstance(item, Mapping):
                        continue
                    item_copy = dict(item)
                    item_copy.setdefault("source_index", index)
                    identifier = str(item_copy.get("id") or item_copy.get("drawable_id") or "")
                    if identifier:
                        result[identifier] = item_copy
                    result[f"@source:{index}"] = item_copy
                return result
            except (OSError, ValueError, TypeError):
                continue
        return {}

    def _build_entries(
        self,
        metadata: Mapping[str, Any],
        sidecar: Mapping[str, Mapping[str, Any]],
    ) -> list[ArtMeshEntry]:
        layers = metadata.get("layers")
        layer_items = layers if isinstance(layers, list) else []
        entries: list[ArtMeshEntry] = []
        for index, item in enumerate(layer_items):
            if not isinstance(item, Mapping):
                continue
            raw = dict(item)
            drawable_id = str(raw.get("drawable_id") or raw.get("id") or raw.get("name") or f"layer-{index}")
            side = sidecar.get(drawable_id) or sidecar.get(f"@source:{raw.get('source_index', -1)}") or {}
            merged = dict(side)
            merged.update({key: value for key, value in raw.items() if value not in (None, "")})
            name = str(merged.get("name") or merged.get("drawable_id") or drawable_id)
            vertices = _as_points(merged.get("vertices"))
            uvs = _as_points(merged.get("uvs"))
            indices = _as_indices(merged.get("indices"))
            bbox_value = merged.get("bbox")
            bbox = None
            if isinstance(bbox_value, (list, tuple)) and len(bbox_value) >= 4:
                try:
                    bbox = tuple(int(float(value)) for value in bbox_value[:4])
                except (TypeError, ValueError):
                    bbox = None
            try:
                texture_index = int(merged.get("texture_index", 0))
            except (TypeError, ValueError):
                texture_index = 0
            try:
                source_index = int(merged.get("source_index", index))
            except (TypeError, ValueError):
                source_index = index
            entries.append(
                ArtMeshEntry(
                    name=name,
                    drawable_id=drawable_id,
                    texture_index=texture_index,
                    vertices=vertices,
                    uvs=uvs,
                    indices=indices,
                    bbox=bbox,
                    source_index=source_index,
                    render_order=float(merged.get("render_order", 0) or 0),
                    draw_order=float(merged.get("draw_order", 0) or 0),
                    visible=bool(merged.get("visible", True)),
                    opacity=float(merged.get("opacity", 1.0) or 0),
                    layer_id=int(merged.get("layer_id", 0) or 0),
                    unit_group_id=int(merged.get("unit_group_id", 0) or 0),
                    binding_path=str(merged.get("binding_path") or ""),
                    coordinate_scale=float(merged.get("coordinate_scale", 1.0) or 1.0),
                    raw=merged,
                )
            )
        if entries:
            return entries
        # A minimal metadata file may only carry source information; expose
        # sidecar drawables in that case instead of silently showing nothing.
        seen_ids: set[str] = set()
        for index, item in enumerate(sidecar.values()):
            if str(item.get("id") or "") == "":
                continue
            raw = dict(item)
            identifier = str(raw.get("id"))
            if identifier in seen_ids:
                continue
            seen_ids.add(identifier)
            entries.append(
                ArtMeshEntry(
                    name=identifier,
                    drawable_id=identifier,
                    texture_index=int(raw.get("texture_index", 0) or 0),
                    vertices=_as_points(raw.get("vertices")),
                    uvs=_as_points(raw.get("uvs")),
                    indices=_as_indices(raw.get("indices")),
                    source_index=int(raw.get("source_index", index) or index),
                    render_order=float(raw.get("render_order", 0) or 0),
                    draw_order=float(raw.get("draw_order", 0) or 0),
                    visible=bool(raw.get("visible", True)),
                    opacity=float(raw.get("opacity", 1.0) or 0),
                    coordinate_scale=float(raw.get("coordinate_scale", 1.0) or 1.0),
                    raw=raw,
                )
            )
        return entries

    @staticmethod
    def _resolve_metadata_path(value: Any, metadata_path: Path) -> Path | None:
        if value in (None, ""):
            return None
        candidate = Path(str(value))
        if not candidate.is_absolute():
            candidate = metadata_path.parent / candidate
        return candidate.resolve()

    def _load_textures(self, metadata: Mapping[str, Any], metadata_path: Path) -> None:
        self.texture_paths = {}
        self.texture_sizes = {}
        source_root = self._resolve_metadata_path(metadata.get("source_root"), metadata_path)
        source_model = str(metadata.get("source_model") or "")
        model_path = self._resolve_metadata_path(source_model, metadata_path)
        model_root = model_path.parent if model_path is not None else None
        texture_items = metadata.get("textures")
        if not isinstance(texture_items, list):
            return
        for fallback_index, item in enumerate(texture_items):
            if not isinstance(item, Mapping):
                continue
            index = int(item.get("index", fallback_index) or fallback_index)
            relative = str(item.get("relative_path") or item.get("name") or "")
            candidates: list[Path] = []
            if source_root and relative:
                candidates.append(source_root / relative)
            if model_root and relative:
                candidates.append(model_root / relative)
            if relative:
                candidates.append(metadata_path.parent / relative)
                candidates.append(metadata_path.parent / Path(relative).name)
            for candidate in candidates:
                if candidate.is_file():
                    self.texture_paths[index] = candidate.resolve()
                    break
            try:
                self.texture_sizes[index] = (
                    int(item.get("width", 1) or 1),
                    int(item.get("height", 1) or 1),
                )
            except (TypeError, ValueError):
                self.texture_sizes[index] = (1, 1)
            if index in self.texture_paths:
                pixmap = QPixmap(str(self.texture_paths[index]))
                if not pixmap.isNull():
                    self.texture_sizes[index] = (pixmap.width(), pixmap.height())

    @staticmethod
    def _companion_psd_path(metadata_path: Path) -> Path:
        name = metadata_path.name
        if name.endswith(".lpkpsd.json"):
            return metadata_path.with_name(name[: -len(".lpkpsd.json")] + ".psd")
        return metadata_path.with_suffix(".psd")

    def _load_pose_preview(self, metadata: Mapping[str, Any], metadata_path: Path) -> None:
        self.pose_pixmap = QPixmap()
        self.pose_preview_available = False
        self.pose_size = self.sidecar_canvas_size
        mode = str(metadata.get("mode") or "")
        if mode == "mesh":
            try:
                from psd_tools import PSDImage

                psd_path = self._companion_psd_path(metadata_path)
                if psd_path.is_file():
                    # PSDImage.topil() reads Photoshop's merged preview and is
                    # fast enough for the UI thread.  composite(force=True)
                    # can rasterize every layer and block for many seconds on
                    # a real model, so the inspector intentionally does not
                    # use it as an implicit fallback.
                    image = PSDImage.open(str(psd_path)).topil()
                    if image is None:
                        raise ValueError("PSD has no merged preview")
                    image = image.convert("RGBA")
                    data = image.tobytes("raw", "RGBA")
                    qimage = QImage(
                        data,
                        image.width,
                        image.height,
                        image.width * 4,
                        QImage.Format.Format_RGBA8888,
                    ).copy()
                    self.pose_pixmap = QPixmap.fromImage(qimage)
                    self.pose_size = (image.width, image.height)
                    self.pose_preview_available = not self.pose_pixmap.isNull()
            except Exception:
                self.pose_pixmap = QPixmap()
        if self.pose_size == (1, 1):
            canvas = metadata.get("canvas")
            if isinstance(canvas, Mapping):
                try:
                    self.pose_size = (
                        max(1, int(canvas.get("width", 1) or 1)),
                        max(1, int(canvas.get("height", 1) or 1)),
                    )
                except (TypeError, ValueError):
                    self.pose_size = (1, 1)

    def _populate_texture_combo(self) -> None:
        current = int(self.texture_combo.currentData() or 0)
        self.texture_combo.blockSignals(True)
        self.texture_combo.clear()
        indices = sorted(self.texture_sizes)
        for index in indices:
            path = self.texture_paths.get(index)
            label = f"Texture {index}"
            if path:
                label += f" — {path.name}"
            self.texture_combo.addItem(label, userData=index)
        target = next(
            (position for position in range(self.texture_combo.count()) if int(self.texture_combo.itemData(position)) == current),
            0,
        ) if self.texture_combo.count() else -1
        if target >= 0:
            self.texture_combo.setCurrentIndex(target)
        self.texture_combo.blockSignals(False)

    def _populate_entry_list(self) -> None:
        self.entry_list.blockSignals(True)
        self.entry_list.clear()
        for index, entry in enumerate(self.entries):
            flags = []
            if not entry.has_pose_geometry:
                flags.append("atlas only")
            if not entry.visible or entry.opacity <= 0:
                flags.append("hidden/transparent")
            suffix = f" [{', '.join(flags)}]" if flags else ""
            item = QListWidgetItem(f"{entry.name}  ({entry.drawable_id}){suffix}")
            item.setData(Qt.ItemDataRole.UserRole, index)
            self.entry_list.addItem(item)
        self.entry_list.blockSignals(False)

    def _texture_changed(self, _index: int) -> None:
        self._update_views()

    def _entry_row_changed(self, row: int) -> None:
        if self._loading:
            return
        self.select_entry(row)

    def select_entry(self, index_or_name: int | str) -> ArtMeshEntry | None:
        index = -1
        if isinstance(index_or_name, int):
            index = index_or_name
        else:
            value = str(index_or_name)
            index = next(
                (position for position, entry in enumerate(self.entries) if entry.drawable_id == value or entry.name == value),
                -1,
            )
        if not (0 <= index < len(self.entries)):
            self.selected_index = -1
            self._update_views()
            return None
        self.selected_index = index
        entry = self.entries[index]
        self._loading = True
        self.entry_list.setCurrentRow(index)
        texture_position = next(
            (position for position in range(self.texture_combo.count()) if int(self.texture_combo.itemData(position)) == entry.texture_index),
            -1,
        )
        if texture_position >= 0:
            self.texture_combo.setCurrentIndex(texture_position)
        self._loading = False
        self._update_views()
        self.selectionChanged.emit(entry)
        return entry

    def current_entry(self) -> ArtMeshEntry | None:
        if 0 <= self.selected_index < len(self.entries):
            return self.entries[self.selected_index]
        return None

    def _shared_entry_indices(self, texture_index: int | None = None) -> set[int]:
        result: set[int] = set()
        for index, entry in enumerate(self.entries):
            if texture_index is not None and entry.texture_index != texture_index:
                continue
            for region in self.shared_regions:
                drawables = {str(value) for value in (region.get("drawables") or [])}
                if entry.drawable_id in drawables or entry.name in drawables:
                    result.add(index)
                    break
                layer_text = str(region.get("layer") or "")
                if entry.drawable_id and entry.drawable_id in layer_text:
                    result.add(index)
                    break
        return result

    def hit_pose(self, x: float, y: float) -> int | None:
        point = (float(x), float(y))
        candidates: list[tuple[tuple[float, float, int, int], int]] = []
        for index, entry in enumerate(self.entries):
            if not entry.has_pose_geometry:
                continue
            if any(_point_in_triangle(point, triangle) for triangle in entry.pose_triangles()):
                order = (entry.render_order, entry.draw_order, entry.source_index, index)
                candidates.append((order, index))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    def hit_atlas(self, texture_index: int, x: float, y: float) -> int | None:
        point = (float(x), float(y))
        candidates: list[tuple[tuple[float, float, int, int], int]] = []
        size = self.texture_sizes.get(int(texture_index), (1, 1))
        for index, entry in enumerate(self.entries):
            if entry.texture_index != int(texture_index) or not entry.has_atlas_geometry:
                continue
            if any(_point_in_triangle(point, triangle) for triangle in entry.atlas_triangles(size)):
                order = (entry.render_order, entry.draw_order, entry.source_index, index)
                candidates.append((order, index))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    def _pose_hit(self, x: float, y: float) -> None:
        index = self.hit_pose(x, y)
        if index is not None:
            self.select_entry(index)

    def _atlas_hit(self, x: float, y: float) -> None:
        texture_index = int(self.texture_combo.currentData() or 0)
        index = self.hit_atlas(texture_index, x, y)
        if index is not None:
            self.select_entry(index)

    def _update_views(self) -> None:
        pose_size = self.pose_size
        if pose_size == (1, 1):
            points = [point for entry in self.entries for point in entry.vertices]
            if points:
                right = max(point[0] for point in points)
                bottom = max(point[1] for point in points)
                pose_size = (max(1, int(math.ceil(right))), max(1, int(math.ceil(bottom))))
        texture_index = int(self.texture_combo.currentData() or 0)
        texture_size = self.texture_sizes.get(texture_index, (1, 1))
        pixmap = QPixmap(str(self.texture_paths[texture_index])) if texture_index in self.texture_paths else QPixmap()
        self.pose_canvas.set_scene(
            self.entries,
            pose_size,
            self.selected_index,
            pixmap=self.pose_pixmap,
            shared_indices=self._shared_entry_indices(),
        )
        self.atlas_canvas.set_scene(
            self.entries,
            texture_size,
            self.selected_index,
            texture_index=texture_index,
            pixmap=pixmap,
            shared_indices=self._shared_entry_indices(texture_index),
        )
        self._update_status()

    def _update_status(self) -> None:
        pose_count = sum(entry.has_pose_geometry for entry in self.entries)
        atlas_count = sum(entry.has_atlas_geometry for entry in self.entries)
        if self.entries and pose_count == 0:
            self.mode_label.setText(tr("psd.inspector.atlas_only"))
        elif self.entries:
            message = tr("psd.inspector.static_hint")
            if not self.pose_preview_available:
                message += " " + tr("psd.inspector.pose_preview_missing")
            self.mode_label.setText(message)
        else:
            self.mode_label.setText(tr("psd.inspector.no_entries"))
        self.shared_label.setText(
            tr(
                "psd.inspector.shared_summary",
                count=len(self.shared_regions),
                geometry=atlas_count,
            )
        )
        entry = self.current_entry()
        if entry is None:
            self.details.clear()
            if self.metadata_path is not None and not self.entries:
                self.status_label.setText(tr("psd.inspector.no_entries"))
            return
        affected = sum(
            1
            for region in self.shared_regions
            if entry.drawable_id in {str(value) for value in (region.get("drawables") or [])}
        )
        self.details.setPlainText(
            "\n".join(
                [
                    f"ArtMesh: {entry.name}",
                    tr("psd.inspector.drawable", value=entry.drawable_id),
                    tr("psd.inspector.texture_index", value=entry.texture_index),
                    tr("psd.inspector.layer_id", value=entry.layer_id or "unavailable"),
                    tr("psd.inspector.unit_id", value=entry.unit_group_id or "unavailable"),
                    tr("psd.inspector.binding", value=entry.binding_path or "unavailable"),
                    tr("psd.inspector.impact", count=affected),
                ]
            )
        )
        self.status_label.setText(tr("psd.inspector.selected", name=entry.name))


class ArtMeshInspectorDialog(QDialog):
    """Convenient standalone dialog used by the PSD workbench button."""

    def __init__(
        self,
        metadata_path: str | Path | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.i18n = get_i18n()
        self.setWindowTitle(tr("psd.inspector.title"))
        self.setMinimumSize(980, 720)
        layout = QVBoxLayout(self)
        self.inspector = ArtMeshInspector(metadata_path, self)
        layout.addWidget(self.inspector, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.close_button = QPushButton(tr("psd.inspector.close"), self)
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self.i18n.languageChanged.connect(self.retranslate_ui)

    def retranslate_ui(self, *_args) -> None:
        self.setWindowTitle(tr("psd.inspector.title"))
        self.close_button.setText(tr("psd.inspector.close"))


__all__ = ["ArtMeshEntry", "ArtMeshInspector", "ArtMeshInspectorDialog"]
