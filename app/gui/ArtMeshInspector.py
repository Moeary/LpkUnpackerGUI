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

from PySide6.QtCore import QEvent, QItemSelectionModel, QPointF, QRectF, QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap, QPolygonF, QTransform
from PySide6.QtWidgets import (
    QDialog,
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QListWidgetItem,
    QSplitter,
    QVBoxLayout,
    QWidget,
    QSizePolicy,
)
from qfluentwidgets import (
    ComboBox as QComboBox, LineEdit as QLineEdit, ListWidget as QListWidget,
    PushButton as QPushButton, TextEdit as QTextEdit,
    ScrollArea, LineEdit, BodyLabel,
)

from app.i18n import get_i18n, tr
from app.gui.editor_workspace import FluentEditorTabs, EditorViewportLayout
from app.gui.editor_dialogs import ThemedEditorDialog
from app.gui.live2d_selection import SelectionScene, barycentric, combine_selection, intersect_convex


ARTMESH_TEXT = {
    "artmesh.inspector.overview": "总览",
    "artmesh.inspector.search": "搜索 ArtMesh…",
    "artmesh.inspector.overlap": "重叠部件",
    "artmesh.inspector.overlap_hint": "请选择此位置的部件；列表按画面前后顺序排列。",
    "artmesh.inspector.selected_count": "已选 {count} 个 ArtMesh",
    "artmesh.inspector.pick_trigger": "选择换装触发部件",
    "artmesh.inspector.no_trigger": "请先预览此工程的主模型，再选择未绑定事件的部件。",
    "artmesh.inspector.zoom_hint": "滚轮缩放，中键拖动，双击复位；总览中使用 Ctrl+滚轮缩放。",
    "artmesh.inspector.selection_hint": "单击选择；Shift+单击增减；拖动框选；Shift+框选合并。",
}


def _text(key, **values):
    return tr(key, default=ARTMESH_TEXT[key], **values)


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
    return barycentric(point, triangle) is not None


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
        if min(triangle_indices, default=-1) < 0 or max(triangle_indices, default=-1) >= len(points):
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
    selectionRequested = Signal(float, float, str)
    regionRequested = Signal(dict, str)

    def __init__(self, mode: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.mode = mode
        self.entries: list[ArtMeshEntry] = []
        self.selected_index = -1
        self.selected_indices: set[int] = set()
        self.shared_indices: set[int] = set()
        self.source_size = (1, 1)
        self.texture_index = 0
        self.pixmap = QPixmap()
        self.image_polygon: list[tuple[float, float]] | None = None
        self.zoom_factor = 1.0
        self.pan_offset = QPointF()
        self._pan_anchor = None
        self.selection_mode = "rectangle"
        self._selection_anchor = None
        self._selection_position = None
        self._selection_shift = False
        self._selection_moved = False
        self._triangle_cache = {}
        self.overview_mode = False
        self.setMinimumSize(100, 100)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

    def set_scene(
        self,
        entries: list[ArtMeshEntry],
        source_size: tuple[int, int],
        selected_index: int,
        *,
        texture_index: int = 0,
        pixmap: QPixmap | None = None,
        image_polygon: list[tuple[float, float]] | None = None,
        shared_indices: set[int] | None = None,
        selected_indices: set[int] | None = None,
    ) -> None:
        if (self.entries is not entries or self.source_size != tuple(map(int, source_size))
                or self.texture_index != texture_index):
            self._triangle_cache.clear()
        self.entries = entries
        if self.source_size != (max(1, int(source_size[0])), max(1, int(source_size[1]))):
            self.zoom_factor = 1.0
            self.pan_offset = QPointF()
        self.source_size = (
            max(1, int(source_size[0])),
            max(1, int(source_size[1])),
        )
        self.selected_index = int(selected_index)
        self.selected_indices = set(selected_indices or ())
        if selected_index >= 0:
            self.selected_indices.add(selected_index)
        self.texture_index = int(texture_index)
        self.pixmap = pixmap or QPixmap()
        self.image_polygon = image_polygon
        self.shared_indices = set(shared_indices or set())
        self.update()

    def _transform(self) -> tuple[float, float, float]:
        width, height = self.source_size
        available_width = max(1, self.width() - 24)
        available_height = max(1, self.height() - 24)
        scale = min(available_width / width, available_height / height)
        scale = max(0.01, scale) * self.zoom_factor
        offset_x = (self.width() - width * scale) / 2.0 + self.pan_offset.x()
        offset_y = (self.height() - height * scale) / 2.0 + self.pan_offset.y()
        return scale, offset_x, offset_y

    def source_to_view(self, point: tuple[float, float]) -> QPointF:
        scale, offset_x, offset_y = self._transform()
        return QPointF(offset_x + point[0] * scale, offset_y + point[1] * scale)

    def view_to_source(self, point: QPointF) -> tuple[float, float]:
        scale, offset_x, offset_y = self._transform()
        return ((point.x() - offset_x) / scale, (point.y() - offset_y) / scale)

    def _entry_triangles(self, entry: ArtMeshEntry):
        key = id(entry)
        if key not in self._triangle_cache:
            self._triangle_cache[key] = (entry.pose_triangles() if self.mode == "pose"
                                         else entry.atlas_triangles(self.source_size) if entry.texture_index == self.texture_index else [])
        return self._triangle_cache[key]

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#20252d"))
        scale, offset_x, offset_y = self._transform()
        width, height = self.source_size
        target = QRectF(offset_x, offset_y, width * scale, height * scale)
        if not self.pixmap.isNull():
            if self.image_polygon is not None:
                # A native frame includes the preview's letterbox, pan and zoom.
                # Its four viewport corners have already been mapped through
                # the SDK MVP into the same canvas coordinates as the meshes.
                p0, p1, _p2, p3 = [self.source_to_view(point) for point in self.image_polygon]
                image_width, image_height = self.pixmap.width(), self.pixmap.height()
                mapping = QTransform((p1.x() - p0.x()) / image_width,
                                     (p1.y() - p0.y()) / image_width,
                                     (p3.x() - p0.x()) / image_height,
                                     (p3.y() - p0.y()) / image_height,
                                     p0.x(), p0.y())
                painter.save()
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                painter.setTransform(mapping)
                painter.drawPixmap(0, 0, self.pixmap)
                painter.restore()
            else:
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
            selected = index in self.selected_indices
            if self.mode == "pose" and self.image_polygon is not None and not selected:
                # The model itself is the selection guide. Drawing every mesh
                # edge on a thousand-drawable model obscures the visible parts.
                continue
            triangles = self._entry_triangles(entry)
            if not triangles:
                continue
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
            if self.mode == "pose" and self.image_polygon is not None:
                edges = {}
                for triangle in triangles:
                    for a, b in zip(triangle, triangle[1:] + triangle[:1]):
                        edge = tuple(sorted((a, b)))
                        edges[edge] = edges.get(edge, 0) + 1
                for (a, b), count in edges.items():
                    if count == 1:
                        painter.drawLine(self.source_to_view(a), self.source_to_view(b))
                continue
            for triangle in triangles:
                polygon = QPolygonF([self.source_to_view(point) for point in triangle])
                painter.drawPolygon(polygon)
        if self._selection_anchor is not None and self.selection_mode == "rectangle" and self._selection_moved:
            painter.setPen(QPen(QColor(0, 190, 220), 1.5, Qt.PenStyle.DashLine))
            painter.setBrush(QColor(0, 190, 220, 30))
            painter.drawRect(QRectF(self._selection_anchor, self._selection_position).normalized())
        painter.end()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self._pan_anchor = QPointF(event.position())
            self._selection_anchor = None
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._selection_anchor = QPointF(event.position())
            self._selection_position = QPointF(event.position())
            self._selection_shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self._selection_moved = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pan_anchor is not None:
            self.pan_offset += event.position() - self._pan_anchor
            self._pan_anchor = QPointF(event.position())
            self.update()
            event.accept()
            return
        if self._selection_anchor is not None:
            self._selection_position = QPointF(event.position())
            self._selection_moved |= (self._selection_position - self._selection_anchor).manhattanLength() >= 4
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self._pan_anchor = None
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._selection_anchor is not None:
            first, last = self._selection_anchor, QPointF(event.position())
            moved = self._selection_moved or (last - first).manhattanLength() >= 4
            self._selection_anchor = None
            if moved and self.selection_mode == "rectangle":
                rectangle = QRectF(first, last).normalized()
                polygon = [self.view_to_source(point) for point in (
                    rectangle.topLeft(), rectangle.topRight(), rectangle.bottomRight(), rectangle.bottomLeft())]
                xs, ys = zip(*polygon)
                region = {"coordinate_space": "canvas-pixels-y-down" if self.mode == "pose" else "atlas-pixels-y-down",
                          "x": min(xs), "y": min(ys), "width": max(xs) - min(xs), "height": max(ys) - min(ys),
                          "polygon": [list(point) for point in polygon]}
                self.regionRequested.emit(region, "add" if self._selection_shift else "replace")
            elif not moved:
                x, y = self.view_to_source(last)
                self.selectionRequested.emit(float(x), float(y), "toggle" if self._selection_shift else "replace")
                self.hitRequested.emit(float(x), float(y))
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def setSelectionMode(self, mode):
        if mode not in ("point", "rectangle"):
            raise ValueError("UV selection mode must be point or rectangle")
        self.selection_mode = mode
        self._selection_anchor = None
        self.update()

    def wheelEvent(self, event):
        if self.overview_mode and not event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            event.ignore()
            return
        anchor = event.position()
        source = self.view_to_source(anchor)
        self.zoom_factor = min(12., max(1., self.zoom_factor * 1.25 ** (event.angleDelta().y() / 120.)))
        self.pan_offset += anchor - self.source_to_view(source)
        self.update()
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.reset_view()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def reset_view(self):
        self.zoom_factor = 1.0
        self.pan_offset = QPointF()
        self.update()


class _SelectionListWidget(QListWidget):
    """Use Shift as a per-row toggle, rather than Qt's range selection."""
    toggle_enabled = True

    def __init__(self, parent=None):
        super().__init__(parent)
        self._shift_click = False

    def mousePressEvent(self, event):
        if (self.toggle_enabled and event.button() == Qt.MouseButton.LeftButton
                and event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self._shift_click = True
            item = self.itemAt(event.position().toPoint())
            if item is not None:
                index = self.indexFromItem(item)
                self.selectionModel().setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
                self.selectionModel().select(index, QItemSelectionModel.SelectionFlag.Toggle)
            event.accept()
            return
        self._shift_click = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self._shift_click and event.button() == Qt.MouseButton.LeftButton:
            self._shift_click = False
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ArtMeshInspector(QWidget):
    """Static ArtMesh list, pose hit-testing and atlas UV inspection widget."""

    selectionChanged = Signal(object)
    metadataChanged = Signal(str)
    selectionIdsChanged = Signal(list)

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
        self.pose_image_polygon: list[tuple[float, float]] | None = None
        self.pose_preview_available = False
        self.sidecar_canvas_size: tuple[int, int] = (1, 1)
        self.selected_index = -1
        self.selected_indices: set[int] = set()
        self._texture_pixmaps = {}
        self._selection_alpha_cache = {}
        self._atlas_geometry_cache = {}
        self._atlas_indices = None
        self._allowed_ids = None
        self.pick_candidates = []
        self._keep_pick_candidates = False
        self._candidate_baseline = []
        self._candidate_primary = None
        self._candidate_selection_mode = "replace"
        self._multi_selection = True
        self._selection_mode = "rectangle"
        self._loading = False
        self.i18n = get_i18n()
        self._build_ui()
        self.retranslate_ui()
        self.i18n.languageChanged.connect(self.retranslate_ui)
        if metadata_path:
            self.load_metadata(metadata_path)

    def _build_ui(self) -> None:
        self.main_layout = EditorViewportLayout(self)
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
        self.texture_label.hide()
        self.texture_combo.hide()  # numeric atlas tabs replace this duplicate selector
        self.search_edit = LineEdit(self)
        self.search_edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.search_edit.textChanged.connect(self._filter_entries)
        self.texture_row.addWidget(self.search_edit, 1)
        self.main_layout.addLayout(self.texture_row)
        self.candidate_combo = QComboBox(self)
        self.candidate_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.candidate_combo.currentIndexChanged.connect(self._candidate_changed)
        self.candidate_combo.hide()
        self.main_layout.addWidget(self.candidate_combo)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.entry_list = _SelectionListWidget(self.splitter)
        self.entry_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.entry_list.itemSelectionChanged.connect(self._entry_selection_changed)
        right = QSplitter(Qt.Orientation.Vertical, self.splitter)
        self.view_splitter = right
        pose_frame = QFrame(right)
        pose_layout = QVBoxLayout(pose_frame)
        pose_layout.setContentsMargins(0, 0, 0, 0)
        self.pose_title_label = QLabel(pose_frame)
        pose_layout.addWidget(self.pose_title_label)
        self.pose_canvas = _MeshCanvas("pose", pose_frame)
        self.pose_canvas.selectionRequested.connect(self._pose_hit)
        self.pose_canvas.regionRequested.connect(self._pose_region)
        pose_layout.addWidget(self.pose_canvas, 1)
        atlas_frame = QFrame(right)
        self.atlas_frame = atlas_frame
        self._atlas_layout = QVBoxLayout(atlas_frame)
        self._atlas_layout.setContentsMargins(0, 0, 0, 0)
        self._atlas_layout.setSpacing(4)
        self.atlas_title_label = QLabel(atlas_frame)
        self.atlas_title_label.hide()
        self.atlas_canvas = _MeshCanvas("atlas", atlas_frame)
        self._atlas_layout.addWidget(self.atlas_canvas, 1)
        self.atlas_tabs = None
        self.atlas_canvases = {}
        self.overview_canvases = {}
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
        self.search_edit.setPlaceholderText(_text("artmesh.inspector.search"))
        if self.metadata_path is None:
            self.status_label.setText(tr("psd.inspector.choose_first"))
        self._update_status()
        self.candidate_combo.setToolTip(_text("artmesh.inspector.overlap_hint"))
        for canvas in [self.pose_canvas, self.atlas_canvas, *self.atlas_canvases.values(), *self.overview_canvases.values()]:
            canvas.setToolTip(_text("artmesh.inspector.zoom_hint") +
                              ("\n" + _text("artmesh.inspector.selection_hint") if self._multi_selection else ""))
        self.entry_list.setToolTip(_text("artmesh.inspector.selection_hint") if self._multi_selection else "")
        if self.atlas_tabs:
            self.atlas_tabs.setTabText(0, _text("artmesh.inspector.overview"))

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
            self._atlas_geometry_cache.clear()
            self._load_textures(metadata, path)
            self._load_pose_preview(metadata, path)
            self._populate_texture_combo()
            self._rebuild_atlas_pages()
            self._populate_entry_list()
            selected = next(
                (index for index, entry in enumerate(self.entries) if entry.drawable_id == previous_id),
                0 if self.entries else -1,
            )
            self.selected_index = -1
            self.selected_indices.clear()
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

    def load_snapshot(self, snapshot: Mapping[str, Any], texture_paths,
                      *, selected_ids=None) -> bool:
        """Use an in-memory current pose without writing inspector metadata."""
        previous = self.selected_drawable_ids() if selected_ids is None else list(selected_ids)
        self._loading = True
        try:
            self.metadata_path = None
            self.metadata_edit.clear()
            self.metadata = dict(snapshot)
            self.shared_regions = list(snapshot.get("shared_regions", []))
            layers = [dict(item, drawable_id=item.get("id", item.get("drawable_id", "")))
                      for item in snapshot.get("drawables", [])]
            self.entries = self._build_entries({"layers": layers}, {})
            self._atlas_geometry_cache.clear()
            self.texture_paths = ({int(i): Path(p) for i, p in texture_paths.items()}
                                  if isinstance(texture_paths, Mapping)
                                  else {i: Path(p) for i, p in enumerate(texture_paths)})
            self.texture_sizes = {}
            for i in self.texture_paths:
                pixmap = self._texture_pixmap(i)
                self.texture_sizes[i] = (max(1, pixmap.width()), max(1, pixmap.height()))
            canvas = snapshot.get("canvas", {})
            self.pose_size = (max(1, int(canvas.get("width", 1))), max(1, int(canvas.get("height", 1))))
            self.pose_pixmap = QPixmap()
            self.pose_image_polygon = None
            self.pose_preview_available = False
            self._populate_texture_combo()
            self._rebuild_atlas_pages()
            self._populate_entry_list()
            self.selected_index = -1
            self.selected_indices.clear()
        finally:
            self._loading = False
        available = {entry.drawable_id for entry in self.entries}
        ids = [identifier for identifier in previous if identifier in available]
        self.select_entries(ids)
        self.metadataChanged.emit("")
        return True

    def set_pose_preview(self, image: QImage | QPixmap, canvas_polygon) -> None:
        """Show a same-pose native frame without using its pixels for hit tests.

        ``canvas_polygon`` is the viewport's TL/TR/BR/BL corners in
        canvas-pixels-y-down, including any letterbox outside the model canvas.
        The SDK's orthographic MVP and preview composition make this affine.
        """
        polygon = _as_points(canvas_polygon)
        if len(polygon) != 4 or len(_finite_points(polygon)) != 4:
            raise ValueError("A pose frame requires four finite canvas corners")
        p0, p1, p2, p3 = polygon
        if (abs(p0[0] + p2[0] - p1[0] - p3[0]) > 1e-4
                or abs(p0[1] + p2[1] - p1[1] - p3[1]) > 1e-4):
            raise ValueError("Pose frame corners must describe an affine viewport")
        area = ((p1[0] - p0[0]) * (p3[1] - p0[1])
                - (p1[1] - p0[1]) * (p3[0] - p0[0]))
        if abs(area) < 1e-8:
            raise ValueError("Pose frame viewport has no area")
        pixmap = QPixmap.fromImage(image) if isinstance(image, QImage) else QPixmap(image)
        if pixmap.isNull():
            raise ValueError("Pose frame is empty")
        # The corner mapping uses physical framebuffer pixels, independently of
        # the screen DPR; Qt must not scale the pixmap a second time.
        pixmap.setDevicePixelRatio(1.0)
        self.pose_pixmap = pixmap
        self.pose_image_polygon = polygon
        self.pose_preview_available = True
        self._update_views()

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
        self.pose_image_polygon = None
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
            label = entry.name if entry.name == entry.drawable_id else f"{entry.name}  ({entry.drawable_id})"
            item = QListWidgetItem(label + suffix)
            item.setToolTip(label + suffix)
            item.setData(Qt.ItemDataRole.UserRole, index)
            self.entry_list.addItem(item)
        self.entry_list.blockSignals(False)
        self._filter_entries()

    def _filter_entries(self, *_args):
        needle = self.search_edit.text().strip().casefold()
        for index, entry in enumerate(self.entries):
            item = self.entry_list.item(index)
            if item is not None:
                item.setHidden((self._allowed_ids is not None and entry.drawable_id not in self._allowed_ids)
                               or bool(needle and needle not in (entry.name + " " + entry.drawable_id).casefold()))

    def _texture_pixmap(self, texture_index: int) -> QPixmap:
        path = self.texture_paths.get(texture_index)
        if path is None:
            return QPixmap()
        try:
            stat = path.stat()
            key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
        except OSError:
            return QPixmap()
        cached = self._texture_pixmaps.get(texture_index)
        if cached is None or cached[0] != key:
            cached = (key, QPixmap(str(path)))
            self._texture_pixmaps[texture_index] = cached
        return cached[1]

    def _rebuild_atlas_pages(self):
        indices = sorted(self.texture_sizes)
        if indices == self._atlas_indices:
            return
        self._atlas_indices = indices
        old_canvas = self.atlas_canvas
        old_canvas.setParent(self.atlas_frame)
        self._atlas_layout.removeWidget(old_canvas)
        if self.atlas_tabs is not None:
            self.atlas_tabs.blockSignals(True)
            self._atlas_layout.removeWidget(self.atlas_tabs)
            self.atlas_tabs.deleteLater()
        self.atlas_tabs = FluentEditorTabs(self.atlas_frame)
        self.atlas_tabs.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.atlas_tabs.setMinimumSize(0, 0)
        self.overview_scroll = ScrollArea(self.atlas_tabs)
        self.overview_scroll.setWidgetResizable(True)
        self.overview_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.overview_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.overview_scroll.enableTransparentBackground()
        self.overview_body = QWidget()
        self.overview_grid = QGridLayout(self.overview_body)
        self.overview_grid.setContentsMargins(0, 0, 0, 0)
        self.overview_grid.setSpacing(8)
        self.overview_grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.overview_scroll.setWidget(self.overview_body)
        self.overview_scroll.viewport().installEventFilter(self)
        self.atlas_tabs.addTab(self.overview_scroll, _text("artmesh.inspector.overview"))
        self.atlas_canvases = {}
        self.overview_canvases = {}
        self._overview_cards = []
        self._overview_columns = 0
        for position, texture_index in enumerate(indices):
            canvas = old_canvas if position == 0 else _MeshCanvas("atlas", self.atlas_frame)
            if hasattr(canvas, "_inspector_hit_callback"):
                canvas.selectionRequested.disconnect(canvas._inspector_hit_callback)
                canvas.regionRequested.disconnect(canvas._inspector_region_callback)
            canvas._inspector_hit_callback = lambda x, y, mode, i=texture_index: self._atlas_hit_texture(i, x, y, mode)
            canvas._inspector_region_callback = lambda region, mode, i=texture_index: self._atlas_region(i, region, mode)
            canvas.selectionRequested.connect(canvas._inspector_hit_callback)
            canvas.regionRequested.connect(canvas._inspector_region_callback)
            canvas.setSelectionMode(self._selection_mode if self._multi_selection else "point")
            canvas.setToolTip(_text("artmesh.inspector.zoom_hint"))
            self.atlas_canvases[texture_index] = canvas
            self.atlas_tabs.addTab(canvas, str(texture_index))
            card = QFrame(self.overview_body)
            card.setMinimumWidth(0)
            card.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(0, 0, 0, 0)
            card_layout.setSpacing(3)
            path = self.texture_paths.get(texture_index)
            label = BodyLabel(f"{texture_index} · {path.name if path else '—'}", card)
            label.setToolTip(str(path or ""))
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            card_layout.addWidget(label)
            overview_canvas = _MeshCanvas("atlas", card)
            overview_canvas.overview_mode = True
            overview_canvas.setToolTip(_text("artmesh.inspector.zoom_hint"))
            overview_canvas.setMinimumHeight(150)
            overview_canvas.setMaximumHeight(260)
            overview_canvas.selectionRequested.connect(lambda x, y, mode, i=texture_index: self._atlas_hit_texture(i, x, y, mode))
            overview_canvas.regionRequested.connect(lambda region, mode, i=texture_index: self._atlas_region(i, region, mode))
            overview_canvas.setSelectionMode(self._selection_mode if self._multi_selection else "point")
            card_layout.addWidget(overview_canvas)
            self.overview_canvases[texture_index] = overview_canvas
            self._overview_cards.append(card)
        if indices:
            self.atlas_canvas = self.atlas_canvases[indices[0]]
        else:
            old_canvas.hide()
        self.atlas_tabs.currentChanged.connect(self._atlas_page_changed)
        self._atlas_layout.addWidget(self.atlas_tabs, 1)
        self._arrange_overview()

    def _arrange_overview(self):
        if not self.atlas_tabs:
            return
        columns = 2 if self.atlas_frame.width() >= 320 else 1
        if columns == self._overview_columns:
            return
        self._overview_columns = columns
        for position, card in enumerate(self._overview_cards):
            self.overview_grid.addWidget(card, position // columns, position % columns)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange_overview()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize and self.atlas_tabs and watched is self.overview_scroll.viewport():
            self._arrange_overview()
        return super().eventFilter(watched, event)

    def sizeHint(self):
        return QSize(620, 480)

    def minimumSizeHint(self):
        return QSize(180, 160)

    def _atlas_page_changed(self, index):
        if self._loading or index <= 0 or not self._atlas_indices:
            return
        texture_index = self._atlas_indices[index - 1]
        with QSignalBlocker(self.texture_combo):
            self.texture_combo.setCurrentIndex(self.texture_combo.findData(texture_index))
        self.atlas_canvas = self.atlas_canvases[texture_index]
        self._update_views()

    def _texture_changed(self, _index: int) -> None:
        if not self._loading and self.atlas_tabs and self._atlas_indices:
            texture_index = int(self.texture_combo.currentData() or 0)
            if texture_index in self._atlas_indices:
                self.atlas_tabs.setCurrentIndex(self._atlas_indices.index(texture_index) + 1)
        self._update_views()

    def _entry_selection_changed(self) -> None:
        if self._loading:
            return
        ids = [self.entries[int(item.data(Qt.ItemDataRole.UserRole))].drawable_id
               for item in self.entry_list.selectedItems()]
        current = self.entry_list.currentRow()
        primary = self.entries[current].drawable_id if 0 <= current < len(self.entries) else None
        self.select_entries(ids, primary_id=primary)

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
            self.select_entries([])
            return None
        selected = self.select_entries([self.entries[index].drawable_id])
        return selected[0] if selected else None

    def select_entries(self, drawable_ids, primary_id=None) -> list[ArtMeshEntry]:
        if not self._keep_pick_candidates:
            self.pick_candidates = []
            with QSignalBlocker(self.candidate_combo):
                self.candidate_combo.clear()
            self.candidate_combo.hide()
        ids = list(dict.fromkeys(str(value) for value in drawable_ids))
        if not self._multi_selection:
            ids = [primary_id] if primary_id in ids else ids[:1]
        by_id = {entry.drawable_id: index for index, entry in enumerate(self.entries)
                 if self._allowed_ids is None or entry.drawable_id in self._allowed_ids}
        self.selected_indices = {by_id[identifier] for identifier in ids if identifier in by_id}
        primary = primary_id if primary_id in ids else next((i for i in ids if i in by_id), None)
        self.selected_index = by_id.get(primary, -1)
        self._loading = True
        try:
            self.entry_list.blockSignals(True)
            self.entry_list.clearSelection()
            if self.selected_index >= 0 and self.entry_list.item(self.selected_index).isHidden():
                self.search_edit.clear()
            if self.selected_index >= 0:
                self.entry_list.setCurrentRow(self.selected_index, QItemSelectionModel.SelectionFlag.NoUpdate)
            else:
                self.entry_list.setCurrentRow(-1)
            for index in self.selected_indices:
                self.entry_list.item(index).setSelected(True)
            if self.selected_index >= 0:
                self.entry_list.scrollToItem(self.entry_list.item(self.selected_index))
                entry = self.entries[self.selected_index]
                self.texture_combo.blockSignals(True)
                self.texture_combo.setCurrentIndex(self.texture_combo.findData(entry.texture_index))
                self.texture_combo.blockSignals(False)
                if self.atlas_tabs and self.atlas_tabs.currentIndex() > 0 and entry.texture_index in self._atlas_indices:
                    self.atlas_tabs.setCurrentIndex(self._atlas_indices.index(entry.texture_index) + 1)
        finally:
            self.entry_list.blockSignals(False)
            self._loading = False
        self._update_views()
        self.selectionIdsChanged.emit(self.selected_drawable_ids())
        self.selectionChanged.emit(self.current_entry())
        return [self.entries[by_id[identifier]] for identifier in ids if identifier in by_id]

    def apply_selection(self, drawable_ids, mode="replace", primary_id=None):
        """Apply UV, list or preview gestures to the same final selection."""
        picked = list(dict.fromkeys(map(str, drawable_ids)))
        mode = mode if self._multi_selection else "replace"
        identifiers = combine_selection(self.selected_drawable_ids(), picked, mode)
        primary = primary_id or (picked[0] if picked else getattr(self.current_entry(), "drawable_id", None))
        return self.select_entries(identifiers, primary_id=primary)

    def set_multi_selection(self, enabled):
        """ViewerEX uses False: modifiers and drags never select many IDs."""
        self._multi_selection = bool(enabled)
        self.entry_list.toggle_enabled = self._multi_selection
        self.entry_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection if enabled
                                         else QAbstractItemView.SelectionMode.SingleSelection)
        self.set_selection_mode(self._selection_mode)
        self.select_entries(self.selected_drawable_ids(), primary_id=getattr(self.current_entry(), "drawable_id", None))
        self.retranslate_ui()

    def set_selection_mode(self, mode):
        if mode not in ("point", "rectangle"):
            raise ValueError("UV selection mode must be point or rectangle")
        self._selection_mode = mode
        for canvas in [self.pose_canvas, *self.atlas_canvases.values(), *self.overview_canvases.values()]:
            canvas.setSelectionMode(mode if self._multi_selection else "point")

    def selected_drawable_ids(self) -> list[str]:
        return [entry.drawable_id for index, entry in enumerate(self.entries) if index in self.selected_indices]

    def current_entry(self) -> ArtMeshEntry | None:
        if 0 <= self.selected_index < len(self.entries):
            return self.entries[self.selected_index]
        return None

    def set_allowed_drawable_ids(self, drawable_ids=None):
        self._allowed_ids = None if drawable_ids is None else set(map(str, drawable_ids))
        self._filter_entries()

    def set_pick_candidates(self, drawable_ids, selection_mode="replace"):
        if selection_mode not in ("replace", "toggle"):
            raise ValueError("Point selection operation must be replace or toggle")
        self._candidate_baseline = self.selected_drawable_ids()
        self._candidate_primary = getattr(self.current_entry(), "drawable_id", None)
        self._candidate_selection_mode = selection_mode if self._multi_selection else "replace"
        available = {entry.drawable_id for entry in self.entries}
        self.pick_candidates = [identifier for identifier in dict.fromkeys(map(str, drawable_ids))
                                if identifier in available and (self._allowed_ids is None or identifier in self._allowed_ids)]
        with QSignalBlocker(self.candidate_combo):
            self.candidate_combo.clear()
            for identifier in self.pick_candidates:
                self.candidate_combo.addItem(identifier, userData=identifier)
        self.candidate_combo.setVisible(len(self.pick_candidates) > 1)
        self._keep_pick_candidates = True
        try:
            self._apply_pick_candidate(self.pick_candidates[0] if self.pick_candidates else None)
        finally:
            self._keep_pick_candidates = False

    def _candidate_changed(self, index):
        if index >= 0 and not self._loading:
            self._keep_pick_candidates = True
            try:
                self._apply_pick_candidate(str(self.candidate_combo.itemData(index)))
            finally:
                self._keep_pick_candidates = False

    def _apply_pick_candidate(self, identifier):
        # Choosing a back layer refines this same gesture. Recompute from its
        # baseline so a Shift click never toggles every layer clicked through.
        ids = combine_selection(self._candidate_baseline, [identifier] if identifier else [],
                                self._candidate_selection_mode)
        self.select_entries(ids, primary_id=identifier or self._candidate_primary)

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
            if (not entry.has_pose_geometry or not entry.visible or entry.opacity <= 0
                    or (self._allowed_ids is not None and entry.drawable_id not in self._allowed_ids)):
                continue
            if any(_point_in_triangle(point, triangle) for triangle in entry.pose_triangles()):
                order = (entry.render_order, entry.draw_order, entry.source_index, index)
                candidates.append((order, index))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    def hit_atlas(self, texture_index: int, x: float, y: float) -> int | None:
        candidates = self._atlas_candidates(texture_index, x, y)
        return candidates[0] if candidates else None

    def _atlas_candidates(self, texture_index, x, y):
        point = (float(x), float(y))
        return [index for index, bounds, triangles in self._atlas_geometry(texture_index)
                if (self._allowed_ids is None or self.entries[index].drawable_id in self._allowed_ids)
                and bounds[0] <= point[0] <= bounds[2] and bounds[1] <= point[1] <= bounds[3]
                and any(_point_in_triangle(point, triangle) for triangle in triangles)]

    def _atlas_geometry(self, texture_index):
        """UVs are static per snapshot; opacity/eligible IDs remain live filters."""
        size = self.texture_sizes.get(int(texture_index), (1, 1))
        cached = self._atlas_geometry_cache.get(int(texture_index))
        if cached is not None and cached[0] == size:
            return cached[1]
        geometry = []
        for index, entry in enumerate(self.entries):
            if entry.texture_index != int(texture_index) or not entry.has_atlas_geometry:
                continue
            triangles = entry.atlas_triangles(size)
            points = [point for triangle in triangles for point in triangle]
            if not points:
                continue
            bounds = (min(p[0] for p in points), min(p[1] for p in points),
                      max(p[0] for p in points), max(p[1] for p in points))
            order = (entry.render_order, entry.draw_order, entry.source_index, index)
            geometry.append((order, index, bounds, triangles))
        result = [(index, bounds, triangles) for _, index, bounds, triangles in sorted(geometry, reverse=True)]
        self._atlas_geometry_cache[int(texture_index)] = (size, result)
        return result

    def _pose_scene(self):
        drawables = self.metadata.get("drawables")
        if not isinstance(drawables, list):
            drawables = [dict(entry.raw, id=entry.drawable_id,
                              vertices=[(x * entry.coordinate_scale, y * entry.coordinate_scale) for x, y in entry.vertices])
                         for entry in self.entries]
        return SelectionScene({"drawables": drawables, "parts": self.metadata.get("parts", [])},
                              self.texture_paths, alpha_cache=self._selection_alpha_cache)

    def _pose_hit(self, x: float, y: float, mode="replace") -> None:
        self.set_pick_candidates(self._pose_scene().hit_point((x, y)), selection_mode=mode)

    def _pose_region(self, region, mode="replace"):
        self.apply_selection(self._pose_scene().hit_region(region), mode=mode)

    def _atlas_hit(self, x: float, y: float) -> None:
        texture_index = int(self.texture_combo.currentData() or 0)
        index = self.hit_atlas(texture_index, x, y)
        if index is not None:
            self.select_entry(index)

    def _atlas_hit_texture(self, texture_index, x, y, mode="replace"):
        indices = self._atlas_candidates(texture_index, x, y)
        self.set_pick_candidates([self.entries[index].drawable_id for index in indices], selection_mode=mode)

    def _atlas_region(self, texture_index, region, mode="replace"):
        polygon = region["polygon"]
        xs, ys = zip(*polygon)
        bounds = (min(xs), min(ys), max(xs), max(ys))
        identifiers = [self.entries[index].drawable_id for index, entry_bounds, triangles in self._atlas_geometry(texture_index)
                       if SelectionScene._bounds_overlap(entry_bounds, bounds)
                       and any(len(intersect_convex(triangle, polygon)) >= 3 for triangle in triangles)]
        self.apply_selection(identifiers, mode=mode)

    def _update_views(self) -> None:
        # Callers clearing a model already clear entries/textures. Clear all
        # atlas pages too rather than leaving an old model's UVs in the stack.
        if not self.entries:
            self.selected_indices.clear()
        pose_size = self.pose_size
        if pose_size == (1, 1):
            points = [point for entry in self.entries for point in entry.vertices]
            if points:
                right = max(point[0] for point in points)
                bottom = max(point[1] for point in points)
                pose_size = (max(1, int(math.ceil(right))), max(1, int(math.ceil(bottom))))
        self.pose_canvas.set_scene(
            self.entries,
            pose_size,
            self.selected_index,
            pixmap=self.pose_pixmap,
            image_polygon=self.pose_image_polygon,
            shared_indices=self._shared_entry_indices(),
            selected_indices=self.selected_indices,
        )
        for canvas_index, canvas in list(self.atlas_canvases.items()) + list(self.overview_canvases.items()):
            canvas.set_scene(self.entries, self.texture_sizes.get(canvas_index, (1, 1)), self.selected_index,
                             texture_index=canvas_index, pixmap=self._texture_pixmap(canvas_index),
                             shared_indices=self._shared_entry_indices(canvas_index),
                             selected_indices=self.selected_indices)
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
        self.status_label.setText(_text("artmesh.inspector.selected_count", count=len(self.selected_indices))
                                  if len(self.selected_indices) > 1 else tr("psd.inspector.selected", name=entry.name))


class ArtMeshInspectorDialog(ThemedEditorDialog):
    """Convenient standalone dialog used by the PSD workbench button."""

    def __init__(
        self,
        metadata_path: str | Path | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.i18n = get_i18n()
        self.setWindowTitle(tr("psd.inspector.title"))
        self.setMinimumSize(620, 460)
        self.setMaximumWidth(1040)
        self.resize(900, 660)
        self.inspector = ArtMeshInspector(metadata_path, self)
        self.viewLayout.addWidget(self.inspector, 1)
        self.inspector.pose_canvas.setMinimumHeight(180)
        self.inspector.view_splitter.setSizes([240, 330])
        self.hideYesButton()
        self.close_button = self.cancelButton
        self.close_button.setText(tr("psd.inspector.close"))
        self.i18n.languageChanged.connect(self.retranslate_ui)

    def retranslate_ui(self, *_args) -> None:
        self.setWindowTitle(tr("psd.inspector.title"))
        self.close_button.setText(tr("psd.inspector.close"))


class ArtMeshPickDialog(ThemedEditorDialog):
    """Pick an eligible ID from actual UV/pose geometry, without a second GPU."""
    def __init__(self, snapshot, texture_paths, eligible_ids, parent=None, selected_id=None,
                 *, pose_frame=None, pose_frame_canvas_polygon=None):
        super().__init__(parent)
        self.setWindowTitle(_text("artmesh.inspector.pick_trigger"))
        self.setMinimumSize(620, 500)
        self.setMaximumWidth(1000)
        self.resize(880, 660)
        self.inspector = ArtMeshInspector(parent=self)
        self.inspector.set_multi_selection(False)
        self._eligible_ids = set(eligible_ids)
        self.inspector.load_snapshot(snapshot, texture_paths)
        if pose_frame is not None:
            self.inspector.set_pose_preview(pose_frame, pose_frame_canvas_polygon)
        self.inspector.set_allowed_drawable_ids(self._eligible_ids)
        for widget in (self.inspector.metadata_edit, self.inspector.open_button,
                       self.inspector.refresh_button, self.inspector.mode_label,
                       self.inspector.details, self.inspector.shared_label, self.inspector.status_label):
            widget.hide()
        self.viewLayout.addWidget(self.inspector, 1)
        self.inspector.pose_canvas.setMinimumHeight(220)
        self.inspector.view_splitter.setSizes([280, 240])
        self.selected_id = ""
        self.inspector.selectionChanged.connect(self._selected)
        self.yesButton.setEnabled(False)
        if selected_id in self._eligible_ids:
            self.inspector.select_entry(selected_id)

    def _selected(self, entry):
        self.selected_id = entry.drawable_id if entry and entry.drawable_id in self._eligible_ids else ""
        self.yesButton.setEnabled(bool(self.selected_id))

    def validate(self):
        return bool(self.selected_id and self.selected_id in self._eligible_ids)

    def retranslate_ui(self, *_args):
        super().retranslate_ui()
        self.setWindowTitle(_text("artmesh.inspector.pick_trigger"))


__all__ = ["ArtMeshEntry", "ArtMeshInspector", "ArtMeshInspectorDialog", "ArtMeshPickDialog"]
