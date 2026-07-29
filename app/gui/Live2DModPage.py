from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PIL import Image
from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    CheckBox,
    ComboBox,
    EditableComboBox,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    SubtitleLabel,
)

from app.core.live2dviewer_mod_project import (
    EXPORTS_DIR,
    PROJECT_FILE_NAME,
    Live2DViewerModProject,
    add_model_to_project,
    create_project_from_base_source,
    delete_project_directory,
    export_live2dviewer_mod,
    load_project,
    model_texture_paths,
    move_model_in_project,
    remove_model_from_project,
    rename_project,
    sanitize_identifier,
    sanitize_project_name,
    sanitize_skin_name,
    save_project,
    update_model_mappings,
)
from app.core.settings_manager import SettingsManager
from app.i18n import get_i18n, tr


class ModTaskThread(QThread):
    progressUpdated = Signal(str)
    taskReady = Signal(object)
    taskError = Signal(str)

    def __init__(self, action: str, **kwargs: Any):
        super().__init__()
        self.action = action
        self.kwargs = kwargs

    def run(self):
        try:
            logger = lambda message: self.progressUpdated.emit(str(message))
            if self.action == "create":
                result = create_project_from_base_source(
                    self.kwargs["source"],
                    project_name=self.kwargs.get("project_name") or None,
                    output_root=self.kwargs.get("output_root"),
                    temp_root=self.kwargs.get("temp_root"),
                    log=logger,
                )
            elif self.action == "add":
                result = add_model_to_project(
                    self.kwargs["project"],
                    self.kwargs["source"],
                    skin_name=self.kwargs.get("skin_name") or None,
                    temp_root=self.kwargs.get("temp_root"),
                    log=logger,
                )
            elif self.action == "export":
                result = export_live2dviewer_mod(
                    self.kwargs["project"],
                    self.kwargs["destination"],
                    log=logger,
                )
            else:
                raise RuntimeError(f"Unsupported mod task: {self.action}")
            self.taskReady.emit(result)
        except Exception as exc:
            self.taskError.emit(str(exc))


class DeleteProjectDialog(QDialog):
    def __init__(self, project_name: str, project_dir: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle(
            tr("mod.project.delete_title", default="删除 MOD 工程")
        )
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(14)

        title = SubtitleLabel(
            tr(
                "mod.project.delete_question",
                default="从工程列表移除“{name}”？",
                name=project_name,
            ),
            self,
        )
        detail = BodyLabel(
            tr(
                "mod.project.delete_record_hint",
                default="默认仅删除应用中的工程记录，磁盘文件仍会保留，之后可用“打开工程”重新加入。",
            ),
            self,
        )
        detail.setWordWrap(True)
        path_label = CaptionLabel(str(project_dir), self)
        path_label.setWordWrap(True)
        self.delete_files_checkbox = CheckBox(
            tr(
                "mod.project.delete_files",
                default="同时永久删除整个工程文件夹",
            ),
            self,
        )

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel_button = PushButton(tr("common.cancel"), self)
        delete_button = PrimaryPushButton(
            tr("mod.project.delete", default="删除工程"),
            self,
        )
        cancel_button.clicked.connect(self.reject)
        delete_button.clicked.connect(self.accept)
        actions.addWidget(cancel_button)
        actions.addWidget(delete_button)

        layout.addWidget(title)
        layout.addWidget(detail)
        layout.addWidget(path_label)
        layout.addWidget(self.delete_files_checkbox)
        layout.addLayout(actions)

    @property
    def delete_files(self) -> bool:
        return self.delete_files_checkbox.isChecked()


class TextureMappingDialog(QDialog):
    def __init__(
        self,
        skin_name: str,
        main_textures: list[Path],
        source_textures: list[Path],
        current_mappings: list[tuple[int, Path]],
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(tr("mod.mapping.dialog_title", default="编辑多贴图映射"))
        self.setMinimumSize(900, 560)
        self.main_textures = [
            path.resolve() for path in main_textures if path.is_file()
        ]
        self.source_textures = [
            path.resolve() for path in source_textures if path.is_file()
        ]
        self.current = {
            str(source.resolve()): int(target)
            for target, source in current_mappings
        }

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)
        title = SubtitleLabel(
            tr(
                "mod.mapping.title_for_skin",
                default="皮肤“{name}”的贴图映射",
                name=skin_name,
            ),
            self,
        )
        hint = CaptionLabel(
            tr(
                "mod.mapping.hint",
                default="每一行都是一张导入贴图。请选择它对应的主模型贴图；同名、同尺寸和同序号会优先自动匹配。",
            ),
            self,
        )
        hint.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(hint)

        self.table = QTableWidget(self)
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(
            [
                tr("mod.mapping.source", default="导入贴图"),
                tr("mod.mapping.size", default="尺寸"),
                tr("mod.mapping.target", default="主模型目标"),
            ]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents
        )
        layout.addWidget(self.table, 1)

        self.status_label = CaptionLabel("", self)
        layout.addWidget(self.status_label)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel_button = PushButton(tr("common.cancel", default="取消"), self)
        save_button = PrimaryPushButton(
            tr("mod.mapping.save", default="保存映射"),
            self,
        )
        cancel_button.clicked.connect(self.reject)
        save_button.clicked.connect(self.accept_if_valid)
        buttons.addWidget(cancel_button)
        buttons.addWidget(save_button)
        layout.addLayout(buttons)
        self.populate()

    @staticmethod
    def image_size_text(path: Path) -> str:
        try:
            with Image.open(path) as image:
                return f"{image.width} × {image.height}"
        except Exception:
            return "—"

    def populate(self):
        self.table.setRowCount(len(self.source_textures))
        for row, source in enumerate(self.source_textures):
            source_item = QTableWidgetItem(source.name)
            source_item.setData(Qt.UserRole, str(source))
            source_item.setToolTip(str(source))
            self.table.setItem(row, 0, source_item)
            self.table.setItem(row, 1, QTableWidgetItem(self.image_size_text(source)))

            combo = ComboBox(self.table)
            combo.addItem(tr("mod.mapping.unmapped", default="不映射"), userData="-1")
            for target_index, target in enumerate(self.main_textures):
                combo.addItem(
                    f"{target_index + 1}. {target.name}  "
                    f"({self.image_size_text(target)})",
                    userData=str(target_index),
                )
            selected = self.current.get(str(source), -1)
            for combo_index in range(combo.count()):
                if str(combo.itemData(combo_index)) == str(selected):
                    combo.setCurrentIndex(combo_index)
                    break
            self.table.setCellWidget(row, 2, combo)

    def mappings(self) -> list[tuple[int, Path]]:
        result: list[tuple[int, Path]] = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            combo = self.table.cellWidget(row, 2)
            if not item or not isinstance(combo, ComboBox):
                continue
            value = str(combo.currentData() or "-1")
            if value.isdigit():
                result.append((int(value), Path(str(item.data(Qt.UserRole)))))
        return result

    def accept_if_valid(self):
        mappings = self.mappings()
        if not mappings:
            self.status_label.setText(
                tr("mod.mapping.need_one", default="至少保留一条贴图映射。")
            )
            return
        targets = [target for target, _source in mappings]
        if len(targets) != len(set(targets)):
            self.status_label.setText(
                tr(
                    "mod.mapping.duplicate",
                    default="同一张主模型贴图不能被重复映射。",
                )
            )
            return
        self.accept()


class TextureGalleryDialog(QDialog):
    def __init__(self, title: str, textures: list[Path], parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(860, 620)
        self.textures = [path.resolve() for path in textures if path.is_file()]
        self.current_index = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
        header = QHBoxLayout()
        self.title_label = SubtitleLabel(title, self)
        self.texture_combo = ComboBox(self)
        for index, path in enumerate(self.textures):
            self.texture_combo.addItem(
                f"{index + 1}. {path.name}",
                userData=str(index),
            )
        self.texture_combo.currentIndexChanged.connect(self.on_combo_changed)
        header.addWidget(self.title_label)
        header.addStretch(1)
        header.addWidget(self.texture_combo)
        layout.addLayout(header)

        self.image_label = QLabel(self)
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setMinimumHeight(460)
        self.image_label.setStyleSheet(
            "QLabel { background: rgba(127,127,127,0.08); "
            "border: 1px solid rgba(127,127,127,0.25); border-radius: 10px; }"
        )
        layout.addWidget(self.image_label, 1)
        self.detail_label = CaptionLabel("", self)
        self.detail_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.detail_label)

        buttons = QHBoxLayout()
        previous_button = PushButton(
            tr("mod.gallery.previous", default="上一张"),
            self,
        )
        next_button = PushButton(tr("mod.gallery.next", default="下一张"), self)
        external_button = PushButton(
            tr("mod.gallery.external", default="用外部程序打开"),
            self,
        )
        close_button = PrimaryPushButton(
            tr("common.close", default="关闭"),
            self,
        )
        previous_button.clicked.connect(self.previous_texture)
        next_button.clicked.connect(self.next_texture)
        external_button.clicked.connect(self.open_external)
        close_button.clicked.connect(self.accept)
        buttons.addWidget(previous_button)
        buttons.addWidget(next_button)
        buttons.addStretch(1)
        buttons.addWidget(external_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        self.refresh_image()

    def on_combo_changed(self, index: int):
        if 0 <= index < len(self.textures):
            self.current_index = index
            self.refresh_image()

    def previous_texture(self):
        if self.textures:
            self.current_index = (self.current_index - 1) % len(self.textures)
            self.texture_combo.setCurrentIndex(self.current_index)

    def next_texture(self):
        if self.textures:
            self.current_index = (self.current_index + 1) % len(self.textures)
            self.texture_combo.setCurrentIndex(self.current_index)

    def open_external(self):
        if self.textures:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(self.textures[self.current_index]))
            )

    def refresh_image(self):
        if not self.textures:
            self.image_label.setText(
                tr("mod.models.no_texture", default="没有可查看的贴图")
            )
            self.detail_label.clear()
            return
        path = self.textures[self.current_index]
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self.image_label.setPixmap(QPixmap())
            self.image_label.setText(path.name)
            return
        self.image_label.setText("")
        self.image_label.setPixmap(
            pixmap.scaled(
                max(1, self.image_label.width() - 16),
                max(1, self.image_label.height() - 16),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )
        self.detail_label.setText(
            tr(
                "mod.gallery.detail",
                default="{index}/{count} · {width} × {height} · {path}",
                index=self.current_index + 1,
                count=len(self.textures),
                width=pixmap.width(),
                height=pixmap.height(),
                path=str(path),
            )
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh_image()


class ModelCard(CardWidget):
    moveRequested = Signal(str, int)
    mappingRequested = Signal(str)
    removeRequested = Signal(str)
    skinNameChanged = Signal(str, str)
    texturesRequested = Signal(str)
    previewRequested = Signal(str)
    folderRequested = Signal(str)

    def __init__(
        self,
        model: dict[str, Any],
        index: int,
        total: int,
        textures: list[Path],
        next_can_be_main: bool,
        parent=None,
    ):
        super().__init__(parent)
        self.model_id = str(model.get("id") or "")
        self.index = index
        self.setObjectName("mainModelCard" if index == 0 else "modelCard")
        self.setMinimumHeight(172)

        root = QHBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(14)

        texture_panel = QWidget(self)
        texture_layout = QHBoxLayout(texture_panel)
        texture_layout.setContentsMargins(0, 0, 0, 0)
        texture_layout.setSpacing(5)
        visible_textures = textures[:3]
        if not visible_textures:
            empty = QLabel(
                tr("mod.models.no_texture", default="无贴图"),
                texture_panel,
            )
            empty.setFixedSize(96, 96)
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(
                "QLabel { background: rgba(127,127,127,0.08); "
                "border: 1px solid rgba(127,127,127,0.22); border-radius: 8px; }"
            )
            texture_layout.addWidget(empty)
        for texture in visible_textures:
            label = QLabel(texture_panel)
            label.setFixedSize(84, 96)
            label.setAlignment(Qt.AlignCenter)
            label.setToolTip(str(texture))
            label.setStyleSheet(
                "QLabel { background: rgba(127,127,127,0.08); "
                "border: 1px solid rgba(127,127,127,0.22); border-radius: 8px; }"
            )
            pixmap = QPixmap(str(texture))
            if pixmap.isNull():
                label.setText(texture.name)
            else:
                label.setPixmap(
                    pixmap.scaled(
                        78,
                        90,
                        Qt.KeepAspectRatio,
                        Qt.SmoothTransformation,
                    )
                )
            texture_layout.addWidget(label)
        if len(textures) > 3:
            more = BodyLabel(f"+{len(textures) - 3}", texture_panel)
            more.setAlignment(Qt.AlignCenter)
            more.setFixedWidth(32)
            texture_layout.addWidget(more)
        root.addWidget(texture_panel)

        content = QVBoxLayout()
        content.setSpacing(6)
        header = QHBoxLayout()
        role = (
            tr("mod.models.main_badge", default="主模型")
            if index == 0
            else tr("mod.models.skin_badge", default="皮肤 {index}", index=index)
        )
        role_label = SubtitleLabel(f"#{index + 1}  {role}", self)
        role_label.setMinimumWidth(118)
        name_label = CaptionLabel(
            tr("mod.models.skin_name_label", default="皮肤名称"),
            self,
        )
        self.skin_name_edit = LineEdit(self)
        self.skin_name_edit.setText(str(model.get("skin_name") or ""))
        self.skin_name_edit.setPlaceholderText(
            tr("mod.models.skin_name_placeholder", default="皮肤名称")
        )
        self.skin_name_edit.editingFinished.connect(
            lambda: self.skinNameChanged.emit(
                self.model_id,
                self.skin_name_edit.text().strip(),
            )
        )
        header.addWidget(role_label)
        header.addWidget(name_label)
        header.addWidget(self.skin_name_edit, 1)
        content.addLayout(header)

        source = str(model.get("source_path") or model.get("workspace_path") or "")
        source_label = CaptionLabel(
            tr("mod.models.source_line", default="来源：{source}", source=source),
            self,
        )
        source_label.setWordWrap(True)
        source_label.setMaximumHeight(36)
        source_label.setToolTip(source)
        content.addWidget(source_label)
        mappings = len(
            [
                item
                for item in model.get("texture_mappings") or []
                if isinstance(item, dict)
            ]
        )
        detail = CaptionLabel(
            tr(
                "mod.models.detail",
                default="{textures} 张贴图 · {mappings} 条映射 · ID: {id}",
                textures=len(textures),
                mappings=mappings,
                id=self.model_id,
            ),
            self,
        )
        content.addWidget(detail)

        actions = QHBoxLayout()
        actions.setSpacing(5)
        view_button = PushButton(
            tr("mod.models.view_short", default="贴图"),
            self,
        )
        preview_button = PushButton(
            tr("mod.models.preview_short", default="预览"),
            self,
        )
        mapping_button = PushButton(
            tr("mod.models.mapping_short", default="映射"),
            self,
        )
        move_up = PushButton(tr("mod.models.move_up", default="上移"), self)
        move_down = PushButton(tr("mod.models.move_down", default="下移"), self)
        folder_button = PushButton(
            tr("mod.models.open_folder", default="目录"),
            self,
        )
        remove_button = PushButton(
            tr("mod.models.remove", default="删除"),
            self,
        )
        has_model = bool(str(model.get("model_json") or ""))
        view_button.setEnabled(bool(textures))
        preview_button.setEnabled(has_model)
        mapping_button.setEnabled(index > 0 and bool(textures))
        move_up.setEnabled(index > 1 or (index == 1 and has_model))
        move_down.setEnabled(
            index < total - 1 and (index > 0 or next_can_be_main)
        )
        view_button.clicked.connect(
            lambda: self.texturesRequested.emit(self.model_id)
        )
        preview_button.clicked.connect(
            lambda: self.previewRequested.emit(self.model_id)
        )
        mapping_button.clicked.connect(
            lambda: self.mappingRequested.emit(self.model_id)
        )
        move_up.clicked.connect(
            lambda: self.moveRequested.emit(self.model_id, self.index - 1)
        )
        move_down.clicked.connect(
            lambda: self.moveRequested.emit(self.model_id, self.index + 1)
        )
        folder_button.clicked.connect(
            lambda: self.folderRequested.emit(self.model_id)
        )
        remove_button.clicked.connect(
            lambda: self.removeRequested.emit(self.model_id)
        )
        view_button.setToolTip(tr("mod.models.view_textures", default="查看贴图"))
        preview_button.setToolTip(tr("mod.models.preview", default="预览模型"))
        mapping_button.setToolTip(
            tr("mod.models.edit_mapping", default="编辑贴图映射")
        )
        folder_button.setToolTip(
            tr("mod.models.open_folder", default="打开模型目录")
        )
        for button in (
            view_button,
            preview_button,
            mapping_button,
            move_up,
            move_down,
            folder_button,
            remove_button,
        ):
            button.setMinimumWidth(50)
            button.setMaximumWidth(82)
            button.setMinimumHeight(30)
            actions.addWidget(button)
        actions.addStretch(1)
        content.addLayout(actions)
        root.addLayout(content, 1)


class Live2DModPage(QFrame):
    previewModelRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("live2dModPage")
        self.setAcceptDrops(True)
        self.i18n = get_i18n()
        self.settings_manager = SettingsManager()
        self.current_project: Live2DViewerModProject | None = None
        self.selected_source = ""
        self.worker: ModTaskThread | None = None
        self.worker_kind = ""
        self.pending_sources: list[str] = []
        self._project_combo_refreshing = False
        self._artmesh_refreshing = False
        self._loading_ui = False
        self._dirty = False
        self.setup_ui()
        self.retranslate_ui()
        self.i18n.languageChanged.connect(self.retranslate_ui)
        self.refresh_project_combo()
        self.load_last_project(silent=True)

    def setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)
        self.title_label = SubtitleLabel("", self)
        root.addWidget(self.title_label)

        self.splitter = QSplitter(Qt.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        root.addWidget(self.splitter, 1)

        left = QWidget(self.splitter)
        left.setMinimumWidth(350)
        self.left_layout = QVBoxLayout(left)
        self.left_layout.setContentsMargins(0, 0, 8, 0)
        self.left_layout.setSpacing(10)

        self.project_card = CardWidget(left)
        project_layout = QVBoxLayout(self.project_card)
        project_layout.setContentsMargins(14, 14, 14, 14)
        project_layout.setSpacing(8)
        self.project_title = SubtitleLabel("", self.project_card)
        self.project_name_edit = LineEdit(self.project_card)
        self.project_name_edit.textChanged.connect(self.mark_dirty)
        self.project_combo = EditableComboBox(self.project_card)
        self.project_combo.currentIndexChanged.connect(self.on_project_combo_changed)
        project_layout.addWidget(self.project_title)
        project_layout.addWidget(self.project_name_edit)
        project_layout.addWidget(self.project_combo)
        project_buttons = QGridLayout()
        project_buttons.setHorizontalSpacing(8)
        project_buttons.setVerticalSpacing(8)
        self.new_project_button = PushButton("", self.project_card)
        self.open_project_button = PushButton("", self.project_card)
        self.rename_project_button = PushButton("", self.project_card)
        self.delete_project_button = PushButton("", self.project_card)
        self.save_project_button = PrimaryPushButton("", self.project_card)
        self.open_folder_button = PushButton("", self.project_card)
        self.new_project_button.clicked.connect(self.create_new_project)
        self.open_project_button.clicked.connect(self.open_project_file)
        self.rename_project_button.clicked.connect(self.rename_current_project)
        self.delete_project_button.clicked.connect(self.delete_current_project)
        self.save_project_button.clicked.connect(self.save_current_project)
        self.open_folder_button.clicked.connect(self.open_current_project_folder)
        project_buttons.addWidget(self.new_project_button, 0, 0)
        project_buttons.addWidget(self.open_project_button, 0, 1)
        project_buttons.addWidget(self.rename_project_button, 1, 0)
        project_buttons.addWidget(self.save_project_button, 1, 1)
        project_buttons.addWidget(self.delete_project_button, 2, 0)
        project_buttons.addWidget(self.open_folder_button, 2, 1)
        project_layout.addLayout(project_buttons)
        self.project_status = CaptionLabel("", self.project_card)
        self.project_status.setWordWrap(True)
        project_layout.addWidget(self.project_status)
        self.left_layout.addWidget(self.project_card)

        self.artmesh_card = CardWidget(left)
        artmesh_layout = QVBoxLayout(self.artmesh_card)
        artmesh_layout.setContentsMargins(14, 14, 14, 14)
        artmesh_layout.setSpacing(8)
        self.artmesh_title = SubtitleLabel("", self.artmesh_card)
        self.artmesh_combo = EditableComboBox(self.artmesh_card)
        self.artmesh_combo.currentIndexChanged.connect(self.on_artmesh_combo_changed)
        artmesh_layout.addWidget(self.artmesh_title)
        artmesh_layout.addWidget(self.artmesh_combo)
        self.left_layout.addWidget(self.artmesh_card)

        self.export_card = CardWidget(left)
        export_layout = QVBoxLayout(self.export_card)
        export_layout.setContentsMargins(14, 14, 14, 14)
        export_layout.setSpacing(8)
        self.export_title = SubtitleLabel("", self.export_card)
        self.export_hint = CaptionLabel("", self.export_card)
        self.export_hint.setWordWrap(True)
        self.export_button = PrimaryPushButton("", self.export_card)
        self.export_folder_button = PushButton("", self.export_card)
        self.export_button.clicked.connect(self.start_export)
        self.export_folder_button.clicked.connect(self.open_export_folder)
        export_layout.addWidget(self.export_title)
        export_layout.addWidget(self.export_hint)
        export_actions = QHBoxLayout()
        export_actions.setSpacing(8)
        export_actions.addWidget(self.export_button, 1)
        export_actions.addWidget(self.export_folder_button, 1)
        export_layout.addLayout(export_actions)
        self.left_layout.addWidget(self.export_card)
        self.left_layout.addStretch(1)

        right = QWidget(self.splitter)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.setSpacing(10)

        self.import_card = CardWidget(right)
        import_layout = QVBoxLayout(self.import_card)
        import_layout.setContentsMargins(16, 14, 16, 14)
        import_layout.setSpacing(8)
        self.import_title = SubtitleLabel("", self.import_card)
        import_layout.addWidget(self.import_title)
        self.drop_frame = QFrame(self.import_card)
        self.drop_frame.setObjectName("modDropFrame")
        drop_layout = QVBoxLayout(self.drop_frame)
        drop_layout.setContentsMargins(14, 10, 14, 10)
        self.drop_main_label = BodyLabel("", self.drop_frame)
        drop_layout.addWidget(self.drop_main_label)
        import_layout.addWidget(self.drop_frame)
        source_row = QHBoxLayout()
        self.source_edit = LineEdit(self.import_card)
        self.source_edit.setReadOnly(True)
        self.browse_file_button = PushButton("", self.import_card)
        self.browse_folder_button = PushButton("", self.import_card)
        self.import_button = PrimaryPushButton("", self.import_card)
        self.browse_file_button.clicked.connect(self.browse_source_file)
        self.browse_folder_button.clicked.connect(self.browse_source_folder)
        self.import_button.clicked.connect(self.import_selected_source)
        source_row.addWidget(self.source_edit, 1)
        source_row.addWidget(self.browse_file_button)
        source_row.addWidget(self.browse_folder_button)
        source_row.addWidget(self.import_button)
        import_layout.addLayout(source_row)
        right_layout.addWidget(self.import_card)

        separator = QFrame(right)
        separator.setFrameShape(QFrame.HLine)
        separator.setFrameShadow(QFrame.Sunken)
        right_layout.addWidget(separator)

        model_header = QHBoxLayout()
        self.models_title = SubtitleLabel("", right)
        self.models_count = CaptionLabel("", right)
        self.add_file_button = PushButton("", right)
        self.add_folder_button = PushButton("", right)
        self.add_file_button.clicked.connect(self.browse_and_add_file)
        self.add_folder_button.clicked.connect(self.browse_and_add_folder)
        model_header.addWidget(self.models_title)
        model_header.addWidget(self.models_count)
        model_header.addStretch(1)
        model_header.addWidget(self.add_file_button)
        model_header.addWidget(self.add_folder_button)
        right_layout.addLayout(model_header)
        self.models_hint = CaptionLabel("", right)
        self.models_hint.setWordWrap(True)
        right_layout.addWidget(self.models_hint)

        self.models_scroll = QScrollArea(right)
        self.models_scroll.setWidgetResizable(True)
        self.models_scroll.setFrameShape(QFrame.NoFrame)
        self.models_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.models_container = QWidget(self.models_scroll)
        self.models_layout = QVBoxLayout(self.models_container)
        self.models_layout.setContentsMargins(0, 0, 5, 0)
        self.models_layout.setSpacing(8)
        self.models_scroll.setWidget(self.models_container)
        right_layout.addWidget(self.models_scroll, 1)

        self.splitter.addWidget(left)
        self.splitter.addWidget(right)
        self.splitter.setSizes([370, 1070])
        self._apply_styles()

    def retranslate_ui(self):
        self.project_title.setText(tr("mod.project.title"))
        self.project_name_edit.setPlaceholderText(tr("mod.project.name_placeholder"))
        self.project_combo.setPlaceholderText(
            tr("mod.project.search_placeholder", default="搜索或选择 MOD 工程…")
        )
        self.new_project_button.setText(tr("mod.project.new"))
        self.open_project_button.setText(tr("mod.project.open"))
        self.rename_project_button.setText(
            tr("mod.project.rename", default="重命名")
        )
        self.delete_project_button.setText(
            tr("mod.project.delete", default="删除工程")
        )
        self.save_project_button.setText(tr("mod.project.save"))
        self.open_folder_button.setText(tr("mod.project.open_folder"))
        self.artmesh_title.setText(
            tr("mod.artmesh.title", default="3 · 换装触发 ArtMesh")
        )
        self.artmesh_combo.setPlaceholderText(
            tr(
                "mod.artmesh.unused_placeholder",
                default="搜索并选择未绑定事件的 ArtMesh…",
            )
        )
        self.export_title.setText(
            tr("mod.v2.export_title", default="4 · 最终导出")
        )
        self.export_hint.setText(
            tr(
                "mod.v2.export_hint",
                default="重新生成所有皮肤模型、ArtMesh 触发参数和 JSON 映射表，再打包为 ZIP。",
            )
        )
        self.export_button.setText(
            tr("mod.v3.export_short", default="导出 ZIP")
        )
        self.export_folder_button.setText(
            tr("mod.v3.export_folder_short", default="导出目录")
        )
        self.import_title.setText(
            tr("mod.v3.import_title", default="1 · 导入模型")
        )
        self.drop_main_label.setText(
            tr(
                "mod.v2.drop_main",
                default="拖入任意 Live2D 相关文件或文件夹",
            )
        )
        self.source_edit.setPlaceholderText(
            tr(
                "mod.v2.source_placeholder",
                default="选择文件或文件夹，也可以直接拖入…",
            )
        )
        self.browse_file_button.setText(tr("mod.source.browse_file"))
        self.browse_folder_button.setText(tr("mod.source.browse_folder"))
        self.models_title.setText(
            tr("mod.v3.models_title", default="2 · 皮肤命名与顺序")
        )
        self.models_hint.setText(
            tr(
                "mod.v3.models_hint",
                default="第一项自然作为主模型；通过上移/下移改变主模型和皮肤顺序，不再使用额外设置按钮。",
            )
        )
        self.add_file_button.setText(tr("mod.v2.add_file", default="添加文件"))
        self.add_folder_button.setText(
            tr("mod.v2.add_folder", default="添加文件夹")
        )
        self.refresh_project_ui()
        self.refresh_artmesh_combo()
        self.refresh_model_cards()
        self.refresh_project_combo()

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls() and any(
            Path(url.toLocalFile()).exists() for url in event.mimeData().urls()
        ):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        paths = [
            str(Path(url.toLocalFile()).resolve())
            for url in event.mimeData().urls()
            if Path(url.toLocalFile()).exists()
        ]
        project_files = [
            path for path in paths if Path(path).name == PROJECT_FILE_NAME
        ]
        if project_files:
            self.load_project_file(project_files[0])
        sources = [path for path in paths if path not in project_files]
        if sources:
            self.queue_source_imports(sources)
        if project_files or sources:
            event.acceptProposedAction()

    def browse_source_file(self) -> str:
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("mod.v2.select_any_file", default="选择任意 Live2D 相关文件"),
            "",
            tr(
                "mod.v2.any_file_filter",
                default="Live2D/资源文件 (*.json *.moc *.moc3 *.png *.jpg *.jpeg *.lpk *.wpk *.assets *.sharedassets *.bundle *.unity3d);;所有文件 (*.*)",
            ),
        )
        if path:
            self.set_source(path)
        return path

    def browse_source_folder(self) -> str:
        path = QFileDialog.getExistingDirectory(
            self,
            tr("mod.v2.select_folder", default="选择包含 Live2D 的文件夹"),
        )
        if path:
            self.set_source(path)
        return path

    def set_source(self, path: str):
        self.selected_source = os.path.abspath(path)
        self.source_edit.setText(self.selected_source)
        if not self.current_project and not self.project_name_edit.text().strip():
            self._loading_ui = True
            self.project_name_edit.setText(self.suggest_project_name(path))
            self._loading_ui = False
        self.refresh_project_ui()

    def import_selected_source(self):
        source = self.source_edit.text().strip()
        if not source:
            self.show_warning(tr("mod.warning.no_source"))
            return
        self.queue_source_imports([source])

    def queue_source_imports(self, sources: list[str]):
        normalized = [
            str(Path(source).resolve())
            for source in sources
            if Path(source).exists()
        ]
        if not normalized:
            return
        if self.worker:
            self.pending_sources.extend(normalized)
            self.append_log(
                tr(
                    "mod.v2.queued",
                    default="已加入导入队列：{count} 项",
                    count=len(normalized),
                )
            )
            return
        if not self.current_project:
            first, *remaining = normalized
            self.pending_sources.extend(remaining)
            self.set_source(first)
            self.start_create_project()
            return
        if not self.flush_pending_changes():
            return
        self.pending_sources.extend(normalized)
        self.start_next_queued_import()

    def start_create_project(self, project_name: str = ""):
        source = self.source_edit.text().strip()
        if not source or not Path(source).exists():
            self.show_warning(tr("mod.warning.no_source"))
            return
        self.start_worker(
            "create",
            source=source,
            project_name=(
                project_name.strip()
                or self.project_name_edit.text().strip()
                or self.suggest_project_name(source)
            ),
            output_root=self.settings_manager.get_output_dir("live2dviewer_mod"),
            temp_root=self.settings_manager.get_temp_dir(),
        )

    def create_new_project(self):
        source = self.source_edit.text().strip()
        if not source or not Path(source).exists():
            self.show_warning(tr("mod.warning.no_source"))
            return
        if not self.confirm_discard_or_save():
            return
        suggested = self.suggest_project_name(source)
        name, accepted = QInputDialog.getText(
            self,
            tr("mod.project.new_title", default="新建 MOD 工程"),
            tr("mod.project.new_name", default="工程名称"),
            text=suggested,
        )
        if not accepted or not name.strip():
            return
        self.start_create_project(sanitize_project_name(name))

    def start_next_queued_import(self):
        if self.worker or not self.current_project or not self.pending_sources:
            return
        source = self.pending_sources.pop(0)
        self.start_worker(
            "add",
            project=self.current_project,
            source=source,
            temp_root=self.settings_manager.get_temp_dir(),
        )

    def start_worker(self, action: str, **kwargs: Any):
        self.worker_kind = action
        self.worker = ModTaskThread(action, **kwargs)
        self.worker.progressUpdated.connect(self.append_log)
        self.worker.taskReady.connect(self.on_worker_ready)
        self.worker.taskError.connect(self.on_worker_error)
        self.worker.finished.connect(self.on_worker_finished)
        self.set_busy(True)
        self.worker.start()

    def on_worker_ready(self, result: object):
        if self.worker_kind == "export":
            project, archive_path = result
            self.set_current_project(project)
            message = tr(
                "mod.v2.exported",
                default="最终包已导出：{path}",
                path=str(archive_path),
            )
            self.append_log(message)
            InfoBar.success(
                title=tr("common.success"),
                content=message,
                parent=self,
                position=InfoBarPosition.TOP,
                duration=4500,
            )
            return
        if isinstance(result, Live2DViewerModProject):
            self.set_current_project(result)
            self.append_log(
                tr(
                    "mod.project.created",
                    path=str(result.project_file),
                )
                if self.worker_kind == "create"
                else tr("mod.v2.model_added", default="模型已加入工程。")
            )

    def on_worker_error(self, error: str):
        self.append_log(
            tr("mod.v2.task_failed", default="操作失败：{error}", error=error)
        )
        InfoBar.error(
            title=tr("common.error"),
            content=error,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=6500,
        )
        if self.worker_kind == "create":
            self.pending_sources.clear()

    def on_worker_finished(self):
        self.worker = None
        self.worker_kind = ""
        self.set_busy(False)
        QTimer.singleShot(0, self.start_next_queued_import)

    def browse_and_add_file(self):
        if not self.current_project:
            return
        path = self.browse_source_file()
        if path:
            self.queue_source_imports([path])

    def browse_and_add_folder(self):
        if not self.current_project:
            return
        path = self.browse_source_folder()
        if path:
            self.queue_source_imports([path])

    def refresh_model_cards(self):
        if not hasattr(self, "models_layout"):
            return
        while self.models_layout.count():
            item = self.models_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        models = self.current_project.models if self.current_project else []
        self.models_count.setText(
            tr(
                "mod.v3.model_count",
                default="共 {count} 项",
                count=len(models),
            )
        )
        if not models:
            empty = CaptionLabel(
                tr(
                    "mod.v2.models_empty",
                    default="还没有模型。拖入任意 Live2D 相关文件即可开始。",
                ),
                self.models_container,
            )
            empty.setAlignment(Qt.AlignCenter)
            empty.setMinimumHeight(180)
            self.models_layout.addWidget(empty)
        for index, model in enumerate(models):
            textures = model_texture_paths(
                self.current_project,
                str(model.get("id") or ""),
            )
            next_can_be_main = bool(
                index + 1 < len(models)
                and str(models[index + 1].get("model_json") or "")
            )
            card = ModelCard(
                model,
                index,
                len(models),
                textures,
                next_can_be_main,
                self.models_container,
            )
            card.moveRequested.connect(self.move_model)
            card.mappingRequested.connect(self.edit_model_mapping)
            card.removeRequested.connect(self.remove_model)
            card.skinNameChanged.connect(self.rename_skin)
            card.texturesRequested.connect(self.view_model_textures)
            card.previewRequested.connect(self.preview_model)
            card.folderRequested.connect(self.open_model_folder)
            self.models_layout.addWidget(card)
        self.models_layout.addStretch(1)

    def model_by_id(self, model_id: str) -> dict[str, Any] | None:
        if not self.current_project:
            return None
        return next(
            (
                model
                for model in self.current_project.models
                if str(model.get("id") or "") == str(model_id)
            ),
            None,
        )

    def move_model(self, model_id: str, target_index: int):
        if not self.current_project or not self.flush_pending_changes():
            return
        try:
            self.set_current_project(
                move_model_in_project(
                    self.current_project,
                    model_id,
                    target_index,
                )
            )
            self.append_log(
                tr(
                    "mod.v2.order_updated",
                    default="模型顺序、主模型和贴图映射已更新。",
                )
            )
        except Exception as exc:
            self.show_error(str(exc))

    def rename_skin(self, model_id: str, skin_name: str):
        model = self.model_by_id(model_id)
        if not model:
            return
        cleaned = sanitize_skin_name(
            skin_name,
            "原皮" if self.current_project.models[0] is model else "皮肤",
        )
        if str(model.get("skin_name") or "") == cleaned:
            return
        model["skin_name"] = cleaned
        self.mark_dirty()

    def edit_model_mapping(self, model_id: str):
        if not self.current_project or not self.current_project.models:
            return
        model = self.model_by_id(model_id)
        if not model or str(model.get("id")) == str(
            self.current_project.models[0].get("id")
        ):
            return
        if not self.flush_pending_changes():
            return
        main = self.current_project.models[0]
        main_textures = model_texture_paths(
            self.current_project,
            str(main.get("id") or ""),
        )
        source_textures = model_texture_paths(self.current_project, model_id)
        current: list[tuple[int, Path]] = []
        for mapping in model.get("texture_mappings") or []:
            if not isinstance(mapping, dict):
                continue
            try:
                target_index = int(mapping.get("target_index"))
            except (TypeError, ValueError):
                continue
            source = self.resolve_project_path(
                str(mapping.get("source_texture") or "")
            )
            if source.is_file():
                current.append((target_index, source))
        dialog = TextureMappingDialog(
            str(model.get("skin_name") or model_id),
            main_textures,
            source_textures,
            current,
            self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            self.set_current_project(
                update_model_mappings(
                    self.current_project,
                    model_id,
                    dialog.mappings(),
                )
            )
            self.append_log(
                tr(
                    "mod.v2.mapping_saved",
                    default="多贴图映射已保存：{name}",
                    name=str(model.get("skin_name") or model_id),
                )
            )
        except Exception as exc:
            self.show_error(str(exc))

    def view_model_textures(self, model_id: str):
        model = self.model_by_id(model_id)
        if not model or not self.current_project:
            return
        textures = model_texture_paths(self.current_project, model_id)
        dialog = TextureGalleryDialog(
            tr(
                "mod.gallery.title",
                default="贴图查看 · {name}",
                name=str(model.get("skin_name") or model_id),
            ),
            textures,
            self,
        )
        dialog.exec()

    def preview_model(self, model_id: str):
        model = self.model_by_id(model_id)
        if not model or not self.current_project:
            return
        model_json = self.resolve_project_path(str(model.get("model_json") or ""))
        if not model_json.is_file():
            self.show_warning(
                tr(
                    "mod.v3.no_model_preview",
                    default="这个来源只有贴图，不能作为完整 Live2D 模型预览。",
                )
            )
            return
        self.previewModelRequested.emit(str(model_json))

    def open_model_folder(self, model_id: str):
        model = self.model_by_id(model_id)
        if not model:
            return
        workspace = self.resolve_project_path(
            str(model.get("workspace_path") or "")
        )
        if workspace.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(workspace)))

    def remove_model(self, model_id: str):
        model = self.model_by_id(model_id)
        if not model or not self.current_project:
            return
        answer = QMessageBox.question(
            self,
            tr("mod.v2.remove_title", default="删除模型"),
            tr(
                "mod.v2.remove_confirm",
                default="将删除“{name}”及其工程内副本。此操作无法撤销，是否继续？",
                name=str(model.get("skin_name") or model_id),
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes or not self.flush_pending_changes():
            return
        try:
            self.set_current_project(
                remove_model_from_project(self.current_project, model_id)
            )
            self.append_log(
                tr(
                    "mod.v2.model_removed",
                    default="已删除模型：{name}",
                    name=str(model.get("skin_name") or model_id),
                )
            )
        except Exception as exc:
            self.show_error(str(exc))

    def refresh_artmesh_combo(self, *_args):
        if not hasattr(self, "artmesh_combo"):
            return
        selected = (
            str(self.current_project.data.get("selected_artmesh_id") or "")
            if self.current_project
            else ""
        )
        areas = (
            [
                item
                for item in self.current_project.data.get("artmesh_areas") or []
                if isinstance(item, dict)
                and not str(item.get("motion") or "").strip()
            ]
            if self.current_project
            else []
        )
        self._artmesh_refreshing = True
        self.artmesh_combo.blockSignals(True)
        self.artmesh_combo.clear()
        self.artmesh_combo.addItem(
            tr("mod.hitarea.none", default="未选择"),
            userData="",
        )
        selected_visible = False
        for area in areas:
            area_id = str(area.get("id") or "").strip()
            name = str(area.get("name") or area_id).strip()
            if not area_id:
                continue
            self.artmesh_combo.addItem(
                f"{name}  ·  {area_id}",
                userData=area_id,
            )
        for index in range(self.artmesh_combo.count()):
            if str(self.artmesh_combo.itemData(index) or "") == selected:
                self.artmesh_combo.setCurrentIndex(index)
                break
        self.artmesh_combo.blockSignals(False)
        self._artmesh_refreshing = False

    def on_artmesh_combo_changed(self, *_args):
        if self._artmesh_refreshing or not self.current_project:
            return
        selected = str(self.artmesh_combo.currentData() or "")
        eligible_ids = {
            str(item.get("id") or "")
            for item in self.current_project.data.get("artmesh_areas") or []
            if isinstance(item, dict)
            and not str(item.get("motion") or "").strip()
        }
        if selected and selected not in eligible_ids:
            return
        self.current_project.data["selected_artmesh_id"] = selected
        self.mark_dirty()

    def start_export(self):
        if not self.current_project or not self.current_project.models:
            self.show_warning(tr("mod.warning.no_project"))
            return
        selected_artmesh = str(
            self.current_project.data.get("selected_artmesh_id") or ""
        ).strip()
        eligible_artmeshes = {
            str(item.get("id") or "")
            for item in self.current_project.data.get("artmesh_areas") or []
            if isinstance(item, dict)
            and not str(item.get("motion") or "").strip()
        }
        if len(self.current_project.models) > 1 and (
            not selected_artmesh or selected_artmesh not in eligible_artmeshes
        ):
            self.show_warning(
                tr(
                    "mod.artmesh.required",
                    default="导出多个皮肤前，请先选择换装触发 ArtMesh 区域。",
                )
            )
            return
        if not self.flush_pending_changes():
            return
        default_dir = self.current_project.project_dir / EXPORTS_DIR
        default_name = (
            f"{sanitize_identifier(self.current_project.project_name, 'live2dviewer_mod')}.zip"
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("mod.v2.export_dialog", default="导出 Live2DViewerEX MOD 包"),
            str(default_dir / default_name),
            tr("mod.v2.export_filter", default="ZIP 压缩包 (*.zip)"),
        )
        if path:
            self.start_worker(
                "export",
                project=self.current_project,
                destination=path,
            )

    def open_export_folder(self):
        if not self.current_project:
            return
        last_export = str(
            self.current_project.data.get("last_export_path") or ""
        )
        directory = (
            Path(last_export).resolve().parent
            if last_export
            else self.current_project.project_dir / EXPORTS_DIR
        )
        directory.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def open_project_file(self):
        if not self.confirm_discard_or_save():
            return
        last = str(
            self.settings_manager.get(
                "live2dviewer_mod.last_project_file",
                "",
            )
            or ""
        )
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("mod.dialog.open_project"),
            str(Path(last).parent) if last else "",
            tr("mod.dialog.filter_project_files"),
        )
        if path:
            self.load_project_file(path)

    def load_last_project(self, silent: bool = False):
        path = str(
            self.settings_manager.get(
                "live2dviewer_mod.last_project_file",
                "",
            )
            or ""
        )
        if path and Path(path).is_file():
            self.load_project_file(path, silent=silent)

    def load_project_file(self, path: str, silent: bool = False):
        try:
            self.set_current_project(load_project(path))
            if not silent:
                self.append_log(tr("mod.project.opened", path=path))
        except Exception as exc:
            if not silent:
                self.show_error(str(exc))

    def set_current_project(self, project: Live2DViewerModProject):
        self.current_project = project
        self._loading_ui = True
        self.project_name_edit.setText(project.project_name)
        self._loading_ui = False
        self._dirty = False
        self.remember_project_file(project)
        self.refresh_project_combo(str(project.project_file))
        self.refresh_artmesh_combo()
        self.refresh_model_cards()
        self.refresh_project_ui()

    def save_current_project(self, silent: bool = False) -> bool:
        if not self.current_project:
            return False
        try:
            data = dict(self.current_project.data)
            data["project_name"] = sanitize_project_name(
                self.project_name_edit.text()
                or self.current_project.project_name
            )
            project_file = save_project(self.current_project.project_dir, data)
            self.set_current_project(load_project(project_file))
            if not silent:
                self.append_log(tr("mod.project.saved", path=str(project_file)))
            return True
        except Exception as exc:
            self.show_error(str(exc))
            return False

    def rename_current_project(self):
        if not self.current_project or not self.flush_pending_changes():
            return
        name, accepted = QInputDialog.getText(
            self,
            tr("mod.project.rename_title", default="重命名 MOD 工程"),
            tr("mod.project.new_name", default="工程名称"),
            text=self.current_project.project_name,
        )
        if not accepted or not name.strip():
            return
        old_path = str(self.current_project.project_file.resolve())
        try:
            renamed = rename_project(
                self.current_project,
                sanitize_project_name(name),
            )
            self.forget_project_file(old_path, hide_from_discovery=False)
            self.set_current_project(renamed)
        except Exception as exc:
            self.show_error(str(exc))

    def delete_current_project(self):
        if not self.current_project or not self.confirm_discard_or_save():
            return
        project = self.current_project
        dialog = DeleteProjectDialog(
            project.project_name,
            project.project_dir,
            self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            if dialog.delete_files:
                delete_project_directory(project)
            self.forget_project_file(
                str(project.project_file.resolve()),
                hide_from_discovery=not dialog.delete_files,
            )
            self.clear_current_project()
        except Exception as exc:
            self.show_error(str(exc))

    def flush_pending_changes(self) -> bool:
        if not self.current_project:
            return False
        return self.save_current_project(silent=True) if self._dirty else True

    def confirm_discard_or_save(self) -> bool:
        if not self._dirty:
            return True
        box = QMessageBox(self)
        box.setWindowTitle(
            tr("mod.v3.unsaved_title", default="工程尚未保存")
        )
        box.setText(
            tr(
                "mod.v3.unsaved_message",
                default="皮肤名称、工程名称或 ArtMesh 触发区有未保存修改。",
            )
        )
        box.setStandardButtons(
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel
        )
        result = box.exec()
        if result == QMessageBox.Save:
            return self.save_current_project(silent=True)
        return result == QMessageBox.Discard

    def open_current_project_folder(self):
        if self.current_project:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(self.current_project.project_dir.resolve()))
            )

    def clear_current_project(self):
        self.current_project = None
        self._dirty = False
        self._loading_ui = True
        self.project_name_edit.clear()
        self._loading_ui = False
        self.refresh_project_combo()
        self.refresh_artmesh_combo()
        self.refresh_model_cards()
        self.refresh_project_ui()

    def remember_project_file(self, project: Live2DViewerModProject):
        path = str(project.project_file.resolve())
        recent = [
            item
            for item in self.recent_project_files()
            if str(Path(item).resolve()) != path
        ]
        recent.insert(0, path)
        ignored = [item for item in self.ignored_project_files() if item != path]
        self.settings_manager.set("live2dviewer_mod.last_project_file", path)
        self.settings_manager.set("live2dviewer_mod.project_files", recent[:30])
        self.settings_manager.set(
            "live2dviewer_mod.ignored_project_files",
            ignored,
        )

    def forget_project_file(self, path: str, hide_from_discovery: bool):
        resolved = str(Path(path).resolve())
        raw = self.settings_manager.get("live2dviewer_mod.project_files", [])
        if isinstance(raw, str):
            raw = [raw]
        recent = [
            str(Path(str(item)).resolve())
            for item in (raw if isinstance(raw, list) else [])
            if str(Path(str(item)).resolve()) != resolved
        ]
        ignored = self.ignored_project_files()
        if hide_from_discovery and resolved not in ignored:
            ignored.append(resolved)
        else:
            ignored = [item for item in ignored if item != resolved]
        self.settings_manager.set("live2dviewer_mod.project_files", recent)
        self.settings_manager.set(
            "live2dviewer_mod.ignored_project_files",
            ignored,
        )
        last = str(
            self.settings_manager.get("live2dviewer_mod.last_project_file", "")
            or ""
        )
        if last and str(Path(last).resolve()) == resolved:
            self.settings_manager.set("live2dviewer_mod.last_project_file", "")

    def ignored_project_files(self) -> list[str]:
        raw = self.settings_manager.get(
            "live2dviewer_mod.ignored_project_files",
            [],
        )
        if isinstance(raw, str):
            raw = [raw]
        return list(
            dict.fromkeys(
                str(Path(str(item)).resolve())
                for item in (raw if isinstance(raw, list) else [])
            )
        )

    def recent_project_files(self) -> list[str]:
        raw = self.settings_manager.get("live2dviewer_mod.project_files", [])
        if isinstance(raw, str):
            raw = [raw]
        result: list[str] = []
        seen: set[str] = set()
        for value in raw if isinstance(raw, list) else []:
            path = str(Path(str(value)).resolve())
            if path not in seen and Path(path).is_file():
                seen.add(path)
                result.append(path)
        return result

    def discover_project_files(self) -> list[str]:
        result = self.recent_project_files()
        seen = set(result)
        ignored = set(self.ignored_project_files())
        try:
            root = Path(self.settings_manager.get_output_dir("live2dviewer_mod"))
            for path in sorted(
                root.rglob(PROJECT_FILE_NAME),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            ):
                value = str(path.resolve())
                if value not in seen and value not in ignored:
                    seen.add(value)
                    result.append(value)
        except Exception:
            pass
        return result

    def refresh_project_combo(self, selected: str = ""):
        if not hasattr(self, "project_combo"):
            return
        if not selected and self.current_project:
            selected = str(self.current_project.project_file.resolve())
        self._project_combo_refreshing = True
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        paths = self.discover_project_files()
        if not paths:
            self.project_combo.addItem(tr("mod.project.no_projects"), userData="")
        for path in paths:
            self.project_combo.addItem(Path(path).parent.name, userData=path)
        for index in range(self.project_combo.count()):
            if str(self.project_combo.itemData(index) or "") == selected:
                self.project_combo.setCurrentIndex(index)
                break
        self.project_combo.blockSignals(False)
        self._project_combo_refreshing = False

    def on_project_combo_changed(self, *_args):
        if self._project_combo_refreshing:
            return
        path = str(self.project_combo.currentData() or "")
        if not path:
            return
        if (
            self.current_project
            and str(self.current_project.project_file.resolve())
            == str(Path(path).resolve())
        ):
            return
        if not self.confirm_discard_or_save():
            self.refresh_project_combo(
                str(self.current_project.project_file)
                if self.current_project
                else ""
            )
            return
        self.load_project_file(path)

    def mark_dirty(self, *_args):
        if self._loading_ui or not self.current_project:
            return
        self._dirty = True
        self.refresh_title()
        self.save_project_button.setEnabled(True)

    def refresh_title(self):
        name = (
            self.project_name_edit.text().strip()
            or (
                self.current_project.project_name
                if self.current_project
                else tr("mod.v3.untitled", default="未命名")
            )
        )
        suffix = " *" if self._dirty else ""
        self.title_label.setText(
            tr(
                "mod.v3.window_title",
                default="MOD 工程：{name}{suffix}",
                name=name,
                suffix=suffix,
            )
        )

    def refresh_project_ui(self):
        if not hasattr(self, "project_status"):
            return
        has_project = self.current_project is not None
        has_models = bool(self.current_project.models) if self.current_project else False
        busy = self.worker is not None
        self.save_project_button.setEnabled(has_project and not busy)
        self.rename_project_button.setEnabled(has_project and not busy)
        self.delete_project_button.setEnabled(has_project and not busy)
        self.open_folder_button.setEnabled(has_project and not busy)
        self.add_file_button.setEnabled(has_project and not busy)
        self.add_folder_button.setEnabled(has_project and not busy)
        self.export_button.setEnabled(has_project and has_models and not busy)
        self.export_folder_button.setEnabled(has_project and not busy)
        self.artmesh_card.setEnabled(has_project and has_models and not busy)
        self.import_button.setEnabled(bool(self.source_edit.text().strip()) and not busy)
        self.import_button.setText(
            tr("mod.v2.add_model", default="添加到工程")
            if has_project
            else tr("mod.project.new")
        )
        if has_project:
            self.project_status.setText(
                tr(
                    "mod.v2.project_summary",
                    default="工程文件：{path}\n模型：{count} 个",
                    path=str(self.current_project.project_file),
                    count=len(self.current_project.models),
                )
            )
        else:
            self.project_status.setText(tr("mod.project.no_project"))
        self.refresh_title()

    def set_busy(self, busy: bool):
        for widget in (
            self.new_project_button,
            self.open_project_button,
            self.rename_project_button,
            self.delete_project_button,
            self.save_project_button,
            self.open_folder_button,
            self.browse_file_button,
            self.browse_folder_button,
            self.import_button,
            self.add_file_button,
            self.add_folder_button,
            self.export_button,
            self.export_folder_button,
            self.project_combo,
            self.artmesh_card,
            self.models_container,
        ):
            widget.setEnabled(not busy)
        self.refresh_project_ui()

    def resolve_project_path(self, value: str | Path) -> Path:
        path = Path(value)
        if path.is_absolute() or not self.current_project:
            return path.resolve()
        return (self.current_project.project_dir / path).resolve()

    def append_log(self, message: str):
        if message:
            self._last_log_message = str(message)

    def show_warning(self, message: str):
        InfoBar.warning(
            title=tr("common.warning"),
            content=message,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=4000,
        )

    def show_error(self, message: str):
        InfoBar.error(
            title=tr("common.error"),
            content=message,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=6500,
        )

    @staticmethod
    def suggest_project_name(path: str) -> str:
        source = Path(path)
        if source.is_file():
            if source.suffix.lower() in {
                ".json",
                ".moc",
                ".moc3",
                ".png",
                ".jpg",
                ".jpeg",
            }:
                return sanitize_project_name(source.parent.name or source.stem)
            return sanitize_project_name(source.stem)
        return sanitize_project_name(source.name)

    def _apply_styles(self):
        self.setStyleSheet(
            """
            QFrame#modDropFrame {
                border: 1px dashed rgba(91, 141, 239, 0.75);
                border-radius: 10px;
                background: rgba(91, 141, 239, 0.055);
            }
            CardWidget#mainModelCard {
                border: 2px solid rgba(91, 141, 239, 0.9);
            }
            """
        )

    def updateUIScale(self, window_width, window_height):
        scale_factor = max(1.0, window_width / 1200.0)
        height = int(32 * scale_factor)
        for button in self.findChildren(PushButton):
            button.setMinimumHeight(height)
        for edit in self.findChildren(LineEdit):
            edit.setMinimumHeight(height)
