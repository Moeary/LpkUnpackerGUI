"""Shared, model-independent keyframe and curve editor.

The page owns history and model encoding. This widget only edits deep copies of
``time`` / numeric fields and emits a complete replacement track on commit.
Bezier controls are segment-relative: ``bezier[field] = [x1, y1, x2, y2]``.
Flat segments may supply ``bezier_value_scale[field]`` to retain value handles.
"""

from __future__ import annotations

import copy
import math
import time

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QTableWidgetItem,
    QGridLayout, QVBoxLayout, QWidget, QSizePolicy,
)
from qfluentwidgets import (
    CaptionLabel, CheckBox, DoubleSpinBox, FluentIcon, PushButton, Slider,
    RoundMenu, TableWidget, TransparentToolButton, TransparentToggleToolButton,
)

from app.i18n import tr
from app.gui.editor_workspace import EditorComboBox, EditorViewportLayout
from app.gui.editor_actions import action_text


TIMELINE_TEXT = {
    "editor.timeline.play": "播放",
    "editor.timeline.pause": "暂停",
    "editor.timeline.loop": "循环",
    "editor.timeline.add": "添加关键帧",
    "editor.timeline.delete": "删除关键帧",
    "editor.timeline.time": "时间 (秒)",
    "editor.timeline.interpolation": "插值",
    "editor.timeline.linear": "线性",
    "editor.timeline.stepped": "阶梯",
    "editor.timeline.bezier": "贝塞尔",
    "editor.timeline.inverse_stepped": "反向阶梯",
    "editor.timeline.apply_curve": "应用控制点",
    "editor.timeline.empty": "选择轨道，在时间线上双击或点击添加关键帧",
    "editor.timeline.readonly": "此轨道仅供查看；原始数据会完整保留",
    "editor.timeline.hint": "拖动菱形调整时间；拖动曲线上的节点调整时间与数值",
    "editor.timeline.invalid": "时间须唯一且在动画范围内，控制点时间须依次增加",
    "editor.timeline.table": "数值表",
    "editor.timeline.controls": "控制点",
    "editor.timeline.selection": "关键帧 {index} · {time} 秒",
}


# Compatibility import for older pages; real Fluent controls own their style.
EDITOR_WIDGET_STYLE = ""


def _text(key: str, **values) -> str:
    return tr(key, TIMELINE_TEXT[key], **values)


def _cubic(a: float, b: float, c: float, d: float, u: float) -> float:
    return (1 - u) ** 3 * a + 3 * (1 - u) ** 2 * u * b + 3 * (1 - u) * u * u * c + u ** 3 * d


def sample_track(frames: list[dict], field: str, seconds: float, default: float = 0.0) -> float:
    """Sample linear, stepped and Bezier segments for display/add-key values."""
    if not frames:
        return default
    if seconds <= float(frames[0].get("time", 0)):
        return float(frames[0].get(field, default))
    for left, right in zip(frames, frames[1:]):
        start, end = float(left.get("time", 0)), float(right.get("time", 0))
        if seconds > end:
            continue
        a, b = float(left.get(field, default)), float(right.get(field, default))
        if seconds >= end:
            return b
        interpolation = left.get("interpolation", "linear")
        if interpolation == "stepped":
            return a
        if interpolation == "inverse_stepped":
            return b
        fraction = (seconds - start) / max(end - start, 1e-9)
        controls = left.get("bezier", {}).get(field) if isinstance(left.get("bezier"), dict) else None
        if controls is None and isinstance(left.get("curve"), list) and len(left["curve"]) == 4:
            controls = left["curve"]
        if interpolation == "bezier" and controls is not None:
            x1, y1, x2, y2 = controls
            lo, hi = 0.0, 1.0
            for _ in range(30):
                u = (lo + hi) / 2
                if _cubic(0, x1, x2, 1, u) < fraction:
                    lo = u
                else:
                    hi = u
            scales = left.get("bezier_value_scale", {})
            scale = scales.get(field, b - a) if isinstance(scales, dict) else float(scales)
            return _cubic(a, a + y1 * scale, a + y2 * scale, b, (lo + hi) / 2)
        return a + (b - a) * fraction
    return float(frames[-1].get(field, default))


class _TimelineCanvas(QWidget):
    """Time ruler, selectable diamonds and a draggable numeric curve."""

    def __init__(self, editor: "AnimationTimelineEditor"):
        super().__init__(editor)
        self.editor = editor
        self.setMinimumHeight(80)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self._drag = None
        self._cached_path = None
        self._cached_range = None

    def invalidate_curve(self):
        self._cached_path = None
        self._cached_range = None
        self.update()

    def _plot(self) -> QRectF:
        return QRectF(56, 48, max(1, self.width() - 72), max(1, self.height() - 66))

    def _x(self, seconds: float) -> float:
        plot = self._plot()
        return plot.left() + seconds / max(self.editor.duration, 1e-9) * plot.width()

    def _time(self, x: float) -> float:
        plot = self._plot()
        return max(0, min(self.editor.duration, (x - plot.left()) / plot.width() * self.editor.duration))

    def _range(self) -> tuple[float, float]:
        if self._cached_range is not None:
            return self._cached_range
        field = self.editor.current_field
        values = [float(frame.get(field, 0)) for frame in self.editor._frames]
        for left, right in zip(self.editor._frames, self.editor._frames[1:]):
            controls = left.get("bezier", {}).get(field) if isinstance(left.get("bezier"), dict) else None
            if left.get("interpolation") == "bezier" and controls:
                scales = left.get("bezier_value_scale", {})
                delta = float(right.get(field, 0)) - float(left.get(field, 0))
                scale = scales.get(field, delta) if isinstance(scales, dict) else float(scales)
                values.extend([float(left.get(field, 0)) + float(controls[i]) * scale for i in (1, 3)])
        low, high = min(values or [0]), max(values or [0])
        margin = max((high - low) * 0.12, 1 if high == low else 0.1)
        self._cached_range = low - margin, high + margin
        return self._cached_range

    def _y(self, value: float) -> float:
        low, high = self._range()
        return self._plot().bottom() - (value - low) / (high - low) * self._plot().height()

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg, fg = self.palette().base().color(), self.palette().text().color()
        painter.fillRect(self.rect(), bg)
        plot = self._plot()
        grid = QColor(fg)
        grid.setAlpha(42)
        painter.setPen(QPen(grid, 1))
        for step in range(11):
            x = plot.left() + plot.width() * step / 10
            painter.drawLine(QPointF(x, 16), QPointF(x, plot.bottom()))
            if step % 2 == 0:
                painter.setPen(fg)
                painter.drawText(QRectF(x - 28, 0, 56, 16), Qt.AlignmentFlag.AlignCenter,
                                 f"{self.editor.duration * step / 10:.2f}")
                painter.setPen(QPen(grid, 1))
        low, high = self._range()
        rows = 5 if plot.height() >= 68 else 3 if plot.height() >= 32 else 2
        for step in range(rows):
            y = plot.top() + plot.height() * step / (rows - 1)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(fg)
            painter.drawText(QRectF(0, y - 8, 51, 16), Qt.AlignmentFlag.AlignRight, f"{high - (high - low) * step / (rows - 1):.2f}")
            painter.setPen(QPen(grid, 1))
        field = self.editor.current_field
        accent = self.palette().highlight().color()
        if self.editor._frames and field:
            if self._cached_path is None:
                path = QPainterPath()
                for step in range(max(80, int(plot.width())) + 1):
                    seconds = step / max(80, int(plot.width())) * self.editor.duration
                    point = QPointF(self._x(seconds), self._y(sample_track(self.editor._frames, field, seconds)))
                    path.moveTo(point) if step == 0 else path.lineTo(point)
                self._cached_path = path
            painter.setPen(QPen(accent, 2))
            painter.drawPath(self._cached_path)
        for index, frame in enumerate(self.editor._frames):
            x = self._x(float(frame.get("time", 0)))
            selected = index == self.editor.selected_index
            painter.setBrush(accent if selected else self.palette().mid().color())
            painter.setPen(QPen(fg, 1))
            painter.drawPolygon(QPolygonF([QPointF(x, 23), QPointF(x + 6, 29), QPointF(x, 35), QPointF(x - 6, 29)]))
            if field:
                painter.drawEllipse(QPointF(x, self._y(float(frame.get(field, 0)))), 5 if selected else 4, 5 if selected else 4)
        painter.setPen(QPen(QColor("#ef7862"), 1.5))
        x = self._x(self.editor.current_time)
        painter.drawLine(QPointF(x, 16), QPointF(x, plot.bottom()))

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        best = None
        for index, frame in enumerate(self.editor._frames):
            x = self._x(float(frame.get("time", 0)))
            in_lane = abs(pos.x() - x) <= 9 and 20 <= pos.y() <= 39
            in_curve = bool(self.editor.current_field and math.hypot(
                pos.x() - x, pos.y() - self._y(float(frame.get(self.editor.current_field, 0)))) <= 9)
            if in_lane or in_curve:
                best = index, in_curve
                break
        if best is not None:
            self.editor.select_frame(best[0])
            if self.editor.editable:
                self.editor.set_playing(False)
                self._drag = (best[0], best[1], copy.deepcopy(self.editor._frames), self._range())
        else:
            self.editor._seek(self._time(pos.x()))
        self.update()

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._drag is None:
            if event.buttons() & Qt.MouseButton.LeftButton:
                self.editor._seek(self._time(event.position().x()))
            return
        index, with_value, baseline, bounds = self._drag
        frame = self.editor._frames[index]
        seconds = self._time(event.position().x())
        lower = float(baseline[index - 1]["time"]) + 1e-5 if index else 0
        upper = float(baseline[index + 1]["time"]) - 1e-5 if index + 1 < len(baseline) else self.editor.duration
        frame["time"] = round(max(lower, min(upper, seconds)), 5)
        if with_value:
            low, high = bounds
            frame[self.editor.current_field] = round(low + (self._plot().bottom() - event.position().y()) / self._plot().height() * (high - low), 5)
        self.editor._seek(frame["time"])
        self.invalidate_curve()

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._drag:
            baseline = self._drag[2]
            self._drag = None
            if self.editor._frames != baseline:
                self.editor._commit()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        if self.editor.editable:
            self.editor._seek(self._time(event.position().x()))
            self.editor.add_keyframe()

    def keyPressEvent(self, event):  # noqa: N802
        if getattr(self.editor, "external_shortcuts", False):
            super().keyPressEvent(event)
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.editor.delete_selected()
        elif event.key() == Qt.Key.Key_Space:
            self.editor.set_playing(not self.editor.is_playing)
        else:
            super().keyPressEvent(event)

    def resizeEvent(self, event):  # noqa: N802
        self.invalidate_curve()
        super().resizeEvent(event)


class AnimationTimelineEditor(QWidget):
    timeChanged = Signal(float)
    framesEdited = Signal(list)
    playStateChanged = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("animationTimelineEditor")
        self._frames: list[dict] = []
        self._fields: list[str] = []
        self.duration = 1.0
        self.current_time = 0.0
        self.selected_index = -1
        self.editable = False
        self._updating = False
        self._last_tick = 0.0
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self._tick)
        layout = EditorViewportLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        bar = QHBoxLayout()
        bar.setSpacing(4)
        self.play_button = TransparentToolButton(FluentIcon.PLAY, self)
        self.play_button.setFixedSize(26, 28)
        self.loop_check = TransparentToggleToolButton(FluentIcon.SYNC, self)
        self.loop_check.setFixedSize(26, 28)
        self.loop_check.setChecked(True)
        self.time_spin = DoubleSpinBox(self)
        self.time_spin.setDecimals(3)
        self.time_spin.setRange(0, self.duration)
        self.time_spin.setSingleStep(1 / 30)
        self.time_spin.setSuffix(" s")
        self.time_spin.setSymbolVisible(False)
        self.time_spin.setFixedWidth(83)
        self.seek_slider = Slider(Qt.Orientation.Horizontal, self)
        self.seek_slider.setRange(0, 10000)
        self.duration_label = CaptionLabel(self)
        self.duration_label.hide()
        self.field_combo = EditorComboBox(self)
        self.field_combo.setFixedWidth(90)
        self.add_button = TransparentToolButton(FluentIcon.ADD, self)
        self.delete_button = TransparentToolButton(FluentIcon.DELETE, self)
        self.add_button.setFixedSize(24, 28)
        self.delete_button.setFixedSize(24, 28)
        self.interpolation_combo = EditorComboBox(self)
        self.interpolation_combo.setMinimumWidth(106)
        self.interpolation_combo.setMaximumWidth(140)
        for value in ("linear", "stepped", "bezier", "inverse_stepped"):
            self.interpolation_combo.addItem(value, value)
        self.hint = CaptionLabel(self)
        self.hint.hide()
        self.hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.table_check = CheckBox(self)
        self.curve_controls_check = CheckBox(self)
        self.more_button = TransparentToolButton(FluentIcon.MORE, self)
        self.more_button.setFixedSize(24, 28)
        for widget in (self.play_button, self.loop_check, self.time_spin, self.seek_slider,
                       self.add_button, self.delete_button, self.field_combo, self.more_button):
            bar.addWidget(widget, 1 if widget is self.seek_slider else 0)
        layout.addLayout(bar)
        self.canvas = _TimelineCanvas(self)
        layout.addWidget(self.canvas, 1)
        self.options_menu = RoundMenu(parent=self)
        options = QWidget(self.options_menu)
        options.setFixedSize(320, 83)
        options_layout = QVBoxLayout(options)
        options_layout.setContentsMargins(8, 4, 8, 4)
        interpolation_row = QHBoxLayout()
        self.interpolation_label = CaptionLabel(self)
        interpolation_row.addWidget(self.interpolation_label)
        interpolation_row.addWidget(self.interpolation_combo, 1)
        options_layout.addLayout(interpolation_row)
        checks = QHBoxLayout()
        checks.addWidget(self.table_check)
        checks.addWidget(self.curve_controls_check)
        options_layout.addLayout(checks)
        self.options_menu.addWidget(options, selectable=False)
        self.more_button.clicked.connect(lambda: self.options_menu.exec(self.more_button.mapToGlobal(QPoint(0, self.more_button.height()))))
        self.table = TableWidget(self)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setMaximumHeight(144)
        self.table.setMinimumHeight(80)
        self.table.setVisible(False)
        layout.addWidget(self.table)
        self.curve_controls = QWidget(self)
        curves = QGridLayout(self.curve_controls)
        curves.setContentsMargins(0, 0, 0, 0)
        curves.setSpacing(7)
        self.control_spins = []
        for index, name in enumerate(("cx1", "cy1", "cx2", "cy2")):
            row, column = divmod(index, 2)
            curves.addWidget(CaptionLabel(name, self), row, column * 2)
            spin = DoubleSpinBox(self)
            spin.setDecimals(4)
            spin.setSingleStep(0.05)
            spin.setRange(0, 1) if name.startswith("cx") else spin.setRange(-10000, 10000)
            spin.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            spin.setSymbolVisible(False)
            spin.setMinimumWidth(75)
            curves.addWidget(spin, row, column * 2 + 1)
            self.control_spins.append(spin)
        self.apply_curve_button = PushButton(FluentIcon.ACCEPT, "", self)
        curves.addWidget(self.apply_curve_button, 2, 0, 1, 4)
        self.curve_controls.setVisible(False)
        layout.addWidget(self.curve_controls)
        self.play_button.clicked.connect(lambda: self.set_playing(not self.is_playing))
        self.time_spin.valueChanged.connect(self._seek)
        self.seek_slider.valueChanged.connect(lambda value: self._seek(self.duration * value / 10000) if not self._updating else None)
        self.field_combo.currentIndexChanged.connect(self._field_changed)
        self.add_button.clicked.connect(self.add_keyframe)
        self.delete_button.clicked.connect(self.delete_selected)
        self.table.itemChanged.connect(self._table_edited)
        self.table.itemSelectionChanged.connect(self._table_selected)
        self.table_check.toggled.connect(self.table.setVisible)
        self.curve_controls_check.toggled.connect(self.curve_controls.setVisible)
        self.interpolation_combo.currentIndexChanged.connect(self._interpolation_changed)
        self.apply_curve_button.clicked.connect(self.apply_curve)
        self.retranslate_ui()
        self.set_track([], 1, [], editable=False)

    def set_allowed_interpolations(self, allowed: list[str]):
        self._updating = True
        self.interpolation_combo.clear()
        for value in allowed:
            self.interpolation_combo.addItem(_text(f"editor.timeline.{value}"), value)
        self._updating = False
        self._refresh_controls()

    @property
    def frames(self) -> list[dict]:
        return copy.deepcopy(self._frames)

    @property
    def current_field(self) -> str:
        return str(self.field_combo.currentData() or "")

    @property
    def is_playing(self) -> bool:
        return self.timer.isActive()

    def set_track(self, frames: list[dict], duration: float, fields: list[str], editable: bool = True):
        self._updating = True
        selected_time = self._frames[self.selected_index].get("time") if 0 <= self.selected_index < len(self._frames) else None
        self._frames = copy.deepcopy(frames)
        self.duration = max(0.001, float(duration)) if math.isfinite(float(duration)) else 1.0
        self._fields = list(fields)
        self.editable = bool(editable and fields)
        previous_field = self.current_field
        self.field_combo.clear()
        for field in self._fields:
            self.field_combo.addItem(field, field)
        self.field_combo.setCurrentIndex(max(0, self.field_combo.findData(previous_field)))
        self.time_spin.setMaximum(self.duration)
        self.duration_label.setText(f"/ {self.duration:.3f} s")
        self.selected_index = next((i for i, f in enumerate(self._frames) if f.get("time") == selected_time), -1)
        self._updating = False
        self._refresh_table()
        self.set_time(self.current_time)
        self._refresh_controls()
        self.canvas.invalidate_curve()

    def set_time(self, seconds: float):
        seconds = float(seconds)
        self.current_time = max(0, min(self.duration, seconds)) if math.isfinite(seconds) else 0
        previous = self._updating
        self._updating = True
        self.time_spin.setValue(self.current_time)
        self.seek_slider.setValue(round(self.current_time / self.duration * 10000))
        self._updating = previous
        self.canvas.update()

    def _seek(self, seconds: float):
        if self._updating:
            return
        self.set_time(seconds)
        self.timeChanged.emit(self.current_time)

    def set_playing(self, playing: bool):
        playing = bool(playing)
        if playing == self.is_playing:
            return
        if playing:
            if self.current_time >= self.duration:
                self._seek(0)
            self._last_tick = time.monotonic()
            self.timer.start()
        else:
            self.timer.stop()
        self.play_button.setIcon(FluentIcon.PAUSE if playing else FluentIcon.PLAY)
        self.play_button.setToolTip(_text("editor.timeline.pause" if playing else "editor.timeline.play"))
        self.playStateChanged.emit(playing)

    def _tick(self):
        now = time.monotonic()
        seconds = self.current_time + min(now - self._last_tick, 0.25)
        self._last_tick = now
        if seconds > self.duration:
            if self.loop_check.isChecked():
                seconds %= self.duration
            else:
                seconds = self.duration
                self.set_playing(False)
        self._seek(seconds)

    def select_frame(self, index: int):
        self.selected_index = index if 0 <= index < len(self._frames) else -1
        self._updating = True
        if self.selected_index >= 0:
            self.table.selectRow(self.selected_index)
        else:
            self.table.clearSelection()
        self._updating = False
        self._refresh_controls()
        self.canvas.update()

    def add_keyframe(self):
        if not self.editable:
            return
        self.set_playing(False)
        seconds = round(self.current_time, 5)
        existing = next((i for i, f in enumerate(self._frames) if abs(float(f.get("time", 0)) - seconds) < 1e-5), None)
        if existing is not None:
            self.select_frame(existing)
            return
        frame = {"time": seconds, "interpolation": "linear"}
        for field in self._fields:
            frame[field] = sample_track(self._frames, field, seconds)
        self._frames.append(frame)
        self._frames.sort(key=lambda item: item["time"])
        self.selected_index = self._frames.index(frame)
        self._commit()

    def delete_selected(self):
        if not self.editable or not 0 <= self.selected_index < len(self._frames):
            return
        self.set_playing(False)
        self._frames.pop(self.selected_index)
        self.selected_index = min(self.selected_index, len(self._frames) - 1)
        self._commit()

    def _refresh_table(self):
        self._updating = True
        self.table.setColumnCount(len(self._fields) + 2)
        self.table.setHorizontalHeaderLabels([_text("editor.timeline.time"), *self._fields, _text("editor.timeline.interpolation")])
        self.table.setRowCount(len(self._frames))
        for row, frame in enumerate(self._frames):
            for column, field in enumerate(["time", *self._fields, "interpolation"]):
                value = frame.get(field, "linear" if field == "interpolation" else 0)
                text = str(value) if field == "interpolation" else f"{float(value):.6g}"
                item = QTableWidgetItem(text)
                if not self.editable or field == "interpolation":
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        if self.selected_index >= 0:
            self.table.selectRow(self.selected_index)
        self._updating = False

    def _table_selected(self):
        if not self._updating:
            self.selected_index = self.table.currentRow()
            self._refresh_controls()
            self.canvas.update()

    def _table_edited(self, item: QTableWidgetItem):
        if self._updating or not self.editable:
            return
        frame = self._frames[item.row()]
        field = ["time", *self._fields, "interpolation"][item.column()]
        try:
            value = float(item.text())
            if not math.isfinite(value):
                raise ValueError()
            if field == "time" and (not 0 <= value <= self.duration or any(
                    f is not frame and abs(float(f.get("time", 0)) - value) < 1e-5 for f in self._frames)):
                raise ValueError()
        except ValueError:
            self._refresh_table()
            self.hint.setText(_text("editor.timeline.invalid"))
            return
        frame[field] = value
        self._frames.sort(key=lambda f: float(f.get("time", 0)))
        self.selected_index = self._frames.index(frame)
        self.set_playing(False)
        self._commit()

    def _field_changed(self):
        if not self._updating:
            self._refresh_controls()
            self.canvas.invalidate_curve()

    def _refresh_controls(self):
        selected = 0 <= self.selected_index < len(self._frames)
        self.add_button.setEnabled(self.editable)
        self.delete_button.setEnabled(self.editable and selected)
        self.interpolation_combo.setEnabled(self.editable and selected)
        hint = _text("editor.timeline.readonly" if not self.editable else "editor.timeline.hint" if self._frames else "editor.timeline.empty")
        self.hint.setToolTip(hint)
        self.canvas.setToolTip(hint)
        self._updating = True
        frame = self._frames[self.selected_index] if selected else {}
        self.hint.setText(_text("editor.timeline.selection", index=self.selected_index + 1, time=f"{float(frame.get('time', 0)):.3f}") if selected else hint)
        interpolation = frame.get("interpolation", "linear")
        self.interpolation_combo.setCurrentIndex(max(0, self.interpolation_combo.findData(interpolation)))
        controls = frame.get("bezier", {}).get(self.current_field, [0.25, 0.25, 0.75, 0.75]) if isinstance(frame.get("bezier", {}), dict) else [0.25, 0.25, 0.75, 0.75]
        if isinstance(frame.get("curve"), list) and len(frame["curve"]) == 4 and not frame.get("bezier"):
            controls = frame["curve"]
        enabled = bool(self.editable and selected and interpolation == "bezier" and self.selected_index + 1 < len(self._frames))
        for spin, value in zip(self.control_spins, controls):
            spin.setValue(float(value))
            spin.setEnabled(enabled)
        self.apply_curve_button.setEnabled(enabled)
        self._updating = False

    def _interpolation_changed(self):
        if self._updating or not self.editable or not 0 <= self.selected_index < len(self._frames):
            return
        frame = self._frames[self.selected_index]
        interpolation = self.interpolation_combo.currentData()
        # Inverse stepped data is retained for readers that support it, but
        # changing to it must be explicitly accepted by the model's encoder.
        frame["interpolation"] = interpolation
        if interpolation == "bezier":
            self.curve_controls_check.setChecked(True)
            controls = frame.setdefault("bezier", {})
            for field in self._fields:
                controls.setdefault(field, [0.25, 0.25, 0.75, 0.75])
        else:
            frame.pop("bezier", None)
            frame.pop("bezier_value_scale", None)
            frame.pop("curve", None)
        self._commit()

    def apply_curve(self):
        if not self.apply_curve_button.isEnabled():
            return
        controls = [spin.value() for spin in self.control_spins]
        if controls[0] > controls[2]:
            self.hint.setText(_text("editor.timeline.invalid"))
            return
        frame = self._frames[self.selected_index]
        bezier = frame.setdefault("bezier", {})
        fields = self._fields if frame.get("bezier_shared") else [self.current_field]
        for field in fields:
            bezier[field] = list(controls)
        self._commit()

    def _commit(self):
        self._refresh_table()
        self._refresh_controls()
        self.canvas.invalidate_curve()
        self.framesEdited.emit(self.frames)

    def retranslate_ui(self):
        self.play_button.setToolTip(_text("editor.timeline.pause" if self.is_playing else "editor.timeline.play"))
        self.add_button.setToolTip(action_text("editor.actions.keyframe"))
        self.add_button.setAccessibleName(_text("editor.timeline.add"))
        self.delete_button.setToolTip(_text("editor.timeline.delete"))
        self.loop_check.setToolTip(_text("editor.timeline.loop"))
        self.loop_check.setAccessibleName(_text("editor.timeline.loop"))
        self.time_spin.setToolTip(_text("editor.timeline.time"))
        self.more_button.setToolTip(_text("editor.timeline.interpolation") + " · " + _text("editor.timeline.table") + " · " + _text("editor.timeline.controls"))
        self.more_button.setAccessibleName(self.more_button.toolTip())
        self.interpolation_label.setText(_text("editor.timeline.interpolation"))
        self.apply_curve_button.setText(_text("editor.timeline.apply_curve"))
        self.table_check.setText(_text("editor.timeline.table"))
        self.curve_controls_check.setText(_text("editor.timeline.controls"))
        self._updating = True
        for index in range(self.interpolation_combo.count()):
            self.interpolation_combo.setItemText(index, _text(f"editor.timeline.{self.interpolation_combo.itemData(index)}"))
        self._updating = False
        self._refresh_table()
        self._refresh_controls()

    def hideEvent(self, event):  # noqa: N802
        self.set_playing(False)
        super().hideEvent(event)


__all__ = ["AnimationTimelineEditor", "sample_track", "TIMELINE_TEXT"]
