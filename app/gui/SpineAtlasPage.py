"""Standalone Spine atlas extraction and write-back workspace."""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    SubtitleLabel,
)

from app.core.spine_atlas import (
    SpineAtlasExportResult,
    extract_spine_atlas,
    parse_atlas,
    writeback_spine_atlas,
)
from app.i18n import get_i18n, tr


class _DropFrame(QFrame):
    pathsDropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.toLocalFile()]
        if paths:
            self.pathsDropped.emit([str(path) for path in paths])
            event.acceptProposedAction()
        else:
            event.ignore()


class _AtlasCanvas(QLabel):
    """Draw one atlas page with the selected physical region highlighted."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap = QPixmap()
        self._region = None
        self.setMinimumSize(260, 220)
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_page(self, path: str | Path, region=None):
        self._pixmap = QPixmap(str(path))
        self._region = region
        self.update()

    def clear_page(self):
        self._pixmap = QPixmap()
        self._region = None
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().base())
        if self._pixmap.isNull():
            painter.end()
            super().paintEvent(event)
            return
        scaled = self._pixmap.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        left = (self.width() - scaled.width()) // 2
        top = (self.height() - scaled.height()) // 2
        painter.drawPixmap(left, top, scaled)
        region = self._region
        if region is not None and self._pixmap.width() > 0 and self._pixmap.height() > 0:
            sx = scaled.width() / self._pixmap.width()
            sy = scaled.height() / self._pixmap.height()
            packed_width, packed_height = region.physical_size
            rect = (
                left + int(round(region.x * sx)),
                top + int(round(region.y * sy)),
                max(1, int(round(packed_width * sx))),
                max(1, int(round(packed_height * sy))),
            )
            pen = QPen(QColor(255, 70, 40), 2)
            painter.setPen(pen)
            painter.drawRect(*rect)
        painter.end()


class _SpineAtlasWorker(QThread):
    resultReady = Signal(object)
    failed = Signal(str)
    progress = Signal(int, str)

    def __init__(self, operation: str, **kwargs):
        super().__init__()
        self.operation = operation
        self.kwargs = kwargs

    def run(self):
        try:
            self.kwargs["progress"] = self.progress.emit
            if self.operation == "extract":
                result = extract_spine_atlas(**self.kwargs)
            else:
                result = writeback_spine_atlas(**self.kwargs)
            self.resultReady.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class SpineAtlasPage(_DropFrame):
    """A non-runtime page for inspecting and editing Spine atlas pages."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("spineAtlasPage")
        self.i18n = get_i18n()
        self._worker = None
        self._atlas_path: Path | None = None
        self._result: SpineAtlasExportResult | None = None
        self._page_paths: dict[int, Path] = {}
        self._regions_by_id = {}
        self._build_ui()
        self.retranslate_ui()
        self.i18n.languageChanged.connect(self.retranslate_ui)
        self.pathsDropped.connect(self._accept_dropped_paths)

    def _build_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(16, 16, 16, 16)
        self.main_layout.setSpacing(10)

        header = QHBoxLayout()
        self.title_label = SubtitleLabel(self)
        self.description_label = CaptionLabel(self)
        self.description_label.setWordWrap(True)
        header_text = QVBoxLayout()
        header_text.addWidget(self.title_label)
        header_text.addWidget(self.description_label)
        header.addLayout(header_text, 1)
        self.main_layout.addLayout(header)

        source_row = QHBoxLayout()
        self.source_label = BodyLabel(self)
        self.source_edit = LineEdit(self)
        self.source_edit.setReadOnly(True)
        self.source_edit.setPlaceholderText(".atlas")
        self.source_browse = PushButton(self)
        self.source_browse.clicked.connect(self._browse_atlas)
        self.folder_browse = PushButton(self)
        self.folder_browse.clicked.connect(self._browse_folder)
        source_row.addWidget(self.source_label)
        source_row.addWidget(self.source_edit, 1)
        source_row.addWidget(self.source_browse)
        source_row.addWidget(self.folder_browse)
        self.main_layout.addLayout(source_row)

        output_row = QHBoxLayout()
        self.output_label = BodyLabel(self)
        self.output_edit = LineEdit(self)
        self.output_browse = PushButton(self)
        self.output_browse.clicked.connect(self._browse_output)
        output_row.addWidget(self.output_label)
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(self.output_browse)
        self.main_layout.addLayout(output_row)

        actions = QHBoxLayout()
        self.psd_checkbox = CheckBox(self)
        self.extract_button = PrimaryPushButton(self)
        self.extract_button.clicked.connect(self._extract)
        self.writeback_button = PushButton(self)
        self.writeback_button.clicked.connect(self._writeback)
        actions.addWidget(self.psd_checkbox)
        actions.addWidget(self.extract_button)
        actions.addWidget(self.writeback_button)
        actions.addStretch(1)
        self.main_layout.addLayout(actions)

        self.splitter = QSplitter(Qt.Horizontal, self)
        self.region_list = QListWidget(self.splitter)
        self.region_list.currentItemChanged.connect(self._region_selected)
        right = QSplitter(Qt.Vertical, self.splitter)
        self.canvas = _AtlasCanvas(right)
        self.part_preview = QLabel(right)
        self.part_preview.setAlignment(Qt.AlignCenter)
        self.part_preview.setMinimumSize(180, 140)
        self.part_preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.splitter.addWidget(self.region_list)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 3)
        self.main_layout.addWidget(self.splitter, 1)

        self.status_label = CaptionLabel(self)
        self.warning_edit = QTextEdit(self)
        self.warning_edit.setReadOnly(True)
        self.warning_edit.setMaximumHeight(90)
        self.main_layout.addWidget(self.status_label)
        self.main_layout.addWidget(self.warning_edit)

    def retranslate_ui(self):
        self.title_label.setText(tr("spine.title"))
        self.description_label.setText(tr("spine.description"))
        self.source_label.setText(tr("spine.atlas"))
        self.output_label.setText(tr("spine.output"))
        self.source_browse.setText(tr("spine.browse_atlas"))
        self.folder_browse.setText(tr("spine.browse_folder"))
        self.output_browse.setText(tr("spine.browse_output"))
        self.psd_checkbox.setText(tr("spine.export_psd"))
        self.extract_button.setText(tr("spine.extract"))
        self.writeback_button.setText(tr("spine.writeback"))
        if not self.status_label.text():
            self.status_label.setText(tr("spine.drop_hint"))

    def updateUIScale(self, width: int, height: int):
        del width, height

    def _browse_atlas(self):
        path, _ = QFileDialog.getOpenFileName(self, tr("spine.choose_atlas"), "", tr("spine.atlas_filter"))
        if path:
            self._set_atlas_path(Path(path))

    def _browse_folder(self):
        path = QFileDialog.getExistingDirectory(self, tr("spine.choose_folder"))
        if path:
            self._set_atlas_path(self._find_atlas(Path(path)))

    def _browse_output(self):
        path = QFileDialog.getExistingDirectory(self, tr("spine.choose_output"))
        if path:
            self.output_edit.setText(path)

    def _accept_dropped_paths(self, paths: list[str]):
        if paths:
            self._set_atlas_path(self._find_atlas(Path(paths[0])))

    @staticmethod
    def _find_atlas(path: Path) -> Path:
        path = path.resolve()
        if path.is_file():
            return path
        if not path.is_dir():
            return path
        candidates = sorted(path.rglob("*.atlas"))
        if candidates:
            return candidates[0]
        for candidate in sorted(item for item in path.rglob("*") if item.is_file()):
            try:
                if "size:" in candidate.read_text(encoding="utf-8-sig", errors="ignore")[:4096]:
                    return candidate
            except OSError:
                continue
        return path

    def _set_atlas_path(self, path: Path):
        self._atlas_path = path.resolve()
        self.source_edit.setText(str(self._atlas_path))
        if not self.output_edit.text():
            self.output_edit.setText(str(self._atlas_path.parent / "spine_atlas_export"))
        self.status_label.setText(tr("spine.ready"))

    def _set_busy(self, busy: bool):
        self.extract_button.setEnabled(not busy)
        self.writeback_button.setEnabled(not busy)
        self.source_browse.setEnabled(not busy)
        self.folder_browse.setEnabled(not busy)
        self.output_browse.setEnabled(not busy)
        self.psd_checkbox.setEnabled(not busy)

    def _extract(self):
        if self._atlas_path is None or not self._atlas_path.is_file():
            self.status_label.setText(tr("spine.error_no_atlas"))
            return
        output = Path(self.output_edit.text().strip() or (self._atlas_path.parent / "spine_atlas_export"))
        self.output_edit.setText(str(output))
        self.warning_edit.clear()
        self._set_busy(True)
        self.status_label.setText(tr("spine.extracting"))
        self._worker = _SpineAtlasWorker(
            "extract",
            atlas_source=self._atlas_path,
            output_dir=output,
            write_psd=self.psd_checkbox.isChecked(),
        )
        self._worker.resultReady.connect(self._extract_finished)
        self._worker.failed.connect(self._worker_failed)
        self._worker.progress.connect(self._worker_progress)
        self._worker.finished.connect(self._release_worker)
        self._worker.start()

    def _writeback(self):
        metadata = self._result.metadata_path if self._result else None
        metadata_dir = str(metadata.parent) if metadata else self.output_edit.text()
        if metadata is None or not metadata.is_file():
            metadata_name, _ = QFileDialog.getOpenFileName(
                self,
                tr("spine.choose_metadata"),
                metadata_dir,
                tr("spine.metadata_filter"),
            )
            if not metadata_name:
                self.status_label.setText(tr("spine.error_extract_first"))
                return
            metadata = Path(metadata_name).resolve()
        psd, _ = QFileDialog.getOpenFileName(self, tr("spine.choose_psd"), str(metadata.parent), tr("spine.psd_filter"))
        if not psd:
            return
        output = QFileDialog.getExistingDirectory(self, tr("spine.choose_output"), self.output_edit.text())
        if not output:
            return
        self._set_busy(True)
        self.warning_edit.clear()
        self.status_label.setText(tr("spine.writing"))
        self._worker = _SpineAtlasWorker(
            "writeback",
            metadata_or_atlas=metadata,
            edited_layers=Path(psd),
            output_dir=Path(output),
        )
        self._worker.resultReady.connect(self._writeback_finished)
        self._worker.failed.connect(self._worker_failed)
        self._worker.progress.connect(self._worker_progress)
        self._worker.finished.connect(self._release_worker)
        self._worker.start()

    def _release_worker(self, *_args):
        worker = self._worker
        self._worker = None
        self._set_busy(False)
        if worker is not None:
            worker.deleteLater()

    def _worker_failed(self, message: str):
        self.status_label.setText(tr("spine.error", error=message))
        self.warning_edit.setPlainText(message)

    def _worker_progress(self, value: int, message: str):
        self.status_label.setText(f"{value}%  {message}")

    def closeEvent(self, event):
        self.stop_worker()
        super().closeEvent(event)

    def stop_worker(self):
        """Stop and join an extraction worker before its page is destroyed."""

        worker = self._worker
        if worker is None:
            return
        if worker.isRunning():
            worker.requestInterruption()
            # Core operations are synchronous and may finish the current
            # filesystem operation before observing interruption.  Joining is
            # intentional here: it prevents Qt from destroying a live thread
            # when the main window closes.
            worker.wait()
        self._worker = None
        self._set_busy(False)
        worker.deleteLater()

    def _extract_finished(self, result: SpineAtlasExportResult):
        self._result = result
        self._regions_by_id = {region.region_id: region for region in result.atlas.regions}
        self._page_paths = {}
        if result.atlas.atlas_path:
            for page in result.atlas.pages:
                self._page_paths[page.index] = (result.atlas.atlas_path.parent / page.name).resolve()
        self.region_list.clear()
        for region in result.atlas.regions:
            item = QListWidgetItem(f"{region.name}  [{region.region_id}]")
            item.setData(Qt.ItemDataRole.UserRole, region.region_id)
            self.region_list.addItem(item)
        messages = list(result.warnings)
        messages.append(tr("spine.exported", count=len(result.region_paths), path=str(result.output_dir)))
        if result.psd_path:
            messages.append(tr("spine.psd_written", path=str(result.psd_path)))
        self.warning_edit.setPlainText("\n".join(messages))
        self.status_label.setText(tr("spine.done"))
        if self.region_list.count():
            self.region_list.setCurrentRow(0)

    def _writeback_finished(self, result):
        messages = list(getattr(result, "warnings", []))
        messages.append(tr("spine.written", count=len(result.changed_regions), path=str(result.output_dir)))
        self.warning_edit.setPlainText("\n".join(messages))
        self.status_label.setText(tr("spine.done"))

    def _region_selected(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None):
        if current is None:
            return
        region = self._regions_by_id.get(str(current.data(Qt.ItemDataRole.UserRole)))
        if region is None:
            return
        page = self._page_paths.get(region.page_index)
        if page and page.is_file():
            self.canvas.set_page(page, region)
        if self._result:
            part = self._result.region_paths.get(region.region_id)
            if part and part.is_file():
                self.part_preview.setPixmap(
                    QPixmap(str(part)).scaled(
                        self.part_preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
                    )
                )
