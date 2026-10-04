from PySide6.QtCore import QProcess, QProcessEnvironment, QThread, Qt, QUrl, Signal, QSize, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QDialog,
    QDialogButtonBox,
    QListWidget,
    QListWidgetItem,
    QToolButton,
    QPlainTextEdit,
    QSizePolicy,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from pathlib import Path
from qfluentwidgets import (
    CardWidget,
    SubtitleLabel,
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    EditableComboBox,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    ListWidget,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    ScrollArea,
    SpinBox,
    PlainTextEdit,
)

from app.core.font_helper import (
    DEFAULT_FONT_SIZE,
    MAX_FONT_SIZE,
    MIN_FONT_SIZE,
    installed_font_families,
)
from app.core.settings_manager import SettingsManager
from app.core.mcp_launch import (
    DEFAULT_MCP_PORT, animation_mcp_config, animation_mcp_endpoint,
    animation_mcp_guide, animation_mcp_launch,
)
from app.core.toolchain import (
    build_spine_native_runtime,
    find_archive_extractor,
    find_assetstudio_cli,
    find_cubism_core,
    find_photoshop,
    find_spine_native_runtime_root,
)
from app.core.toolchain_download import (
    ToolDownloadProgress,
    ToolInstallResult,
    extract_cubism_core_from_live2d_py,
    official_download_url,
    download_tool_package,
    list_tool_package_manifests,
)
from app.core.spine_preview import (
    SPINE_COMPATIBILITY_VERSION,
    SPINE_RUNTIME_SUPPORTED_VERSIONS,
    list_installed_spine_runtimes,
)
from app.i18n import get_i18n, normalize_language_code, tr


class ToolchainInstallWorker(QThread):
    """Run a tool download/build without blocking the settings page."""

    progressChanged = Signal(object)
    resultReady = Signal(object)
    failed = Signal(str)

    def __init__(self, package_id: str, parent=None):
        super().__init__(parent)
        self.package_id = package_id

    def run(self):
        try:
            callback = self.progressChanged.emit
            cancel = self.isInterruptionRequested
            if self.package_id == "cubism_core":
                result = extract_cubism_core_from_live2d_py(
                    progress=callback,
                )
            elif self.package_id.startswith("spine_native"):
                result = build_spine_native_runtime(
                    self.package_id,
                    progress=callback,
                    cancel=cancel,
                )
            else:
                result = download_tool_package(
                    self.package_id,
                    progress=callback,
                    cancel=cancel,
                )
            self.resultReady.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc) or exc.__class__.__name__)


class SettingsPage(QFrame):
    """Application settings page."""

    languageChanged = Signal(str)
    themeChanged = Signal(str)
    fontChanged = Signal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("settingsPage")

        self.settings_manager = SettingsManager()
        self.i18n = get_i18n()

        self._language_codes = ["en_US", "zh_CN", "ja_JP"]
        self._theme_values = ["auto", "light", "dark"]
        self._font_family_values = [""]
        self._texture_viewer_values = ["internal", "system", "custom"]
        self._spine_native_family_values = ["3.8.75", "4.0"]
        self._syncing_ui = False
        # The combo is deliberately forced to 3.8.75 while compatibility is
        # enabled.  Keep the user's off-mode choice separately so toggling
        # the switch back does not turn a remembered 4.0 preference into 3.8.
        self._spine_runtime_version_before_compatibility = SPINE_COMPATIBILITY_VERSION

        self.title_label = None
        self.language_section_title = None
        self.language_label = None
        self.language_desc = None
        self.language_combo = None
        self.language_note = None

        self.theme_section_title = None
        self.theme_label = None
        self.theme_desc = None
        self.theme_combo = None
        self.font_label = None
        self.font_desc = None
        self.font_family_label = None
        self.font_family_desc = None
        self.font_family_combo = None
        self.font_size_label = None
        self.font_size_desc = None
        self.font_size_spin = None
        self.font_reset_button = None

        self.runtime_section_title = None
        self.output_root_label = None
        self.output_root_desc = None
        self.output_root_edit = None
        self.output_root_button = None
        self.archive_tool_label = None
        self.archive_tool_desc = None
        self.archive_tool_status = None
        self.archive_tool_edit = None
        self.archive_tool_button = None
        self.archive_tool_auto_button = None
        self.assetstudio_tool_label = None
        self.assetstudio_tool_desc = None
        self.assetstudio_tool_status = None
        self.assetstudio_tool_edit = None
        self.assetstudio_tool_button = None
        self.assetstudio_tool_auto_button = None
        self.cubism_core_label = None
        self.cubism_core_desc = None
        self.cubism_core_status = None
        self.cubism_core_edit = None
        self.cubism_core_button = None
        self.cubism_core_auto_button = None
        self.photoshop_label = None
        self.photoshop_desc = None
        self.photoshop_status = None
        self.photoshop_edit = None
        self.photoshop_button = None
        self.photoshop_clear_button = None
        self.spine_runtime_label = None
        self.spine_runtime_desc = None
        self.spine_runtime_status = None
        self.spine_runtime_edit = None
        self.spine_runtime_button = None
        self.spine_runtime_auto_button = None
        self.spine_editor_label = None
        self.spine_editor_desc = None
        self.spine_editor_status = None
        self.spine_editor_edit = None
        self.spine_editor_button = None
        self.spine_editor_clear_button = None
        self.spine_create_project_label = None
        self.spine_create_project_desc = None
        self.spine_create_project_checkbox = None
        self.spine_auto_convert_label = None
        self.spine_auto_convert_desc = None
        self.spine_auto_convert_checkbox = None
        self.spine_target_version_label = None
        self.spine_target_version_desc = None
        self.spine_target_version_edit = None
        self.spine_preview_unify_label = None
        self.spine_preview_unify_desc = None
        self.spine_preview_unify_checkbox = None
        self.spine_compatibility_label = None
        self.spine_compatibility_desc = None
        self.spine_compatibility_checkbox = None
        self.spine_runtime_version_label = None
        self.spine_runtime_version_desc = None
        self.spine_runtime_version_combo = None
        self.spine_runtime_download_button = None
        self.spine_runtime_advanced_toggle = None
        self.tool_download_section_title = None
        self.tool_download_desc = None
        self.assetstudio_install_button = None
        self.cubism_official_button = None
        self.cubism_extract_button = None
        self.spine_native_install_button = None
        self.spine_native_family_combo = None
        self.tool_download_progress = None
        self.tool_download_status = None
        self.tool_download_cancel = None
        self.texture_viewer_label = None
        self.texture_viewer_desc = None
        self.texture_viewer_combo = None
        self.image_viewer_label = None
        self.image_viewer_desc = None
        self.image_viewer_edit = None
        self.image_viewer_button = None
        self.image_viewer_clear_button = None
        self.setting_file_note = None
        self.detect_tools_button = None
        self.save_button = None
        self.settings_content = None
        self.settings_columns_layout = None
        self.settings_left_column = None
        self.settings_right_column = None
        self.general_card = None
        self.runtime_card = None
        self.archive_card = None
        self.asset_tools_card = None
        self.download_card = None
        self.photoshop_card = None
        self.spine_card = None
        self.spine_section_title = None
        self.texture_card = None
        self._tool_install_worker = None
        self._mcp_process = None
        self._mcp_starting = False
        self._mcp_stopping = False
        self._mcp_status_key = "settings.mcp.stopped"
        self._mcp_status_error = ""
        self._mcp_ready_buffer = ""
        self._mcp_transport_values = ["streamable-http", "stdio"]

        self.setup_ui()
        self.retranslate_ui()
        self.load_current_settings()
        self.i18n.languageChanged.connect(self.retranslate_ui)
        QApplication.instance().aboutToQuit.connect(self.shutdown_animation_mcp)

    @staticmethod
    def _configure_expanding(widget):
        """Keep settings controls usable in a narrow/high-DPI card."""

        widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        # A minimum width derived from a translated button or a 200% DPI font
        # can force a horizontal scroll bar.  The card columns are the width
        # constraint; controls should yield to them instead.
        widget.setMinimumWidth(0)
        return widget

    def _new_settings_card(self, parent, object_name: str):
        """Create a theme-aware card with a compact, predictable layout."""

        card = CardWidget(parent)
        card.setObjectName(object_name)
        card.setBorderRadius(8)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        card.setMinimumWidth(0)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        return card, layout

    @staticmethod
    def _add_text_block(layout, label, description, status=None):
        for text_widget in (label, description, status):
            if text_widget is None:
                continue
            text_widget.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
            )
            text_widget.setMinimumWidth(0)
        layout.addWidget(label)
        layout.addWidget(description)
        if status is not None:
            layout.addWidget(status)

    def _add_path_block(self, layout, label, description, editor, buttons, status=None):
        """Add a path editor whose action buttons can wrap below the field."""

        self._add_text_block(layout, label, description, status)
        self._configure_expanding(editor)
        layout.addWidget(editor)
        button_layout = QHBoxLayout()
        button_layout.setSpacing(8)
        for button in buttons:
            self._configure_expanding(button)
            button_layout.addWidget(button, 1)
        layout.addLayout(button_layout)

    def _add_choice_block(self, layout, label, description, control):
        self._add_text_block(layout, label, description)
        self._configure_expanding(control)
        layout.addWidget(control)

    def setup_ui(self):
        """Keep categories fixed on the left and scroll the selected settings."""

        outer_layout = QHBoxLayout(self)
        outer_layout.setContentsMargins(20, 18, 20, 20)
        outer_layout.setSpacing(18)
        self.category_list = ListWidget(self)
        self.category_list.setObjectName("settingsCategories")
        self.category_list.setFixedWidth(190)
        self.category_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.category_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer_layout.addWidget(self.category_list)
        self.settings_scroll = ScrollArea(self)
        self.settings_scroll.enableTransparentBackground()
        self.settings_scroll.setObjectName("settingsScroll")
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.settings_scroll.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.settings_scroll.setStyleSheet(
            "QScrollArea#settingsScroll { background: transparent; border: none; }"
            "QScrollArea#settingsScroll > QWidget { background: transparent; border: none; }"
            "QWidget#qt_scrollarea_viewport { background: transparent; border: none; }"
        )
        self.settings_scroll.viewport().setStyleSheet(
            "background: transparent; border: none;"
        )
        content = QWidget()
        content.setObjectName("settingsScrollContent")
        self.settings_content = content
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        content.setMinimumWidth(0)
        content.setStyleSheet(
            "QWidget#settingsScrollContent { background: transparent; border: none; }"
        )
        self.settings_scroll.setWidget(content)
        outer_layout.addWidget(self.settings_scroll, 1)
        main_layout = QVBoxLayout(content)
        main_layout.setContentsMargins(0, 0, 12, 12)
        main_layout.setSpacing(16)

        self.title_label = SubtitleLabel("", self)
        self.title_label.setWordWrap(True)
        self.title_label.setMinimumWidth(0)
        self.title_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        main_layout.addWidget(self.title_label)

        # Preserve the existing control construction and signal wiring. Cards
        # now share one content column; category selection controls visibility.
        left_column = right_column = main_layout
        self.settings_columns_layout = None
        self.settings_left_column = self.settings_right_column = main_layout

        # General preferences -------------------------------------------------
        general_card, general_layout = self._new_settings_card(
            content, "settingsGeneralCard"
        )
        self.general_card = general_card
        left_column.addWidget(general_card)

        self.language_section_title = SubtitleLabel("", general_card)
        self.language_section_title.setWordWrap(True)
        self.language_section_title.setMinimumWidth(0)
        self.language_section_title.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        general_layout.addWidget(self.language_section_title)
        self.language_label = BodyLabel("", general_card)
        self.language_desc = CaptionLabel("", general_card)
        self.language_desc.setWordWrap(True)
        self.language_combo = ComboBox(general_card)
        self.language_combo.currentIndexChanged.connect(self.on_language_combo_changed)
        self._add_choice_block(
            general_layout,
            self.language_label,
            self.language_desc,
            self.language_combo,
        )
        self.language_note = CaptionLabel("", general_card)
        self.language_note.setWordWrap(True)
        general_layout.addWidget(self.language_note)

        self.theme_section_title = SubtitleLabel("", general_card)
        self.theme_section_title.setWordWrap(True)
        self.theme_section_title.setMinimumWidth(0)
        self.theme_section_title.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        general_layout.addWidget(self.theme_section_title)
        self.theme_label = BodyLabel("", general_card)
        self.theme_desc = CaptionLabel("", general_card)
        self.theme_desc.setWordWrap(True)
        self.theme_combo = ComboBox(general_card)
        self.theme_combo.currentIndexChanged.connect(self.on_theme_combo_changed)
        self._add_choice_block(
            general_layout,
            self.theme_label,
            self.theme_desc,
            self.theme_combo,
        )
        for widget in (self.theme_label, self.theme_desc, self.theme_combo):
            widget.hide()

        # Application font ---------------------------------------------------
        # QFluentWidgets assigns explicit fonts to its labels and controls,
        # so this preference is applied live by MainWindow after persistence.
        self.font_label = BodyLabel("", general_card)
        self.font_desc = CaptionLabel("", general_card)
        self.font_desc.setWordWrap(True)
        self._add_text_block(general_layout, self.font_label, self.font_desc)

        self.font_family_label = BodyLabel("", general_card)
        self.font_family_desc = CaptionLabel("", general_card)
        self.font_family_desc.setWordWrap(True)
        self.font_family_combo = ComboBox(general_card)
        self.font_family_combo.currentIndexChanged.connect(
            self.on_font_family_combo_changed
        )
        self._add_choice_block(
            general_layout,
            self.font_family_label,
            self.font_family_desc,
            self.font_family_combo,
        )

        self.font_size_label = BodyLabel("", general_card)
        self.font_size_desc = CaptionLabel("", general_card)
        self.font_size_desc.setWordWrap(True)
        self.font_size_spin = SpinBox(general_card)
        self.font_size_spin.setRange(MIN_FONT_SIZE, MAX_FONT_SIZE)
        self.font_size_spin.setSingleStep(1)
        self.font_size_spin.valueChanged.connect(self.on_font_size_changed)
        self._add_choice_block(
            general_layout,
            self.font_size_label,
            self.font_size_desc,
            self.font_size_spin,
        )

        self.font_reset_button = PushButton("", general_card)
        self.font_reset_button.clicked.connect(self.reset_font_preferences)
        self._configure_expanding(self.font_reset_button)
        general_layout.addWidget(self.font_reset_button)

        # Output and archive settings ----------------------------------------
        runtime_card, runtime_layout = self._new_settings_card(
            content, "settingsRuntimeCard"
        )
        self.runtime_card = runtime_card
        left_column.addWidget(runtime_card)
        self.runtime_section_title = SubtitleLabel("", runtime_card)
        self.runtime_section_title.setWordWrap(True)
        self.runtime_section_title.setMinimumWidth(0)
        self.runtime_section_title.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        runtime_layout.addWidget(self.runtime_section_title)
        self.output_root_label = BodyLabel("", runtime_card)
        self.output_root_desc = CaptionLabel("", runtime_card)
        self.output_root_desc.setWordWrap(True)
        self.output_root_edit = LineEdit(runtime_card)
        self.output_root_edit.setReadOnly(True)
        self.output_root_button = PushButton("", runtime_card)
        self.output_root_button.clicked.connect(self.browse_output_root)
        self._add_path_block(
            runtime_layout,
            self.output_root_label,
            self.output_root_desc,
            self.output_root_edit,
            (self.output_root_button,),
        )
        self.setting_file_note = CaptionLabel("", runtime_card)
        self.setting_file_note.setWordWrap(True)
        self.setting_file_note.setMinimumWidth(0)
        self.setting_file_note.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        runtime_layout.addWidget(self.setting_file_note)
        runtime_action_row = QHBoxLayout()
        runtime_action_row.setSpacing(8)
        self.detect_tools_button = PushButton("", runtime_card)
        self.detect_tools_button.clicked.connect(self.auto_detect_toolchain)
        self.save_button = PrimaryPushButton("", runtime_card)
        self.save_button.clicked.connect(self.save_runtime_settings)
        self._configure_expanding(self.detect_tools_button)
        self._configure_expanding(self.save_button)
        runtime_action_row.addWidget(self.detect_tools_button, 1)
        runtime_action_row.addWidget(self.save_button, 1)
        runtime_layout.addLayout(runtime_action_row)

        archive_card, archive_layout = self._new_settings_card(
            content, "settingsArchiveCard"
        )
        self.archive_card = archive_card
        left_column.addWidget(archive_card)
        self.archive_tool_label = BodyLabel("", archive_card)
        self.archive_tool_desc = CaptionLabel("", archive_card)
        self.archive_tool_desc.setWordWrap(True)
        self.archive_tool_status = CaptionLabel("", archive_card)
        self.archive_tool_status.setWordWrap(True)
        self.archive_tool_edit = LineEdit(archive_card)
        self.archive_tool_edit.editingFinished.connect(self.on_archive_tool_edit_finished)
        self.archive_tool_button = PushButton("", archive_card)
        self.archive_tool_button.clicked.connect(self.browse_archive_tool)
        self.archive_tool_auto_button = PushButton("", archive_card)
        self.archive_tool_auto_button.clicked.connect(self.auto_detect_archive_tool)
        self._add_path_block(
            archive_layout,
            self.archive_tool_label,
            self.archive_tool_desc,
            self.archive_tool_edit,
            (self.archive_tool_button, self.archive_tool_auto_button),
            self.archive_tool_status,
        )

        # Asset and Live2D tools ---------------------------------------------
        asset_card, asset_layout = self._new_settings_card(
            content, "settingsAssetToolsCard"
        )
        self.asset_tools_card = asset_card
        left_column.addWidget(asset_card)
        self.assetstudio_tool_label = BodyLabel("", asset_card)
        self.assetstudio_tool_desc = CaptionLabel("", asset_card)
        self.assetstudio_tool_desc.setWordWrap(True)
        self.assetstudio_tool_status = CaptionLabel("", asset_card)
        self.assetstudio_tool_status.setWordWrap(True)
        self.assetstudio_tool_edit = LineEdit(asset_card)
        self.assetstudio_tool_edit.editingFinished.connect(self.on_assetstudio_tool_edit_finished)
        self.assetstudio_tool_button = PushButton("", asset_card)
        self.assetstudio_tool_button.clicked.connect(self.browse_assetstudio_tool)
        self.assetstudio_tool_auto_button = PushButton("", asset_card)
        self.assetstudio_tool_auto_button.clicked.connect(self.auto_detect_assetstudio_tool)
        self._add_path_block(
            asset_layout,
            self.assetstudio_tool_label,
            self.assetstudio_tool_desc,
            self.assetstudio_tool_edit,
            (self.assetstudio_tool_button, self.assetstudio_tool_auto_button),
            self.assetstudio_tool_status,
        )
        cubism_card, cubism_layout = self._new_settings_card(content, "settingsCubismCard")
        self.cubism_card = cubism_card
        left_column.addWidget(cubism_card)
        self.cubism_core_label = BodyLabel("", cubism_card)
        self.cubism_core_desc = CaptionLabel("", cubism_card)
        self.cubism_core_desc.setWordWrap(True)
        self.cubism_core_status = CaptionLabel("", cubism_card)
        self.cubism_core_status.setWordWrap(True)
        self.cubism_core_edit = LineEdit(cubism_card)
        self.cubism_core_edit.editingFinished.connect(self.on_cubism_core_edit_finished)
        self.cubism_core_button = PushButton("", cubism_card)
        self.cubism_core_button.clicked.connect(self.browse_cubism_core)
        self.cubism_core_auto_button = PushButton("", cubism_card)
        self.cubism_core_auto_button.clicked.connect(self.auto_detect_cubism_core)
        self._add_path_block(
            cubism_layout,
            self.cubism_core_label,
            self.cubism_core_desc,
            self.cubism_core_edit,
            (self.cubism_core_button, self.cubism_core_auto_button),
            self.cubism_core_status,
        )

        # Optional installation sources --------------------------------------
        download_card, download_layout = self._new_settings_card(
            content, "settingsDownloadCard"
        )
        self.download_card = download_card
        left_column.addWidget(download_card)
        self.tool_download_section_title = SubtitleLabel("", download_card)
        self.tool_download_section_title.setWordWrap(True)
        self.tool_download_section_title.setMinimumWidth(0)
        self.tool_download_section_title.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        download_layout.addWidget(self.tool_download_section_title)
        self.tool_download_desc = CaptionLabel("", download_card)
        self.tool_download_desc.setWordWrap(True)
        self.tool_download_desc.setMinimumWidth(0)
        self.tool_download_desc.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        download_layout.addWidget(self.tool_download_desc)
        self.assetstudio_install_button = PushButton("", download_card)
        self.assetstudio_install_button.clicked.connect(
            lambda: self.start_tool_install("assetstudio_cli")
        )
        self.cubism_official_button = PushButton("", download_card)
        self.cubism_official_button.clicked.connect(self.open_cubism_download_page)
        self.cubism_extract_button = PushButton("", download_card)
        self.cubism_extract_button.clicked.connect(self.install_cubism_from_live2d_py)
        for button in (
            self.assetstudio_install_button,
            self.cubism_official_button,
            self.cubism_extract_button,
        ):
            self._configure_expanding(button)
            download_layout.addWidget(button)

        spine_download_row = QHBoxLayout()
        spine_download_row.setSpacing(8)
        self.spine_native_family_combo = ComboBox(download_card)
        self._configure_expanding(self.spine_native_family_combo)
        spine_download_row.addWidget(self.spine_native_family_combo, 1)
        self.spine_native_install_button = PushButton("", download_card)
        self._configure_expanding(self.spine_native_install_button)
        self.spine_native_install_button.clicked.connect(
            lambda: self.start_tool_install("spine_native")
        )
        spine_download_row.addWidget(self.spine_native_install_button, 1)
        download_layout.addLayout(spine_download_row)

        self.tool_download_progress = ProgressBar(download_card)
        self.tool_download_progress.setRange(0, 100)
        self.tool_download_progress.setValue(0)
        self.tool_download_progress.setVisible(False)
        download_layout.addWidget(self.tool_download_progress)
        self.tool_download_cancel = PushButton("", download_card)
        self.tool_download_cancel.clicked.connect(self.cancel_tool_install)
        self.tool_download_cancel.setVisible(False)
        self._configure_expanding(self.tool_download_cancel)
        download_layout.addWidget(self.tool_download_cancel)
        self.tool_download_status = CaptionLabel("", download_card)
        self.tool_download_status.setWordWrap(True)
        download_layout.addWidget(self.tool_download_status)

        # Photoshop and Spine -------------------------------------------------
        photoshop_card, photoshop_layout = self._new_settings_card(
            content, "settingsPhotoshopCard"
        )
        self.photoshop_card = photoshop_card
        right_column.addWidget(photoshop_card)
        self.photoshop_label = BodyLabel("", photoshop_card)
        self.photoshop_desc = CaptionLabel("", photoshop_card)
        self.photoshop_desc.setWordWrap(True)
        self.photoshop_status = CaptionLabel("", photoshop_card)
        self.photoshop_status.setWordWrap(True)
        self.photoshop_edit = LineEdit(photoshop_card)
        self.photoshop_edit.editingFinished.connect(self.on_photoshop_edit_finished)
        self.photoshop_button = PushButton("", photoshop_card)
        self.photoshop_button.clicked.connect(self.browse_photoshop)
        self.photoshop_clear_button = PushButton("", photoshop_card)
        self.photoshop_clear_button.clicked.connect(self.clear_photoshop)
        self._add_path_block(
            photoshop_layout,
            self.photoshop_label,
            self.photoshop_desc,
            self.photoshop_edit,
            (self.photoshop_button, self.photoshop_clear_button),
            self.photoshop_status,
        )

        spine_card, spine_layout = self._new_settings_card(
            content, "settingsSpineCard"
        )
        self.spine_card = spine_card
        right_column.addWidget(spine_card)
        self.spine_section_title = SubtitleLabel(
            tr("settings.spine_section", "Spine"), spine_card
        )
        self.spine_section_title.setWordWrap(True)
        self.spine_section_title.setMinimumWidth(0)
        self.spine_section_title.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        spine_layout.addWidget(self.spine_section_title)

        # One compatibility switch owns both formal extraction and preview.
        # The old independent controls remain available to the settings
        # migration layer but are hidden from this consolidated surface.
        self.spine_compatibility_label = BodyLabel("", spine_card)
        self.spine_compatibility_desc = CaptionLabel("", spine_card)
        self.spine_compatibility_desc.setWordWrap(True)
        self.spine_compatibility_checkbox = CheckBox(spine_card)
        self._add_text_block(
            spine_layout,
            self.spine_compatibility_label,
            self.spine_compatibility_desc,
        )
        self._configure_expanding(self.spine_compatibility_checkbox)
        self.spine_compatibility_checkbox.toggled.connect(
            self._on_spine_compatibility_toggled
        )
        spine_layout.addWidget(self.spine_compatibility_checkbox)

        self.spine_runtime_version_label = BodyLabel("", spine_card)
        self.spine_runtime_version_desc = CaptionLabel("", spine_card)
        self.spine_runtime_version_desc.setWordWrap(True)
        self.spine_runtime_version_combo = EditableComboBox(spine_card)
        self.spine_runtime_version_combo.setPlaceholderText("3.8.75 / 4.0")
        self.spine_runtime_version_combo.currentTextChanged.connect(
            self._on_spine_runtime_version_changed
        )
        self.spine_runtime_download_button = QToolButton(spine_card)
        self.spine_runtime_download_button.setText("↓")
        self.spine_runtime_download_button.setToolTip("Download verified runtime")
        self.spine_runtime_download_button.clicked.connect(
            self.open_spine_runtime_catalog
        )
        self._add_text_block(
            spine_layout,
            self.spine_runtime_version_label,
            self.spine_runtime_version_desc,
        )
        runtime_version_row = QHBoxLayout()
        runtime_version_row.setSpacing(8)
        self._configure_expanding(self.spine_runtime_version_combo)
        runtime_version_row.addWidget(self.spine_runtime_version_combo, 1)
        runtime_version_row.addWidget(self.spine_runtime_download_button)
        spine_layout.addLayout(runtime_version_row)

        self.spine_runtime_advanced_toggle = PushButton(spine_card)
        self.spine_runtime_advanced_toggle.setCheckable(True)
        self.spine_runtime_advanced_toggle.setChecked(False)
        self.spine_runtime_advanced_toggle.toggled.connect(
            self._toggle_spine_runtime_advanced
        )
        self._configure_expanding(self.spine_runtime_advanced_toggle)
        spine_layout.addWidget(self.spine_runtime_advanced_toggle)

        self.spine_runtime_label = BodyLabel("", spine_card)
        self.spine_runtime_desc = CaptionLabel("", spine_card)
        self.spine_runtime_desc.setWordWrap(True)
        self.spine_runtime_status = CaptionLabel("", spine_card)
        self.spine_runtime_status.setWordWrap(True)
        self.spine_runtime_edit = LineEdit(spine_card)
        self.spine_runtime_edit.editingFinished.connect(self.on_spine_runtime_edit_finished)
        self.spine_runtime_button = PushButton("", spine_card)
        self.spine_runtime_button.clicked.connect(self.browse_spine_runtime)
        self.spine_runtime_auto_button = PushButton("", spine_card)
        self.spine_runtime_auto_button.clicked.connect(self.auto_detect_spine_runtime)
        self._add_path_block(
            spine_layout,
            self.spine_runtime_label,
            self.spine_runtime_desc,
            self.spine_runtime_edit,
            (self.spine_runtime_button, self.spine_runtime_auto_button),
            self.spine_runtime_status,
        )
        self._set_spine_runtime_advanced_visible(False)

        self.spine_editor_label = BodyLabel("", spine_card)
        self.spine_editor_desc = CaptionLabel("", spine_card)
        self.spine_editor_desc.setWordWrap(True)
        self.spine_editor_status = CaptionLabel("", spine_card)
        self.spine_editor_status.setWordWrap(True)
        self.spine_editor_edit = LineEdit(spine_card)
        self.spine_editor_edit.editingFinished.connect(self.on_spine_editor_edit_finished)
        self.spine_editor_button = PushButton("", spine_card)
        self.spine_editor_button.clicked.connect(self.browse_spine_editor)
        self.spine_editor_clear_button = PushButton("", spine_card)
        self.spine_editor_clear_button.clicked.connect(self.clear_spine_editor)
        self._add_path_block(
            spine_layout,
            self.spine_editor_label,
            self.spine_editor_desc,
            self.spine_editor_edit,
            (self.spine_editor_button, self.spine_editor_clear_button),
            self.spine_editor_status,
        )

        self.spine_create_project_label = BodyLabel("", spine_card)
        self.spine_create_project_desc = CaptionLabel("", spine_card)
        self.spine_create_project_desc.setWordWrap(True)
        self.spine_create_project_checkbox = CheckBox(spine_card)
        self._add_text_block(
            spine_layout,
            self.spine_create_project_label,
            self.spine_create_project_desc,
        )
        self._configure_expanding(self.spine_create_project_checkbox)
        spine_layout.addWidget(self.spine_create_project_checkbox)

        self.spine_auto_convert_label = BodyLabel("", spine_card)
        self.spine_auto_convert_desc = CaptionLabel("", spine_card)
        self.spine_auto_convert_desc.setWordWrap(True)
        self.spine_auto_convert_checkbox = CheckBox(spine_card)
        self._add_text_block(
            spine_layout,
            self.spine_auto_convert_label,
            self.spine_auto_convert_desc,
        )
        self._configure_expanding(self.spine_auto_convert_checkbox)
        spine_layout.addWidget(self.spine_auto_convert_checkbox)

        self.spine_target_version_label = BodyLabel("", spine_card)
        self.spine_target_version_desc = CaptionLabel("", spine_card)
        self.spine_target_version_desc.setWordWrap(True)
        self.spine_target_version_edit = LineEdit(spine_card)
        self._add_choice_block(
            spine_layout,
            self.spine_target_version_label,
            self.spine_target_version_desc,
            self.spine_target_version_edit,
        )

        self.spine_preview_unify_label = BodyLabel("", spine_card)
        self.spine_preview_unify_desc = CaptionLabel("", spine_card)
        self.spine_preview_unify_desc.setWordWrap(True)
        self.spine_preview_unify_checkbox = CheckBox(spine_card)
        self._add_text_block(
            spine_layout,
            self.spine_preview_unify_label,
            self.spine_preview_unify_desc,
        )
        self._configure_expanding(self.spine_preview_unify_checkbox)
        spine_layout.addWidget(self.spine_preview_unify_checkbox)
        for legacy_widget in (
            self.spine_create_project_label,
            self.spine_create_project_desc,
            self.spine_create_project_checkbox,
            self.spine_auto_convert_label,
            self.spine_auto_convert_desc,
            self.spine_auto_convert_checkbox,
            self.spine_target_version_label,
            self.spine_target_version_desc,
            self.spine_target_version_edit,
            self.spine_preview_unify_label,
            self.spine_preview_unify_desc,
            self.spine_preview_unify_checkbox,
        ):
            legacy_widget.setVisible(False)

        # Texture preview settings -------------------------------------------
        texture_card, texture_layout = self._new_settings_card(
            content, "settingsTextureCard"
        )
        self.texture_card = texture_card
        right_column.addWidget(texture_card)
        self.texture_viewer_label = BodyLabel("", texture_card)
        self.texture_viewer_desc = CaptionLabel("", texture_card)
        self.texture_viewer_desc.setWordWrap(True)
        self.texture_viewer_combo = ComboBox(texture_card)
        self._add_choice_block(
            texture_layout,
            self.texture_viewer_label,
            self.texture_viewer_desc,
            self.texture_viewer_combo,
        )
        self.preview_image_limit_label = BodyLabel("", texture_card)
        self.preview_image_limit_desc = CaptionLabel("", texture_card)
        self.preview_image_limit_desc.setWordWrap(True)
        self.preview_image_limit_spin = SpinBox(texture_card)
        self.preview_image_limit_spin.setRange(1, 500)
        self.preview_image_limit_spin.valueChanged.connect(self.on_preview_image_limit_changed)
        self._add_choice_block(
            texture_layout, self.preview_image_limit_label,
            self.preview_image_limit_desc, self.preview_image_limit_spin,
        )
        self.image_viewer_edit = LineEdit(texture_card)
        self.image_viewer_label = BodyLabel("", texture_card)
        self.image_viewer_desc = CaptionLabel("", texture_card)
        self.image_viewer_desc.setWordWrap(True)
        self.image_viewer_button = PushButton("", texture_card)
        self.image_viewer_button.clicked.connect(self.browse_image_viewer)
        self.image_viewer_clear_button = PushButton("", texture_card)
        self.image_viewer_clear_button.clicked.connect(self.clear_image_viewer)
        self._add_path_block(
            texture_layout,
            self.image_viewer_label,
            self.image_viewer_desc,
            self.image_viewer_edit,
            (self.image_viewer_button, self.image_viewer_clear_button),
        )

        mcp_card, mcp_layout = self._new_settings_card(content, "settingsMcpCard")
        self.mcp_card = mcp_card
        right_column.addWidget(mcp_card)
        self.mcp_title = SubtitleLabel("", mcp_card)
        self.mcp_description = CaptionLabel("", mcp_card)
        self.mcp_description.setWordWrap(True)
        mcp_layout.addWidget(self.mcp_title)
        mcp_layout.addWidget(self.mcp_description)
        transport_row = QHBoxLayout()
        self.mcp_transport_label = BodyLabel("", mcp_card)
        self.mcp_transport_combo = ComboBox(mcp_card)
        for value in self._mcp_transport_values:
            self.mcp_transport_combo.addItem(value)
        transport_row.addWidget(self.mcp_transport_label)
        transport_row.addWidget(self.mcp_transport_combo, 1)
        mcp_layout.addLayout(transport_row)
        self.mcp_workspace_label = BodyLabel("", mcp_card)
        self.mcp_workspace_description = CaptionLabel("", mcp_card)
        self.mcp_workspace_description.setWordWrap(True)
        self.mcp_workspace_edit = self._configure_expanding(LineEdit(mcp_card))
        self.mcp_workspace_button = PushButton("", mcp_card)
        self.mcp_workspace_button.clicked.connect(self._choose_mcp_workspace)
        self._add_path_block(
            mcp_layout, self.mcp_workspace_label, self.mcp_workspace_description,
            self.mcp_workspace_edit, (self.mcp_workspace_button,),
        )
        self.mcp_http_options = QWidget(mcp_card)
        port_layout = QHBoxLayout(self.mcp_http_options)
        port_layout.setContentsMargins(0, 0, 0, 0)
        self.mcp_port_label = BodyLabel("", self.mcp_http_options)
        self.mcp_port_spin = SpinBox(self.mcp_http_options)
        self.mcp_port_spin.setRange(1, 65535)
        self.mcp_port_spin.setValue(DEFAULT_MCP_PORT)
        self.mcp_port_spin.setKeyboardTracking(False)
        self.mcp_port_spin.setMinimumWidth(105)
        self.mcp_port_hint = CaptionLabel("", self.mcp_http_options)
        self.mcp_port_hint.setWordWrap(True)
        port_layout.addWidget(self.mcp_port_label)
        port_layout.addWidget(self.mcp_port_spin)
        port_layout.addWidget(self.mcp_port_hint, 1)
        mcp_layout.addWidget(self.mcp_http_options)

        connection_card, connection_layout = self._new_settings_card(content, "settingsMcpConnectionCard")
        self.mcp_connection_card = connection_card
        right_column.addWidget(connection_card)
        self.mcp_connection_title = SubtitleLabel("", connection_card)
        self.mcp_status = CaptionLabel("", connection_card)
        self.mcp_status.setWordWrap(True)
        connection_layout.addWidget(self.mcp_connection_title)
        connection_layout.addWidget(self.mcp_status)
        self.mcp_endpoint_row = QWidget(connection_card)
        endpoint_layout = QHBoxLayout(self.mcp_endpoint_row)
        endpoint_layout.setContentsMargins(0, 0, 0, 0)
        self.mcp_endpoint_edit = self._configure_expanding(LineEdit(self.mcp_endpoint_row))
        self.mcp_endpoint_edit.setReadOnly(True)
        self.mcp_copy_endpoint_button = PushButton("", self.mcp_endpoint_row)
        self.mcp_copy_endpoint_button.clicked.connect(lambda: QApplication.clipboard().setText(self.mcp_endpoint_edit.text()))
        endpoint_layout.addWidget(self.mcp_endpoint_edit, 1)
        endpoint_layout.addWidget(self.mcp_copy_endpoint_button)
        connection_layout.addWidget(self.mcp_endpoint_row)
        self.mcp_stdio_hint = CaptionLabel("", connection_card)
        self.mcp_stdio_hint.setWordWrap(True)
        connection_layout.addWidget(self.mcp_stdio_hint)
        actions = QHBoxLayout()
        self.mcp_start_button = PrimaryPushButton("", connection_card)
        self.mcp_stop_button = PushButton("", connection_card)
        self.mcp_config_button = PushButton("", connection_card)
        self.mcp_start_button.clicked.connect(self.start_animation_mcp)
        self.mcp_stop_button.clicked.connect(self.shutdown_animation_mcp)
        self.mcp_config_button.clicked.connect(self.show_animation_mcp_config)
        for button in (self.mcp_start_button, self.mcp_stop_button, self.mcp_config_button):
            actions.addWidget(button)
        actions.addStretch(1)
        connection_layout.addLayout(actions)
        self.mcp_log_toggle = PushButton("", connection_card)
        connection_layout.addWidget(self.mcp_log_toggle)
        self.mcp_log = PlainTextEdit(connection_card)
        self.mcp_log.setReadOnly(True)
        self.mcp_log.setMaximumBlockCount(200)
        self.mcp_log.setFixedHeight(130)
        self.mcp_log.hide()
        self.mcp_log_toggle.clicked.connect(lambda: self.mcp_log.setVisible(self.mcp_log.isHidden()))
        connection_layout.addWidget(self.mcp_log)

        guide_card, guide_layout = self._new_settings_card(content, "settingsMcpGuideCard")
        self.mcp_guide_card = guide_card
        right_column.addWidget(guide_card)
        self.mcp_guide_title = SubtitleLabel("", guide_card)
        self.mcp_guide_hint = CaptionLabel("", guide_card)
        self.mcp_guide_hint.setWordWrap(True)
        guide_layout.addWidget(self.mcp_guide_title)
        guide_layout.addWidget(self.mcp_guide_hint)
        guide_actions = QHBoxLayout()
        self.mcp_view_guide_button = PushButton("", guide_card)
        self.mcp_copy_guide_button = PushButton("", guide_card)
        self.mcp_save_guide_button = PushButton("", guide_card)
        self.mcp_view_guide_button.clicked.connect(self.show_animation_mcp_guide)
        self.mcp_copy_guide_button.clicked.connect(self.copy_animation_mcp_guide)
        self.mcp_save_guide_button.clicked.connect(self.save_animation_mcp_guide)
        for button in (self.mcp_view_guide_button, self.mcp_copy_guide_button, self.mcp_save_guide_button):
            guide_actions.addWidget(button)
        guide_actions.addStretch(1)
        guide_layout.addLayout(guide_actions)
        self.mcp_transport_combo.currentIndexChanged.connect(self._mcp_preferences_changed)
        self.mcp_port_spin.valueChanged.connect(self._mcp_preferences_changed)
        self.mcp_workspace_edit.editingFinished.connect(self._mcp_preferences_changed)

        from app.gui.editor_preferences import RecoverySettingsCard, ShortcutSettingsCard
        self.recovery_settings_card = RecoverySettingsCard(self.settings_manager, self.settings_content)
        self.live2d_shortcuts_card = ShortcutSettingsCard("live2d", self.settings_manager, self.settings_content)
        self.spine_shortcuts_card = ShortcutSettingsCard("spine", self.settings_manager, self.settings_content)
        right_column.addWidget(self.recovery_settings_card)
        right_column.addWidget(self.live2d_shortcuts_card)
        right_column.addWidget(self.spine_shortcuts_card)
        main_layout.addStretch(1)
        self._settings_categories = [
            ("general", [general_card, runtime_card, self.recovery_settings_card]),
            ("live2d", [cubism_card, photoshop_card, self.live2d_shortcuts_card]),
            ("spine", [spine_card, self.spine_shortcuts_card]),
            ("resources", [archive_card, asset_card, download_card]),
            ("ai", [mcp_card, connection_card, guide_card]),
            ("other", [texture_card]),
        ]
        for key, _cards in self._settings_categories:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setSizeHint(QSize(0, 46))
            self.category_list.addItem(item)
        self.category_list.currentRowChanged.connect(self._on_settings_category_changed)
        self.category_list.setCurrentRow(0)

    def _on_settings_category_changed(self, row: int) -> None:
        if not 0 <= row < len(self._settings_categories):
            return
        key, selected = self._settings_categories[row]
        for _key, cards in self._settings_categories:
            for card in cards:
                card.setVisible(card in selected)
        self.title_label.setText(tr(f"settings.category.{key}"))
        self.settings_content.layout().activate()
        QTimer.singleShot(0, self.settings_scroll, lambda: self.settings_scroll.verticalScrollBar().setValue(0))

    def select_settings_category(self, key: str) -> bool:
        for row, (category, _cards) in enumerate(self._settings_categories):
            if category == key:
                self.category_list.setCurrentRow(row)
                return True
        return False

    def sync_theme_selection(self, theme: str) -> None:
        """Update the compatibility combo without reloading settings or models."""
        self.settings_manager.settings["theme"] = str(theme).lower()
        self.theme_combo.blockSignals(True)
        try:
            self._set_combo_by_value(self.theme_combo, self._theme_values, theme)
        finally:
            self.theme_combo.blockSignals(False)

    def _choose_mcp_workspace(self):
        directory = QFileDialog.getExistingDirectory(
            self,
            tr("settings.mcp.workspace"),
            self.mcp_workspace_edit.text() or self.settings_manager.get_output_root(),
        )
        if directory:
            self.mcp_workspace_edit.setText(directory)
            self._mcp_preferences_changed()

    def _mcp_connection_options(self) -> dict:
        return {
            "workspace": self.mcp_workspace_edit.text().strip(),
            "transport": self._current_combo_value(self.mcp_transport_combo, self._mcp_transport_values),
            "port": self.mcp_port_spin.value(),
        }

    def _mcp_preferences_changed(self, *_args):
        if not self._syncing_ui:
            self.settings_manager.set("mcp", self._mcp_connection_options())
            if self._mcp_process is None:
                self._mcp_status_key = "settings.mcp.stopped"
                self._mcp_status_error = ""
        self._refresh_mcp_ui()

    def _refresh_mcp_ui(self):
        http = self._mcp_connection_options()["transport"] == "streamable-http"
        busy = self._mcp_process is not None
        self.mcp_http_options.setVisible(http)
        self.mcp_endpoint_row.setVisible(http)
        self.mcp_stdio_hint.setVisible(not http)
        self.mcp_start_button.setVisible(http)
        self.mcp_stop_button.setVisible(http)
        self.mcp_log_toggle.setVisible(http)
        if not http:
            self.mcp_log.hide()
        self.mcp_endpoint_edit.setText(animation_mcp_endpoint(self.mcp_port_spin.value()))
        for widget in (self.mcp_transport_combo, self.mcp_port_spin, self.mcp_workspace_edit, self.mcp_workspace_button):
            widget.setEnabled(not busy)
        self.mcp_start_button.setEnabled(http and not busy)
        self.mcp_stop_button.setEnabled(busy and not self._mcp_stopping)
        self.mcp_status.setText(tr(self._mcp_status_key, error=self._mcp_status_error) if http or self._mcp_status_error else tr("settings.mcp.stdio_owned"))

    def _mcp_error(self, error):
        self._mcp_status_key = "settings.mcp.failed"
        self._mcp_status_error = str(error)
        self._refresh_mcp_ui()
        self.mcp_log.appendPlainText(str(error))

    def _show_mcp_text(self, title: str, text: str, explanation: str):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(790, 540)
        layout = QVBoxLayout(dialog)
        description = BodyLabel(explanation, dialog)
        description.setWordWrap(True)
        layout.addWidget(description)
        editor = PlainTextEdit(dialog)
        editor.setReadOnly(True)
        editor.setPlainText(text)
        layout.addWidget(editor, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        copy_button = PushButton(tr("settings.mcp.copy"), dialog)
        close_button = PushButton(tr("common.close"), dialog)
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(text))
        close_button.clicked.connect(dialog.accept)
        buttons.addWidget(copy_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        dialog.exec()

    def show_animation_mcp_config(self):
        import json
        try:
            configuration = json.dumps(animation_mcp_config(**self._mcp_connection_options()), ensure_ascii=False, indent=2)
        except (OSError, ValueError) as exc:
            self._mcp_error(exc)
            return
        self._show_mcp_text(tr("settings.mcp.dialog_title"), configuration, tr("settings.mcp.instructions"))

    def _animation_mcp_guide_text(self) -> str | None:
        try:
            return animation_mcp_guide(**self._mcp_connection_options())
        except (OSError, ValueError) as exc:
            self._mcp_error(exc)
            return None

    def show_animation_mcp_guide(self):
        guide = self._animation_mcp_guide_text()
        if guide is not None:
            self._show_mcp_text(tr("settings.mcp.guide_title"), guide, tr("settings.mcp.guide_generated"))

    def copy_animation_mcp_guide(self):
        guide = self._animation_mcp_guide_text()
        if guide is not None:
            QApplication.clipboard().setText(guide)

    def save_animation_mcp_guide(self):
        guide = self._animation_mcp_guide_text()
        if guide is None:
            return
        path, _filter = QFileDialog.getSaveFileName(
            self, tr("settings.mcp.guide_save"),
            str(Path(self.mcp_workspace_edit.text()) / "animation-ai-guide.md"),
            tr("settings.mcp.guide_filter"),
        )
        if path:
            try:
                Path(path).write_bytes(guide.encode("utf-8"))
            except OSError as exc:
                self._mcp_error(exc)

    def start_animation_mcp(self) -> bool:
        if self._mcp_process is not None:
            return False
        options = self._mcp_connection_options()
        if options["transport"] != "streamable-http":
            return False
        try:
            launch = animation_mcp_launch(**options, managed=True)
        except (OSError, ValueError) as exc:
            self._mcp_error(exc)
            return False
        self._mcp_preferences_changed()
        self.mcp_log.clear()
        self._mcp_ready_buffer = ""
        self._mcp_starting = True
        self._mcp_stopping = False
        self._mcp_status_key = "settings.mcp.starting"
        self._mcp_status_error = ""
        process = QProcess(self)
        self._mcp_process = process
        environment = QProcessEnvironment.systemEnvironment()
        for key, value in launch.get("env", {}).items():
            environment.insert(key, value)
        environment.insert("PYTHONUNBUFFERED", "1")
        process.setProcessEnvironment(environment)
        process.setWorkingDirectory(options["workspace"])
        process.readyReadStandardError.connect(lambda: self._read_mcp_output(process, False))
        process.readyReadStandardOutput.connect(lambda: self._read_mcp_output(process, True))
        process.errorOccurred.connect(lambda _error: self._on_mcp_process_error(process))
        process.finished.connect(lambda code, _status: self._on_mcp_process_finished(process, code))
        self._refresh_mcp_ui()
        process.start(launch["command"], launch["args"])
        return True

    def _read_mcp_output(self, process, stdout: bool):
        if process is not self._mcp_process:
            return
        raw = process.readAllStandardOutput() if stdout else process.readAllStandardError()
        text = bytes(raw).decode("utf-8", errors="replace")
        if text:
            self.mcp_log.appendPlainText(text.rstrip())
            self._mcp_ready_buffer = (self._mcp_ready_buffer + text)[-4096:]
            if "LPK_MCP_READY " in self._mcp_ready_buffer and not self._mcp_stopping:
                self._mcp_starting = False
                self._mcp_status_key = "settings.mcp.running"
                self._refresh_mcp_ui()

    def _on_mcp_process_error(self, process):
        if process is not self._mcp_process:
            return
        error = process.errorString()
        if process.state() == QProcess.ProcessState.NotRunning:
            self._mcp_process = None
            self._mcp_starting = False
            process.deleteLater()
        self._mcp_error(error)

    def _on_mcp_process_finished(self, process, exit_code: int):
        if process is not self._mcp_process:
            return
        self._read_mcp_output(process, False)
        self._read_mcp_output(process, True)
        self._mcp_process = None
        self._mcp_starting = False
        if exit_code and not self._mcp_stopping:
            self._mcp_status_key = "settings.mcp.failed"
            self._mcp_status_error = self.mcp_log.toPlainText().strip()[-1000:] or str(exit_code)
        else:
            self._mcp_status_key = "settings.mcp.stopped"
            self._mcp_status_error = ""
        process.deleteLater()
        self._refresh_mcp_ui()

    def shutdown_animation_mcp(self) -> bool:
        process = self._mcp_process
        if process is None:
            return True
        self._mcp_stopping = True
        self._mcp_status_key = "settings.mcp.stopping"
        self._refresh_mcp_ui()
        if process.state() == QProcess.ProcessState.Starting:
            process.waitForStarted(1000)
        if self._mcp_process is not process:
            self._mcp_stopping = False
            return True
        process.write(b"stop\n")
        process.closeWriteChannel()
        if not process.waitForFinished(2000):
            process.terminate()
            if not process.waitForFinished(1000):
                process.kill()
                process.waitForFinished(1000)
        finished = self._mcp_process is not process or process.state() == QProcess.ProcessState.NotRunning
        self._mcp_stopping = False
        self._refresh_mcp_ui()
        return finished

    def closeEvent(self, event):
        if not self.shutdown_animation_mcp():
            event.ignore()
            return
        super().closeEvent(event)

    def on_preview_image_limit_changed(self, value):
        if not self._syncing_ui:
            self.settings_manager.set("preview.image_limit", int(value))

    def load_current_settings(self):
        self._syncing_ui = True
        try:
            self.preview_image_limit_spin.setValue(int(self.settings_manager.get("preview.image_limit", 48) or 48))
            transport = str(self.settings_manager.get("mcp.transport", "streamable-http"))
            self._set_combo_by_value(self.mcp_transport_combo, self._mcp_transport_values, transport)
            self.mcp_workspace_edit.setText(str(self.settings_manager.get("mcp.workspace", self.settings_manager.get_output_dir("animations"))))
            port = self.settings_manager.get("mcp.port", DEFAULT_MCP_PORT)
            self.mcp_port_spin.setValue(port if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535 else DEFAULT_MCP_PORT)
            language = normalize_language_code(self.settings_manager.get("language", "en_US"))
            self._set_combo_by_value(self.language_combo, self._language_codes, language)

            theme = str(self.settings_manager.get("theme", "auto")).lower()
            if theme not in self._theme_values:
                theme = "auto"
            self._set_combo_by_value(self.theme_combo, self._theme_values, theme)

            if self.font_family_combo:
                self._refresh_font_family_combo(
                    self.settings_manager.get_font_family()
                )
            if self.font_size_spin:
                self.font_size_spin.setValue(self.settings_manager.get_font_size())

            if self.output_root_edit:
                self.output_root_edit.setText(self.settings_manager.get_output_root())
            if self.archive_tool_edit:
                self.archive_tool_edit.setText(self.settings_manager.get_archive_extractor_path())
            if self.assetstudio_tool_edit:
                self.assetstudio_tool_edit.setText(self.settings_manager.get_assetstudio_cli_path())
            if self.cubism_core_edit:
                self.cubism_core_edit.setText(self.settings_manager.get_cubism_core_dll_path())
            if self.photoshop_edit:
                self.photoshop_edit.setText(self.settings_manager.get_photoshop_path())
            if self.spine_runtime_edit:
                self.spine_runtime_edit.setText(self.settings_manager.get_spine_runtime_dir())
            if self.spine_compatibility_checkbox:
                get_mode = getattr(
                    self.settings_manager, "get_spine_compatibility_mode", None
                )
                self.spine_compatibility_checkbox.setChecked(
                    bool(get_mode()) if callable(get_mode) else True
                )
            self._refresh_spine_runtime_version_combo()
            if self.spine_editor_edit:
                get_editor_path = getattr(
                    self.settings_manager, "get_spine_editor_path", None
                )
                self.spine_editor_edit.setText(
                    get_editor_path() if callable(get_editor_path) else ""
                )
            if self.spine_create_project_checkbox:
                get_create_project = getattr(
                    self.settings_manager, "get_spine_create_project", None
                )
                self.spine_create_project_checkbox.setChecked(
                    bool(get_create_project()) if callable(get_create_project) else True
                )
            if self.spine_auto_convert_checkbox:
                self.spine_auto_convert_checkbox.setChecked(
                    self.settings_manager.get_spine_conversion_enabled()
                )
            if self.spine_target_version_edit:
                self.spine_target_version_edit.setText(
                    self.settings_manager.get_spine_conversion_target_version()
                )
            if self.spine_preview_unify_checkbox:
                get_unify_preview = getattr(
                    self.settings_manager, "get_spine_preview_unify_version", None
                )
                self.spine_preview_unify_checkbox.setChecked(
                    bool(get_unify_preview()) if callable(get_unify_preview) else True
                )
            if self.texture_viewer_combo:
                self._set_combo_by_value(
                    self.texture_viewer_combo,
                    self._texture_viewer_values,
                    self.settings_manager.get_texture_viewer_mode(),
                )
            if self.image_viewer_edit:
                self.image_viewer_edit.setText(
                    self.settings_manager.get_image_viewer_path()
                )
            self.refresh_tool_status_labels()
            self._update_spine_runtime_selection_ui()
        finally:
            self._syncing_ui = False
        self._refresh_mcp_ui()

    def _set_spine_runtime_advanced_visible(self, visible: bool) -> None:
        for widget in (
            getattr(self, "spine_runtime_label", None),
            getattr(self, "spine_runtime_desc", None),
            getattr(self, "spine_runtime_status", None),
            getattr(self, "spine_runtime_edit", None),
            getattr(self, "spine_runtime_button", None),
            getattr(self, "spine_runtime_auto_button", None),
        ):
            if widget is not None:
                widget.setVisible(bool(visible))

    def _toggle_spine_runtime_advanced(self, visible: bool) -> None:
        self._set_spine_runtime_advanced_visible(bool(visible))

    def _on_spine_compatibility_toggled(self, _checked: bool) -> None:
        if self._syncing_ui:
            return
        if self.spine_runtime_version_combo is not None:
            if _checked:
                # Capture the persisted off-mode choice before the display is
                # forced to the compatibility target.
                selected = self._normalize_spine_runtime_choice(
                    self.spine_runtime_version_combo.currentText()
                )
                if selected:
                    self._spine_runtime_version_before_compatibility = selected
            else:
                selected = self._normalize_spine_runtime_choice(
                    self._spine_runtime_version_before_compatibility
                )
                if selected:
                    combo = self.spine_runtime_version_combo
                    if combo.findText(selected) < 0:
                        combo.addItem(selected)
                    combo.blockSignals(True)
                    combo.setCurrentText(selected)
                    combo.blockSignals(False)
        self._update_spine_runtime_selection_ui()

    @staticmethod
    def _normalize_spine_runtime_choice(value: str) -> str | None:
        text = str(value or "").strip()
        if text == SPINE_COMPATIBILITY_VERSION:
            return SPINE_COMPATIBILITY_VERSION
        if text == "4.0" or text.startswith("4.0."):
            return "4.0"
        return None

    def _on_spine_runtime_version_changed(self, _value: str) -> None:
        if self._syncing_ui:
            return
        # EditableComboBox is searchable, but the value remains constrained to
        # verified native families when the user leaves the field.
        if self.spine_runtime_version_combo is None:
            return
        text = self.spine_runtime_version_combo.currentText().strip()
        if text.startswith("4.0."):
            text = "4.0"
        if text not in SPINE_RUNTIME_SUPPORTED_VERSIONS:
            return
        self.settings_manager.set_spine_runtime_version(text)

    def _refresh_spine_runtime_version_combo(self) -> None:
        combo = self.spine_runtime_version_combo
        if combo is None:
            return
        selected = SPINE_COMPATIBILITY_VERSION
        getter = getattr(self.settings_manager, "get_spine_runtime_version", None)
        if callable(getter):
            selected = str(getter() or selected)
        selected = self._normalize_spine_runtime_choice(selected) or SPINE_COMPATIBILITY_VERSION
        self._spine_runtime_version_before_compatibility = selected
        root = self.settings_manager.get_spine_runtime_dir()
        try:
            installed = list_installed_spine_runtimes(root or None)
        except Exception:
            installed = ()
        values = [
            self._normalize_spine_runtime_choice(str(item.version))
            for item in installed
        ]
        values = [value for value in values if value]
        if selected not in values:
            values.append(selected)
        if not values:
            values = [selected]
        values = list(dict.fromkeys(values))
        combo.blockSignals(True)
        combo.clear()
        for value in values:
            combo.addItem(value)
        if selected in values:
            combo.setCurrentText(selected)
        combo.blockSignals(False)
        self._update_spine_runtime_selection_ui()

    def _update_spine_runtime_selection_ui(self) -> None:
        compatibility = bool(
            self.spine_compatibility_checkbox
            and self.spine_compatibility_checkbox.isChecked()
        )
        combo = self.spine_runtime_version_combo
        if combo is not None:
            combo.blockSignals(True)
            if compatibility:
                combo.setCurrentText(SPINE_COMPATIBILITY_VERSION)
            else:
                selected = self._normalize_spine_runtime_choice(
                    self._spine_runtime_version_before_compatibility
                )
                if selected:
                    if combo.findText(selected) < 0:
                        combo.addItem(selected)
                    combo.setCurrentText(selected)
            combo.setEnabled(not compatibility)
            combo.blockSignals(False)
        if self.spine_runtime_download_button:
            self.spine_runtime_download_button.setEnabled(True)

    def open_spine_runtime_catalog(self) -> None:
        """Show the verified Spine source-build catalog.

        The arrow intentionally lists only the two pinned native packages.
        They are official source archives built by the local bridge builder;
        this dialog never invents a pre-built DLL URL for an arbitrary Spine
        version.
        """

        manifests = tuple(
            manifest
            for manifest in list_tool_package_manifests()
            if manifest.package_id in {"spine_native", "spine_native_4_0"}
            and manifest.runtime_family in {"3.8", "4.0"}
            and (manifest.is_pinned_archive or manifest.requires_builder)
        )
        dialog = QDialog(self)
        dialog.setWindowTitle(
            tr("settings.spine_runtime_catalog_title", "Verified Spine runtimes")
        )
        dialog.setMinimumSize(500, 300)
        layout = QVBoxLayout(dialog)
        filter_edit = LineEdit(dialog)
        filter_edit.setPlaceholderText(
            tr("settings.spine_runtime_catalog_filter", "Filter versions")
        )
        layout.addWidget(filter_edit)
        entries = QListWidget(dialog)
        layout.addWidget(entries, 1)
        note = CaptionLabel(
            tr(
                "settings.spine_runtime_catalog_note",
                "Packages are pinned official source archives and require the local CMake/compiler builder.",
            ),
            dialog,
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=dialog,
        )
        layout.addWidget(buttons)

        def populate(query: str = "") -> None:
            needle = str(query or "").strip().casefold()
            entries.clear()
            for manifest in manifests:
                label = f"Spine {manifest.version} ({manifest.runtime_family})"
                if needle and needle not in label.casefold() and needle not in manifest.notes.casefold():
                    continue
                item = QListWidgetItem(label, entries)
                item.setData(Qt.ItemDataRole.UserRole, manifest.package_id)
                item.setToolTip(manifest.notes)
            entries.setCurrentRow(0 if entries.count() else -1)

        filter_edit.textChanged.connect(populate)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        populate()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        item = entries.currentItem()
        package_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        if package_id:
            self.start_tool_install(str(package_id))

    def retranslate_ui(self):
        self._syncing_ui = True
        try:
            for row, (key, _cards) in enumerate(self._settings_categories):
                self.category_list.item(row).setText(tr(f"settings.category.{key}"))
            row = max(0, self.category_list.currentRow())
            self.title_label.setText(tr(f"settings.category.{self._settings_categories[row][0]}"))
            self.mcp_title.setText(tr("settings.mcp.title"))
            self.mcp_description.setText(tr("settings.mcp.description"))
            self.mcp_config_button.setText(tr("settings.mcp.button"))
            self.mcp_transport_label.setText(tr("settings.mcp.transport_label"))
            current_transport = self._current_combo_value(self.mcp_transport_combo, self._mcp_transport_values)
            self.mcp_transport_combo.clear()
            self.mcp_transport_combo.addItems([tr("settings.mcp.http_transport"), tr("settings.mcp.stdio_transport")])
            self._set_combo_by_value(self.mcp_transport_combo, self._mcp_transport_values, current_transport)
            for widget, key in (
                (self.mcp_workspace_label, "workspace_label"),
                (self.mcp_workspace_description, "workspace_description"),
                (self.mcp_workspace_button, "choose_workspace"),
                (self.mcp_port_label, "port_label"),
                (self.mcp_port_hint, "port_hint"),
                (self.mcp_connection_title, "connection_title"),
                (self.mcp_copy_endpoint_button, "copy_endpoint"),
                (self.mcp_stdio_hint, "stdio_owned"),
                (self.mcp_start_button, "start"),
                (self.mcp_stop_button, "stop"),
                (self.mcp_log_toggle, "log"),
                (self.mcp_guide_title, "guide_title"),
                (self.mcp_guide_hint, "guide_hint"),
                (self.mcp_view_guide_button, "guide_view"),
                (self.mcp_copy_guide_button, "guide_copy"),
                (self.mcp_save_guide_button, "guide_save"),
            ):
                widget.setText(tr(f"settings.mcp.{key}"))
            self._refresh_mcp_ui()

            self.language_section_title.setText(tr("settings.section.language"))
            self.language_label.setText(tr("settings.language_label"))
            self.language_desc.setText(tr("settings.language_desc"))
            self.language_note.setText(tr("settings.language_note"))

            current_language = self._current_combo_value(self.language_combo, self._language_codes)
            self.language_combo.clear()
            for code in self._language_codes:
                if code == "en_US":
                    label = tr("settings.language.english")
                elif code == "zh_CN":
                    label = tr("settings.language.chinese")
                else:
                    label = tr("settings.language.japanese")
                self.language_combo.addItem(label)
            self._set_combo_by_value(self.language_combo, self._language_codes, current_language)

            self.theme_section_title.setText(tr("settings.section.appearance"))
            self.theme_label.setText(tr("settings.theme_label"))
            self.theme_desc.setText(tr("settings.theme_desc"))

            current_theme = self._current_combo_value(self.theme_combo, self._theme_values)
            self.theme_combo.clear()
            for theme in self._theme_values:
                if theme == "auto":
                    label = tr("settings.theme.auto")
                elif theme == "light":
                    label = tr("settings.theme.light")
                else:
                    label = tr("settings.theme.dark")
                self.theme_combo.addItem(label)
            self._set_combo_by_value(self.theme_combo, self._theme_values, current_theme)

            self.font_label.setText(tr("settings.font_label"))
            self.font_desc.setText(tr("settings.font_desc"))
            self.font_family_label.setText(tr("settings.font_family_label"))
            self.font_family_desc.setText(tr("settings.font_family_desc"))
            self.font_size_label.setText(tr("settings.font_size_label"))
            self.font_size_desc.setText(tr("settings.font_size_desc"))
            self.font_size_spin.setSuffix(
                f" {tr('settings.font_size_unit', 'pt')}"
            )
            self.font_reset_button.setText(tr("settings.font_reset_button"))
            self._refresh_font_family_combo(
                self.settings_manager.get_font_family()
            )

            self.runtime_section_title.setText(tr("settings.section.runtime"))
            self.output_root_label.setText(tr("settings.output_root_label"))
            self.output_root_desc.setText(tr("settings.output_root_desc"))
            self.output_root_button.setText(tr("common.browse"))
            self.output_root_edit.setText(self.settings_manager.get_output_root())
            self.archive_tool_label.setText(tr("settings.archive_tool_label"))
            self.archive_tool_desc.setText(tr("settings.archive_tool_desc"))
            self.archive_tool_edit.setPlaceholderText(tr("settings.archive_tool_placeholder"))
            self.archive_tool_button.setText(tr("common.browse"))
            self.archive_tool_auto_button.setText(tr("settings.archive_tool_auto"))
            self.assetstudio_tool_label.setText(tr("settings.assetstudio_tool_label"))
            self.assetstudio_tool_desc.setText(tr("settings.assetstudio_tool_desc"))
            self.assetstudio_tool_edit.setPlaceholderText(tr("settings.assetstudio_tool_placeholder"))
            self.assetstudio_tool_button.setText(tr("common.browse"))
            self.assetstudio_tool_auto_button.setText(tr("settings.tool_auto"))
            self.cubism_core_label.setText(tr("settings.cubism_core_label"))
            self.cubism_core_desc.setText(tr("settings.cubism_core_desc"))
            self.cubism_core_edit.setPlaceholderText(tr("settings.cubism_core_placeholder"))
            self.cubism_core_button.setText(tr("common.browse"))
            self.cubism_core_auto_button.setText(tr("settings.tool_auto"))
            self.photoshop_label.setText(tr("settings.photoshop_label"))
            self.photoshop_desc.setText(tr("settings.photoshop_desc"))
            self.photoshop_edit.setPlaceholderText(tr("settings.photoshop_placeholder"))
            self.photoshop_button.setText(tr("common.browse"))
            self.photoshop_clear_button.setText(tr("common.clear"))
            # The single Spine path is the common native bridge root.
            # ``preview.spine_runtime_dir`` remains the stable settings key
            # consumed by the preview page while old web-runtime wording is
            # intentionally no longer exposed here.
            self.spine_runtime_label.setText(
                tr("settings.spine_native_runtime_label")
            )
            self.spine_runtime_desc.setText(
                tr("settings.spine_native_runtime_desc")
            )
            self.spine_runtime_edit.setPlaceholderText(
                tr("settings.spine_native_runtime_placeholder")
            )
            self.spine_runtime_button.setText(tr("common.browse"))
            self.spine_runtime_auto_button.setText(tr("settings.tool_auto"))
            self.spine_section_title.setText(tr("settings.spine_section", "Spine"))
            self.spine_compatibility_label.setText(
                tr("settings.spine_compatibility_label", "Spine compatibility mode")
            )
            self.spine_compatibility_desc.setText(
                tr(
                    "settings.spine_compatibility_desc",
                    "Use verified Spine 3.8.75 for preview and formal extraction. "
                    "Formal extraction also creates an independent .spine project when the official CLI is available.",
                )
            )
            self.spine_compatibility_checkbox.setText(
                tr(
                    "settings.spine_compatibility_checkbox",
                    "Always use compatible Spine 3.8.75",
                )
            )
            self.spine_runtime_version_label.setText(
                tr("settings.spine_runtime_version_label", "Installed Spine runtime")
            )
            self.spine_runtime_version_desc.setText(
                tr(
                    "settings.spine_runtime_version_desc",
                    "When compatibility mode is off, prefer an installed verified runtime. "
                    "The download arrow lists only supported catalog packages.",
                )
            )
            self.spine_runtime_download_button.setToolTip(
                tr("settings.spine_runtime_download_tooltip", "Find or build a verified runtime")
            )
            self.spine_runtime_advanced_toggle.setText(
                tr("settings.spine_runtime_advanced", "Advanced runtime root")
            )
            self.spine_editor_label.setText(
                tr("settings.spine_editor_label", "Spine editor executable")
            )
            self.spine_editor_desc.setText(
                tr(
                    "settings.spine_editor_desc",
                    "Select the locally installed Spine.com or Spine.exe used to create .spine projects.",
                )
            )
            self.spine_editor_edit.setPlaceholderText(
                tr(
                    "settings.spine_editor_placeholder",
                    "Example: C:\\Program Files\\Spine\\Spine.com",
                )
            )
            self.spine_editor_button.setText(
                tr("settings.spine_editor_select", "Select Spine editor")
            )
            self.spine_editor_clear_button.setText(tr("common.clear"))
            self.spine_create_project_label.setText(
                tr("settings.spine_create_project_label", "Automatic Spine project export")
            )
            self.spine_create_project_desc.setText(
                tr(
                    "settings.spine_create_project_desc",
                    "When enabled, export creates an independent .spine project through the selected editor.",
                )
            )
            self.spine_create_project_checkbox.setText(
                tr(
                    "settings.spine_create_project_checkbox",
                    "Create a Spine project during export",
                )
            )
            self.spine_auto_convert_label.setText(tr("settings.spine_auto_convert_label"))
            self.spine_auto_convert_desc.setText(tr("settings.spine_auto_convert_desc"))
            self.spine_auto_convert_checkbox.setText(tr("settings.spine_auto_convert_checkbox"))
            self.spine_target_version_label.setText(tr("settings.spine_target_version_label"))
            self.spine_target_version_desc.setText(tr("settings.spine_target_version_desc"))
            self.spine_target_version_edit.setPlaceholderText(
                tr("settings.spine_target_version_placeholder")
            )
            self.spine_preview_unify_label.setText(
                tr("settings.spine_preview_unify_label")
            )
            self.spine_preview_unify_desc.setText(
                tr("settings.spine_preview_unify_desc")
            )
            self.spine_preview_unify_checkbox.setText(
                tr("settings.spine_preview_unify_checkbox")
            )
            self.tool_download_section_title.setText(
                tr("settings.tool_download_section")
            )
            self.tool_download_desc.setText(tr("settings.tool_download_desc"))
            self.tool_download_cancel.setText(tr("settings.tool_download_cancel"))
            self.assetstudio_install_button.setText(
                tr("settings.assetstudio_install")
            )
            self.cubism_official_button.setText(tr("settings.cubism_official"))
            self.cubism_extract_button.setText(tr("settings.cubism_extract"))
            self.spine_native_install_button.setText(
                tr("settings.spine_native_install")
            )
            current_spine_family = self._current_combo_value(
                self.spine_native_family_combo,
                self._spine_native_family_values,
            )
            self.spine_native_family_combo.clear()
            for family in self._spine_native_family_values:
                self.spine_native_family_combo.addItem(family)
            self._set_combo_by_value(
                self.spine_native_family_combo,
                self._spine_native_family_values,
                current_spine_family,
            )
            self.preview_image_limit_label.setText(tr("settings.preview_image_limit_label"))
            self.preview_image_limit_desc.setText(tr("settings.preview_image_limit_desc"))
            self.texture_viewer_label.setText(tr("settings.texture_viewer_label"))
            self.texture_viewer_desc.setText(tr("settings.texture_viewer_desc"))
            current_texture_viewer = self._current_combo_value(
                self.texture_viewer_combo,
                self._texture_viewer_values,
            )
            self.texture_viewer_combo.clear()
            for mode in self._texture_viewer_values:
                self.texture_viewer_combo.addItem(
                    tr(f"settings.texture_viewer.{mode}")
                )
            self._set_combo_by_value(
                self.texture_viewer_combo,
                self._texture_viewer_values,
                current_texture_viewer,
            )
            self.image_viewer_edit.setPlaceholderText(
                tr("settings.image_viewer_placeholder")
            )
            self.image_viewer_label.setText(
                tr("settings.image_viewer_label", "Custom image viewer")
            )
            self.image_viewer_desc.setText(
                tr(
                    "settings.image_viewer_desc",
                    "Used only when the texture preview mode is set to Custom.",
                )
            )
            self.image_viewer_button.setText(tr("common.browse"))
            self.image_viewer_clear_button.setText(tr("common.clear"))
            self.refresh_tool_status_labels()
            self.setting_file_note.setText(
                tr("settings.setting_file_note", path=self.settings_manager.settings_file)
            )
            self.save_button.setText(tr("settings.save_button"))
            self.detect_tools_button.setText(tr("settings.detect_tools_button"))
        finally:
            self._syncing_ui = False

    def on_language_combo_changed(self, index: int):
        if self._syncing_ui:
            return
        if index < 0 or index >= len(self._language_codes):
            return

        language = self._language_codes[index]
        self.settings_manager.set("language", language)
        self.i18n.set_language(language)
        self.languageChanged.emit(language)

        InfoBar.success(
            title=tr("common.success"),
            content=tr("settings.language_saved"),
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2500,
            parent=self
        )

    def on_theme_combo_changed(self, index: int):
        if self._syncing_ui:
            return
        if index < 0 or index >= len(self._theme_values):
            return

        theme = self._theme_values[index]
        self.settings_manager.set("theme", theme)
        self.themeChanged.emit(theme)

        InfoBar.success(
            title=tr("common.success"),
            content=tr("settings.theme_saved"),
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2500,
            parent=self
        )

    def _refresh_font_family_combo(self, current_family: str = ""):
        """Rebuild the installed-family picker while retaining its value."""

        if self.font_family_combo is None:
            return

        current = str(current_family or "").strip()
        families = installed_font_families()
        # Keep a stale persisted value visible for diagnosis, while all
        # selectable values discovered here still come from Qt's font database.
        if current and current.casefold() not in {
            family.casefold() for family in families
        }:
            families.insert(0, current)

        self._font_family_values = [""] + families
        self.font_family_combo.clear()
        self.font_family_combo.addItem(tr("settings.font_default"), userData="")
        for family in families:
            self.font_family_combo.addItem(family, userData=family)

        target = current.casefold()
        for index, family in enumerate(self._font_family_values):
            if family.casefold() == target:
                self.font_family_combo.setCurrentIndex(index)
                return
        self.font_family_combo.setCurrentIndex(0)

    def _current_font_family(self) -> str:
        if self.font_family_combo is None:
            return ""
        data = self.font_family_combo.currentData()
        if data is not None:
            return str(data or "").strip()
        index = self.font_family_combo.currentIndex()
        if 0 <= index < len(self._font_family_values):
            return str(self._font_family_values[index] or "").strip()
        return ""

    def _persist_font_preference(self):
        if self._syncing_ui:
            return
        family = self._current_font_family()
        size = self.font_size_spin.value() if self.font_size_spin else DEFAULT_FONT_SIZE
        self.settings_manager.set_font_preferences(family, size)
        self.fontChanged.emit(family, int(size))

    def on_font_family_combo_changed(self, index: int):
        if self._syncing_ui or index < 0:
            return
        self._persist_font_preference()

    def on_font_size_changed(self, value: int):
        if self._syncing_ui:
            return
        self._persist_font_preference()

    def reset_font_preferences(self):
        """Restore automatic CJK-aware selection and the default size."""

        self.settings_manager.reset_font_preferences()
        self._syncing_ui = True
        try:
            self._refresh_font_family_combo("")
            if self.font_size_spin:
                self.font_size_spin.setValue(DEFAULT_FONT_SIZE)
        finally:
            self._syncing_ui = False

        self.fontChanged.emit("", DEFAULT_FONT_SIZE)
        InfoBar.success(
            title=tr("common.success"),
            content=tr("settings.font_saved"),
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2500,
            parent=self,
        )

    def browse_output_root(self):
        path = QFileDialog.getExistingDirectory(
            self,
            tr("dialog.select_output_directory"),
            self.output_root_edit.text() or self.settings_manager.get_output_root(),
        )
        if not path:
            return
        self.settings_manager.set_output_root(path)
        self.output_root_edit.setText(self.settings_manager.get_output_root())
        self.load_current_settings()
        self.setting_file_note.setText(
            tr("settings.setting_file_note", path=self.settings_manager.settings_file)
        )
        InfoBar.success(
            title=tr("common.success"),
            content=tr("settings.output_root_saved"),
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2500,
            parent=self,
        )

    def save_runtime_settings(self):
        compatibility = bool(
            self.spine_compatibility_checkbox
            and self.spine_compatibility_checkbox.isChecked()
        )
        set_compatibility = getattr(
            self.settings_manager, "set_spine_compatibility_mode", None
        )
        if callable(set_compatibility):
            set_compatibility(compatibility)
        # While compatibility is on the visible combo is intentionally forced
        # to 3.8.75.  Do not write that display value over the preserved
        # off-mode selection; the toggle handler captured it separately.
        if not compatibility:
            set_runtime_version = getattr(
                self.settings_manager, "set_spine_runtime_version", None
            )
            selected = self._normalize_spine_runtime_choice(
                self.spine_runtime_version_combo.currentText()
                if self.spine_runtime_version_combo
                else ""
            )
            if callable(set_runtime_version) and selected:
                set_runtime_version(selected)

        output_root = self.output_root_edit.text().strip() if self.output_root_edit else ""
        if output_root:
            self.settings_manager.set_output_root(output_root)
        self.settings_manager.set_archive_extractor_path(
            self.archive_tool_edit.text() if self.archive_tool_edit else ""
        )
        self.settings_manager.set_assetstudio_cli_path(
            self.assetstudio_tool_edit.text() if self.assetstudio_tool_edit else ""
        )
        self.settings_manager.set_cubism_core_dll_path(
            self.cubism_core_edit.text() if self.cubism_core_edit else ""
        )
        self.settings_manager.set_photoshop_path(
            self.photoshop_edit.text() if self.photoshop_edit else ""
        )
        self.settings_manager.set_spine_runtime_dir(
            self.spine_runtime_edit.text() if self.spine_runtime_edit else ""
        )
        set_editor_path = getattr(self.settings_manager, "set_spine_editor_path", None)
        if callable(set_editor_path):
            set_editor_path(self.spine_editor_edit.text() if self.spine_editor_edit else "")
        # The old create-project, automatic-conversion, target-version and
        # preview-unify controls remain only for migration/backward-compatible
        # converter pages.  They are hidden here and must not override the
        # consolidated policy when the user saves Runtime settings.  The
        # manual converter page can still validate and write its own target.
        self.settings_manager.set_texture_viewer_mode(
            self._current_combo_value(
                self.texture_viewer_combo,
                self._texture_viewer_values,
            )
        )
        self.settings_manager.set_image_viewer_path(
            self.image_viewer_edit.text() if self.image_viewer_edit else ""
        )
        self.settings_manager.set_font_preferences(
            self._current_font_family(),
            self.font_size_spin.value() if self.font_size_spin else DEFAULT_FONT_SIZE,
        )
        self.load_current_settings()
        InfoBar.success(
            title=tr("common.success"),
            content=tr("settings.saved"),
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2500,
            parent=self,
        )
        return True

    def browse_archive_tool(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("settings.archive_tool_select"),
            self.archive_tool_edit.text() or "",
            tr("settings.archive_tool_filter"),
        )
        if not path:
            return
        self.archive_tool_edit.setText(path)
        self.save_archive_tool(path)

    def clear_archive_tool(self):
        self.archive_tool_edit.clear()
        self.save_archive_tool("")

    def on_archive_tool_edit_finished(self):
        if self._syncing_ui:
            return
        self.save_archive_tool(self.archive_tool_edit.text())

    def save_archive_tool(self, path: str):
        self.settings_manager.set_archive_extractor_path(path)
        InfoBar.success(
            title=tr("common.success"),
            content=tr("settings.archive_tool_saved"),
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2500,
            parent=self,
        )

    def browse_assetstudio_tool(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("settings.assetstudio_tool_select"),
            self.assetstudio_tool_edit.text() or "",
            tr("settings.assetstudio_tool_filter"),
        )
        if not path:
            return
        self.assetstudio_tool_edit.setText(path)
        self.save_assetstudio_tool(path)

    def clear_assetstudio_tool(self):
        self.assetstudio_tool_edit.clear()
        self.save_assetstudio_tool("")

    def on_assetstudio_tool_edit_finished(self):
        if self._syncing_ui:
            return
        self.save_assetstudio_tool(self.assetstudio_tool_edit.text())

    def save_assetstudio_tool(self, path: str):
        self.settings_manager.set_assetstudio_cli_path(path)
        self.refresh_tool_status_labels()
        InfoBar.success(
            title=tr("common.success"),
            content=tr("settings.assetstudio_tool_saved"),
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2500,
            parent=self,
        )

    def browse_cubism_core(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("settings.cubism_core_select"),
            self.cubism_core_edit.text() or "",
            tr("settings.cubism_core_filter"),
        )
        if not path:
            return
        self.cubism_core_edit.setText(path)
        self.save_cubism_core(path)

    def clear_cubism_core(self):
        self.cubism_core_edit.clear()
        self.save_cubism_core("")

    def on_cubism_core_edit_finished(self):
        if self._syncing_ui:
            return
        self.save_cubism_core(self.cubism_core_edit.text())

    def save_cubism_core(self, path: str):
        self.settings_manager.set_cubism_core_dll_path(path)
        self.refresh_tool_status_labels()
        InfoBar.success(
            title=tr("common.success"),
            content=tr("settings.cubism_core_saved"),
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2500,
            parent=self,
        )

    def browse_photoshop(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("settings.photoshop_select"),
            self.photoshop_edit.text() or "",
            tr("settings.photoshop_filter"),
        )
        if path:
            self.photoshop_edit.setText(path)
            self.save_photoshop(path)

    def browse_image_viewer(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("settings.image_viewer_select"),
            self.image_viewer_edit.text() or "",
            tr("settings.image_viewer_filter"),
        )
        if path:
            self.image_viewer_edit.setText(path)

    def clear_image_viewer(self):
        self.image_viewer_edit.clear()
        self.settings_manager.set_image_viewer_path("")

    def clear_photoshop(self):
        self.photoshop_edit.clear()
        self.save_photoshop("")

    def on_photoshop_edit_finished(self):
        if not self._syncing_ui:
            self.save_photoshop(self.photoshop_edit.text())

    def save_photoshop(self, path: str):
        self.settings_manager.set_photoshop_path(path)
        self.refresh_tool_status_labels()
        InfoBar.success(
            title=tr("common.success"),
            content=tr("settings.photoshop_saved"),
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2500,
            parent=self,
        )

    def browse_spine_runtime(self):
        path = QFileDialog.getExistingDirectory(
            self,
            tr("settings.spine_native_runtime_select"),
            self.spine_runtime_edit.text() or "",
        )
        if not path:
            return
        self.spine_runtime_edit.setText(path)
        self.save_spine_runtime(path)

    def clear_spine_runtime(self):
        self.spine_runtime_edit.clear()
        self.save_spine_runtime("")

    def on_spine_runtime_edit_finished(self):
        if self._syncing_ui:
            return
        self.save_spine_runtime(self.spine_runtime_edit.text())

    def save_spine_runtime(self, path: str):
        self.settings_manager.set_spine_runtime_dir(path)
        self.refresh_tool_status_labels()

    def browse_spine_editor(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("settings.spine_editor_select", "Select Spine editor"),
            self.spine_editor_edit.text() or "",
            tr(
                "settings.spine_editor_filter",
                "Spine editor (Spine.com Spine.exe *.com *.exe);;All files (*.*)",
            ),
        )
        if not path:
            return
        self.spine_editor_edit.setText(path)
        self.save_spine_editor(path)

    def clear_spine_editor(self):
        self.spine_editor_edit.clear()
        self.save_spine_editor("")

    def on_spine_editor_edit_finished(self):
        if self._syncing_ui:
            return
        self.save_spine_editor(self.spine_editor_edit.text())

    def save_spine_editor(self, path: str):
        setter = getattr(self.settings_manager, "set_spine_editor_path", None)
        if callable(setter):
            setter(path)
        self.refresh_tool_status_labels()
        InfoBar.success(
            title=tr("common.success"),
            content=tr("settings.spine_editor_saved", "Spine editor path saved."),
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2500,
            parent=self,
        )

    def open_cubism_download_page(self):
        url = official_download_url("cubism_core")
        if not url or not QDesktopServices.openUrl(QUrl(url)):
            InfoBar.error(
                title=tr("common.error"),
                content=tr("settings.tool_download_open_failed", url=url or ""),
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=4000,
                parent=self,
            )

    def install_cubism_from_live2d_py(self):
        self.start_tool_install("cubism_core")

    def is_tool_install_running(self) -> bool:
        """Return whether a download/build worker still owns a QThread."""

        worker = self._tool_install_worker
        if worker is None:
            return False
        try:
            return bool(worker.isRunning())
        except RuntimeError:
            return False

    def notify_close_while_installing(self) -> bool:
        """Warn the host window before it destroys a running worker thread."""

        if not self.is_tool_install_running():
            return False
        InfoBar.warning(
            title=tr("settings.tool_download_busy_title"),
            content=tr("settings.tool_download_busy"),
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=4000,
            parent=self,
        )
        return True

    def start_tool_install(self, package_id: str):
        if self.is_tool_install_running():
            self.notify_close_while_installing()
            return
        if package_id == "spine_native":
            family = self._current_combo_value(
                self.spine_native_family_combo,
                self._spine_native_family_values,
            )
            package_id = "spine_native_4_0" if family == "4.0" else "spine_native"
        worker = ToolchainInstallWorker(package_id, self)
        self._tool_install_worker = worker
        worker.progressChanged.connect(self._on_tool_install_progress)
        worker.resultReady.connect(self._on_tool_install_result)
        worker.failed.connect(self._on_tool_install_failed)
        worker.finished.connect(self._on_tool_install_finished)
        self._set_tool_install_busy(True)
        worker.start()

    def cancel_tool_install(self):
        if self.is_tool_install_running():
            self._tool_install_worker.requestInterruption()
            self.tool_download_cancel.setEnabled(False)
            self.tool_download_status.setText(tr("settings.tool_download_cancelling"))

    def _set_tool_install_busy(self, busy: bool):
        self.tool_download_cancel.setVisible(busy)
        self.tool_download_cancel.setEnabled(busy)
        for button in (
            self.assetstudio_install_button,
            self.cubism_official_button,
            self.cubism_extract_button,
            self.spine_native_install_button,
            self.spine_native_family_combo,
        ):
            if button:
                button.setEnabled(not busy)
        if self.tool_download_progress:
            self.tool_download_progress.setVisible(busy)
            if busy:
                self.tool_download_progress.setRange(0, 100)
                self.tool_download_progress.setValue(0)

    def _on_tool_install_progress(self, value: ToolDownloadProgress):
        if self.tool_download_status:
            self.tool_download_status.setText(value.message or value.phase)
        if self.tool_download_progress:
            fraction = value.fraction
            if fraction is None:
                self.tool_download_progress.setRange(0, 0)
            else:
                self.tool_download_progress.setRange(0, 100)
                self.tool_download_progress.setValue(round(fraction * 100))

    def _on_tool_install_result(self, result: ToolInstallResult):
        entrypoint = str(result.entrypoint) if result.entrypoint else str(result.install_dir)
        if result.package_id == "assetstudio_cli":
            self.settings_manager.set_assetstudio_cli_path(entrypoint)
            self.assetstudio_tool_edit.setText(entrypoint)
        elif result.package_id == "cubism_core":
            self.settings_manager.set_cubism_core_dll_path(entrypoint)
            self.cubism_core_edit.setText(entrypoint)
        elif result.package_id.startswith("spine_native"):
            # Keep one common root so both 3.8.75 and 4.0 installs remain
            # discoverable.  The preview backend selects the family by the
            # skeleton version and calls spine_native.find_native_library.
            common_root = result.install_dir.parent
            self.settings_manager.set_spine_runtime_dir(str(common_root))
            self.spine_runtime_edit.setText(str(common_root))
        self.refresh_tool_status_labels()
        if self.tool_download_status:
            self.tool_download_status.setText(
                tr("settings.tool_download_installed", path=entrypoint)
            )
        InfoBar.success(
            title=tr("common.success"),
            content=tr("settings.tool_download_installed", path=entrypoint),
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=4000,
            parent=self,
        )

    def _on_tool_install_failed(self, message: str):
        if self.tool_download_status:
            self.tool_download_status.setText(tr("settings.tool_download_failed", error=message))
        InfoBar.error(
            title=tr("settings.tool_download_failed_title"),
            content=tr("settings.tool_download_failed", error=message),
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=6000,
            parent=self,
        )

    def _on_tool_install_finished(self):
        worker = self.sender()
        if worker is self._tool_install_worker:
            self._tool_install_worker = None
        self._set_tool_install_busy(False)
        if worker:
            worker.deleteLater()

    def auto_detect_toolchain(self):
        """Detect available tools and fill only currently empty settings."""

        changed = self.settings_manager.auto_detect_toolchain()
        self.load_current_settings()
        if changed:
            content = tr("settings.toolchain_detected", count=len(changed))
        else:
            content = tr("settings.toolchain_not_found")
        InfoBar.success(
            title=tr("common.success"),
            content=content,
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=3000,
            parent=self,
        )

    def _auto_detect_single(self, key: str, editor, setter):
        """Populate one editor when it has no configured value."""

        if str(self.settings_manager.get(key, "") or "").strip():
            self.refresh_tool_status_labels()
            return
        found = self.settings_manager.detect_toolchain_paths().get(key, "")
        if not found:
            self.refresh_tool_status_labels()
            return
        editor.setText(found)
        setter(found)
        self.refresh_tool_status_labels()

    def auto_detect_archive_tool(self):
        self._auto_detect_single(
            "tools.archive_extractor_path",
            self.archive_tool_edit,
            self.save_archive_tool,
        )

    def auto_detect_assetstudio_tool(self):
        self._auto_detect_single(
            "tools.assetstudio_cli_path",
            self.assetstudio_tool_edit,
            self.save_assetstudio_tool,
        )

    def auto_detect_cubism_core(self):
        self._auto_detect_single(
            "tools.cubism_core_dll_path",
            self.cubism_core_edit,
            self.save_cubism_core,
        )

    def auto_detect_spine_runtime(self):
        self._auto_detect_single(
            "preview.spine_runtime_dir",
            self.spine_runtime_edit,
            self.save_spine_runtime,
        )

    def refresh_tool_status_labels(self):
        if self.archive_tool_status:
            self.archive_tool_status.setText(
                self.tool_status_text(
                    self.settings_manager.get_archive_extractor_path(),
                    self.detect_archive_tool_path,
                )
            )
        if self.assetstudio_tool_status:
            self.assetstudio_tool_status.setText(
                self.tool_status_text(
                    self.settings_manager.get_assetstudio_cli_path(),
                    self.detect_assetstudio_path,
                )
            )
        if self.cubism_core_status:
            self.cubism_core_status.setText(
                self.tool_status_text(
                    self.settings_manager.get_cubism_core_dll_path(),
                    self.detect_cubism_core_path,
                )
            )
        if self.photoshop_status:
            configured = self.settings_manager.get_photoshop_path()
            executable = self.settings_manager.get_photoshop_executable()
            if executable:
                self.photoshop_status.setText(
                    tr("settings.tool_status.configured", path=executable)
                )
            elif configured:
                self.photoshop_status.setText(
                    tr("settings.tool_status.invalid", path=configured)
                )
            else:
                detected = self.detect_photoshop_path()
                if detected:
                    self.photoshop_status.setText(
                        tr("settings.tool_status.auto_found", path=str(detected))
                    )
                else:
                    self.photoshop_status.setText(tr("settings.tool_status.not_found"))
        if self.spine_runtime_status:
            self.spine_runtime_status.setText(
                self.tool_status_text(
                    self.settings_manager.get_spine_runtime_dir(),
                    self.detect_spine_runtime_path,
                    directory=True,
                )
            )
        if self.spine_editor_status:
            get_editor_path = getattr(
                self.settings_manager, "get_spine_editor_path", None
            )
            configured_path = (
                str(get_editor_path() or "").strip()
                if callable(get_editor_path)
                else (self.spine_editor_edit.text().strip() if self.spine_editor_edit else "")
            )
            if configured_path:
                configured = Path(configured_path).expanduser()
                if configured.is_file():
                    self.spine_editor_status.setText(
                        tr(
                            "settings.tool_status.configured",
                            path=str(configured.resolve()),
                        )
                    )
                else:
                    self.spine_editor_status.setText(
                        tr("settings.tool_status.invalid", path=configured_path)
                    )
            else:
                self.spine_editor_status.setText(
                    tr(
                        "settings.spine_editor_not_configured",
                        "Not configured. Select Spine.com or Spine.exe to create .spine projects.",
                    )
                )

    def tool_status_text(self, configured_path: str, detector, directory: bool = False):
        configured_path = str(configured_path or "").strip()
        if configured_path:
            configured = Path(configured_path).expanduser()
            if (configured.is_dir() if directory else configured.is_file()):
                return tr("settings.tool_status.configured", path=str(configured.resolve()))
            return tr("settings.tool_status.invalid", path=configured_path)
        detected = detector()
        if detected:
            return tr("settings.tool_status.auto_found", path=str(detected))
        return tr("settings.tool_status.not_found")

    @staticmethod
    def detect_archive_tool_path() -> str:
        try:
            path = find_archive_extractor()
            return str(path) if path else ""
        except Exception:
            return ""

    @staticmethod
    def detect_assetstudio_path() -> str:
        try:
            path = find_assetstudio_cli()
            return str(path) if path else ""
        except Exception:
            return ""

    @staticmethod
    def detect_cubism_core_path() -> str:
        try:
            path = find_cubism_core()
            return str(path) if path else ""
        except Exception:
            return ""

    @staticmethod
    def detect_photoshop_path() -> str:
        try:
            path = find_photoshop()
            return str(path) if path else ""
        except Exception:
            return ""

    @staticmethod
    def detect_spine_runtime_path() -> str:
        try:
            path = find_spine_native_runtime_root()
            return str(path) if path else ""
        except Exception:
            return ""

    @staticmethod
    def _set_combo_by_value(combo: ComboBox, values: list, value: str):
        if value in values:
            combo.setCurrentIndex(values.index(value))
        elif values:
            combo.setCurrentIndex(0)

    @staticmethod
    def _current_combo_value(combo: ComboBox, values: list):
        index = combo.currentIndex()
        if 0 <= index < len(values):
            return values[index]
        return values[0] if values else ""
