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
    QSizePolicy,
    QVBoxLayout,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
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
    from app.core.spine_editor import discover_spine_editor
except ImportError:  # Keep the page importable while the optional backend is absent.
    convert_spine = None
    discover_native_converter = None
    discover_spine_editor = None


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

    def __init__(
        self,
        source: str,
        output_dir: str | None,
        target_version: str,
        output_format: str,
        converter_path: str | None,
        remove_curve: bool,
        create_project: bool = False,
        editor_path: str | None = None,
    ):
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
        self.create_project = bool(create_project)
        self.editor_path = editor_path

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
                create_project=self.create_project,
                editor_path=self.editor_path,
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
        self._editor_info = None
        self._status_state = "idle"
        self._status_error = ""
        self._result = None
        self._result_output_dir = ""
        self._result_skeleton_path = ""
        self._result_report_path = ""
        self._result_editor_project_path = ""

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
        self.create_project_checkbox.setChecked(
            bool(self.settings_manager.get_spine_create_project())
        )
        self._editor_info = None
        # The converter is bundled as a native DLL.  The old EXE setting is
        # intentionally not surfaced here; the backend may ignore that legacy
        # value while discovering the installed DLL.
        self._converter_info = None
        self._update_editor_project_availability()

    def _build_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(20, 18, 20, 20)
        self.main_layout.setSpacing(14)

        self.title_label = SubtitleLabel(self)
        self.title_label.setWordWrap(True)
        self.title_label.setMinimumWidth(0)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.main_layout.addWidget(self.title_label)
        self.description_label = CaptionLabel(self)
        self.description_label.setWordWrap(True)
        self.description_label.setMinimumWidth(0)
        self.description_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.main_layout.addWidget(self.description_label)

        # Keep the work surface balanced at high DPI: paths and options live
        # in the left column, while detection, status, and the result occupy
        # the right.  Cards are allowed to shrink to zero so translated text
        # cannot force a horizontal scrollbar when the window is narrowed.
        self.columns_layout = QHBoxLayout()
        self.columns_layout.setSpacing(14)
        self.left_column = QVBoxLayout()
        self.right_column = QVBoxLayout()
        self.left_column.setSpacing(14)
        self.right_column.setSpacing(14)
        self.columns_layout.addLayout(self.left_column, 1)
        self.columns_layout.addLayout(self.right_column, 1)
        self.main_layout.addLayout(self.columns_layout)

        self.input_card, input_layout = self._new_card(
            self, "spineConverterInputCard"
        )
        # Stable handles for visual smoke tests and callers that want to
        # highlight the main controls/result surfaces.
        self.controls_card = self.input_card
        self.source_card_title = SubtitleLabel(self.input_card)
        input_layout.addWidget(self.source_card_title)

        self.source_label = BodyLabel(self.input_card)
        self.source_label.setWordWrap(True)
        self.source_label.setMinimumWidth(0)
        self.source_edit = LineEdit(self.input_card)
        self.source_edit.setReadOnly(True)
        self.source_edit.setPlaceholderText(".json / .skel / folder")
        self._make_compact(self.source_edit)
        self.source_browse_button = PushButton(self.input_card)
        self.source_browse_button.setIcon(FluentIcon.FOLDER)
        self.source_browse_button.clicked.connect(self.browse_source_file)
        self.source_folder_button = PushButton(self.input_card)
        self.source_folder_button.setIcon(FluentIcon.FOLDER_ADD)
        self.source_folder_button.clicked.connect(self.browse_source_folder)
        for button in (self.source_browse_button, self.source_folder_button):
            self._make_compact(button)

        input_layout.addWidget(self.source_label)
        input_layout.addWidget(self.source_edit)
        source_actions = QHBoxLayout()
        source_actions.setSpacing(8)
        source_actions.addWidget(self.source_browse_button, 1)
        source_actions.addWidget(self.source_folder_button, 1)
        input_layout.addLayout(source_actions)

        self.output_label = BodyLabel(self.input_card)
        self.output_label.setWordWrap(True)
        self.output_label.setMinimumWidth(0)
        self.output_edit = LineEdit(self.input_card)
        self.output_edit.setReadOnly(True)
        self.output_edit.setPlaceholderText(tr("spine_converter.output_default"))
        self._make_compact(self.output_edit)
        self.output_browse_button = PushButton(self.input_card)
        self.output_browse_button.setIcon(FluentIcon.FOLDER)
        self.output_browse_button.clicked.connect(self.browse_output)
        self._make_compact(self.output_browse_button)
        input_layout.addWidget(self.output_label)
        input_layout.addWidget(self.output_edit)
        input_layout.addWidget(self.output_browse_button)
        self.left_column.addWidget(self.input_card)

        self.options_card, options_layout = self._new_card(
            self, "spineConverterOptionsCard"
        )
        self.options_card_title = SubtitleLabel(self.options_card)
        options_layout.addWidget(self.options_card_title)

        self.target_version_label = BodyLabel(self.options_card)
        self.target_version_label.setWordWrap(True)
        self.target_version_label.setMinimumWidth(0)
        self.target_version_combo = EditableComboBox(self.options_card)
        self.target_version_combo.addItems(list(self.TARGET_PRESETS))
        self.target_version_combo.setCurrentText(self.TARGET_PRESETS[0])
        self._make_compact(self.target_version_combo)
        self.output_format_label = BodyLabel(self.options_card)
        self.output_format_label.setWordWrap(True)
        self.output_format_label.setMinimumWidth(0)
        self.output_format_combo = ComboBox(self.options_card)
        self.output_format_combo.addItems(list(self.OUTPUT_FORMATS))
        self.output_format_combo.setCurrentIndex(0)
        self._make_compact(self.output_format_combo)
        self.remove_curve_checkbox = CheckBox(self.options_card)
        self.remove_curve_checkbox.setChecked(False)
        self._make_compact(self.remove_curve_checkbox)
        self.create_project_checkbox = CheckBox(self.options_card)
        self.create_project_checkbox.setChecked(True)
        self._make_compact(self.create_project_checkbox)
        self.target_version_combo.currentTextChanged.connect(
            self._update_editor_project_availability
        )
        self.output_format_combo.currentTextChanged.connect(
            self._update_editor_project_availability
        )
        options_layout.addWidget(self.target_version_label)
        options_layout.addWidget(self.target_version_combo)
        options_layout.addWidget(self.output_format_label)
        options_layout.addWidget(self.output_format_combo)
        options_layout.addWidget(self.remove_curve_checkbox)
        options_layout.addWidget(self.create_project_checkbox)
        self.left_column.addWidget(self.options_card)

        self.action_card, action_layout = self._new_card(
            self, "spineConverterActionCard"
        )
        self.action_card_title = SubtitleLabel(self.action_card)
        action_layout.addWidget(self.action_card_title)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.convert_button = PrimaryPushButton(self.action_card)
        self.convert_button.setIcon(FluentIcon.SYNC)
        self.convert_button.clicked.connect(self.start_conversion)
        self._make_compact(self.convert_button)
        self.progress_bar = ProgressBar(self.action_card)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        self.progress_bar.setMinimumWidth(0)
        self.progress_bar.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        actions.addWidget(self.convert_button, 0)
        actions.addWidget(self.progress_bar, 1)
        action_layout.addLayout(actions)
        self.left_column.addWidget(self.action_card)
        self.left_column.addStretch(1)

        self.dependencies_card, dependencies_layout = self._new_card(
            self, "spineConverterDependenciesCard"
        )
        self.dependencies_card_title = SubtitleLabel(self.dependencies_card)
        dependencies_layout.addWidget(self.dependencies_card_title)
        self.converter_status_label = CaptionLabel(self.dependencies_card)
        self.converter_status_label.setWordWrap(True)
        self.converter_status_label.setMinimumWidth(0)
        self.converter_status_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        dependencies_layout.addWidget(self.converter_status_label)
        self.editor_status_label = CaptionLabel(self.dependencies_card)
        self.editor_status_label.setWordWrap(True)
        self.editor_status_label.setMinimumWidth(0)
        self.editor_status_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        dependencies_layout.addWidget(self.editor_status_label)
        self.right_column.addWidget(self.dependencies_card)

        self.status_card, status_layout = self._new_card(
            self, "spineConverterStatusCard"
        )
        self.status_card_title = SubtitleLabel(self.status_card)
        status_layout.addWidget(self.status_card_title)
        self.status_label = BodyLabel(self.status_card)
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumWidth(0)
        self.status_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        status_layout.addWidget(self.status_label)
        self.warning_label = QLabel(self.status_card)
        self.warning_label.setWordWrap(True)
        self.warning_label.setMinimumWidth(0)
        self.warning_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        # palette(link) stays legible in both bundled light and dark themes.
        self.warning_label.setStyleSheet("color: palette(link);")
        status_layout.addWidget(self.warning_label)
        self.right_column.addWidget(self.status_card)

        self.result_card, result_layout = self._new_card(
            self, "spineConverterResultCard"
        )
        self.results_card = self.result_card
        self.result_card_title = SubtitleLabel(self.result_card)
        result_layout.addWidget(self.result_card_title)
        self.result_text = QPlainTextEdit(self.result_card)
        self.result_text.setReadOnly(True)
        # Keep the empty log useful but compact; the full report remains
        # copyable and scrollable inside this bounded result surface.
        self.result_text.setMinimumHeight(104)
        self.result_text.setMaximumHeight(160)
        self.result_text.setMinimumWidth(0)
        self.result_text.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.result_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        # Keep the old attribute name for callers that only inspect the result
        # widget, while exposing a real text editor for copy/scroll support.
        self.result_label = self.result_text
        result_layout.addWidget(self.result_text)

        result_actions = QHBoxLayout()
        result_actions.setSpacing(8)
        self.open_output_button = PushButton(self.result_card)
        self.open_output_button.setIcon(FluentIcon.FOLDER)
        self.open_output_button.setEnabled(False)
        self.open_output_button.clicked.connect(self.open_output_directory)
        self._make_compact(self.open_output_button)
        self.preview_button = PushButton(self.result_card)
        self.preview_button.setIcon(FluentIcon.VIEW)
        self.preview_button.setEnabled(False)
        self.preview_button.clicked.connect(self.preview_result)
        self._make_compact(self.preview_button)
        result_actions.addWidget(self.open_output_button, 1)
        result_actions.addWidget(self.preview_button, 1)
        result_layout.addLayout(result_actions)
        self.right_column.addWidget(self.result_card)

        self.flow_card, flow_layout = self._new_card(
            self, "spineConverterFlowCard"
        )
        self.flow_card_title = SubtitleLabel(self.flow_card)
        flow_layout.addWidget(self.flow_card_title)
        self.flow_source_label = self._new_flow_step(
            flow_layout, "1", self.flow_card
        )
        self.flow_target_label = self._new_flow_step(
            flow_layout, "2", self.flow_card
        )
        self.flow_review_label = self._new_flow_step(
            flow_layout, "3", self.flow_card
        )
        self.right_column.addWidget(self.flow_card)
        self.right_column.addStretch(1)
        self.setStyleSheet(
            """
            QFrame#spineConverterPage {
                background: palette(window);
            }
            CardWidget#spineConverterInputCard,
            CardWidget#spineConverterOptionsCard,
            CardWidget#spineConverterActionCard,
            CardWidget#spineConverterDependenciesCard,
            CardWidget#spineConverterStatusCard,
            CardWidget#spineConverterResultCard,
            CardWidget#spineConverterFlowCard {
                background: palette(base);
                border: 1px solid palette(mid);
                border-radius: 10px;
            }
            QPlainTextEdit#spineConverterResultText {
                background: palette(alternate-base);
                border: 1px solid palette(mid);
                border-radius: 7px;
            }
            """
        )
        self.result_text.setObjectName("spineConverterResultText")

    @staticmethod
    def _make_compact(widget):
        """Let controls yield to their card at high DPI and narrow widths."""

        widget.setMinimumWidth(0)
        widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        return widget

    @staticmethod
    def _new_card(parent, object_name):
        card = CardWidget(parent)
        card.setObjectName(object_name)
        card.setBorderRadius(10)
        card.setMinimumWidth(0)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 15, 16, 15)
        layout.setSpacing(10)
        return card, layout

    @staticmethod
    def _new_flow_step(layout, number, parent):
        row = QHBoxLayout()
        row.setSpacing(10)
        badge = BodyLabel(parent)
        badge.setText(number)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedWidth(22)
        badge.setStyleSheet(
            "color: palette(highlight); font-weight: 700;"
        )
        detail = CaptionLabel(parent)
        detail.setWordWrap(True)
        detail.setMinimumWidth(0)
        detail.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(badge)
        row.addWidget(detail, 1)
        layout.addLayout(row)
        return detail

    def retranslate_ui(self):
        self.title_label.setText(tr("spine_converter.title"))
        self.description_label.setText(tr("spine_converter.description"))
        self.source_card_title.setText(
            tr("spine_converter.input_card_title", default="Input & output")
        )
        self.options_card_title.setText(
            tr("spine_converter.options_card_title", default="Conversion options")
        )
        self.action_card_title.setText(
            tr("spine_converter.action_card_title", default="Run conversion")
        )
        self.dependencies_card_title.setText(
            tr("spine_converter.dependencies_card_title", default="Dependencies")
        )
        self.status_card_title.setText(
            tr("spine_converter.status_card_title", default="Status")
        )
        self.result_card_title.setText(
            tr("spine_converter.result_card_title", default="Result")
        )
        self.flow_card_title.setText(
            tr("spine_converter.flow_card_title", default="Conversion flow")
        )
        self.flow_source_label.setText(
            tr(
                "spine_converter.flow_source",
                default="Choose a skeleton source file or folder.",
            )
        )
        self.flow_target_label.setText(
            tr(
                "spine_converter.flow_target",
                default="Set the target Spine version and output format.",
            )
        )
        self.flow_review_label.setText(
            tr(
                "spine_converter.flow_review",
                default="Review the report, then open or preview the converted copy.",
            )
        )
        self.source_label.setText(tr("spine_converter.source"))
        self.source_browse_button.setText(tr("spine_converter.browse_file"))
        self.source_folder_button.setText(tr("spine_converter.browse_folder"))
        self.output_label.setText(tr("spine_converter.output"))
        self.output_browse_button.setText(tr("spine_converter.browse_output"))
        self.output_edit.setPlaceholderText(tr("spine_converter.output_default"))
        self.target_version_label.setText(tr("spine_converter.target_version"))
        self.output_format_label.setText(tr("spine_converter.output_format"))
        self.remove_curve_checkbox.setText(tr("spine_converter.remove_curve"))
        self.create_project_checkbox.setText(tr("spine_converter.create_project"))
        self.warning_label.setText(tr("spine_converter.warning"))
        self.convert_button.setText(tr("spine_converter.convert"))
        self.open_output_button.setText(tr("spine_converter.open_output"))
        self.preview_button.setText(tr("spine_converter.preview"))
        self._render_status()
        self._render_converter_status()
        self._render_editor_status()
        self._render_result()
        self._update_editor_project_availability()

    def updateUIScale(self, width: int, height: int):
        del height
        # The columns remain side by side on the normal 1858 logical-pixel
        # work area.  Tightening the gutter below that width gives translated
        # labels a little more room without imposing a page-wide minimum.
        self.columns_layout.setSpacing(10 if width < 1200 else 14)
        self.left_column.setSpacing(10 if width < 1200 else 14)
        self.right_column.setSpacing(10 if width < 1200 else 14)

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
        self._detect_editor()

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

    def _detect_editor(self):
        if discover_spine_editor is None:
            self._editor_info = None
            self._render_editor_status()
            return None
        try:
            configured = self.settings_manager.get_spine_editor_path()
            self._editor_info = discover_spine_editor(configured or None, target_version="3.8.75")
        except Exception as exc:
            self._editor_info = None
            self._show_error(str(exc))
        self._render_editor_status()
        return self._editor_info

    # Kept as a small compatibility hook for callers that used the old
    # auto-detect action; it never accepts or launches an executable.
    def _detect_converter(self):
        native = self._detect_native_converter()
        self._detect_editor()
        return native

    def _render_converter_status(self):
        path = str(self._converter_info or "").strip()
        if path:
            self.converter_status_label.setText(tr("spine_converter.converter_found", path=path))
        else:
            self.converter_status_label.setText(tr("spine_converter.converter_auto"))

    def _render_editor_status(self):
        if not self._editor_project_target_supported():
            self.editor_status_label.setText(tr("spine_converter.editor_target_only"))
            return
        if not self._editor_project_format_supported():
            self.editor_status_label.setText(tr("spine_converter.editor_json_only"))
            return
        path = str(self._editor_info or "").strip()
        if path:
            self.editor_status_label.setText(tr("spine_converter.editor_found", path=path))
        else:
            self.editor_status_label.setText(tr("spine_converter.editor_auto"))

    def _editor_project_target_supported(self) -> bool:
        return str(self.target_version_combo.currentText() or "").strip() == "3.8.75"

    def _editor_project_format_supported(self) -> bool:
        return str(self.output_format_combo.currentText() or "").strip().lower() == "json"

    def _editor_project_available(self) -> bool:
        return self._editor_project_target_supported() and self._editor_project_format_supported()

    def _update_editor_project_availability(self, *_args):
        """Keep the editor-project option explicit about its 3.8.75 scope."""

        supported = self._editor_project_available()
        if not supported and self.create_project_checkbox.isChecked():
            self.create_project_checkbox.setChecked(False)
        self.create_project_checkbox.setEnabled(bool(supported and self._worker is None))
        self.create_project_checkbox.setToolTip(
            ""
            if supported
            else tr(
                "spine_converter.editor_target_only"
                if not self._editor_project_target_supported()
                else "spine_converter.editor_json_only"
            )
        )
        self._render_editor_status()

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
        self.create_project_checkbox.setEnabled(
            bool(enabled and self._editor_project_available())
        )
        if enabled:
            self.convert_button.setEnabled(True)
        self._render_editor_status()

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
        editor_project = self._result_editor_project_path
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
            + (
                f"\n{tr('spine_converter.editor_project')}: {editor_project}"
                if editor_project
                else ""
            )
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
        if not target_version:
            self._show_error(tr("spine_converter.error_version"))
            return
        if output_format not in self.OUTPUT_FORMATS:
            self._show_error(tr("spine_converter.error_format"))
            return
        if self.create_project_checkbox.isChecked() and not self._editor_project_target_supported():
            self._show_error(tr("spine_converter.editor_target_only"))
            return
        if self.create_project_checkbox.isChecked() and output_format != "json":
            self._show_error(tr("spine_converter.editor_json_only"))
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
            self.create_project_checkbox.isChecked(),
            self.settings_manager.get_spine_editor_path() or None,
        )
        self._worker = worker
        self._worker_finished = False
        self._worker_reported = False
        self._result = None
        self._result_output_dir = ""
        self._result_skeleton_path = ""
        self._result_report_path = ""
        self._result_editor_project_path = ""
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
        self._result_editor_project_path = str(
            _result_value(result, "editor_project_path", "") or ""
        )
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
