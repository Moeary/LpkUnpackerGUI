from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFileDialog, QFrame, QVBoxLayout, QHBoxLayout
from pathlib import Path
from qfluentwidgets import (
    CardWidget,
    SubtitleLabel,
    BodyLabel,
    CaptionLabel,
    ComboBox,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PrimaryPushButton,
    PushButton,
)

from app.core.assetstudio_cli import AssetStudioCLI, AssetStudioCLIError
from app.core.cubism_core import resolve_cubism_core_dll
from app.core.settings_manager import SettingsManager
from app.i18n import get_i18n, normalize_language_code, tr


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
        self.texture_viewer_label = None
        self.texture_viewer_desc = None
        self.texture_viewer_combo = None
        self.image_viewer_edit = None
        self.image_viewer_button = None
        self.image_viewer_clear_button = None
        self.setting_file_note = None
        self.save_button = None

        self.setup_ui()
        self.retranslate_ui()
        self.load_current_settings()
        self.i18n.languageChanged.connect(self.retranslate_ui)

    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(16)

        self.title_label = SubtitleLabel("", self)
        main_layout.addWidget(self.title_label)

        language_card = CardWidget(self)
        language_layout = QVBoxLayout(language_card)
        language_layout.setContentsMargins(16, 16, 16, 16)
        language_layout.setSpacing(10)

        self.language_section_title = SubtitleLabel("", language_card)
        language_layout.addWidget(self.language_section_title)

        language_row = QHBoxLayout()
        language_row.setSpacing(12)

        language_text_layout = QVBoxLayout()
        language_text_layout.setSpacing(4)
        self.language_label = BodyLabel("", language_card)
        self.language_desc = CaptionLabel("", language_card)
        self.language_desc.setWordWrap(True)
        language_text_layout.addWidget(self.language_label)
        language_text_layout.addWidget(self.language_desc)

        self.language_combo = ComboBox(language_card)
        self.language_combo.setMinimumWidth(180)
        self.language_combo.currentIndexChanged.connect(self.on_language_combo_changed)

        language_row.addLayout(language_text_layout, 1)
        language_row.addWidget(self.language_combo, 0, Qt.AlignRight)
        language_layout.addLayout(language_row)

        self.language_note = CaptionLabel("", language_card)
        self.language_note.setWordWrap(True)
        language_layout.addWidget(self.language_note)

        main_layout.addWidget(language_card)

        theme_card = CardWidget(self)
        theme_layout = QVBoxLayout(theme_card)
        theme_layout.setContentsMargins(16, 16, 16, 16)
        theme_layout.setSpacing(10)

        self.theme_section_title = SubtitleLabel("", theme_card)
        theme_layout.addWidget(self.theme_section_title)

        theme_row = QHBoxLayout()
        theme_row.setSpacing(12)

        theme_text_layout = QVBoxLayout()
        theme_text_layout.setSpacing(4)
        self.theme_label = BodyLabel("", theme_card)
        self.theme_desc = CaptionLabel("", theme_card)
        self.theme_desc.setWordWrap(True)
        theme_text_layout.addWidget(self.theme_label)
        theme_text_layout.addWidget(self.theme_desc)

        self.theme_combo = ComboBox(theme_card)
        self.theme_combo.setMinimumWidth(180)
        self.theme_combo.currentIndexChanged.connect(self.on_theme_combo_changed)

        theme_row.addLayout(theme_text_layout, 1)
        theme_row.addWidget(self.theme_combo, 0, Qt.AlignRight)
        theme_layout.addLayout(theme_row)

        main_layout.addWidget(theme_card)

        runtime_card = CardWidget(self)
        runtime_layout = QVBoxLayout(runtime_card)
        runtime_layout.setContentsMargins(16, 16, 16, 16)
        runtime_layout.setSpacing(10)

        self.runtime_section_title = SubtitleLabel("", runtime_card)
        runtime_layout.addWidget(self.runtime_section_title)

        output_row = QHBoxLayout()
        output_row.setSpacing(12)

        output_text_layout = QVBoxLayout()
        output_text_layout.setSpacing(4)
        self.output_root_label = BodyLabel("", runtime_card)
        self.output_root_desc = CaptionLabel("", runtime_card)
        self.output_root_desc.setWordWrap(True)
        output_text_layout.addWidget(self.output_root_label)
        output_text_layout.addWidget(self.output_root_desc)

        self.output_root_edit = LineEdit(runtime_card)
        self.output_root_edit.setReadOnly(True)
        self.output_root_button = PushButton("", runtime_card)
        self.output_root_button.clicked.connect(self.browse_output_root)

        output_row.addLayout(output_text_layout, 1)
        output_row.addWidget(self.output_root_edit, 2)
        output_row.addWidget(self.output_root_button)
        runtime_layout.addLayout(output_row)

        archive_tool_row = QHBoxLayout()
        archive_tool_row.setSpacing(12)

        archive_tool_text_layout = QVBoxLayout()
        archive_tool_text_layout.setSpacing(4)
        self.archive_tool_label = BodyLabel("", runtime_card)
        self.archive_tool_desc = CaptionLabel("", runtime_card)
        self.archive_tool_desc.setWordWrap(True)
        archive_tool_text_layout.addWidget(self.archive_tool_label)
        archive_tool_text_layout.addWidget(self.archive_tool_desc)

        self.archive_tool_edit = LineEdit(runtime_card)
        self.archive_tool_edit.editingFinished.connect(self.on_archive_tool_edit_finished)
        self.archive_tool_button = PushButton("", runtime_card)
        self.archive_tool_button.clicked.connect(self.browse_archive_tool)
        self.archive_tool_auto_button = PushButton("", runtime_card)
        self.archive_tool_auto_button.clicked.connect(self.clear_archive_tool)

        archive_tool_row.addLayout(archive_tool_text_layout, 1)
        archive_tool_row.addWidget(self.archive_tool_edit, 2)
        archive_tool_row.addWidget(self.archive_tool_button)
        archive_tool_row.addWidget(self.archive_tool_auto_button)
        runtime_layout.addLayout(archive_tool_row)

        assetstudio_tool_row = QHBoxLayout()
        assetstudio_tool_row.setSpacing(12)

        assetstudio_tool_text_layout = QVBoxLayout()
        assetstudio_tool_text_layout.setSpacing(4)
        self.assetstudio_tool_label = BodyLabel("", runtime_card)
        self.assetstudio_tool_desc = CaptionLabel("", runtime_card)
        self.assetstudio_tool_desc.setWordWrap(True)
        self.assetstudio_tool_status = CaptionLabel("", runtime_card)
        self.assetstudio_tool_status.setWordWrap(True)
        assetstudio_tool_text_layout.addWidget(self.assetstudio_tool_label)
        assetstudio_tool_text_layout.addWidget(self.assetstudio_tool_desc)
        assetstudio_tool_text_layout.addWidget(self.assetstudio_tool_status)

        self.assetstudio_tool_edit = LineEdit(runtime_card)
        self.assetstudio_tool_edit.editingFinished.connect(self.on_assetstudio_tool_edit_finished)
        self.assetstudio_tool_button = PushButton("", runtime_card)
        self.assetstudio_tool_button.clicked.connect(self.browse_assetstudio_tool)
        self.assetstudio_tool_auto_button = PushButton("", runtime_card)
        self.assetstudio_tool_auto_button.clicked.connect(self.clear_assetstudio_tool)

        assetstudio_tool_row.addLayout(assetstudio_tool_text_layout, 1)
        assetstudio_tool_row.addWidget(self.assetstudio_tool_edit, 2)
        assetstudio_tool_row.addWidget(self.assetstudio_tool_button)
        assetstudio_tool_row.addWidget(self.assetstudio_tool_auto_button)
        runtime_layout.addLayout(assetstudio_tool_row)

        cubism_core_row = QHBoxLayout()
        cubism_core_row.setSpacing(12)

        cubism_core_text_layout = QVBoxLayout()
        cubism_core_text_layout.setSpacing(4)
        self.cubism_core_label = BodyLabel("", runtime_card)
        self.cubism_core_desc = CaptionLabel("", runtime_card)
        self.cubism_core_desc.setWordWrap(True)
        self.cubism_core_status = CaptionLabel("", runtime_card)
        self.cubism_core_status.setWordWrap(True)
        cubism_core_text_layout.addWidget(self.cubism_core_label)
        cubism_core_text_layout.addWidget(self.cubism_core_desc)
        cubism_core_text_layout.addWidget(self.cubism_core_status)

        self.cubism_core_edit = LineEdit(runtime_card)
        self.cubism_core_edit.editingFinished.connect(self.on_cubism_core_edit_finished)
        self.cubism_core_button = PushButton("", runtime_card)
        self.cubism_core_button.clicked.connect(self.browse_cubism_core)
        self.cubism_core_auto_button = PushButton("", runtime_card)
        self.cubism_core_auto_button.clicked.connect(self.clear_cubism_core)

        cubism_core_row.addLayout(cubism_core_text_layout, 1)
        cubism_core_row.addWidget(self.cubism_core_edit, 2)
        cubism_core_row.addWidget(self.cubism_core_button)
        cubism_core_row.addWidget(self.cubism_core_auto_button)
        runtime_layout.addLayout(cubism_core_row)

        photoshop_row = QHBoxLayout()
        photoshop_row.setSpacing(12)
        photoshop_text_layout = QVBoxLayout()
        photoshop_text_layout.setSpacing(4)
        self.photoshop_label = BodyLabel("", runtime_card)
        self.photoshop_desc = CaptionLabel("", runtime_card)
        self.photoshop_desc.setWordWrap(True)
        self.photoshop_status = CaptionLabel("", runtime_card)
        self.photoshop_status.setWordWrap(True)
        photoshop_text_layout.addWidget(self.photoshop_label)
        photoshop_text_layout.addWidget(self.photoshop_desc)
        photoshop_text_layout.addWidget(self.photoshop_status)
        self.photoshop_edit = LineEdit(runtime_card)
        self.photoshop_edit.editingFinished.connect(self.on_photoshop_edit_finished)
        self.photoshop_button = PushButton("", runtime_card)
        self.photoshop_button.clicked.connect(self.browse_photoshop)
        self.photoshop_clear_button = PushButton("", runtime_card)
        self.photoshop_clear_button.clicked.connect(self.clear_photoshop)
        photoshop_row.addLayout(photoshop_text_layout, 1)
        photoshop_row.addWidget(self.photoshop_edit, 2)
        photoshop_row.addWidget(self.photoshop_button)
        photoshop_row.addWidget(self.photoshop_clear_button)
        runtime_layout.addLayout(photoshop_row)

        texture_viewer_row = QHBoxLayout()
        texture_viewer_row.setSpacing(12)
        texture_viewer_text_layout = QVBoxLayout()
        texture_viewer_text_layout.setSpacing(4)
        self.texture_viewer_label = BodyLabel("", runtime_card)
        self.texture_viewer_desc = CaptionLabel("", runtime_card)
        self.texture_viewer_desc.setWordWrap(True)
        texture_viewer_text_layout.addWidget(self.texture_viewer_label)
        texture_viewer_text_layout.addWidget(self.texture_viewer_desc)
        self.texture_viewer_combo = ComboBox(runtime_card)
        self.texture_viewer_combo.setMinimumWidth(150)
        self.image_viewer_edit = LineEdit(runtime_card)
        self.image_viewer_button = PushButton("", runtime_card)
        self.image_viewer_button.clicked.connect(self.browse_image_viewer)
        self.image_viewer_clear_button = PushButton("", runtime_card)
        self.image_viewer_clear_button.clicked.connect(self.clear_image_viewer)
        texture_viewer_row.addLayout(texture_viewer_text_layout, 1)
        texture_viewer_row.addWidget(self.texture_viewer_combo)
        texture_viewer_row.addWidget(self.image_viewer_edit, 2)
        texture_viewer_row.addWidget(self.image_viewer_button)
        texture_viewer_row.addWidget(self.image_viewer_clear_button)
        runtime_layout.addLayout(texture_viewer_row)

        self.setting_file_note = CaptionLabel("", runtime_card)
        self.setting_file_note.setWordWrap(True)
        runtime_layout.addWidget(self.setting_file_note)

        runtime_action_row = QHBoxLayout()
        runtime_action_row.addStretch(1)
        self.save_button = PrimaryPushButton("", runtime_card)
        self.save_button.clicked.connect(self.save_runtime_settings)
        runtime_action_row.addWidget(self.save_button)
        runtime_layout.addLayout(runtime_action_row)

        main_layout.addWidget(runtime_card)
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
            self.image_viewer_button.setText(tr("common.browse"))
            self.image_viewer_clear_button.setText(tr("common.clear"))
            self.refresh_tool_status_labels()
            self.setting_file_note.setText(
                tr("settings.setting_file_note", path=self.settings_manager.settings_file)
            )
            self.save_button.setText(tr("settings.save_button"))
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

    def refresh_tool_status_labels(self):
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
                self.photoshop_status.setText(tr("settings.tool_status.not_found"))

    def tool_status_text(self, configured_path: str, detector):
        configured_path = str(configured_path or "").strip()
        if configured_path:
            if Path(configured_path).is_file():
                return tr("settings.tool_status.configured", path=str(Path(configured_path).resolve()))
            return tr("settings.tool_status.invalid", path=configured_path)
        detected = detector()
        if detected:
            return tr("settings.tool_status.auto_found", path=str(detected))
        return tr("settings.tool_status.not_found")

    @staticmethod
    def detect_assetstudio_path() -> str:
        try:
            return str(AssetStudioCLI.find_executable())
        except AssetStudioCLIError:
            return ""
        except Exception:
            return ""

    @staticmethod
    def detect_cubism_core_path() -> str:
        try:
            path = resolve_cubism_core_dll(None)
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
