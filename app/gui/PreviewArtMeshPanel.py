from __future__ import annotations

import json
import tempfile
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QAbstractItemView, QHBoxLayout, QHeaderView, QSizePolicy, QTableWidgetItem, QWidget
from qfluentwidgets import CaptionLabel, CheckBox, DoubleSpinBox, FluentIcon, PushButton, SearchLineEdit, TableWidget, TransparentToggleToolButton

from app.core.cubism_core import CubismCore, resolve_live2d_source
from app.gui.ArtMeshInspector import ArtMeshInspector, _point_in_triangle
from app.gui.editor_workspace import EditorViewportLayout
from app.i18n import get_i18n, tr


PREVIEW_ARTMESH_TEXT = {
    "preview.artmesh.empty": "载入 Live2D 模型后查看部件。",
    "preview.artmesh.visible": "所属 Part 可见",
    "preview.artmesh.reset": "还原部件",
    "preview.artmesh.selection": "{drawable} → Part {part}（{count} 个 ArtMesh）",
    "preview.artmesh.no_part": "{drawable} 未关联可调整的 Part。",
    "preview.artmesh.unavailable": "部件信息不可用：{error}",
    "preview.artmesh.pose_parameters": "当前姿态参数",
    "preview.artmesh.all_parameters": "全部",
    "preview.artmesh.parameter_search": "搜索参数 ID",
    "preview.artmesh.parameter_scope": "默认显示当前动作的 Parameter 曲线；搜索可查看全部参数。这不是 ArtMesh 的绑定关系表。双击参数可前往调整。",
    "preview.artmesh.parameter_id": "参数 ID",
    "preview.artmesh.parameter_value": "当前值",
}


def _text(key, **values):
    return tr(key, PREVIEW_ARTMESH_TEXT[key], **values)


class PreviewArtMeshPanel(QWidget):
    """Inspect native drawable geometry; opacity changes affect preview only."""

    overridesChanged = Signal(dict, dict)
    parameterRequested = Signal(str)
    freezeRequested = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._temporary = tempfile.TemporaryDirectory(prefix="lpk-preview-artmesh-")
        self._core_model = None
        self._source = None
        self.mesh_data = {}
        self.part_overrides = {}
        self.part_defaults = {}
        self._parameter_meta = {}
        self._motion_parameter_ids = set()
        self._parameter_rows = []
        layout = EditorViewportLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.inspector = ArtMeshInspector(parent=self)
        inspector = self.inspector
        for widget in (inspector.metadata_edit, inspector.open_button, inspector.mode_label,
                       inspector.shared_label, inspector.details, inspector.status_label):
            widget.hide()
        inspector.pose_canvas.parentWidget().hide()
        inspector.splitter.setOrientation(Qt.Vertical)
        inspector.entry_list.setMinimumHeight(90)
        inspector.entry_list.setMaximumHeight(170)
        inspector.atlas_canvas.setMinimumSize(160, 120)
        inspector.texture_combo.setMaximumWidth(260)
        inspector.main_layout.setContentsMargins(0, 0, 0, 0)
        inspector.refresh_button.clicked.disconnect()
        inspector.refresh_button.clicked.connect(self.refresh)
        inspector.selectionChanged.connect(self._selection_changed)
        layout.addWidget(inspector, 1)
        controls = QHBoxLayout()
        self.part_visible = CheckBox(self)
        self.part_opacity = DoubleSpinBox(self)
        self.part_opacity.setRange(0, 1)
        self.part_opacity.setDecimals(2)
        self.part_opacity.setSingleStep(.05)
        self.part_opacity.setFixedWidth(110)
        self.part_opacity.setSymbolVisible(False)
        self.part_visible.toggled.connect(self._visibility_changed)
        self.part_opacity.valueChanged.connect(self._opacity_changed)
        controls.addWidget(self.part_visible, 1)
        controls.addWidget(self.part_opacity)
        layout.addLayout(controls)
        self.selection_label = CaptionLabel(self)
        self.selection_label.setWordWrap(True)
        self.selection_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.selection_label)
        self.reset_button = PushButton(FluentIcon.RETURN, "", self)
        self.reset_button.clicked.connect(self.reset_parts)
        layout.addWidget(self.reset_button)
        parameter_row = QHBoxLayout()
        self.parameter_title = CaptionLabel(self)
        self.freeze_pose = TransparentToggleToolButton(FluentIcon.PAUSE, self)
        self.freeze_pose.setFixedSize(28, 28)
        self.freeze_pose.toggled.connect(self.freezeRequested)
        self.all_parameters = CheckBox(self)
        parameter_row.addWidget(self.parameter_title, 1)
        parameter_row.addWidget(self.freeze_pose)
        parameter_row.addWidget(self.all_parameters)
        layout.addLayout(parameter_row)
        self.parameter_search = SearchLineEdit(self)
        self.parameter_search.textChanged.connect(self._filter_parameters)
        self.all_parameters.toggled.connect(self._filter_parameters)
        layout.addWidget(self.parameter_search)
        self.parameter_table = TableWidget(self)
        self.parameter_table.setColumnCount(2)
        self.parameter_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.parameter_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.parameter_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.parameter_table.verticalHeader().hide()
        self.parameter_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.parameter_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.parameter_table.setMinimumHeight(80)
        self.parameter_table.setMaximumHeight(140)
        self.parameter_table.cellDoubleClicked.connect(self._parameter_requested)
        layout.addWidget(self.parameter_table)
        get_i18n().languageChanged.connect(self.retranslate_ui)
        self.clear()
        self.retranslate_ui()

    def load_source(self, source, dll_path=None):
        self.clear()
        try:
            self._source = resolve_live2d_source(Path(source))
            self._core_model = CubismCore(dll_path or None).load_moc(self._source.moc3)
            self.refresh()
            return True
        except Exception as exc:
            self.selection_label.setText(_text("preview.artmesh.unavailable", error=exc))
            return False

    def refresh(self, parameters=None):
        if self._core_model is None or self._source is None:
            return
        selected = self.inspector.current_entry()
        previous = selected.drawable_id if selected else None
        self.mesh_data = self._core_model.drawable_snapshot(parameters or {})
        if not self.part_defaults:
            self.part_defaults = {str(part["id"]): float(part.get("opacity", 1))
                                  for part in self.mesh_data.get("parts", [])}
        metadata = {
            "source_model": str(self._source.model_json), "source_root": str(self._source.root_dir),
            "layers": self.mesh_data.get("drawables", []),
            "canvas": self.mesh_data.get("canvas", {}),
            "textures": [{"index": index, "relative_path": str(path)}
                         for index, path in enumerate(self._source.textures)],
        }
        path = Path(self._temporary.name) / "preview.lpkpsd.json"
        path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        self.inspector.load_metadata(path)
        canvas = self.mesh_data.get("canvas", {})
        self.inspector.sidecar_canvas_size = (int(canvas.get("width", 1)), int(canvas.get("height", 1)))
        if previous:
            self.inspector.select_entry(previous)
        self.reset_button.setEnabled(bool(self.part_overrides))

    def clear(self):
        self._core_model = self._source = None
        self.mesh_data = {}
        self.part_overrides = {}
        self.part_defaults = {}
        self._parameter_meta = {}
        self._motion_parameter_ids = set()
        self._parameter_rows = []
        self.parameter_table.setRowCount(0)
        with QSignalBlocker(self.parameter_search), QSignalBlocker(self.all_parameters), QSignalBlocker(self.freeze_pose):
            self.parameter_search.clear()
            self.all_parameters.setChecked(False)
            self.freeze_pose.setChecked(False)
        inspector = self.inspector
        inspector.entries = []
        inspector.selected_index = -1
        inspector.metadata_path = None
        inspector.metadata = {}
        inspector.pose_size = (1, 1)
        inspector.pose_pixmap = QPixmap()
        inspector.shared_regions = []
        inspector.texture_paths = {}
        inspector.texture_sizes = {}
        inspector.entry_list.clear()
        inspector.texture_combo.clear()
        inspector._update_views()
        self.part_visible.setEnabled(False)
        self.part_opacity.setEnabled(False)
        self.reset_button.setEnabled(False)
        self.selection_label.setText(_text("preview.artmesh.empty"))
        self.overridesChanged.emit({}, {})

    def set_parameter_context(self, meta, motion_parameter_ids=(), frozen=False):
        """Current native values filtered by proven motion-curve IDs only.

        Core exposes no drawable-to-parameter binding table. Preserve the
        selected ArtMesh while updating this independent pose inspection.
        """
        self._parameter_meta = {str(item["id"]): item for item in meta if item.get("id")}
        self._motion_parameter_ids = set(map(str, motion_parameter_ids or ()))
        with QSignalBlocker(self.freeze_pose):
            self.freeze_pose.setChecked(bool(frozen))
        self._filter_parameters()

    def _filter_parameters(self, *_args):
        query = self.parameter_search.text().strip().casefold()
        rows = [parameter_id for parameter_id in self._parameter_meta
                if ((query in parameter_id.casefold()) if query else
                    (self.all_parameters.isChecked() or parameter_id in self._motion_parameter_ids))]
        if rows != self._parameter_rows:
            self._parameter_rows = rows
            self.parameter_table.setRowCount(len(rows))
            for row, parameter_id in enumerate(rows):
                item = QTableWidgetItem(parameter_id)
                item.setToolTip(parameter_id)
                self.parameter_table.setItem(row, 0, item)
                self.parameter_table.setItem(row, 1, QTableWidgetItem())
        for row, parameter_id in enumerate(rows):
            value = float(self._parameter_meta[parameter_id].get("value", 0))
            item = self.parameter_table.item(row, 1)
            text = f"{value:.6g}"
            if item.text() != text:
                item.setText(text)

    def _parameter_requested(self, row, _column):
        if 0 <= row < len(self._parameter_rows):
            self.parameterRequested.emit(self._parameter_rows[row])

    def _selection_changed(self, entry):
        if entry is None:
            return
        part_id = str(entry.raw.get("parent_part_id") or "")
        available = bool(part_id and int(entry.raw.get("parent_part_index", -1)) >= 0)
        self.part_visible.setEnabled(available)
        self.part_opacity.setEnabled(available)
        value = self.part_overrides.get(part_id, self.part_defaults.get(part_id, 1))
        with QSignalBlocker(self.part_visible), QSignalBlocker(self.part_opacity):
            self.part_visible.setChecked(value > 0)
            self.part_opacity.setValue(value)
        count = sum(str(item.get("parent_part_id") or "") == part_id
                    for item in self.mesh_data.get("drawables", []))
        self.selection_label.setText(_text("preview.artmesh.selection", drawable=entry.drawable_id, part=part_id, count=count)
                                     if available else _text("preview.artmesh.no_part", drawable=entry.drawable_id))

    def _visibility_changed(self, visible):
        self._opacity_changed((self.part_opacity.value() or 1) if visible else 0)

    def _opacity_changed(self, value):
        entry = self.inspector.current_entry()
        part_id = str(entry.raw.get("parent_part_id") or "") if entry else ""
        if not part_id or not self.part_opacity.isEnabled():
            return
        self.part_overrides[part_id] = float(value)
        self.overridesChanged.emit(dict(self.part_overrides), dict(self.part_defaults))
        self._selection_changed(entry)
        self.reset_button.setEnabled(True)

    def reset_parts(self):
        self.part_overrides.clear()
        self.overridesChanged.emit({}, dict(self.part_defaults))
        self._selection_changed(self.inspector.current_entry())
        self.reset_button.setEnabled(False)

    def select_drawable(self, drawable_id):
        return self.inspector.select_entry(str(drawable_id))

    def select_model_point(self, normalized_x, normalized_y, parameters=None):
        self.refresh(parameters)
        canvas = self.mesh_data.get("canvas", {})
        point = (float(normalized_x) * float(canvas.get("width", 1)),
                 float(normalized_y) * float(canvas.get("height", 1)))
        candidates = []
        for index, entry in enumerate(self.inspector.entries):
            part = str(entry.raw.get("parent_part_id") or "")
            if not entry.visible or entry.opacity <= .001 or self.part_overrides.get(part, 1) <= .001:
                continue
            if any(_point_in_triangle(point, triangle) for triangle in entry.pose_triangles()):
                candidates.append(((entry.render_order, entry.draw_order, entry.source_index), index))
        if candidates:
            return self.inspector.select_entry(max(candidates)[1])

    def retranslate_ui(self, *_args):
        self.inspector.retranslate_ui()
        self.part_visible.setText(_text("preview.artmesh.visible"))
        self.reset_button.setText(_text("preview.artmesh.reset"))
        self.parameter_title.setText(_text("preview.artmesh.pose_parameters"))
        self.parameter_title.setToolTip(_text("preview.artmesh.parameter_scope"))
        self.parameter_table.setToolTip(_text("preview.artmesh.parameter_scope"))
        self.freeze_pose.setToolTip(tr("preview.enable_advanced_overrides"))
        self.all_parameters.setText(_text("preview.artmesh.all_parameters"))
        self.parameter_search.setPlaceholderText(_text("preview.artmesh.parameter_search"))
        self.parameter_table.setHorizontalHeaderLabels([_text("preview.artmesh.parameter_id"), _text("preview.artmesh.parameter_value")])
        entry = self.inspector.current_entry()
        if entry:
            self._selection_changed(entry)
        elif not self._source:
            self.selection_label.setText(_text("preview.artmesh.empty"))

    def shutdown(self):
        self.clear()
        self._temporary.cleanup()
