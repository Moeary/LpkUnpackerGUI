"""Animation export shared by the Spine and Live2D editors.

An editor supplies one callback that poses its preview at a time and renders
an off-screen image; this module owns the options dialog, the render loop on
the GUI thread (which owns the GL context) and the background encoder.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QFileDialog, QFormLayout, QHBoxLayout
from qfluentwidgets import BodyLabel, CaptionLabel, CheckBox, SpinBox

from app.core.animation_export import (
    MAX_MEMORY_BYTES, SUFFIXES, estimated_memory, frame_times, write_animation,
)
from app.gui.editor_dialogs import ThemedEditorDialog
from app.gui.editor_workspace import EditorComboBox
from app.i18n import tr

MAX_EXPORT_SIZE = 4096

ANIMATION_EXPORT_TEXT = {
    "editor.animation_export.title": "导出动画",
    "editor.animation_export.hint": "把当前动作导出为 GIF、WebP 或 PNG 序列，便于分享展示；画面与预览的缩放、平移和隐藏部件一致",
    "editor.animation_export.format": "格式",
    "editor.animation_export.png_sequence": "PNG 序列（文件夹）",
    "editor.animation_export.fps": "帧率 (fps)",
    "editor.animation_export.size": "尺寸 (像素)",
    "editor.animation_export.keep_aspect": "保持预览比例",
    "editor.animation_export.transparent": "透明背景",
    "editor.animation_export.transparent_gif": "GIF 只有全透明或不透明两种像素，柔和边缘会变硬；需要半透明请用 WebP 或 PNG",
    "editor.animation_export.range": "只导出播放区间 ({start} – {end} 秒)",
    "editor.animation_export.summary": "{count} 帧 · 时长 {seconds} 秒 · 播放速度 {speed} · 约占内存 {memory} MB",
    "editor.animation_export.too_large": "帧数或尺寸过大（约需 {memory} MB 内存），请降低帧率、尺寸或缩短区间",
    "editor.animation_export.save_dialog": "保存动画",
    "editor.animation_export.needs_animation": "请先选择一个动作再导出动画",
    "editor.animation_export.unavailable": "预览尚未就绪，无法渲染动画帧",
    "editor.animation_export.rendering": "正在渲染第 {index} / {count} 帧…",
    "editor.animation_export.encoding": "正在编码 {count} 帧…",
    "editor.animation_export.done": "已导出动画：{path}",
    "editor.animation_export.failed": "导出动画失败：{error}",
}


def export_text(key: str, **values) -> str:
    key = "editor.animation_export." + key
    return tr(key, ANIMATION_EXPORT_TEXT[key], **values)


class AnimationExportWorker(QThread):
    """Encode already rendered frames off the GUI thread."""

    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, frames, output: str, options: dict, parent=None):
        super().__init__(parent)
        self.frames, self.output, self.options = frames, output, options

    def run(self):
        try:
            path = write_animation(self.frames, self.output, self.options["format"], self.options["fps"],
                                   transparent=self.options["transparent"], background=self.options["background"],
                                   cancelled=self.isInterruptionRequested)
            self.completed.emit(str(path))
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.frames = None


class AnimationExportDialog(ThemedEditorDialog):
    """Format, frame rate, size, background and range of an animation export."""

    def __init__(self, parent, *, size: tuple[int, int], duration: float, play_range, speed: float):
        super().__init__(parent)
        self.setWindowTitle(export_text("title"))
        self.setMinimumWidth(400)
        self._duration, self._play_range, self._speed = duration, play_range, speed
        self._aspect = size[0] / max(1, size[1])
        layout = self.viewLayout
        form = QFormLayout()
        form.setSpacing(10)
        self.format_combo = EditorComboBox(self)
        for fmt, label in (("gif", "GIF"), ("webp", "WebP"), ("png", export_text("png_sequence"))):
            self.format_combo.addItem(label, fmt)
        form.addRow(BodyLabel(export_text("format"), self), self.format_combo)
        self.fps_spin = SpinBox(self)
        self.fps_spin.setRange(1, 60)
        self.fps_spin.setValue(30)
        form.addRow(BodyLabel(export_text("fps"), self), self.fps_spin)
        size_row = QHBoxLayout()
        self.width_spin, self.height_spin = SpinBox(self), SpinBox(self)
        for spin, value in ((self.width_spin, size[0]), (self.height_spin, size[1])):
            spin.setRange(16, MAX_EXPORT_SIZE)
            spin.setValue(int(value))
            size_row.addWidget(spin, 1)
        form.addRow(BodyLabel(export_text("size"), self), size_row)
        layout.addLayout(form)
        self.aspect_check = CheckBox(export_text("keep_aspect"), self)
        self.aspect_check.setChecked(True)
        self.transparent_check = CheckBox(export_text("transparent"), self)
        self.range_check = CheckBox(self)
        if play_range is not None:
            self.range_check.setText(export_text("range", start=f"{play_range[0]:.3f}", end=f"{play_range[1]:.3f}"))
            self.range_check.setChecked(True)
        else:
            self.range_check.hide()
        for widget in (self.aspect_check, self.transparent_check, self.range_check):
            layout.addWidget(widget)
        self.summary = CaptionLabel(self)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self._following = False
        self.width_spin.valueChanged.connect(lambda value: self._follow_aspect(self.height_spin, value / self._aspect))
        self.height_spin.valueChanged.connect(lambda value: self._follow_aspect(self.width_spin, value * self._aspect))
        self.format_combo.currentIndexChanged.connect(self._format_changed)
        for signal in (self.fps_spin.valueChanged, self.width_spin.valueChanged, self.height_spin.valueChanged,
                       self.range_check.toggled):
            signal.connect(lambda *_args: self._refresh_summary())
        self.yesButton.setText(export_text("title"))
        self._format_changed()

    def retranslate_ui(self, *_args):
        super().retranslate_ui()
        self.yesButton.setText(export_text("title"))

    def _follow_aspect(self, spin, value: float):
        if self._following or not self.aspect_check.isChecked():
            return
        self._following = True
        spin.setValue(max(16, round(value)))
        self._following = False

    def _format_changed(self, *_args):
        gif = self.options()["format"] == "gif"
        # Hard GIF edges look worse than a solid background, so GIF starts opaque.
        self.transparent_check.setChecked(not gif)
        self.transparent_check.setToolTip(export_text("transparent_gif") if gif else "")
        self._refresh_summary()

    def options(self) -> dict:
        play_range = self._play_range if self._play_range is not None and self.range_check.isChecked() else None
        start, end = play_range or (0.0, self._duration)
        return {"format": self.format_combo.currentData() or "gif", "fps": self.fps_spin.value(),
                "width": self.width_spin.value(), "height": self.height_spin.value(),
                "transparent": self.transparent_check.isChecked(), "start": start, "end": end, "speed": self._speed}

    def _refresh_summary(self):
        options = self.options()
        count = len(frame_times(options["start"], options["end"], options["fps"], options["speed"]))
        memory = estimated_memory(count, options["width"], options["height"])
        megabytes = max(1, round(memory / 1024 ** 2))
        too_large = memory > MAX_MEMORY_BYTES
        self.summary.setText(export_text("too_large", memory=megabytes) if too_large else
                             export_text("summary", count=count, memory=megabytes,
                                         seconds=f"{count / options['fps']:.2f}", speed=f"{options['speed']:g}×"))
        self.yesButton.setEnabled(not too_large)


def ask_output(parent, fmt: str, directory: Path, name: str) -> str:
    """Ask where to write: a file for GIF / WebP, a new folder for PNG frames."""
    if fmt == "png":
        folder = QFileDialog.getExistingDirectory(parent, export_text("save_dialog"), str(directory))
        return str(Path(folder) / f"{name}_frames") if folder else ""
    filters = {"gif": "GIF (*.gif)", "webp": "WebP (*.webp)"}[fmt]
    output, _filter = QFileDialog.getSaveFileName(parent, export_text("save_dialog"),
                                                  str(directory / f"{name}{SUFFIXES[fmt]}"), filters)
    return output


class AnimationExportController(QObject):
    """Render frames through ``render_frame`` and encode them in the background.

    ``render_frame(seconds, width, height, transparent)`` poses the preview at
    an animation time and returns a QImage (or None when it cannot render);
    ``restore()`` returns the preview to the editor's own time afterwards.
    """

    busyChanged = Signal(bool)
    message = Signal(str)

    def __init__(self, parent, *, render_frame: Callable, restore: Callable[[], None],
                 background: Callable[[], tuple[int, int, int]]):
        super().__init__(parent)
        self._render_frame, self._restore, self._background = render_frame, restore, background
        self._rendering = False
        self.worker: AnimationExportWorker | None = None

    @property
    def busy(self) -> bool:
        return self._rendering or self.worker is not None

    def start(self, output: str, options: dict) -> bool:
        """Render every frame now, then encode on a worker; True when encoding started."""
        if self.busy or not output:
            return False
        try:
            frames = self._render(options)
        except Exception as exc:
            self.message.emit(export_text("failed", error=exc))
            return False
        options = dict(options, background=tuple(self._background()))
        worker = AnimationExportWorker(frames, output, options, self)
        worker.completed.connect(lambda path: self.message.emit(export_text("done", path=path)))
        worker.failed.connect(lambda error: self.message.emit(export_text("failed", error=error)))
        worker.finished.connect(lambda w=worker: self._finished(w))
        self.worker = worker
        self.message.emit(export_text("encoding", count=len(frames)))
        self.busyChanged.emit(True)
        worker.start()
        return True

    def _render(self, options: dict) -> list:
        from PIL import Image
        from PySide6.QtCore import QEventLoop
        from PySide6.QtWidgets import QApplication
        times = frame_times(options["start"], options["end"], options["fps"], options["speed"])
        width, height = int(options["width"]), int(options["height"])
        memory = estimated_memory(len(times), width, height)
        if memory > MAX_MEMORY_BYTES:
            raise ValueError(export_text("too_large", memory=round(memory / 1024 ** 2)))
        frames = []
        self._rendering = True
        self.busyChanged.emit(True)
        try:
            for index, seconds in enumerate(times):
                image = self._render_frame(seconds, width, height, options["transparent"])
                if image is None or image.isNull():
                    raise RuntimeError(export_text("unavailable"))
                image = image.convertToFormat(QImage.Format.Format_RGBA8888)
                frames.append(Image.frombytes("RGBA", (image.width(), image.height()), bytes(image.constBits()),
                                              "raw", "RGBA", image.bytesPerLine()))
                if index % 5 == 0:
                    self.message.emit(export_text("rendering", index=index + 1, count=len(times)))
                    # Paint progress only; edits must wait for the frames.
                    QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        finally:
            self._rendering = False
            self._restore()
            self.busyChanged.emit(self.busy)
        return frames

    def _finished(self, worker):
        if self.worker is worker:
            self.worker = None
        worker.deleteLater()
        self.busyChanged.emit(self.busy)

    def shutdown(self):
        worker = self.worker
        if worker is not None:
            worker.requestInterruption()
            worker.wait()


__all__ = [
    "ANIMATION_EXPORT_TEXT", "AnimationExportController", "AnimationExportDialog", "AnimationExportWorker",
    "ask_output", "export_text",
]
