from PySide6.QtCore import QThread, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
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
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
)

from app.core.settings_manager import SettingsManager
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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("settingsPage")

        self.settings_manager = SettingsManager()
        self.i18n = get_i18n()

        self._language_codes = ["en_US", "zh_CN", "ja_JP"]
        self._theme_values = ["auto", "light", "dark"]
        self._texture_viewer_values = ["internal", "system", "custom"]
        self._spine_native_family_values = ["3.8.75", "4.0"]
        self._syncing_ui = False

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

        self.setup_ui()
        self.retranslate_ui()
        self.load_current_settings()
        self.i18n.languageChanged.connect(self.retranslate_ui)

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
        """Build a two-column, scrollable settings surface.

        Every field is retained, but path controls are stacked inside their
        card.  This lets the columns shrink at high DPI without requiring a
        minimum page width or a second horizontal scrollbar.
        """

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        self.settings_scroll = QScrollArea(self)
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
        outer_layout.addWidget(self.settings_scroll)
        main_layout = QVBoxLayout(content)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(16)

        self.title_label = SubtitleLabel("", self)
        self.title_label.setWordWrap(True)
        self.title_label.setMinimumWidth(0)
        self.title_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        main_layout.addWidget(self.title_label)

        columns_layout = QHBoxLayout()
        columns_layout.setSpacing(16)
        left_column = QVBoxLayout()
        right_column = QVBoxLayout()
        left_column.setSpacing(16)
        right_column.setSpacing(16)
        columns_layout.addLayout(left_column, 1)
        columns_layout.addLayout(right_column, 1)
        self.settings_columns_layout = columns_layout
        self.settings_left_column = left_column
        self.settings_right_column = right_column
        main_layout.addLayout(columns_layout, 1)

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
        self.cubism_core_label = BodyLabel("", asset_card)
        self.cubism_core_desc = CaptionLabel("", asset_card)
        self.cubism_core_desc.setWordWrap(True)
        self.cubism_core_status = CaptionLabel("", asset_card)
        self.cubism_core_status.setWordWrap(True)
        self.cubism_core_edit = LineEdit(asset_card)
        self.cubism_core_edit.editingFinished.connect(self.on_cubism_core_edit_finished)
        self.cubism_core_button = PushButton("", asset_card)
        self.cubism_core_button.clicked.connect(self.browse_cubism_core)
        self.cubism_core_auto_button = PushButton("", asset_card)
        self.cubism_core_auto_button.clicked.connect(self.auto_detect_cubism_core)
        self._add_path_block(
            asset_layout,
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

        left_column.addStretch(1)
        right_column.addStretch(1)
        main_layout.addStretch(1)

    def load_current_settings(self):
        self._syncing_ui = True
        try:
            language = normalize_language_code(self.settings_manager.get("language", "en_US"))
            self._set_combo_by_value(self.language_combo, self._language_codes, language)

            theme = str(self.settings_manager.get("theme", "auto")).lower()
            if theme not in self._theme_values:
                theme = "auto"
            self._set_combo_by_value(self.theme_combo, self._theme_values, theme)

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
        finally:
            self._syncing_ui = False

    def retranslate_ui(self):
        self._syncing_ui = True
        try:
            self.title_label.setText(tr("settings.title"))

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
        target_version = (
            self.spine_target_version_edit.text().strip()
            if self.spine_target_version_edit
            else ""
        )
        try:
            target_version = self.settings_manager.validate_spine_conversion_target_version(
                target_version
            )
        except ValueError as exc:
            InfoBar.error(
                title=tr("common.error"),
                content=tr("settings.spine_target_version_invalid", error=str(exc)),
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=4000,
                parent=self,
            )
            return False

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
        set_editor_path = getattr(
            self.settings_manager, "set_spine_editor_path", None
        )
        if callable(set_editor_path):
            set_editor_path(
                self.spine_editor_edit.text() if self.spine_editor_edit else ""
            )
        set_create_project = getattr(
            self.settings_manager, "set_spine_create_project", None
        )
        if callable(set_create_project):
            set_create_project(
                self.spine_create_project_checkbox.isChecked()
                if self.spine_create_project_checkbox
                else True
            )
        self.settings_manager.set_spine_conversion_enabled(
            self.spine_auto_convert_checkbox.isChecked()
            if self.spine_auto_convert_checkbox
            else False
        )
        self.settings_manager.set_spine_conversion_target_version(
            target_version
        )
        self.settings_manager.set_texture_viewer_mode(
            self._current_combo_value(
                self.texture_viewer_combo,
                self._texture_viewer_values,
            )
        )
        self.settings_manager.set_image_viewer_path(
            self.image_viewer_edit.text() if self.image_viewer_edit else ""
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
