"""GUI for converting Spine skeleton data with the bundled native DLL."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    EditableComboBox,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    SubtitleLabel,
)

from app.i18n import get_i18n, tr
from app.core.settings_manager import SettingsManager


try:
    from shiboken6 import isValid as _is_qt_object_valid
except ImportError:  # pragma: no cover - bundled with PySide6 in normal builds
    def _is_qt_object_valid(obj) -> bool:
        return obj is not None


try:
    from app.core.spine_converter import convert_spine, discover_native_converter
except ImportError:  # Keep the page importable while the optional backend is absent.
    convert_spine = None
    discover_native_converter = None


def _thread_is_running(thread) -> bool:
    return bool(thread is not None and _is_qt_object_valid(thread) and thread.isRunning())


def _result_value(result: Any, name: str, default: Any = "") -> Any:
    if result is None:
        return default
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


class _SpineConverterDropFrame(QFrame):
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
        paths = [
            url.toLocalFile()
            for url in event.mimeData().urls()
            if url.isLocalFile() and url.toLocalFile()
        ]
        if paths:
            self.pathsDropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()


class SpineConverterWorker(QThread):
    resultReady = Signal(object)
    failed = Signal(str)

    def __init__(self, source: str, output_dir: str, target_version: str, output_format: str,
                 converter_path: str | None, remove_curve: bool):
        # The worker is deliberately unparented.  A running QThread must not
        # be destroyed with the page; the page keeps it until its signals have
        # been delivered and its finished state is observed.
        super().__init__(None)
        self.source = source
        self.output_dir = output_dir
        self.target_version = target_version
        self.output_format = output_format
        self.converter_path = converter_path
        self.remove_curve = bool(remove_curve)

    def run(self):
        try:
            if convert_spine is None:
                raise RuntimeError("Spine converter backend is unavailable.")
            result = convert_spine(
                self.source,
                self.output_dir,
                target_version=self.target_version,
                output_format=self.output_format,
                converter_path=self.converter_path,
                remove_curve=self.remove_curve,
            )
            self.resultReady.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class SpineConverterPage(_SpineConverterDropFrame):
    """Standalone conversion utility with no external executable entry point."""

    previewRequested = Signal(str)

    TARGET_PRESETS = ("3.8.75", "3.8.99", "4.0.64", "4.1.24", "4.2.11")
    OUTPUT_FORMATS = ("json", "skel")

    def __init__(self, parent=None, settings_manager: SettingsManager | None = None):
        super().__init__(parent)
        self.setObjectName("spineConverterPage")
        self.i18n = get_i18n()
        self.settings_manager = settings_manager or SettingsManager()
        self._worker = None
        self._conversion_generation = 0
        self._worker_finished = False
        self._worker_reported = False
        self._converter_info = None
        self._status_state = "idle"
        self._status_error = ""
        self._result = None
        self._result_output_dir = ""
        self._result_skeleton_path = ""
        self._result_report_path = ""

        self._build_ui()
        self._load_saved_conversion_settings()
        self.retranslate_ui()
        self.i18n.languageChanged.connect(self.retranslate_ui)
        self.pathsDropped.connect(self._accept_dropped_paths)
        self._detect_converter()

    def _load_saved_conversion_settings(self):
        """Load converter defaults from Settings without changing them."""

        target = self.settings_manager.get_spine_conversion_target_version()
        self.target_version_combo.setCurrentText(target or "")
        output_format = self.settings_manager.get_spine_conversion_output_format()
        if output_format in self.OUTPUT_FORMATS:
            self.output_format_combo.setCurrentText(output_format)
        # The converter is bundled as a native DLL.  The old EXE setting is
        # intentionally not surfaced here; the backend may ignore that legacy
        # value while discovering the installed DLL.
        self._converter_info = None

    def _build_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(20, 18, 20, 20)
        self.main_layout.setSpacing(12)

        self.title_label = SubtitleLabel(self)
        self.main_layout.addWidget(self.title_label)
        self.description_label = CaptionLabel(self)
        self.description_label.setWordWrap(True)
        self.main_layout.addWidget(self.description_label)

        source_row = QHBoxLayout()
        self.source_label = BodyLabel(self)
        self.source_edit = LineEdit(self)
        self.source_edit.setReadOnly(True)
        self.source_edit.setPlaceholderText(".json / .skel / folder")
        self.source_browse_button = PushButton(self)
        self.source_browse_button.setIcon(FluentIcon.FOLDER)
        self.source_browse_button.clicked.connect(self.browse_source_file)
        self.source_folder_button = PushButton(self)
        self.source_folder_button.setIcon(FluentIcon.FOLDER_ADD)
        self.source_folder_button.clicked.connect(self.browse_source_folder)
        source_row.addWidget(self.source_label)
        source_row.addWidget(self.source_edit, 1)
        source_row.addWidget(self.source_browse_button)
        source_row.addWidget(self.source_folder_button)
        self.main_layout.addLayout(source_row)

        output_row = QHBoxLayout()
        self.output_label = BodyLabel(self)
        self.output_edit = LineEdit(self)
        self.output_edit.setReadOnly(True)
        self.output_browse_button = PushButton(self)
        self.output_browse_button.setIcon(FluentIcon.FOLDER)
        self.output_browse_button.clicked.connect(self.browse_output)
        output_row.addWidget(self.output_label)
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(self.output_browse_button)
        self.main_layout.addLayout(output_row)

        options_row = QHBoxLayout()
        self.target_version_label = BodyLabel(self)
        self.target_version_combo = EditableComboBox(self)
        self.target_version_combo.addItems(list(self.TARGET_PRESETS))
        self.target_version_combo.setCurrentText(self.TARGET_PRESETS[0])
        self.target_version_combo.setMinimumWidth(150)
        self.output_format_label = BodyLabel(self)
        self.output_format_combo = ComboBox(self)
        self.output_format_combo.addItems(list(self.OUTPUT_FORMATS))
        self.output_format_combo.setCurrentIndex(0)
        self.remove_curve_checkbox = CheckBox(self)
        self.remove_curve_checkbox.setChecked(False)
        options_row.addWidget(self.target_version_label)
        options_row.addWidget(self.target_version_combo)
        options_row.addWidget(self.output_format_label)
        options_row.addWidget(self.output_format_combo)
        options_row.addWidget(self.remove_curve_checkbox)
        options_row.addStretch(1)
        self.main_layout.addLayout(options_row)

        self.converter_status_label = CaptionLabel(self)
        self.converter_status_label.setWordWrap(True)
        self.main_layout.addWidget(self.converter_status_label)

        self.warning_label = QLabel(self)
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #8A5A00;")
        self.main_layout.addWidget(self.warning_label)

        actions = QHBoxLayout()
        self.convert_button = PrimaryPushButton(self)
        self.convert_button.setIcon(FluentIcon.SYNC)
        self.convert_button.clicked.connect(self.start_conversion)
        self.progress_bar = ProgressBar(self)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        actions.addWidget(self.convert_button)
        actions.addWidget(self.progress_bar, 1)
        self.main_layout.addLayout(actions)

        self.status_label = BodyLabel(self)
        self.status_label.setWordWrap(True)
        self.main_layout.addWidget(self.status_label)
        self.result_text = QPlainTextEdit(self)
        self.result_text.setReadOnly(True)
        self.result_text.setMinimumHeight(160)
        self.result_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        # Keep the old attribute name for callers that only inspect the result
        # widget, while exposing a real text editor for copy/scroll support.
        self.result_label = self.result_text
        self.main_layout.addWidget(self.result_text, 1)

        result_actions = QHBoxLayout()
        self.open_output_button = PushButton(self)
        self.open_output_button.setIcon(FluentIcon.FOLDER)
        self.open_output_button.setEnabled(False)
        self.open_output_button.clicked.connect(self.open_output_directory)
        self.preview_button = PushButton(self)
        self.preview_button.setIcon(FluentIcon.VIEW)
        self.preview_button.setEnabled(False)
        self.preview_button.clicked.connect(self.preview_result)
        result_actions.addWidget(self.open_output_button)
        result_actions.addWidget(self.preview_button)
        result_actions.addStretch(1)
        self.main_layout.addLayout(result_actions)
        self.main_layout.addStretch(1)

    def retranslate_ui(self):
        self.title_label.setText(tr("spine_converter.title"))
        self.description_label.setText(tr("spine_converter.description"))
        self.source_label.setText(tr("spine_converter.source"))
        self.source_browse_button.setText(tr("spine_converter.browse_file"))
        self.source_folder_button.setText(tr("spine_converter.browse_folder"))
        self.output_label.setText(tr("spine_converter.output"))
        self.output_browse_button.setText(tr("spine_converter.browse_output"))
        self.target_version_label.setText(tr("spine_converter.target_version"))
        self.output_format_label.setText(tr("spine_converter.output_format"))
        self.remove_curve_checkbox.setText(tr("spine_converter.remove_curve"))
        self.warning_label.setText(tr("spine_converter.warning"))
        self.convert_button.setText(tr("spine_converter.convert"))
        self.open_output_button.setText(tr("spine_converter.open_output"))
        self.preview_button.setText(tr("spine_converter.preview"))
        self._render_status()
        self._render_converter_status()
        self._render_result()

    def updateUIScale(self, width: int, height: int):
        del width, height

    def showEvent(self, event):
        """Refresh saved converter defaults whenever this page is shown."""

        super().showEvent(event)
        # Never rewrite controls while a conversion is active.  The settings
        # page may save a new target while this page is hidden; reload it the
        # next time the page becomes visible.
        if self.is_conversion_running():
            return
        self.settings_manager.reload_settings()
        self._load_saved_conversion_settings()
        self._detect_native_converter()

    def _accept_dropped_paths(self, paths: list[str]):
        if self._worker is not None:
            return
        if paths:
            self._set_source_path(paths[0])

    def _set_source_path(self, path: str):
        candidate = Path(str(path or "")).expanduser()
        if not candidate.exists() or not (candidate.is_dir() or candidate.suffix.lower() in {".json", ".skel"}):
            self._show_error(tr("spine_converter.error_source"))
            return
        self.source_edit.setText(str(candidate.resolve()))
        if self._status_state == "error":
            self._status_state = "idle"
            self._status_error = ""
            self._render_status()

    def browse_source_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("spine_converter.choose_source"),
            "",
            tr("spine_converter.source_filter"),
        )
        if path:
            self._set_source_path(path)

    def browse_source_folder(self):
        path = QFileDialog.getExistingDirectory(self, tr("spine_converter.choose_folder"))
        if path:
            self._set_source_path(path)

    def browse_output(self):
        path = QFileDialog.getExistingDirectory(self, tr("spine_converter.choose_output"))
        if path:
            self.output_edit.setText(path)

    def _detect_native_converter(self):
        if discover_native_converter is None:
            self._converter_info = None
            self._render_converter_status()
            return None
        try:
            self._converter_info = discover_native_converter()
        except Exception as exc:
            self._converter_info = None
            self._show_error(str(exc))
        self._render_converter_status()
        return self._converter_info

    # Kept as a small compatibility hook for callers that used the old
    # auto-detect action; it never accepts or launches an executable.
    def _detect_converter(self):
        return self._detect_native_converter()

    def _render_converter_status(self):
        path = str(self._converter_info or "").strip()
        if path:
            self.converter_status_label.setText(tr("spine_converter.converter_found", path=path))
        else:
            self.converter_status_label.setText(tr("spine_converter.converter_auto"))

    def _set_controls_enabled(self, enabled: bool):
        for control in (
            self.source_browse_button,
            self.source_folder_button,
            self.output_browse_button,
            self.target_version_combo,
            self.output_format_combo,
            self.remove_curve_checkbox,
            self.convert_button,
        ):
            control.setEnabled(bool(enabled))
        if enabled:
            self.convert_button.setEnabled(True)

    def _show_error(self, message: str):
        self._status_state = "error"
        self._status_error = str(message or tr("spine_converter.error_unknown"))
        self._render_status()
        InfoBar.error(
            title=tr("spine_converter.error_title"),
            content=self._status_error,
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=3500,
            parent=self,
        )

    def notify_close_while_running(self):
        """Tell the user why the page/window cannot close yet."""

        InfoBar.warning(
            title=tr("spine_converter.title"),
            content=tr("spine_converter.close_while_running"),
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=3500,
            parent=self,
        )

    def _render_status(self):
        if self._status_state == "running":
            self.status_label.setText(tr("spine_converter.converting"))
        elif self._status_state == "success":
            self.status_label.setText(tr("spine_converter.success"))
        elif self._status_state == "error":
            self.status_label.setText(tr("spine_converter.error", error=self._status_error))
        else:
            self.status_label.setText(tr("spine_converter.ready"))

    def _render_result(self):
        if not self._result:
            self.result_text.clear()
            return
        output_dir = self._result_output_dir
        skeleton = self._result_skeleton_path
        report = self._result_report_path
        warnings = _result_value(self._result, "warnings", []) or []
        warning_text = "\n".join(str(item) for item in warnings)
        self.result_text.setPlainText(
            tr(
                "spine_converter.result",
                output=output_dir,
                skeleton=skeleton,
                report=report or tr("spine_converter.no_report"),
            )
            + (f"\n{warning_text}" if warning_text else "")
        )

    def start_conversion(self):
        if self._worker is not None:
            return
        source = str(self.source_edit.text() or "").strip()
        output_dir = str(self.output_edit.text() or "").strip()
        target_version = str(self.target_version_combo.currentText() or "").strip()
        output_format = str(self.output_format_combo.currentText() or "json").strip().lower()
        if not source or not Path(source).exists():
            self._show_error(tr("spine_converter.error_source"))
            return
        if not output_dir:
            self._show_error(tr("spine_converter.error_output"))
            return
        if not target_version:
            self._show_error(tr("spine_converter.error_version"))
            return
        if output_format not in self.OUTPUT_FORMATS:
            self._show_error(tr("spine_converter.error_format"))
            return

        self._conversion_generation += 1
        generation = self._conversion_generation
        worker = SpineConverterWorker(
            source,
            output_dir,
            target_version,
            output_format,
            None,
            self.remove_curve_checkbox.isChecked(),
        )
        self._worker = worker
        self._worker_finished = False
        self._worker_reported = False
        self._result = None
        self._result_output_dir = ""
        self._result_skeleton_path = ""
        self._result_report_path = ""
        self.open_output_button.setEnabled(False)
        self.preview_button.setEnabled(False)
        self._set_controls_enabled(False)
        self.progress_bar.setVisible(True)
        self._status_state = "running"
        self._status_error = ""
        self._render_status()
        worker.resultReady.connect(
            lambda result, w=worker, g=generation: self._on_conversion_result(result, w, g)
        )
        worker.failed.connect(
            lambda error, w=worker, g=generation: self._on_conversion_failed(error, w, g)
        )
        worker.finished.connect(
            lambda w=worker, g=generation: self._on_worker_finished(w, g)
        )
        worker.start()

    def _on_conversion_result(self, result, worker, generation: int):
        if generation != self._conversion_generation or worker is not self._worker:
            return
        self._worker_reported = True
        self._result = result
        self._result_output_dir = str(_result_value(result, "output_dir", "") or "")
        self._result_skeleton_path = str(_result_value(result, "skeleton_path", "") or "")
        self._result_report_path = str(_result_value(result, "report_path", "") or "")
        self._status_state = "success"
        self._render_status()
        self._render_result()
        self.open_output_button.setEnabled(bool(self._result_output_dir))
        self.preview_button.setEnabled(bool(self._result_skeleton_path))
        self._maybe_release_worker(worker, generation)

    def _on_conversion_failed(self, error: str, worker, generation: int):
        if generation != self._conversion_generation or worker is not self._worker:
            return
        self._worker_reported = True
        self._show_error(str(error))
        self._maybe_release_worker(worker, generation)

    def _on_worker_finished(self, worker, generation: int):
        if generation != self._conversion_generation or worker is not self._worker:
            return
        self._worker_finished = True
        if not self._worker_reported:
            # resultReady/failed are queued signals.  Give a result event
            # already queued by run() a chance to arrive before declaring an
            # unexpected empty result.
            QTimer.singleShot(
                0,
                lambda w=worker, g=generation: self._report_missing_result(w, g),
            )
        self._maybe_release_worker(worker, generation)

    def _report_missing_result(self, worker, generation: int):
        if (
            generation != self._conversion_generation
            or worker is not self._worker
            or not self._worker_finished
            or self._worker_reported
        ):
            return
        self._worker_reported = True
        self._show_error(tr("spine_converter.error_no_result"))
        self._maybe_release_worker(worker, generation)

    def _maybe_release_worker(self, worker, generation: int):
        if (
            generation != self._conversion_generation
            or worker is not self._worker
            or not self._worker_finished
            or not self._worker_reported
        ):
            return
        self._worker = None
        self.progress_bar.setVisible(False)
        self._set_controls_enabled(True)
        if _is_qt_object_valid(worker):
            worker.deleteLater()

    def is_conversion_running(self) -> bool:
        # Keep the page alive until both the QThread finished event and its
        # queued result/error event have been consumed.
        return self._worker is not None

    def open_output_directory(self):
        path = self._result_output_dir
        if not path:
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            self._show_error(tr("spine_converter.error_open_output"))

    def preview_result(self):
        if self._result_skeleton_path:
            self.previewRequested.emit(self._result_skeleton_path)

    def closeEvent(self, event):
        # A running conversion is intentionally allowed to finish.  Refusing
        # this close event keeps the unparented worker alive and avoids a
        # QThread destruction race; the page can be closed once it is done.
        if self.is_conversion_running():
            self.notify_close_while_running()
            event.ignore()
            return
        worker = self._worker
        self._worker = None
        if worker is not None and _is_qt_object_valid(worker):
            worker.deleteLater()
        super().closeEvent(event)


__all__ = ["SpineConverterPage", "SpineConverterWorker"]
