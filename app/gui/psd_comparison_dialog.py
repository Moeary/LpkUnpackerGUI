"""Read-only before/after inspection; expensive rendering stays in a child process."""
from __future__ import annotations

import copy
import tempfile
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QThread, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QHBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CheckBox, ComboBox, PushButton, Slider

from app.core.psd_worker import run_psd_job
from app.gui.editor_dialogs import ThemedEditorDialog
from app.i18n import tr


class ComparisonWorker(QThread):
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, request, parent=None):
        super().__init__(parent)
        self.request = copy.deepcopy(request)

    def run(self):
        try:
            with tempfile.TemporaryDirectory(prefix="lpk-comparison-") as temporary:
                report = run_psd_job(dict(self.request, operation="compare"), Path(temporary),
                                     cancelled=self.isInterruptionRequested)
                for view in report["views"]:
                    for key in ("before", "after", "highlight"):
                        view[key] = QImage(view[key])
                        if view[key].isNull():
                            raise ValueError("Comparison image could not be loaded.")
                self.ready.emit(report)
        except InterruptedError:
            pass
        except Exception as exc:
            self.failed.emit(str(exc))


class ComparisonCanvas(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(280, 220)
        self.view = None
        self.mode = "side"
        self.highlight = False
        self.fraction = .5
        self.zoom = 1.
        self.pan = QPointF()
        self._drag = None

    def fit(self):
        self.zoom, self.pan = 1., QPointF()
        self.update()

    def wheelEvent(self, event):
        self.zoom = min(16., max(1., self.zoom * (1.2 if event.angleDelta().y() > 0 else 1 / 1.2)))
        if self.zoom == 1:
            self.pan = QPointF()
        self.update()
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.position()

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            self.pan += event.position() - self._drag
            self._drag = event.position()
            self.update()

    def mouseReleaseEvent(self, event):
        self._drag = None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#353940"))
        for y in range(0, self.height(), 18):
            for x in range(0, self.width(), 18):
                if (x // 18 + y // 18) % 2 == 0:
                    painter.fillRect(x, y, 18, 18, QColor("#424750"))
        if self.view is None:
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self.zoom <= 1)
        image = self.view["before"]

        def draw(key, area, overlay=False):
            scale = min(area.width() / image.width(), area.height() / image.height()) * self.zoom
            target = QRectF(0, 0, image.width() * scale, image.height() * scale)
            target.moveCenter(area.center() + self.pan)
            painter.save()
            painter.setClipRect(area, Qt.ClipOperation.IntersectClip)
            painter.drawImage(target, self.view[key])
            if overlay:
                painter.drawImage(target, self.view["highlight"])
            painter.restore()

        bounds = QRectF(self.rect())
        if self.mode == "side":
            half = bounds.width() / 2
            draw("before", QRectF(0, 0, half - 2, bounds.height()))
            draw("after", QRectF(half + 2, 0, half - 2, bounds.height()), self.highlight)
        elif self.mode == "wipe":
            draw("before", bounds)
            split = bounds.width() * self.fraction
            painter.save()
            painter.setClipRect(QRectF(split, 0, bounds.width() - split, bounds.height()))
            draw("after", bounds, self.highlight)
            painter.restore()
            painter.setPen(QColor("#2dd4d9"))
            painter.drawLine(int(split), 0, int(split), self.height())
        else:
            draw("after", bounds, True)


class PsdComparisonDialog(ThemedEditorDialog):
    def __init__(self, request, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("psd.compare.title"))
        self.setMaximumWidth(16777215)
        self.resize(1050, 780)
        self._closing = False
        self.report = None
        self.yesButton.hide()
        self.cancelButton.setText(tr("editor.dialogs.close"))
        self.view_combo = ComboBox(self)
        self.mode_combo = ComboBox(self)
        for key in ("side", "wipe", "difference"):
            self.mode_combo.addItem(tr("psd.compare." + key), userData=key)
        self.highlight = CheckBox(tr("psd.compare.highlight"), self)
        self.fit_button = PushButton(tr("psd.compare.fit"), self)
        self.controls = QHBoxLayout()
        for control in (self.view_combo, self.mode_combo, self.highlight, self.fit_button):
            self.controls.addWidget(control)
        self.viewLayout.addLayout(self.controls)
        self.legend = BodyLabel(tr("psd.compare.legend"), self)
        self.legend.setWordWrap(True)
        self.viewLayout.addWidget(self.legend)
        self.canvas = ComparisonCanvas(self)
        self.viewLayout.addWidget(self.canvas, 1)
        self.slider = Slider(Qt.Orientation.Horizontal, self)
        self.slider.setRange(0, 100)
        self.slider.setValue(50)
        self.slider.hide()
        self.viewLayout.addWidget(self.slider)
        self.status = BodyLabel(tr("psd.compare.loading"), self)
        self.status.setWordWrap(True)
        self.viewLayout.addWidget(self.status)
        self.warning = BodyLabel(self)
        self.warning.setWordWrap(True)
        self.viewLayout.addWidget(self.warning)
        self.view_combo.currentIndexChanged.connect(self.select_view)
        self.mode_combo.currentIndexChanged.connect(self.update_display)
        self.highlight.toggled.connect(self.update_display)
        self.slider.valueChanged.connect(self.update_display)
        self.fit_button.clicked.connect(self.canvas.fit)
        self.worker = ComparisonWorker(request, self)
        self.worker.ready.connect(self.loaded)
        self.worker.failed.connect(self.status.setText)
        self.worker.finished.connect(self.worker_finished)
        self.worker.start()

    def loaded(self, report):
        self.report = report
        for index, view in enumerate(report["views"]):
            self.view_combo.addItem(tr("psd.compare.pose") if view["label"] == "pose"
                                    else view["label"], userData=index)
        self.warning.setText("\n".join(report["warnings"]))
        self.select_view()

    def select_view(self, *_args):
        if not self.report or not self.report["views"]:
            return
        view = self.report["views"][self.view_combo.currentIndex()]
        self.canvas.view = view
        self.canvas.fit()
        affected = ", ".join(view["affected_ids"][:12]) or tr("psd.compare.none")
        if len(view["affected_ids"]) > 12:
            affected += f" … (+{len(view['affected_ids']) - 12})"
        self.status.setText(tr("psd.compare.stats", count=view["changed_pixels"],
                               total=view["total_pixels"], ids=affected))
        self.status.setToolTip(", ".join(view["affected_ids"]))
        self.legend.setText(tr("psd.compare.pose_hint" if view["label"] == "pose" else "psd.compare.legend"))

    def update_display(self, *_args):
        self.canvas.mode = self.mode_combo.currentData()
        self.canvas.highlight = self.highlight.isChecked()
        self.canvas.fraction = self.slider.value() / 100
        self.slider.setVisible(self.canvas.mode == "wipe")
        self.canvas.update()

    def reject(self):
        if self.worker.isRunning():
            self._closing = True
            self.worker.requestInterruption()
            return
        super().reject()

    def closeEvent(self, event):
        if self.worker.isRunning():
            event.ignore()
            self.reject()
        else:
            super().closeEvent(event)

    def worker_finished(self):
        if self._closing:
            super().reject()
