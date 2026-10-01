"""Fluent skin catalog controls shared by texture, PSD and export workflows."""
from pathlib import Path

from PySide6.QtCore import QEvent, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, FluentIcon, PushButton, TransparentToolButton

from app.gui.editor_actions import ActionComboBox
from app.gui.editor_dialogs import EditorTextDialog
from app.i18n import tr


SKIN_WORKSPACE_TEXT = {
    "editor.skin.tab": "皮肤与贴图",
    "editor.skin.psd_tab": "PSD 编辑",
    "editor.skin.export_tab": "导出",
    "editor.skin.select": "搜索并选择当前皮肤",
    "editor.skin.original": "原始皮肤",
    "editor.skin.capture": "保存当前贴图为新皮肤",
    "editor.skin.rename": "重命名皮肤",
    "editor.skin.delete": "删除皮肤",
    "editor.skin.import_model": "导入模型贴图",
    "editor.skin.import_textures": "导入贴图并映射",
    "editor.skin.edit_psd": "用 PSD 编辑当前皮肤",
    "editor.skin.active": "当前皮肤：{name}",
    "editor.skin.modified": "当前皮肤：{name} · 工作贴图有修改",
    "editor.skin.modified_hint": "工作贴图不会覆盖具名皮肤。可保存为新皮肤；切换时会自动保留工作编辑，切换可撤销。",
    "editor.skin.empty": "打开模型后管理皮肤与工作贴图。",
    "editor.skin.current_working": "当前工作贴图（含修改）",
    "editor.skin.name": "皮肤名称",
    "editor.skin.new_title": "保存为新皮肤",
    "editor.skin.import_title": "导入皮肤",
    "editor.skin.rename_title": "重命名皮肤",
    "editor.skin.delete_title": "删除皮肤",
    "editor.skin.delete_confirm": "删除皮肤“{name}”？可通过撤销恢复。原始皮肤始终保留。",
    "editor.skin.model_picker": "选择提供皮肤贴图的 Live2D 模型",
    "editor.skin.texture_picker": "选择皮肤贴图（可多选）",
    "editor.skin.drop_hint": "将 Live2D 模型文件、目录或贴图拖到此页以导入皮肤；不会更换工程主模型。",
    "editor.skin.mapping_hint": "请选择每张导入贴图对应的当前模型图集。模型不同或缺少 UV 兼容证据时必须手动确认；尺寸相同不代表 UV 相同。未映射的图集保留当前工作贴图。",
    "editor.skin.empty_name": "请输入皮肤名称。",
    "editor.skin.duplicate_name": "皮肤名称已存在，请换一个名称。",
    "editor.skin.applied": "已应用皮肤：{name}；动作保持不变，可撤销。",
    "editor.skin.live2d_export": "完整 Live2D 模型",
    "editor.skin.live2d_hint": "导出当前模型、当前编辑动作和所选皮肤，可直接加载使用。继续编辑请用顶部“保存”保存完整工程。",
    "editor.skin.export_button": "导出完整 Live2D",
    "editor.skin.export_parent": "选择导出的父目录",
    "editor.skin.export_done": "Live2D 已导出：{path}",
    "editor.skin.viewer_export": "ViewerEX 点击换装",
    "editor.skin.viewer_hint": "专用多皮肤导出：绑定 ArtMesh / HitArea，保留皮肤映射、命令与高级工程配置。",
    "editor.skin.viewer_add": "添加当前皮肤到换装工程",
    "editor.skin.psd_current": "PSD 导出源：当前皮肤 {name}（含工作贴图修改）",
    "editor.skin.psd_result": "回写生成皮肤：{name}",
    "editor.skin.psd_save": "将版本保存为皮肤并应用",
    "editor.skin.psd_viewer": "将版本用于 ViewerEX 换装",
    "editor.skin.psd_hint": "PSD 快照与回写历史保留各自基准；回写会生成新皮肤，当前动作保持不变。",
}


def skin_text(key, **values):
    return tr(key, default=SKIN_WORKSPACE_TEXT[key], **values)


class SkinNameDialog(EditorTextDialog):
    def __init__(self, title, parent, names, initial="", allow_name=None):
        super().__init__(title, skin_text("editor.skin.name"), parent, initial_text=initial)
        self.names = {name.casefold() for name in names} - ({allow_name.casefold()} if allow_name else set())
        self.name_edit.setMaxLength(80)
        self.error_label = CaptionLabel(self)
        self.error_label.setStyleSheet("color: #de5d68;")
        self.viewLayout.addWidget(self.error_label)
        self.error_label.hide()

    def accept(self):
        name = self.name_edit.text().strip()
        error = skin_text("editor.skin.empty_name") if not name else (
            skin_text("editor.skin.duplicate_name") if name.casefold() in self.names else "")
        self.error_label.setText(error)
        self.error_label.setVisible(bool(error))
        if error:
            self.name_edit.setFocus()
        else:
            super().accept()

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.accept()
            event.accept()
        else:
            super().keyPressEvent(event)


class SkinDropFrame(QWidget):
    filesDropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def watch_drop_surface(self, widget):
        widget.setAcceptDrops(True)
        widget.installEventFilter(self)

    @staticmethod
    def _paths(event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        suffixes = {".json", ".moc3", ".lpk", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}
        return paths if paths and all(Path(path).is_dir() or Path(path).suffix.lower() in suffixes for path in paths) else []

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() in {QEvent.DragEnter, QEvent.DragMove, QEvent.Drop}:
            paths = self._paths(event)
            if paths:
                event.acceptProposedAction()
                if event.type() == QEvent.Drop:
                    self.filesDropped.emit(paths)
                return True
        return super().eventFilter(watched, event)

    def dragEnterEvent(self, event):  # noqa: N802
        if self._paths(event):
            event.acceptProposedAction()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, event):  # noqa: N802
        paths = self._paths(event)
        if paths:
            event.acceptProposedAction()
            self.filesDropped.emit(paths)


class TexturePreviewLabel(QLabel):
    """Fit the atlas to its current viewport after every layout resize."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._source_pixmap = QPixmap()

    def setPixmap(self, pixmap):  # noqa: N802
        self._source_pixmap = QPixmap(pixmap)
        self._fit_pixmap()

    def _fit_pixmap(self):
        if not self._source_pixmap.isNull():
            size = self.contentsRect().adjusted(4, 4, -4, -4).size()
            if size.width() > 0 and size.height() > 0:
                super().setPixmap(self._source_pixmap.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._fit_pixmap()

    def clear(self):
        self._source_pixmap = QPixmap()
        super().clear()


class SkinCatalogControls(QWidget):
    skinSelected = Signal(str)
    captureRequested = Signal()
    renameRequested = Signal()
    deleteRequested = Signal()
    importModelRequested = Signal()
    importTexturesRequested = Signal()
    editPsdRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._records = []
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(7)
        row = QHBoxLayout()
        self.combo = ActionComboBox(self)
        self.combo.setMaximumWidth(16777215)
        self.combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.combo.currentIndexChanged.connect(self._selected)
        row.addWidget(self.combo, 1)
        self.capture_button = TransparentToolButton(FluentIcon.ADD, self)
        self.rename_button = TransparentToolButton(FluentIcon.EDIT, self)
        self.delete_button = TransparentToolButton(FluentIcon.REMOVE, self)
        for button, signal in ((self.capture_button, self.captureRequested),
                               (self.rename_button, self.renameRequested),
                               (self.delete_button, self.deleteRequested)):
            button.setFixedSize(28, 28)
            button.clicked.connect(signal)
            row.addWidget(button)
        root.addLayout(row)
        imports = QVBoxLayout()
        self.import_model_button = PushButton(FluentIcon.FOLDER, "", self)
        self.import_textures_button = PushButton(FluentIcon.PHOTO, "", self)
        self.import_model_button.clicked.connect(self.importModelRequested)
        self.import_textures_button.clicked.connect(self.importTexturesRequested)
        imports.addWidget(self.import_model_button)
        imports.addWidget(self.import_textures_button)
        root.addLayout(imports)
        self.state_label = CaptionLabel(self)
        self.state_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        root.addWidget(self.state_label)
        self.psd_button = PushButton(FluentIcon.EDIT, "", self)
        self.psd_button.clicked.connect(self.editPsdRequested)
        root.addWidget(self.psd_button)
        self.retranslate_ui()

    def _selected(self, _index):
        if self.combo.currentData():
            self.skinSelected.emit(str(self.combo.currentData()))

    def refresh(self, records, active_id, modified=False, enabled=True):
        self._records = list(records)
        with QSignalBlocker(self.combo):
            self.combo.clear()
            for record in self._records:
                label = skin_text("editor.skin.original") if record.get("is_original") else record["name"]
                self.combo.addItem(label, record["id"])
            self.combo.setCurrentIndex(max(0, self.combo.findData(active_id)))
        active = next((entry for entry in self._records if entry["id"] == active_id), {})
        name = skin_text("editor.skin.original") if active.get("is_original") else active.get("name", "")
        self.state_label.setText(skin_text("editor.skin.modified" if modified else "editor.skin.active", name=name)
                                 if active else skin_text("editor.skin.empty"))
        self.state_label.setToolTip(skin_text("editor.skin.modified_hint") if modified else self.state_label.text())
        self.setEnabled(enabled)
        self.rename_button.setEnabled(enabled and bool(active) and not active.get("is_original"))
        self.delete_button.setEnabled(enabled and bool(active) and not active.get("is_original"))

    def retranslate_ui(self):
        self.combo.setPlaceholderText(skin_text("editor.skin.select"))
        self.combo.setToolTip(skin_text("editor.skin.select"))
        self.combo.setAccessibleName(skin_text("editor.skin.select"))
        for button, key in ((self.capture_button, "capture"), (self.rename_button, "rename"),
                            (self.delete_button, "delete")):
            button.setToolTip(skin_text("editor.skin." + key))
            button.setAccessibleName(button.toolTip())
        for button, key in ((self.import_model_button, "import_model"), (self.import_textures_button, "import_textures"),
                            (self.psd_button, "edit_psd")):
            button.setText(skin_text("editor.skin." + key))
