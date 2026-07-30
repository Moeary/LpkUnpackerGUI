from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from PIL import Image
from PySide6.QtCore import (
    QEvent,
    QMimeData,
    QPoint,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QDrag,
    QDragEnterEvent,
    QDropEvent,
    QImageReader,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
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
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    MessageBoxBase,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    SubtitleLabel,
    TransparentToolButton,
)

from app.core.live2dviewer_mod_project import (
    EXPORTS_DIR,
    PROJECT_FILE_NAME,
    Live2DViewerModProject,
    add_model_to_project,
    create_empty_project,
    create_project_from_base_source,
    delete_project_directory,
    export_live2dviewer_mod,
    load_project,
    model_texture_paths,
    move_model_in_project,
    remove_model_from_project,
    rename_project,
    sanitize_project_name,
    sanitize_skin_name,
    save_project,
    update_model_mappings,
)
from app.core.settings_manager import SettingsManager
from app.i18n import get_i18n, tr


MODEL_DRAG_MIME = "application/x-live2d-mod-model"
_TEXTURE_PIXMAP_CACHE: dict[tuple[str, int, int, int], QPixmap] = {}
_TEXTURE_HASH_CACHE: dict[tuple[str, int, int], str] = {}


def _scaled_texture_pixmap(path: Path, width: int, height: int) -> QPixmap:
    try:
        stat = path.stat()
        key = (str(path.resolve()), stat.st_mtime_ns, width, height)
    except OSError:
        return QPixmap()
    cached = _TEXTURE_PIXMAP_CACHE.get(key)
    if cached is not None:
        return cached
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    source_size = reader.size()
    if source_size.isValid():
        reader.setScaledSize(
            source_size.scaled(
                QSize(max(1, width), max(1, height)),
                Qt.KeepAspectRatio,
            )
        )
    pixmap = QPixmap.fromImage(reader.read())
    if len(_TEXTURE_PIXMAP_CACHE) >= 96:
        _TEXTURE_PIXMAP_CACHE.pop(next(iter(_TEXTURE_PIXMAP_CACHE)))
    _TEXTURE_PIXMAP_CACHE[key] = pixmap
    return pixmap


def _texture_sha256(path: Path) -> str:
    stat = path.stat()
    key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    cached = _TEXTURE_HASH_CACHE.get(key)
    if cached:
        return cached
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    result = digest.hexdigest()
    _TEXTURE_HASH_CACHE[key] = result
    return result


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


class ProjectNameDialog(MessageBoxBase):
    def __init__(self, title: str, initial_name: str = "", parent=None):
        super().__init__(parent)
        self.title_label = SubtitleLabel(title, self.widget)
        self.name_label = BodyLabel(
            tr("mod.project.new_name", default="工程名称"),
            self.widget,
        )
        self.name_edit = LineEdit(self.widget)
        self.name_edit.setText(initial_name)
        self.name_edit.setPlaceholderText(
            tr("mod.project.name_placeholder", default="输入 MOD 工程名称…")
        )
        self.name_edit.selectAll()
        self.yesButton.setText(tr("common.confirm", default="确定"))
        self.cancelButton.setText(tr("common.cancel", default="取消"))
        self.viewLayout.addWidget(self.title_label)
        self.viewLayout.addWidget(self.name_label)
        self.viewLayout.addWidget(self.name_edit)
        self.widget.setMinimumWidth(440)

    def validate(self) -> bool:
        return bool(self.name_edit.text().strip())

    @property
    def project_name(self) -> str:
        return sanitize_project_name(self.name_edit.text())


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
        self.detail_label.setWordWrap(True)
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
        available_width = max(1, self.image_label.width() - 16)
        available_height = max(1, self.image_label.height() - 16)
        pixmap = _scaled_texture_pixmap(
            path,
            available_width,
            available_height,
        )
        if pixmap.isNull():
            self.image_label.setPixmap(QPixmap())
            self.image_label.setText(path.name)
            return
        self.image_label.setText("")
        self.image_label.setPixmap(pixmap)
        try:
            with Image.open(path) as image:
                width, height = image.size
                image_format = str(image.format or path.suffix.lstrip(".")).upper()
                image_mode = str(image.mode)
            size_bytes = path.stat().st_size
            digest = _texture_sha256(path)
            exact_matches: list[int] = []
            same_dimensions: list[int] = []
            for index, candidate in enumerate(self.textures):
                if index == self.current_index:
                    continue
                reader = QImageReader(str(candidate))
                candidate_size = reader.size()
                if (
                    candidate_size.isValid()
                    and candidate_size.width() == width
                    and candidate_size.height() == height
                ):
                    same_dimensions.append(index + 1)
                if (
                    candidate.stat().st_size == size_bytes
                    and _texture_sha256(candidate) == digest
                ):
                    exact_matches.append(index + 1)
            duplicate_text = (
                tr(
                    "mod.gallery.exact_duplicates",
                    default="完全相同：第 {indexes} 张",
                    indexes="、".join(map(str, exact_matches)),
                )
                if exact_matches
                else tr(
                    "mod.gallery.no_exact_duplicate",
                    default="未发现内容完全相同的贴图",
                )
            )
            same_size_text = (
                tr(
                    "mod.gallery.same_dimensions",
                    default="同尺寸：第 {indexes} 张",
                    indexes="、".join(map(str, same_dimensions)),
                )
                if same_dimensions
                else tr(
                    "mod.gallery.unique_dimensions",
                    default="没有其他同尺寸贴图",
                )
            )
            detail = tr(
                "mod.gallery.detail_extended",
                default=(
                    "{index}/{count} · {width} × {height} · {format}/{mode} · "
                    "{size} MiB · SHA-256 {hash}\n"
                    "{duplicates}；{same_dimensions}\n{path}"
                ),
                index=self.current_index + 1,
                count=len(self.textures),
                width=width,
                height=height,
                format=image_format,
                mode=image_mode,
                size=f"{size_bytes / (1024 * 1024):.2f}",
                hash=digest[:16],
                duplicates=duplicate_text,
                same_dimensions=same_size_text,
                path=str(path),
            )
        except Exception:
            detail = tr(
                "mod.gallery.detail",
                default="{index}/{count} · {width} × {height} · {path}",
                index=self.current_index + 1,
                count=len(self.textures),
                width=pixmap.width(),
                height=pixmap.height(),
                path=str(path),
            )
        self.detail_label.setText(
            detail
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh_image()


class ModelListContainer(QWidget):
    reorderRequested = Signal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.cards: list[ModelCard] = []
        self.drop_index = -1

    def set_cards(self, cards: list["ModelCard"]):
        self.cards = cards
        self.drop_index = -1
        self.update()

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasFormat(MODEL_DRAG_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if not event.mimeData().hasFormat(MODEL_DRAG_MIME):
            return
        y = event.position().toPoint().y()
        self.drop_index = len(self.cards)
        for index, card in enumerate(self.cards):
            if y < card.geometry().center().y():
                self.drop_index = index
                break
        self.update()
        event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self.drop_index = -1
        self.update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent):
        if not event.mimeData().hasFormat(MODEL_DRAG_MIME):
            return
        model_id = bytes(
            event.mimeData().data(MODEL_DRAG_MIME)
        ).decode("utf-8", errors="ignore")
        source_index = next(
            (
                index
                for index, card in enumerate(self.cards)
                if card.model_id == model_id
            ),
            -1,
        )
        target_index = self.drop_index
        self.drop_index = -1
        self.update()
        if source_index < 0:
            return
        if target_index > source_index:
            target_index -= 1
        target_index = max(0, min(target_index, len(self.cards) - 1))
        if target_index != source_index:
            self.reorderRequested.emit(model_id, target_index)
        event.acceptProposedAction()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.drop_index < 0 or not self.cards:
            return
        if self.drop_index < len(self.cards):
            y = self.cards[self.drop_index].geometry().top() - 4
        else:
            y = self.cards[-1].geometry().bottom() + 4
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#00a6ad"), 3))
        painter.drawLine(8, y, max(8, self.width() - 14), y)


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
        textures: list[Path],
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
            pixmap = _scaled_texture_pixmap(texture, 78, 90)
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
        self.drag_handle = TransparentToolButton(FluentIcon.MOVE, self)
        self.drag_handle.setFixedSize(34, 30)
        self.drag_handle.setCursor(Qt.OpenHandCursor)
        self.drag_handle.setToolTip(
            tr(
                "mod.models.drag_tooltip",
                default="按住拖动以调整顺序；拖到顶部会设为主模型",
            )
        )
        self.drag_handle.installEventFilter(self)
        self._drag_start = QPoint()
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
        header.addWidget(self.drag_handle)
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
        make_main_button = PushButton(
            (
                tr("mod.models.current_main", default="当前主模型")
                if index == 0
                else tr("mod.models.make_main", default="设为主模型")
            ),
            self,
        )
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
        make_main_button.setEnabled(index > 0 and has_model)
        view_button.clicked.connect(
            lambda: self.texturesRequested.emit(self.model_id)
        )
        preview_button.clicked.connect(
            lambda: self.previewRequested.emit(self.model_id)
        )
        mapping_button.clicked.connect(
            lambda: self.mappingRequested.emit(self.model_id)
        )
        make_main_button.clicked.connect(
            lambda: self.moveRequested.emit(self.model_id, 0)
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
            make_main_button,
            folder_button,
            remove_button,
        ):
            button.setMinimumWidth(50)
            button.setMaximumWidth(104)
            button.setMinimumHeight(30)
            actions.addWidget(button)
        actions.addStretch(1)
        content.addLayout(actions)
        root.addLayout(content, 1)

    def eventFilter(self, watched, event):
        if watched is getattr(self, "drag_handle", None):
            if (
                event.type() == QEvent.MouseButtonPress
                and event.button() == Qt.LeftButton
            ):
                self._drag_start = event.position().toPoint()
                self.drag_handle.setCursor(Qt.ClosedHandCursor)
            elif (
                event.type() == QEvent.MouseMove
                and event.buttons() & Qt.LeftButton
                and (
                    event.position().toPoint() - self._drag_start
                ).manhattanLength()
                >= QApplication.startDragDistance()
            ):
                mime = QMimeData()
                mime.setData(MODEL_DRAG_MIME, self.model_id.encode("utf-8"))
                drag = QDrag(self)
                drag.setMimeData(mime)
                drag.exec(Qt.MoveAction)
                self.drag_handle.setCursor(Qt.OpenHandCursor)
                return True
            elif event.type() == QEvent.MouseButtonRelease:
                self.drag_handle.setCursor(Qt.OpenHandCursor)
        return super().eventFilter(watched, event)


class ProjectSearchComboBox(EditableComboBox):
    """Editable project selector that never creates arbitrary entries."""

    def _onReturnPressed(self):
        # The page resolves exact or unique partial matches.  The stock
        # EditableComboBox would append unmatched search text as a new item.
        pass


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
        self._project_search_timer = QTimer(self)
        self._project_search_timer.setSingleShot(True)
        self._project_search_timer.setInterval(220)
        self._project_search_timer.timeout.connect(
            self.open_project_from_search
        )
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
        self.project_combo = ProjectSearchComboBox(self.project_card)
        self.project_combo.currentIndexChanged.connect(self.on_project_combo_changed)
        self.project_combo.textChanged.connect(
            self.on_project_search_text_changed
        )
        self.project_combo.returnPressed.connect(
            self.open_project_from_search
        )
        project_layout.addWidget(self.project_title)
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
        self.artmesh_combo.setMaxVisibleItems(12)
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

        self.models_scroll = ScrollArea(right)
        self.models_scroll.setWidgetResizable(True)
        self.models_scroll.setFrameShape(QFrame.NoFrame)
        self.models_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.models_scroll.enableTransparentBackground()
        self.models_container = ModelListContainer(self.models_scroll)
        self.models_container.reorderRequested.connect(self.move_model)
        self.models_layout = QVBoxLayout(self.models_container)
        self.models_layout.setContentsMargins(0, 0, 5, 0)
        self.models_layout.setSpacing(8)
        self.models_scroll.setWidget(self.models_container)
        self.models_scroll.enableTransparentBackground()
        right_layout.addWidget(self.models_scroll, 1)

        self.splitter.addWidget(left)
        self.splitter.addWidget(right)
        self.splitter.setSizes([370, 1070])
        self._apply_styles()

    def retranslate_ui(self):
        self.project_title.setText(tr("mod.project.title"))
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
                default="生成完整模型、换装触发参数和 JSON 映射表，输出为可上传创意工坊的文件夹。",
            )
        )
        self.export_button.setText(
            tr("mod.v4.export_folder", default="导出文件夹")
        )
        self.export_folder_button.setText(
            tr("mod.v4.open_export", default="打开导出位置")
        )
        self.import_title.setText(
            tr("mod.v3.import_title", default="1 · 导入模型")
        )
        self.source_edit.setPlaceholderText(
            tr(
                "mod.v5.source_placeholder",
                default="把任意 Live2D 相关文件或文件夹拖到这里，也可浏览选择…",
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
                default="拖动左侧手柄自由排序；拖到最上方或点击“设为主模型”即可更换主模型。",
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
                or self.suggest_project_name(source)
            ),
            output_root=self.settings_manager.get_output_dir("live2dviewer_mod"),
            temp_root=self.settings_manager.get_temp_dir(),
        )

    def create_new_project(self):
        if not self.confirm_discard_or_save():
            return
        source = self.source_edit.text().strip()
        suggested = (
            self.suggest_project_name(source)
            if source and Path(source).exists()
            else ""
        )
        dialog = ProjectNameDialog(
            tr("mod.project.new_title", default="新建 MOD 工程"),
            suggested,
            self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            project = create_empty_project(
                dialog.project_name,
                output_root=self.settings_manager.get_output_dir(
                    "live2dviewer_mod"
                ),
            )
            self.set_current_project(project)
        except Exception as exc:
            self.show_error(str(exc))

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
            project, export_path = result
            self.set_current_project(project)
            message = tr(
                "mod.v2.exported",
                default="创意工坊文件夹已导出：{path}",
                path=str(export_path),
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
        scroll_value = self.models_scroll.verticalScrollBar().value()
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
        cards: list[ModelCard] = []
        for index, model in enumerate(models):
            textures = model_texture_paths(
                self.current_project,
                str(model.get("id") or ""),
            )
            card = ModelCard(
                model,
                index,
                textures,
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
            cards.append(card)
        self.models_layout.addStretch(1)
        self.models_container.set_cards(cards)
        QTimer.singleShot(
            0,
            lambda value=scroll_value: self.models_scroll.verticalScrollBar().setValue(
                min(value, self.models_scroll.verticalScrollBar().maximum())
            ),
        )

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
        old_main_id = str(
            self.current_project.models[0].get("id") or ""
        ) if self.current_project.models else ""
        try:
            moved = move_model_in_project(
                self.current_project,
                model_id,
                target_index,
            )
            self.current_project = moved
            self._dirty = False
            new_main_id = (
                str(moved.models[0].get("id") or "") if moved.models else ""
            )
            if new_main_id != old_main_id:
                self.refresh_artmesh_combo()
            self.refresh_model_cards()
            self.refresh_project_ui()
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
        for area in areas:
            area_id = str(area.get("id") or "").strip()
            name = str(area.get("name") or area_id).strip()
            if not area_id:
                continue
            self.artmesh_combo.addItem(
                area_id if name == area_id else f"{name}  ·  {area_id}",
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
        last_export = str(
            self.current_project.data.get("last_export_path") or ""
        )
        default_dir = (
            Path(last_export).resolve().parent
            if last_export
            else self.current_project.project_dir / EXPORTS_DIR
        )
        path = QFileDialog.getExistingDirectory(
            self,
            tr(
                "mod.v4.export_dialog",
                default="选择创意工坊 MOD 文件夹的保存位置",
            ),
            str(default_dir),
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
        exported = Path(last_export).resolve() if last_export else None
        directory = (
            exported
            if exported and exported.is_dir()
            else (
                exported.parent
                if exported
                else self.current_project.project_dir / EXPORTS_DIR
            )
        )
        directory.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def open_project_file(self):
        if not self.open_project_from_search():
            self.show_warning(
                tr(
                    "mod.warning.project_not_found",
                    default="请先在上方搜索框中选择一个已有工程。",
                )
            )

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
        dialog = ProjectNameDialog(
            tr("mod.project.rename_title", default="重命名 MOD 工程"),
            self.current_project.project_name,
            self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        old_path = str(self.current_project.project_file.resolve())
        try:
            renamed = rename_project(
                self.current_project,
                dialog.project_name,
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
                default="皮肤名称或 ArtMesh 触发区有未保存修改。",
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
        else:
            self.project_combo.setCurrentIndex(-1)
        self.project_combo.blockSignals(False)
        self._project_combo_refreshing = False

    def project_path_from_search(
        self,
        query: str | None = None,
        *,
        allow_partial: bool = True,
    ) -> str:
        text = str(
            self.project_combo.currentText() if query is None else query
        ).strip()
        if not text:
            return ""
        folded = text.casefold()
        entries = [
            (
                str(self.project_combo.itemText(index)).strip(),
                str(self.project_combo.itemData(index) or ""),
            )
            for index in range(self.project_combo.count())
            if self.project_combo.itemData(index)
        ]
        exact = [path for name, path in entries if name.casefold() == folded]
        if len(exact) == 1:
            return exact[0]
        if not allow_partial:
            return ""
        partial = [
            path for name, path in entries if folded in name.casefold()
        ]
        return partial[0] if len(partial) == 1 else ""

    def on_project_search_text_changed(self, text: str):
        if self._project_combo_refreshing:
            return
        self._project_search_timer.stop()
        index = self.project_combo.currentIndex()
        if (
            index >= 0
            and self.project_combo.itemData(index)
            and str(self.project_combo.itemText(index)).strip().casefold()
            == str(text).strip().casefold()
        ):
            return
        if len(str(text).strip()) >= 2:
            self._project_search_timer.start()

    def open_project_from_search(self) -> bool:
        self._project_search_timer.stop()
        path = self.project_path_from_search()
        if not path:
            return False
        return self.switch_to_project(path)

    def switch_to_project(self, path: str) -> bool:
        if (
            self.current_project
            and str(self.current_project.project_file.resolve())
            == str(Path(path).resolve())
        ):
            return True
        if not self.confirm_discard_or_save():
            self.refresh_project_combo(
                str(self.current_project.project_file)
                if self.current_project
                else ""
            )
            return False
        self.load_project_file(path)
        return True

    def on_project_combo_changed(self, *_args):
        if self._project_combo_refreshing:
            return
        path = str(self.project_combo.currentData() or "")
        if not path:
            return
        self.switch_to_project(path)

    def mark_dirty(self, *_args):
        if self._loading_ui or not self.current_project:
            return
        self._dirty = True
        self.refresh_title()
        self.save_project_button.setEnabled(True)

    def refresh_title(self):
        name = (
            self.current_project.project_name
            if self.current_project
            else tr("mod.v3.untitled", default="未命名")
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
            else tr(
                "mod.v4.auto_create_import",
                default="自动建工程并导入",
            )
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
