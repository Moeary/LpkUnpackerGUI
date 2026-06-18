from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    EditableComboBox,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PushButton,
    SubtitleLabel,
    TextEdit,
)

from app.core.live2dviewer_mod_project import (
    Live2DViewerModProject,
    PROJECT_FILE_NAME,
    create_project_from_base_source,
    generate_live2dviewer_mod,
    load_project,
    sanitize_identifier,
    sanitize_project_name,
    save_project,
)
from app.core.model import resolve_live2d_package
from app.core.settings_manager import SettingsManager
from app.gui.Live2DPreviewPanel import Live2DPreviewPanel
from app.i18n import get_i18n, tr


SUPPORTED_SOURCE_SUFFIXES = {
    ".lpk",
    ".wpk",
    ".json",
    ".moc3",
    ".assets",
    ".sharedassets",
    ".bundle",
    ".unity3d",
}
SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tga"}


class ModProjectImportThread(QThread):
    progressUpdated = Signal(str)
    projectReady = Signal(object)
    projectError = Signal(str)

    def __init__(self, source_path: str, project_name: str, output_root: str, temp_root: str):
        super().__init__()
        self.source_path = source_path
        self.project_name = project_name
        self.output_root = output_root
        self.temp_root = temp_root

    def run(self):
        try:
            project = create_project_from_base_source(
                self.source_path,
                project_name=self.project_name or None,
                output_root=self.output_root,
                temp_root=self.temp_root,
                log=lambda message: self.progressUpdated.emit(str(message)),
            )
            self.projectReady.emit(project)
        except Exception as exc:
            self.projectError.emit(str(exc))


class ModGenerateThread(QThread):
    progressUpdated = Signal(str)
    projectReady = Signal(object)
    projectError = Signal(str)

    def __init__(self, project: Live2DViewerModProject):
        super().__init__()
        self.project = project

    def run(self):
        try:
            project = generate_live2dviewer_mod(
                self.project,
                log=lambda message: self.progressUpdated.emit(str(message)),
            )
            self.projectReady.emit(project)
        except Exception as exc:
            self.projectError.emit(str(exc))


class Live2DModPage(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("live2dModPage")
        self.setAcceptDrops(True)

        self.i18n = get_i18n()
        self.settings_manager = SettingsManager()
        self.current_project: Live2DViewerModProject | None = None
        self.selected_source = ""
        self.project_worker: ModProjectImportThread | None = None
        self.generate_worker: ModGenerateThread | None = None
        self._project_combo_refreshing = False
        self._hitarea_combo_refreshing = False
        self._skin_table_refreshing = False

        self.setupUI()
        self.retranslate_ui()
        self.i18n.languageChanged.connect(self.retranslate_ui)
        self.refresh_project_combo()
        self.load_last_project(silent=True)

    def setupUI(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(20, 20, 20, 20)
        self.main_layout.setSpacing(12)

        self.title_label = SubtitleLabel("", self)
        self.main_layout.addWidget(self.title_label)

        self.drop_frame = QFrame(self)
        self.drop_frame.setObjectName("modDropFrame")
        self.drop_frame.setMinimumHeight(42)
        self.drop_frame.setMaximumHeight(52)
        drop_layout = QVBoxLayout(self.drop_frame)
        drop_layout.setContentsMargins(14, 6, 14, 6)
        drop_layout.setSpacing(0)
        self.drop_main_label = BodyLabel("", self.drop_frame)
        self.drop_sub_label = CaptionLabel("", self.drop_frame)
        self.drop_sub_label.setVisible(False)
        drop_layout.addWidget(self.drop_main_label)
        drop_layout.addWidget(self.drop_sub_label)
        self.main_layout.addWidget(self.drop_frame)

        self.content_splitter = QSplitter(Qt.Horizontal, self)
        self.content_splitter.setChildrenCollapsible(False)
        self.main_layout.addWidget(self.content_splitter, 1)

        self.left_panel = QWidget(self.content_splitter)
        self.left_panel.setMinimumWidth(340)
        self.left_layout = QVBoxLayout(self.left_panel)
        self.left_layout.setContentsMargins(0, 0, 6, 0)
        self.left_layout.setSpacing(10)
        self.content_splitter.addWidget(self.left_panel)

        self.center_panel = QWidget(self.content_splitter)
        self.center_panel.setMinimumWidth(420)
        self.center_layout = QVBoxLayout(self.center_panel)
        self.center_layout.setContentsMargins(6, 0, 6, 0)
        self.center_layout.setSpacing(10)
        self.content_splitter.addWidget(self.center_panel)

        self.right_panel = QWidget(self.content_splitter)
        self.right_panel.setMinimumWidth(360)
        self.right_layout = QVBoxLayout(self.right_panel)
        self.right_layout.setContentsMargins(6, 0, 0, 0)
        self.right_layout.setSpacing(10)
        self.content_splitter.addWidget(self.right_panel)
        self.content_splitter.setSizes([420, 760, 520])

        self.project_frame, self.project_layout = self._create_card(self.left_panel)
        self.project_title_label = SubtitleLabel("", self.project_frame)
        self.project_layout.addWidget(self.project_title_label)

        self.project_name_edit = LineEdit(self.project_frame)
        self.project_layout.addWidget(self.project_name_edit)

        self.project_combo = EditableComboBox(self.project_frame)
        self.project_combo.textChanged.connect(self.on_project_combo_text_changed)
        self.project_combo.currentIndexChanged.connect(self.on_project_combo_changed)
        self.project_layout.addWidget(self.project_combo)

        self.project_buttons = QGridLayout()
        self.project_buttons.setHorizontalSpacing(8)
        self.project_buttons.setVerticalSpacing(8)
        self.new_project_button = PushButton("", self.project_frame)
        self.new_project_button.clicked.connect(self.create_project_from_current_source)
        self.open_project_button = PushButton("", self.project_frame)
        self.open_project_button.clicked.connect(self.open_project_file)
        self.save_project_button = PushButton("", self.project_frame)
        self.save_project_button.clicked.connect(self.save_current_project)
        self.open_project_folder_button = PushButton("", self.project_frame)
        self.open_project_folder_button.clicked.connect(self.open_current_project_folder)
        self.project_buttons.addWidget(self.new_project_button, 0, 0)
        self.project_buttons.addWidget(self.open_project_button, 0, 1)
        self.project_buttons.addWidget(self.save_project_button, 1, 0)
        self.project_buttons.addWidget(self.open_project_folder_button, 1, 1)
        self.project_layout.addLayout(self.project_buttons)

        self.project_status_label = CaptionLabel("", self.project_frame)
        self.project_status_label.setWordWrap(True)
        self.project_layout.addWidget(self.project_status_label)
        self.left_layout.addWidget(self.project_frame)

        self.source_frame, self.source_frame_layout = self._create_card(self.left_panel)
        self.source_title_label = SubtitleLabel("", self.source_frame)
        self.source_frame_layout.addWidget(self.source_title_label)
        self.source_edit = LineEdit(self.source_frame)
        self.source_edit.setReadOnly(True)
        self.source_frame_layout.addWidget(self.source_edit)
        self.source_buttons = QHBoxLayout()
        self.source_buttons.setSpacing(8)
        self.source_file_button = PushButton("", self.source_frame)
        self.source_file_button.clicked.connect(self.browse_source_file)
        self.source_folder_button = PushButton("", self.source_frame)
        self.source_folder_button.clicked.connect(self.browse_source_folder)
        self.source_buttons.addWidget(self.source_file_button, 1)
        self.source_buttons.addWidget(self.source_folder_button, 1)
        self.source_frame_layout.addLayout(self.source_buttons)
        self.source_hint_label = CaptionLabel("", self.source_frame)
        self.source_hint_label.setWordWrap(True)
        self.source_frame_layout.addWidget(self.source_hint_label)
        self.left_layout.addWidget(self.source_frame)

        self.hitarea_frame, self.hitarea_layout = self._create_card(self.left_panel)
        self.hitarea_title_label = SubtitleLabel("", self.hitarea_frame)
        self.hitarea_layout.addWidget(self.hitarea_title_label)
        self.hitarea_search_edit = LineEdit(self.hitarea_frame)
        self.hitarea_search_edit.textChanged.connect(self.refresh_hitarea_combo)
        self.hitarea_layout.addWidget(self.hitarea_search_edit)
        self.hitarea_combo = ComboBox(self.hitarea_frame)
        self.hitarea_combo.currentIndexChanged.connect(self.on_hitarea_changed)
        self.hitarea_layout.addWidget(self.hitarea_combo)
        self.hitarea_status_label = CaptionLabel("", self.hitarea_frame)
        self.hitarea_status_label.setWordWrap(True)
        self.hitarea_layout.addWidget(self.hitarea_status_label)
        self.left_layout.addWidget(self.hitarea_frame)

        self.skin_config_frame, self.skin_config_layout = self._create_card(self.right_panel)
        self.skin_config_title_label = SubtitleLabel("", self.skin_config_frame)
        self.skin_config_layout.addWidget(self.skin_config_title_label)
        self.skin_name_edit = LineEdit(self.skin_config_frame)
        self.skin_config_layout.addWidget(self.skin_name_edit)
        self.skin_config_buttons = QHBoxLayout()
        self.skin_config_buttons.setSpacing(8)
        self.add_empty_skin_button = PushButton("", self.skin_config_frame)
        self.add_empty_skin_button.clicked.connect(self.add_empty_skin)
        self.remove_skin_button = PushButton("", self.skin_config_frame)
        self.remove_skin_button.clicked.connect(self.remove_selected_skin)
        self.generate_button = PushButton("", self.skin_config_frame)
        self.generate_button.clicked.connect(self.start_generate_mod)
        self.skin_config_buttons.addWidget(self.add_empty_skin_button, 1)
        self.skin_config_buttons.addWidget(self.remove_skin_button, 1)
        self.skin_config_buttons.addWidget(self.generate_button, 1)
        self.skin_config_layout.addLayout(self.skin_config_buttons)
        self.right_layout.addWidget(self.skin_config_frame)

        self.preview_control_frame, self.preview_control_layout = self._create_card(self.center_panel)
        self.preview_control_title_label = SubtitleLabel("", self.preview_control_frame)
        self.preview_control_layout.addWidget(self.preview_control_title_label)

        self.preview_source_layout = QHBoxLayout()
        self.preview_source_label = BodyLabel("", self.preview_control_frame)
        self.preview_source_combo = ComboBox(self.preview_control_frame)
        self.preview_source_combo.currentIndexChanged.connect(self.on_preview_source_changed)
        self.preview_source_layout.addWidget(self.preview_source_label)
        self.preview_source_layout.addWidget(self.preview_source_combo, 1)
        self.preview_control_layout.addLayout(self.preview_source_layout)

        self.preview_texture_layout = QHBoxLayout()
        self.preview_texture_label = BodyLabel("", self.preview_control_frame)
        self.preview_texture_combo = ComboBox(self.preview_control_frame)
        self.preview_texture_layout.addWidget(self.preview_texture_label)
        self.preview_texture_layout.addWidget(self.preview_texture_combo, 1)
        self.preview_control_layout.addLayout(self.preview_texture_layout)

        self.preview_buttons = QHBoxLayout()
        self.preview_buttons.setSpacing(8)
        self.preview_images_button = PushButton("", self.preview_control_frame)
        self.preview_images_button.clicked.connect(self.load_preview_images)
        self.preview_live2d_button = PushButton("", self.preview_control_frame)
        self.preview_live2d_button.clicked.connect(self.load_preview_live2d)
        self.preview_close_button = PushButton("", self.preview_control_frame)
        self.preview_close_button.clicked.connect(self.close_preview)
        self.preview_buttons.addWidget(self.preview_images_button, 1)
        self.preview_buttons.addWidget(self.preview_live2d_button, 1)
        self.preview_buttons.addWidget(self.preview_close_button, 1)
        self.preview_control_layout.addLayout(self.preview_buttons)
        self.center_layout.addWidget(self.preview_control_frame)

        self.left_layout.addStretch(1)

        self.preview_panel = Live2DPreviewPanel(self.center_panel, "modPreviewPanel")
        self.center_layout.addWidget(self.preview_panel, 1)

        self.skin_frame, self.skin_layout = self._create_card(self.right_panel)
        self.skin_header_layout = QHBoxLayout()
        self.skin_header_layout.setSpacing(8)
        self.skin_title_label = SubtitleLabel("", self.skin_frame)
        self.add_skin_file_button = PushButton("", self.skin_frame)
        self.add_skin_file_button.clicked.connect(self.add_skin_file)
        self.add_skin_folder_button = PushButton("", self.skin_frame)
        self.add_skin_folder_button.clicked.connect(self.add_skin_folder)
        self.skin_header_layout.addWidget(self.skin_title_label)
        self.skin_header_layout.addStretch(1)
        self.skin_header_layout.addWidget(self.add_skin_file_button)
        self.skin_header_layout.addWidget(self.add_skin_folder_button)
        self.skin_layout.addLayout(self.skin_header_layout)

        self.skin_table = QTableWidget(self.skin_frame)
        self.skin_table.setColumnCount(4)
        self.skin_table.verticalHeader().setVisible(False)
        self.skin_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.skin_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.skin_table.itemChanged.connect(self.on_skin_item_changed)
        self.skin_table.itemSelectionChanged.connect(self.update_skin_buttons)
        self.skin_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.skin_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.skin_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.skin_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.skin_layout.addWidget(self.skin_table, 1)
        self.right_layout.addWidget(self.skin_frame, 1)

        self.texture_frame, self.texture_layout = self._create_card(self.right_panel)
        self.texture_header_layout = QHBoxLayout()
        self.texture_header_layout.setSpacing(8)
        self.texture_title_label = SubtitleLabel("", self.texture_frame)
        self.texture_target_label = BodyLabel("", self.texture_frame)
        self.texture_target_combo = ComboBox(self.texture_frame)
        self.add_texture_button = PushButton("", self.texture_frame)
        self.add_texture_button.clicked.connect(self.add_texture_replacement)
        self.texture_header_layout.addWidget(self.texture_title_label)
        self.texture_header_layout.addStretch(1)
        self.texture_header_layout.addWidget(self.texture_target_label)
        self.texture_header_layout.addWidget(self.texture_target_combo, 1)
        self.texture_header_layout.addWidget(self.add_texture_button)
        self.texture_layout.addLayout(self.texture_header_layout)

        self.texture_table = QTableWidget(self.texture_frame)
        self.texture_table.setColumnCount(6)
        self.texture_table.verticalHeader().setVisible(False)
        self.texture_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.texture_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.texture_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.texture_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.texture_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.texture_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.texture_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.texture_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.texture_layout.addWidget(self.texture_table, 1)
        self.right_layout.addWidget(self.texture_frame, 1)

        self.log_frame, self.log_layout = self._create_card(self.right_panel)
        self.log_label = SubtitleLabel("", self.log_frame)
        self.log_layout.addWidget(self.log_label)
        self.log_text = TextEdit(self.log_frame)
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(160)
        self.log_layout.addWidget(self.log_text, 1)
        self.right_layout.addWidget(self.log_frame, 1)

        for button in self.findChildren(PushButton):
            button.setMinimumHeight(32)
            button.setMaximumHeight(36)
        for line_edit in self.findChildren(LineEdit):
            line_edit.setMinimumHeight(32)
            line_edit.setMaximumHeight(36)
        self._apply_static_styles()

    def retranslate_ui(self):
        self.title_label.setText(tr("mod.title"))
        self.drop_main_label.setText(tr("mod.drop_main"))
        self.drop_sub_label.setText(tr("mod.drop_sub"))
        self.project_title_label.setText(tr("mod.project.title"))
        self.project_name_edit.setPlaceholderText(tr("mod.project.name_placeholder"))
        self.project_combo.setPlaceholderText(tr("mod.project.search_placeholder"))
        self.new_project_button.setText(tr("mod.project.new"))
        self.open_project_button.setText(tr("mod.project.open"))
        self.save_project_button.setText(tr("mod.project.save"))
        self.open_project_folder_button.setText(tr("mod.project.open_folder"))
        self.source_title_label.setText(tr("mod.source.title"))
        self.source_edit.setPlaceholderText(tr("mod.source.placeholder"))
        self.source_file_button.setText(tr("mod.source.browse_file"))
        self.source_folder_button.setText(tr("mod.source.browse_folder"))
        self.source_hint_label.setText(tr("mod.source.hint"))
        self.hitarea_title_label.setText(tr("mod.hitarea.title"))
        self.hitarea_search_edit.setPlaceholderText(tr("mod.hitarea.search_placeholder"))
        self.skin_config_title_label.setText(tr("mod.skin.config_title"))
        self.skin_name_edit.setPlaceholderText(tr("mod.skin.name_placeholder"))
        self.add_empty_skin_button.setText(tr("mod.skin.add_empty"))
        self.remove_skin_button.setText(tr("mod.skin.remove"))
        self.generate_button.setText(tr("mod.generate.button"))
        self.preview_control_title_label.setText(tr("mod.preview.controls"))
        self.preview_source_label.setText(tr("mod.preview.source"))
        self.preview_texture_label.setText(tr("mod.preview.texture"))
        self.preview_images_button.setText(tr("mod.preview.load_images"))
        self.preview_live2d_button.setText(tr("mod.preview.load_live2d"))
        self.preview_close_button.setText(tr("mod.preview.close"))
        self.preview_panel.retranslate_ui()
        self.skin_title_label.setText(tr("mod.skin.title"))
        self.add_skin_file_button.setText(tr("mod.skin.add_file"))
        self.add_skin_folder_button.setText(tr("mod.skin.add_folder"))
        self.texture_title_label.setText(tr("mod.texture.title"))
        self.texture_target_label.setText(tr("mod.texture.target"))
        self.add_texture_button.setText(tr("mod.texture.add"))
        self.log_label.setText(tr("mod.log"))
        self.skin_table.setHorizontalHeaderLabels(
            [
                tr("mod.skin.column.name"),
                tr("mod.skin.column.source"),
                tr("mod.skin.column.status"),
                tr("mod.skin.column.preview"),
            ]
        )
        self.texture_table.setHorizontalHeaderLabels(
            [
                tr("mod.texture.column.skin"),
                tr("mod.texture.column.target"),
                tr("mod.texture.column.source"),
                tr("mod.texture.column.status"),
                tr("mod.texture.column.note"),
                tr("mod.texture.column.preview"),
            ]
        )
        self.refresh_project_ui()
        self.refresh_project_combo()
        self.refresh_hitarea_combo()
        self.refresh_skin_table()
        self.refresh_texture_target_combo()
        self.refresh_texture_table()
        self.refresh_preview_source_combo()
        self.refresh_preview_texture_combo()
        self.refresh_preview_source_combo()
        self.refresh_preview_texture_combo()

    def dragEnterEvent(self, event: QDragEnterEvent):
        if not event.mimeData().hasUrls():
            return
        for url in event.mimeData().urls():
            if self._is_supported_drop_path(url.toLocalFile()):
                event.acceptProposedAction()
                return

    def dropEvent(self, event: QDropEvent):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if not self._is_supported_drop_path(path):
                continue
            if Path(path).is_file() and Path(path).name == PROJECT_FILE_NAME:
                self.load_project_file(path)
                event.acceptProposedAction()
                return
            if self.current_project and self._is_image_path(path):
                self.record_texture_replacement(path)
            elif self.current_project and self._is_supported_source_path(path):
                self.record_skin_source(path)
            elif self._is_supported_source_path(path):
                self.set_source(path)
            event.acceptProposedAction()
            return

    def browse_source_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("mod.dialog.select_source_file"),
            "",
            tr("mod.dialog.filter_source_files"),
        )
        if path:
            self.set_source(path)

    def browse_source_folder(self):
        path = QFileDialog.getExistingDirectory(
            self,
            tr("mod.dialog.select_source_folder"),
        )
        if path:
            self.set_source(path)

    def set_source(self, path: str):
        self.selected_source = os.path.abspath(path)
        self.source_edit.setText(self.selected_source)
        if not self.project_name_edit.text().strip():
            self.project_name_edit.setText(self.suggest_project_name(self.selected_source))
        self.append_log(tr("mod.source.selected", path=self.selected_source))

    def create_project_from_current_source(self):
        source = self.source_edit.text().strip()
        if not source:
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("mod.warning.no_source"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return
        if not self._is_supported_source_path(source):
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("mod.warning.unsupported_source"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return

        self.set_busy(True)
        self.project_status_label.setText(tr("mod.project.creating"))
        self.log_text.clear()
        self.append_log(tr("mod.project.creating_from", path=source))
        self.project_worker = ModProjectImportThread(
            source,
            self.project_name_edit.text().strip(),
            self.settings_manager.get_output_dir("live2dviewer_mod"),
            self.settings_manager.get_temp_dir(),
        )
        self.project_worker.progressUpdated.connect(self.append_log)
        self.project_worker.projectReady.connect(self.on_project_created)
        self.project_worker.projectError.connect(self.on_project_error)
        self.project_worker.finished.connect(lambda: setattr(self, "project_worker", None))
        self.project_worker.start()

    def on_project_created(self, project: Live2DViewerModProject):
        self.set_busy(False)
        self.set_current_project(project)
        self.append_log(tr("mod.project.created", path=str(project.project_file)))
        InfoBar.success(
            title=tr("common.success"),
            content=tr("mod.project.created", path=str(project.project_file)),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=3500,
        )

    def on_project_error(self, error: str):
        self.set_busy(False)
        self.project_status_label.setText(tr("mod.project.no_project"))
        self.append_log(tr("mod.error.create_failed", error=error))
        InfoBar.error(
            title=tr("common.error"),
            content=error,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=6000,
        )

    def open_project_file(self):
        last_project = str(self.settings_manager.get("live2dviewer_mod.last_project_file", "") or "")
        start_dir = (
            str(Path(last_project).parent)
            if last_project and Path(last_project).exists()
            else self.settings_manager.get_output_dir("live2dviewer_mod")
        )
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("mod.dialog.open_project"),
            start_dir,
            tr("mod.dialog.filter_project_files"),
        )
        if path:
            self.load_project_file(path)

    def load_last_project(self, silent: bool = False):
        path = str(self.settings_manager.get("live2dviewer_mod.last_project_file", "") or "").strip()
        if not path or not Path(path).is_file():
            if not silent:
                InfoBar.warning(
                    title=tr("common.warning"),
                    content=tr("mod.project.no_last"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=3000,
                )
            return
        self.load_project_file(path, silent=silent)

    def load_project_file(self, path: str, silent: bool = False):
        try:
            project = load_project(path)
            self.set_current_project(project)
            self.append_log(tr("mod.project.opened", path=str(project.project_file)))
        except Exception as exc:
            if not silent:
                InfoBar.error(
                    title=tr("common.error"),
                    content=str(exc),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=5000,
                )

    def set_current_project(self, project: Live2DViewerModProject):
        self.current_project = project
        self.remember_project_file(project)
        self.project_name_edit.setText(project.project_name)
        source = project.data.get("base_source") or {}
        self.selected_source = str(source.get("path") or "")
        self.source_edit.setText(str(project.base_model_json))
        self.refresh_project_ui()
        self.refresh_project_combo(select_project_file=str(project.project_file))
        self.refresh_hitarea_combo()
        self.refresh_skin_table()
        self.refresh_texture_target_combo()
        self.refresh_texture_table()

    def save_current_project(self):
        if not self.current_project:
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("mod.warning.no_project"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return
        data = dict(self.current_project.data)
        data["project_name"] = sanitize_project_name(
            self.project_name_edit.text() or self.current_project.project_name
        )
        selected_hit_area = str(self.hitarea_combo.currentData() or "")
        data["selected_hit_area"] = selected_hit_area
        self.sync_skin_names_from_table(data)
        project_file = save_project(self.current_project.project_dir, data)
        self.current_project = Live2DViewerModProject(self.current_project.project_dir, project_file, data)
        self.remember_project_file(self.current_project)
        self.refresh_project_ui()
        self.refresh_project_combo(select_project_file=str(project_file))
        self.append_log(tr("mod.project.saved", path=str(project_file)))
        InfoBar.success(
            title=tr("common.success"),
            content=tr("mod.project.saved", path=str(project_file)),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=2500,
        )

    def sync_skin_names_from_table(self, data: dict[str, Any] | None = None):
        if self._skin_table_refreshing or not self.current_project:
            return
        target_data = data if data is not None else self.current_project.data
        skins = list(target_data.get("skins") or [])
        if not skins or not hasattr(self, "skin_table"):
            return
        by_id = {str(item.get("id") or ""): item for item in skins if isinstance(item, dict)}
        for row in range(self.skin_table.rowCount()):
            item = self.skin_table.item(row, 0)
            if not item:
                continue
            skin_id = str(item.data(Qt.UserRole) or "")
            skin = by_id.get(skin_id)
            if not skin:
                continue
            name = item.text().strip()
            if name:
                skin["name"] = name
        target_data["skins"] = skins

    def open_current_project_folder(self):
        if not self.current_project:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.current_project.project_dir.resolve())))

    def remember_project_file(self, project: Live2DViewerModProject):
        path = str(project.project_file.resolve())
        recent = self._recent_project_files()
        recent = [item for item in recent if str(Path(item).resolve()) != path]
        recent.insert(0, path)
        self.settings_manager.set("live2dviewer_mod.last_project_file", path)
        self.settings_manager.set("live2dviewer_mod.project_files", recent[:30])

    def _recent_project_files(self) -> list[str]:
        raw = self.settings_manager.get("live2dviewer_mod.project_files", [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        result = []
        seen = set()
        for item in raw:
            try:
                path = str(Path(str(item)).resolve())
            except Exception:
                continue
            if path in seen or not Path(path).is_file():
                continue
            seen.add(path)
            result.append(path)
        return result

    def discover_project_files(self) -> list[str]:
        paths: list[str] = []
        seen = set()

        def add(path: str | Path):
            try:
                resolved = Path(path).resolve()
            except Exception:
                return
            if not resolved.is_file() or resolved.name != PROJECT_FILE_NAME:
                return
            key = str(resolved)
            if key in seen:
                return
            seen.add(key)
            paths.append(key)

        last_project = str(self.settings_manager.get("live2dviewer_mod.last_project_file", "") or "").strip()
        if last_project:
            add(last_project)
        for item in self._recent_project_files():
            add(item)
        try:
            root = Path(self.settings_manager.get_output_dir("live2dviewer_mod"))
            if root.is_dir():
                for path in sorted(root.rglob(PROJECT_FILE_NAME), key=lambda p: p.stat().st_mtime, reverse=True):
                    add(path)
        except Exception:
            pass
        return paths

    def refresh_project_combo(self, *_args, select_project_file: str | None = None):
        if not hasattr(self, "project_combo"):
            return
        selected = select_project_file
        if selected is None and self.current_project:
            selected = str(self.current_project.project_file)
        selected = str(Path(selected).resolve()) if selected else ""
        query = self.project_combo.text().strip().lower() if hasattr(self, "project_combo") else ""

        self._project_combo_refreshing = True
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        matched = []
        for path in self.discover_project_files():
            label = Path(path).parent.name
            haystack = f"{label} {path}".lower()
            if query and query not in haystack:
                continue
            matched.append(path)
            self.project_combo.addItem(label, userData=path)
        if not matched:
            self.project_combo.addItem(tr("mod.project.no_projects"), userData="")
        index = self._combo_index_by_data(self.project_combo, selected)
        self.project_combo.setCurrentIndex(index if index >= 0 else 0)
        self.project_combo.blockSignals(False)
        self._project_combo_refreshing = False

    def on_project_combo_text_changed(self, *_args):
        if self._project_combo_refreshing:
            return
        self.refresh_project_combo()

    def on_project_combo_changed(self, *_args):
        if self._project_combo_refreshing:
            return
        path = str(self.project_combo.currentData() or "")
        if not path:
            return
        if self.current_project and str(self.current_project.project_file.resolve()) == str(Path(path).resolve()):
            return
        self.load_project_file(path)

    def refresh_project_ui(self):
        if not hasattr(self, "project_status_label"):
            return
        has_project = self.current_project is not None
        self.save_project_button.setEnabled(has_project and self.project_worker is None)
        self.open_project_folder_button.setEnabled(has_project and self.project_worker is None)
        self.add_skin_file_button.setEnabled(has_project and self.project_worker is None)
        self.add_skin_folder_button.setEnabled(has_project and self.project_worker is None)
        self.add_texture_button.setEnabled(has_project and self.project_worker is None)
        self.add_empty_skin_button.setEnabled(has_project and self.project_worker is None)
        self.generate_button.setEnabled(has_project and self.project_worker is None)
        self.preview_images_button.setEnabled(has_project and self.project_worker is None)
        self.preview_live2d_button.setEnabled(has_project and self.project_worker is None)
        self.preview_close_button.setEnabled(has_project)
        if not has_project:
            self.project_status_label.setText(tr("mod.project.no_project"))
            self.hitarea_status_label.setText(tr("mod.hitarea.no_project"))
            self.refresh_preview_source_combo()
            self.refresh_preview_texture_combo()
            return
        self.project_status_label.setText(
            tr("mod.project.current", path=str(self.current_project.project_file))
        )
        self.refresh_preview_source_combo()
        self.refresh_preview_texture_combo()

    def refresh_hitarea_combo(self, *_args):
        if not hasattr(self, "hitarea_combo"):
            return
        selected = ""
        if self.current_project:
            selected = str(self.current_project.data.get("selected_hit_area") or "")
        current_data = str(self.hitarea_combo.currentData() or "")
        if current_data:
            selected = current_data
        query = self.hitarea_search_edit.text().strip().lower() if hasattr(self, "hitarea_search_edit") else ""
        hit_areas = self.current_project.data.get("hit_areas", []) if self.current_project else []

        self._hitarea_combo_refreshing = True
        self.hitarea_combo.blockSignals(True)
        self.hitarea_combo.clear()
        self.hitarea_combo.addItem(tr("mod.hitarea.none"), userData="")
        for item in hit_areas:
            hit_id = str(item.get("id") or "").strip()
            name = str(item.get("name") or hit_id).strip()
            if not hit_id:
                continue
            label = f"{name} ({hit_id})" if name and name != hit_id else hit_id
            if query and query not in f"{name} {hit_id}".lower():
                continue
            self.hitarea_combo.addItem(label, userData=hit_id)
        index = self._combo_index_by_data(self.hitarea_combo, selected)
        self.hitarea_combo.setCurrentIndex(index if index >= 0 else 0)
        self.hitarea_combo.blockSignals(False)
        self._hitarea_combo_refreshing = False
        self.update_hitarea_status()

    def on_hitarea_changed(self, *_args):
        if self._hitarea_combo_refreshing or not self.current_project:
            return
        hit_id = str(self.hitarea_combo.currentData() or "")
        self.current_project.data["selected_hit_area"] = hit_id
        self.update_hitarea_status()

    def update_hitarea_status(self):
        if not self.current_project:
            self.hitarea_status_label.setText(tr("mod.hitarea.no_project"))
            return
        hit_id = str(self.hitarea_combo.currentData() or "")
        hit_areas = self.current_project.data.get("hit_areas", [])
        if not hit_areas:
            self.hitarea_status_label.setText(tr("mod.hitarea.empty"))
        elif hit_id:
            self.hitarea_status_label.setText(tr("mod.hitarea.selected", id=hit_id))
        else:
            self.hitarea_status_label.setText(tr("mod.hitarea.unselected", count=len(hit_areas)))

    def add_empty_skin(self):
        if not self.current_project:
            self._warn_no_project()
            return
        name = self.skin_name_edit.text().strip() or tr(
            "mod.skin.default_name",
            index=len(self.current_project.data.get("skins") or []) + 1,
        )
        skin = self.create_skin_entry(name)
        skins = list(self.current_project.data.get("skins") or [])
        skins.append(skin)
        self.current_project.data["skins"] = skins
        self.persist_current_project()
        self.refresh_skin_table(select_skin_id=str(skin.get("id") or ""))
        self.refresh_texture_table()
        self.append_log(tr("mod.skin.added", name=skin["name"]))

    def remove_selected_skin(self):
        if not self.current_project:
            return
        skin_id = self.selected_skin_id()
        if not skin_id:
            return
        skins = [
            item
            for item in (self.current_project.data.get("skins") or [])
            if isinstance(item, dict) and str(item.get("id") or "") != skin_id
        ]
        self.current_project.data["skins"] = skins
        self.persist_current_project()
        self.refresh_skin_table()
        self.refresh_texture_table()
        self.append_log(tr("mod.skin.removed"))

    def add_skin_file(self):
        if not self.current_project:
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("mod.dialog.select_skin_file"),
            "",
            tr("mod.dialog.filter_skin_files"),
        )
        if path:
            self.record_skin_source(path)

    def add_skin_folder(self):
        if not self.current_project:
            return
        path = QFileDialog.getExistingDirectory(
            self,
            tr("mod.dialog.select_skin_folder"),
        )
        if path:
            self.record_skin_source(path)

    def record_skin_source(self, path: str):
        if not self.current_project:
            return
        source = str(Path(path).resolve())
        skin_name = self.skin_name_edit.text().strip() or self.suggest_project_name(source)
        skin = self.create_skin_entry(skin_name, source=source)
        image_paths = self.image_paths_from_source(source)
        if image_paths:
            base_textures = self.base_texture_paths()
            selected_target = self.selected_target_index()
            if len(image_paths) == 1 and selected_target >= 0:
                indexes = [selected_target]
            else:
                indexes = list(range(min(len(image_paths), len(base_textures))))
            for target_index, image_path in zip(indexes, image_paths):
                skin["texture_replacements"].append(
                    self.texture_replacement_entry(str(image_path), target_index)
                )

        skins = list(self.current_project.data.get("skins") or [])
        skins.append(skin)
        self.current_project.data["skins"] = skins
        self.persist_current_project()
        self.refresh_skin_table(select_skin_id=str(skin.get("id") or ""))
        self.refresh_texture_table()
        self.append_log(tr("mod.skin.recorded", path=source))

    def add_texture_replacement(self):
        if not self.current_project:
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("mod.dialog.select_texture_file"),
            "",
            tr("dialog.filter_image_files"),
        )
        if path:
            self.record_texture_replacement(path)

    def record_texture_replacement(self, path: str):
        if not self.current_project:
            return
        target_index = self.selected_target_index()
        if target_index < 0:
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("mod.warning.no_texture_target"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return
        skin = self.selected_skin()
        if skin is None:
            name = self.skin_name_edit.text().strip() or tr(
                "mod.skin.default_name",
                index=len(self.current_project.data.get("skins") or []) + 1,
            )
            skin = self.create_skin_entry(name)
            self.current_project.data.setdefault("skins", []).append(skin)
        source = str(Path(path).resolve())
        replacements = list(skin.get("texture_replacements") or [])
        replacements.append(self.texture_replacement_entry(source, target_index))
        skin["texture_replacements"] = replacements
        skin["status"] = "source"
        self.persist_current_project()
        self.refresh_skin_table(select_skin_id=str(skin.get("id") or ""))
        self.refresh_texture_table()
        self.append_log(tr("mod.texture.recorded", path=source))

    def create_skin_entry(self, name: str, source: str = "") -> dict[str, Any]:
        skins = self.current_project.data.get("skins") if self.current_project else []
        base_id = sanitize_identifier(name or f"skin_{len(skins) + 1}", f"skin_{len(skins) + 1}")
        existing = {str(item.get("id") or "") for item in skins if isinstance(item, dict)}
        skin_id = base_id
        suffix = 1
        while skin_id in existing:
            suffix += 1
            skin_id = f"{base_id}_{suffix}"
        return {
            "id": skin_id,
            "name": name.strip() or skin_id,
            "source": source,
            "texture_replacements": [],
            "generated_skin_json_path": "",
            "generated_model_json_path": "",
            "status": "source",
        }

    def texture_replacement_entry(self, source: str, target_index: int) -> dict[str, Any]:
        textures = self.base_texture_paths()
        target_texture = self.relative_to_project(textures[target_index]) if 0 <= target_index < len(textures) else ""
        return {
            "target_index": target_index,
            "target_texture": target_texture,
            "source": source,
            "workspace_path": "",
            "status": "source",
            "note": "",
        }

    def persist_current_project(self):
        if not self.current_project:
            return
        project_file = save_project(self.current_project.project_dir, self.current_project.data)
        self.current_project = Live2DViewerModProject(
            self.current_project.project_dir,
            project_file,
            self.current_project.data,
        )
        self.remember_project_file(self.current_project)

    def start_generate_mod(self):
        if not self.current_project:
            self._warn_no_project()
            return
        self.sync_skin_names_from_table()
        selected_hit_area = str(self.hitarea_combo.currentData() or "")
        self.current_project.data["selected_hit_area"] = selected_hit_area
        if not self.current_project.data.get("skins"):
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("mod.warning.no_skins"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return
        self.persist_current_project()
        self.set_busy(True)
        self.append_log(tr("mod.generate.started"))
        self.generate_worker = ModGenerateThread(self.current_project)
        self.generate_worker.progressUpdated.connect(self.append_log)
        self.generate_worker.projectReady.connect(self.on_generate_finished)
        self.generate_worker.projectError.connect(self.on_generate_error)
        self.generate_worker.finished.connect(lambda: setattr(self, "generate_worker", None))
        self.generate_worker.start()

    def on_generate_finished(self, project: Live2DViewerModProject):
        self.set_busy(False)
        self.set_current_project(project)
        self.append_log(tr("mod.generate.finished", path=str(project.project_dir / project.data.get("generated_output_dir", ""))))
        InfoBar.success(
            title=tr("common.success"),
            content=tr("mod.generate.success"),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=3000,
        )

    def on_generate_error(self, error: str):
        self.set_busy(False)
        self.append_log(tr("mod.generate.failed", error=error))
        InfoBar.error(
            title=tr("common.error"),
            content=error,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=6000,
        )

    def image_paths_from_source(self, path: str, limit: int | None = None) -> list[Path]:
        source = Path(path)
        if source.is_file() and self._is_image_path(str(source)):
            return [source.resolve()]
        if not source.is_dir():
            return []
        max_count = limit or max(1, len(self.base_texture_paths()))
        paths = [
            item.resolve()
            for item in sorted(source.rglob("*"), key=lambda p: str(p).lower())
            if item.is_file() and item.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES
        ]
        return paths[:max_count]

    def refresh_skin_table(self, select_skin_id: str = ""):
        if not hasattr(self, "skin_table"):
            return
        current_id = select_skin_id or self.selected_skin_id()
        self._skin_table_refreshing = True
        self.skin_table.setRowCount(0)
        skins = self.current_project.data.get("skins", []) if self.current_project else []
        if not skins:
            self._set_empty_table_row(self.skin_table, tr("mod.skin.empty"), 4)
            self._skin_table_refreshing = False
            self.update_skin_buttons()
            return
        self.skin_table.setRowCount(len(skins))
        for row, item in enumerate(skins):
            name_item = QTableWidgetItem(str(item.get("name") or ""))
            name_item.setData(Qt.UserRole, str(item.get("id") or ""))
            self.skin_table.setItem(row, 0, name_item)
            source_item = QTableWidgetItem(str(item.get("source") or self.replacement_summary(item)))
            source_item.setFlags(source_item.flags() & ~Qt.ItemIsEditable)
            self.skin_table.setItem(row, 1, source_item)
            status_item = QTableWidgetItem(self.status_text(item.get("status")))
            status_item.setFlags(status_item.flags() & ~Qt.ItemIsEditable)
            self.skin_table.setItem(row, 2, status_item)
            skin_id = str(item.get("id") or "")
            preview_button = PushButton(tr("mod.preview.preview_model"), self.skin_table)
            preview_button.clicked.connect(lambda _checked=False, sid=skin_id: self.preview_skin_entry(sid))
            self.skin_table.setCellWidget(row, 3, preview_button)
            if current_id and str(item.get("id") or "") == current_id:
                self.skin_table.selectRow(row)
        self._skin_table_refreshing = False
        self.update_skin_buttons()

    def on_skin_item_changed(self, item: QTableWidgetItem):
        if self._skin_table_refreshing or not self.current_project or item.column() != 0:
            return
        skin_id = str(item.data(Qt.UserRole) or "")
        if not skin_id:
            return
        for skin in self.current_project.data.get("skins") or []:
            if isinstance(skin, dict) and str(skin.get("id") or "") == skin_id:
                name = item.text().strip()
                if name:
                    skin["name"] = name
                    self.persist_current_project()
                break

    def update_skin_buttons(self):
        has_project = self.current_project is not None
        has_skin = bool(self.selected_skin_id())
        if hasattr(self, "remove_skin_button"):
            self.remove_skin_button.setEnabled(has_project and has_skin and self.project_worker is None)

    def selected_skin_id(self) -> str:
        if not hasattr(self, "skin_table"):
            return ""
        row = self.skin_table.currentRow()
        if row < 0:
            return ""
        item = self.skin_table.item(row, 0)
        return str(item.data(Qt.UserRole) or "") if item else ""

    def selected_skin(self) -> dict[str, Any] | None:
        if not self.current_project:
            return None
        skin_id = self.selected_skin_id()
        if not skin_id:
            return None
        for skin in self.current_project.data.get("skins") or []:
            if isinstance(skin, dict) and str(skin.get("id") or "") == skin_id:
                return skin
        return None

    @staticmethod
    def replacement_summary(skin: dict[str, Any]) -> str:
        count = len([item for item in skin.get("texture_replacements") or [] if isinstance(item, dict)])
        return f"{count} texture(s)"

    def refresh_texture_target_combo(self):
        if not hasattr(self, "texture_target_combo"):
            return
        previous = str(self.texture_target_combo.currentData() or "")
        self.texture_target_combo.blockSignals(True)
        self.texture_target_combo.clear()
        paths = self.base_texture_paths()
        if not paths:
            self.texture_target_combo.addItem(tr("mod.texture.no_targets"), userData="")
        else:
            for index, path in enumerate(paths):
                self.texture_target_combo.addItem(f"{index}: {path.name}", userData=str(index))
            index = self._combo_index_by_data(self.texture_target_combo, previous)
            self.texture_target_combo.setCurrentIndex(index if index >= 0 else 0)
        self.texture_target_combo.blockSignals(False)

    def selected_target_index(self) -> int:
        raw = str(self.texture_target_combo.currentData() or "")
        return int(raw) if raw.isdigit() else -1

    def refresh_texture_table(self):
        if not hasattr(self, "texture_table"):
            return
        self.texture_table.setRowCount(0)
        rows = self.flatten_texture_replacements()
        replacements = rows
        if not replacements:
            self._set_empty_table_row(self.texture_table, tr("mod.texture.empty"), 6)
            return
        self.texture_table.setRowCount(len(replacements))
        for row, item in enumerate(replacements):
            self.texture_table.setItem(row, 0, QTableWidgetItem(str(item.get("skin_name") or "")))
            self.texture_table.setItem(row, 1, QTableWidgetItem(str(item.get("target_texture") or "")))
            self.texture_table.setItem(row, 2, QTableWidgetItem(str(item.get("source") or "")))
            self.texture_table.setItem(row, 3, QTableWidgetItem(self.status_text(item.get("status"))))
            self.texture_table.setItem(row, 4, QTableWidgetItem(str(item.get("note") or "")))
            preview_button = PushButton(tr("mod.preview.preview_image"), self.texture_table)
            preview_button.clicked.connect(
                lambda _checked=False, source=str(item.get("source") or ""), workspace=str(item.get("workspace_path") or ""): (
                    self.preview_texture_replacement(source, workspace)
                )
            )
            self.texture_table.setCellWidget(row, 5, preview_button)

    def flatten_texture_replacements(self) -> list[dict[str, Any]]:
        if not self.current_project:
            return []
        rows: list[dict[str, Any]] = []
        for skin in self.current_project.data.get("skins") or []:
            if not isinstance(skin, dict):
                continue
            for replacement in skin.get("texture_replacements") or []:
                if not isinstance(replacement, dict):
                    continue
                row = dict(replacement)
                row["skin_name"] = str(skin.get("name") or skin.get("id") or "")
                rows.append(row)
        return rows

    def refresh_preview_source_combo(self):
        if not hasattr(self, "preview_source_combo"):
            return
        previous = str(self.preview_source_combo.currentData() or "")
        self.preview_source_combo.blockSignals(True)
        self.preview_source_combo.clear()
        if not self.current_project:
            self.preview_source_combo.addItem(tr("mod.preview.no_project_source"), userData="")
        else:
            self.preview_source_combo.addItem(tr("mod.preview.original"), userData="original")
            if self.generated_model_json_path():
                self.preview_source_combo.addItem(tr("mod.preview.generated"), userData="generated")
        index = self._combo_index_by_data(self.preview_source_combo, previous)
        self.preview_source_combo.setCurrentIndex(index if index >= 0 else 0)
        self.preview_source_combo.blockSignals(False)

    def on_preview_source_changed(self, *_args):
        self.refresh_preview_texture_combo()

    def refresh_preview_texture_combo(self):
        if not hasattr(self, "preview_texture_combo"):
            return
        previous = str(self.preview_texture_combo.currentData() or "")
        paths = self.preview_texture_paths()
        self.preview_texture_combo.blockSignals(True)
        self.preview_texture_combo.clear()
        if paths:
            self.preview_texture_combo.addItem(tr("mod.preview.all_textures"), userData="")
            for path in paths:
                self.preview_texture_combo.addItem(path.name, userData=str(path))
            index = self._combo_index_by_data(self.preview_texture_combo, previous)
            self.preview_texture_combo.setCurrentIndex(index if index >= 0 else 0)
        else:
            self.preview_texture_combo.addItem(tr("mod.preview.no_textures"), userData="")
        self.preview_texture_combo.blockSignals(False)

    def preview_model_json_path(self) -> Path | None:
        if not self.current_project:
            return None
        token = str(self.preview_source_combo.currentData() or "original")
        if token == "generated":
            generated = self.generated_model_json_path()
            return generated if generated and generated.is_file() else None
        return self.current_project.base_model_json

    def generated_model_json_path(self) -> Path | None:
        if not self.current_project:
            return None
        paths = self.current_project.data.get("generated_model_json_paths") or []
        if not paths:
            return None
        return self.resolve_project_path(paths[0])

    def preview_texture_paths(self) -> list[Path]:
        model_json = self.preview_model_json_path()
        if not model_json or not model_json.is_file():
            return []
        try:
            package = resolve_live2d_package(model_json)
            return [path for path in package.texture_paths if path.is_file()]
        except Exception as exc:
            self.append_log(tr("mod.warning.model_textures_failed", error=str(exc)))
            return []

    def selected_preview_image_paths(self) -> list[str]:
        selected = str(self.preview_texture_combo.currentData() or "")
        if selected and Path(selected).is_file():
            return [selected]
        return [str(path) for path in self.preview_texture_paths()]

    def preview_skin_entry(self, skin_id: str):
        if not self.current_project:
            self._warn_no_project()
            return
        skin = None
        for item in self.current_project.data.get("skins") or []:
            if isinstance(item, dict) and str(item.get("id") or "") == str(skin_id):
                skin = item
                break
        if not skin:
            return

        model_path = self._skin_preview_model_path(skin)
        if not model_path:
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("mod.preview.generate_first"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return
        try:
            self.preview_panel.show_live2d(
                model_path,
                tr("mod.preview.skin_title", name=str(skin.get("name") or skin.get("id") or "")),
                {"show_controls": True},
            )
        except Exception as exc:
            InfoBar.error(
                title=tr("common.error"),
                content=str(exc),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )

    def _skin_preview_model_path(self, skin: dict[str, Any]) -> Path | None:
        generated = str(skin.get("generated_model_json_path") or "")
        if generated:
            path = self.resolve_project_path(generated)
            if path.is_file():
                return path
        source = str(skin.get("source") or "")
        if not source:
            return None
        try:
            package = resolve_live2d_package(source)
            return package.model_json if package.model_json.is_file() else None
        except Exception:
            return None

    def preview_texture_replacement(self, source: str, workspace_path: str = ""):
        path = None
        if workspace_path:
            candidate = self.resolve_project_path(workspace_path)
            if candidate.is_file():
                path = candidate
        if path is None and source:
            candidate = Path(source)
            if not candidate.is_absolute():
                candidate = self.resolve_project_path(candidate)
            if candidate.is_file():
                path = candidate
        if path is None:
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("mod.preview.no_images"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return
        self.preview_panel.show_images([path], tr("mod.preview.texture_title", name=path.name))

    def load_preview_images(self):
        if not self.current_project:
            self._warn_no_project()
            return
        image_paths = self.selected_preview_image_paths()
        if not image_paths:
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("mod.preview.no_images"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return
        self.preview_panel.show_images(image_paths, tr("mod.preview.images_title", count=len(image_paths)))

    def load_preview_live2d(self):
        model_json = self.preview_model_json_path()
        if not model_json or not model_json.is_file():
            InfoBar.warning(
                title=tr("common.warning"),
                content=tr("mod.preview.no_model"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return
        try:
            self.preview_panel.show_live2d(model_json, tr("mod.preview.live2d_running"), {"show_controls": True})
        except Exception as exc:
            self.preview_panel.show_placeholder(tr("mod.preview.failed", error=str(exc)), tr("mod.preview.title"))
            InfoBar.error(
                title=tr("common.error"),
                content=str(exc),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )

    def close_preview(self):
        self.preview_panel.close_preview(tr("mod.preview.closed"))

    def base_texture_paths(self) -> list[Path]:
        if not self.current_project:
            return []
        try:
            package = resolve_live2d_package(self.current_project.base_model_json)
            return [path for path in package.texture_paths if path.is_file()]
        except Exception as exc:
            self.append_log(tr("mod.warning.model_textures_failed", error=str(exc)))
            return []

    def resolve_project_path(self, path: str | Path) -> Path:
        if not self.current_project:
            return Path(path).resolve()
        candidate = Path(path)
        if candidate.is_absolute():
            return candidate.resolve()
        return (self.current_project.project_dir / candidate).resolve()

    def relative_to_project(self, path: str | Path) -> str:
        if not self.current_project:
            return str(path)
        try:
            return Path(path).resolve().relative_to(self.current_project.project_dir.resolve()).as_posix()
        except Exception:
            return str(Path(path).resolve())

    def set_busy(self, busy: bool):
        for widget in (
            self.new_project_button,
            self.open_project_button,
            self.save_project_button,
            self.open_project_folder_button,
            self.source_file_button,
            self.source_folder_button,
            self.add_skin_file_button,
            self.add_skin_folder_button,
            self.add_texture_button,
            self.add_empty_skin_button,
            self.remove_skin_button,
            self.generate_button,
            self.preview_images_button,
            self.preview_live2d_button,
            self.preview_close_button,
        ):
            widget.setEnabled(not busy)
        if not busy:
            self.refresh_project_ui()
            self.update_skin_buttons()

    def append_log(self, message: str):
        if message:
            self.log_text.append(str(message))

    def status_text(self, value: Any) -> str:
        if str(value or "") == "source":
            return tr("mod.status.source_recorded")
        if str(value or "") == "generated":
            return tr("mod.status.generated")
        return str(value or "")

    def _warn_no_project(self):
        InfoBar.warning(
            title=tr("common.warning"),
            content=tr("mod.warning.no_project"),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=3000,
        )

    def suggest_project_name(self, path: str) -> str:
        source_path = Path(path)
        try:
            if source_path.is_file() and source_path.suffix.lower() in {".json", ".moc3"}:
                return sanitize_project_name(source_path.parent.name or source_path.stem)
            if source_path.is_file():
                return sanitize_project_name(source_path.stem)
            return sanitize_project_name(source_path.name)
        except Exception:
            return sanitize_project_name(source_path.stem or source_path.name)

    @staticmethod
    def _is_image_path(path: str) -> bool:
        return Path(path).suffix.lower() in SUPPORTED_IMAGE_SUFFIXES and Path(path).is_file()

    @staticmethod
    def _is_supported_source_path(path: str) -> bool:
        candidate = Path(path)
        return candidate.is_dir() or candidate.suffix.lower() in SUPPORTED_SOURCE_SUFFIXES

    def _is_supported_drop_path(self, path: str) -> bool:
        candidate = Path(path)
        if candidate.is_file() and candidate.name == PROJECT_FILE_NAME:
            return True
        return self._is_image_path(path) or self._is_supported_source_path(path)

    @staticmethod
    def _combo_index_by_data(combo: ComboBox | EditableComboBox, data: str) -> int:
        for index in range(combo.count()):
            if str(combo.itemData(index) or "") == str(data or ""):
                return index
        return -1

    @staticmethod
    def _set_empty_table_row(table: QTableWidget, text: str, columns: int):
        table.setRowCount(1)
        for column in range(columns):
            item = QTableWidgetItem(text if column == 0 else "")
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            table.setItem(0, column, item)

    def _create_card(self, parent: QWidget) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame(parent)
        frame.setObjectName("modCard")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        return frame, layout

    def _apply_static_styles(self):
        self.drop_frame.setStyleSheet(
            """
            QFrame#modDropFrame {
                border: 1px dashed #8a8a8a;
                border-radius: 6px;
                background: #fafafa;
            }
            """
        )
        for frame in self.findChildren(QFrame, "modCard"):
            frame.setStyleSheet(
                """
                QFrame#modCard {
                    border: 1px solid #dde2ea;
                    border-radius: 8px;
                    background: #ffffff;
                }
                """
            )

    def updateUIScale(self, window_width, window_height):
        scale_factor = max(1.0, window_width / 1000.0)
        button_height = int(30 * scale_factor)
        for button in self.findChildren(PushButton):
            button.setMinimumHeight(button_height)
        for line_edit in self.findChildren(LineEdit):
            line_edit.setMinimumHeight(button_height)
