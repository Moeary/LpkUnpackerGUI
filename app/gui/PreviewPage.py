import os
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile

from PySide6.QtWidgets import (
    QFrame,
    QVBoxLayout,
    QHBoxLayout,
    QFileDialog,
    QWidget,
    QSplitter,
    QGridLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
)
from PySide6.QtCore import Qt, Signal, QTimer, QCoreApplication, QThread, QPoint, QEvent
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QColor, QPixmap
from qfluentwidgets import (SubtitleLabel, BodyLabel, PushButton, Slider, CheckBox, SpinBox, InfoBar, InfoBarPosition,
                           CardWidget, SingleDirectionScrollArea, TextBrowser, ColorDialog, FluentIcon, IconWidget,
                           ComboBox, LineEdit)

from app.core.assetstudio_cli import AssetStudioCLI
from app.core.model import is_model_json_path, resolve_live2d_package
from app.core.model.motions import load_live2d_motions
from app.core.preview import prepare_preview_import
from app.core.settings_manager import SettingsManager
from app.i18n import get_i18n, tr
from app.paths import PROJECT_ROOT
IMAGE_PREVIEW_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tga"}
PACKAGE_PREVIEW_EXTENSIONS = {".lpk", ".wpk"}
ARCHIVE_PREVIEW_EXTENSIONS = {".zip", ".7z", ".rar"}
SPINE_PREVIEW_EXTENSIONS = {".skel", ".atlas"}
UNITY_PREVIEW_EXTENSIONS = {
    "",
    ".assets",
    ".sharedassets",
    ".bundle",
    ".unity3d",
    ".resource",
    ".ress",
    ".resss",
}

def _is_model_json(path: str) -> bool:
    return is_model_json_path(path)


def _is_image_file(path: str) -> bool:
    return os.path.isfile(path) and os.path.splitext(path)[1].lower() in IMAGE_PREVIEW_EXTENSIONS


def _is_unity_preview_source(path: str) -> bool:
    if not os.path.exists(path):
        return False
    if os.path.isdir(path):
        return True
    return os.path.splitext(path)[1].lower() in UNITY_PREVIEW_EXTENSIONS


def _is_spine_preview_source(path: str) -> bool:
    if not os.path.isfile(path):
        return False
    suffix = os.path.splitext(path)[1].lower()
    if suffix in SPINE_PREVIEW_EXTENSIONS:
        return True
    if suffix == ".json":
        try:
            with open(path, "r", encoding="utf-8") as f:
                head = f.read(4096).lower()
            return '"skeleton"' in head and '"bones"' in head
        except Exception:
            return False
    return False


def _is_archive_preview_source(path: str) -> bool:
    return os.path.isfile(path) and os.path.splitext(path)[1].lower() in ARCHIVE_PREVIEW_EXTENSIONS


def _is_supported_preview_source(path: str) -> bool:
    suffix = os.path.splitext(path)[1].lower()
    return (
        _is_model_json(path)
        or _is_image_file(path)
        or _is_archive_preview_source(path)
        or _is_spine_preview_source(path)
        or suffix in PACKAGE_PREVIEW_EXTENSIONS
        or os.path.isdir(path)
        or _is_unity_preview_source(path)
    )


def _collect_preview_images(path: str, limit: int = 48) -> list[str]:
    if _is_image_file(path):
        return [path]
    if not os.path.isdir(path):
        return []
    images = []
    for root, _dirs, filenames in os.walk(path):
        for filename in sorted(filenames):
            full_path = os.path.join(root, filename)
            if os.path.splitext(filename)[1].lower() in IMAGE_PREVIEW_EXTENSIONS:
                images.append(full_path)
                if len(images) >= limit:
                    return images
    return images


def _safe_export_name(label: str | None, fallback: str) -> str:
    raw = os.path.basename(os.path.normpath(label or "")) or fallback
    stem, _ext = os.path.splitext(raw)
    name = stem or raw or fallback
    safe = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in name).strip(" .")
    return safe or fallback


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    index = 1
    while True:
        candidate = f"{base}_{index}{ext}"
        if not os.path.exists(candidate):
            return candidate
        index += 1


def _unique_dir(path: str) -> str:
    return _unique_path(path)


def _safe_extract_zip(zip_path: str, target_dir: str):
    target_root = os.path.abspath(target_dir)
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        for member in zip_ref.infolist():
            target_path = os.path.abspath(os.path.join(target_root, member.filename))
            if os.path.commonpath([target_root, target_path]) != target_root:
                raise RuntimeError(f"Unsafe archive path: {member.filename}")
        zip_ref.extractall(target_root)


def _safe_extract_archive(archive_path: str, target_dir: str, extractor_path: str = ""):
    suffix = os.path.splitext(archive_path)[1].lower()
    if suffix == ".zip":
        try:
            _safe_extract_zip(archive_path, target_dir)
            return
        except zipfile.BadZipFile:
            pass
    _extract_archive_with_external_tool(archive_path, target_dir, extractor_path)


def _extract_archive_with_external_tool(archive_path: str, target_dir: str, extractor_path: str = ""):
    target_root = os.path.abspath(target_dir)
    os.makedirs(target_root, exist_ok=True)

    commands = _archive_extract_commands(archive_path, target_root, extractor_path)
    if not commands:
        raise RuntimeError(
            "No archive extractor found. Set bz.exe, 7z.exe, WinRAR.exe, or UnRAR.exe in Settings, or make one available in PATH."
        )

    errors = []
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        except Exception as exc:
            errors.append(f"{command[0]}: {exc}")
            continue
        if completed.returncode == 0:
            return
        message = (completed.stderr or completed.stdout or "").strip()
        errors.append(f"{command[0]} exited with {completed.returncode}: {message}")

    raise RuntimeError("Archive extraction failed. " + " | ".join(errors))


def _archive_extract_commands(archive_path: str, target_dir: str, extractor_path: str = "") -> list[list[str]]:
    commands: list[list[str]] = []

    configured = (extractor_path or "").strip().strip('"')
    if configured:
        commands.extend(_archive_commands_for_executable(configured, archive_path, target_dir))

    bz = shutil.which("bz")
    if bz:
        commands.append([bz, "x", "-y", "-aoa", f"-o:{target_dir}", archive_path])

    for name in ("7z", "7za", "7zr"):
        exe = shutil.which(name)
        if exe:
            commands.append([exe, "x", "-y", f"-o{target_dir}", archive_path])

    for name in ("WinRAR", "UnRAR"):
        exe = shutil.which(name)
        if exe:
            commands.append([exe, "x", "-y", archive_path, target_dir + os.sep])

    return commands


def _archive_commands_for_executable(exe: str, archive_path: str, target_dir: str) -> list[list[str]]:
    if not os.path.isfile(exe):
        return []
    name = os.path.splitext(os.path.basename(exe))[0].lower()
    if name in {"bz", "bandizip"}:
        return [[exe, "x", "-y", "-aoa", f"-o:{target_dir}", archive_path]]
    if name in {"7z", "7za", "7zr"}:
        return [[exe, "x", "-y", f"-o{target_dir}", archive_path]]
    if name in {"winrar", "unrar", "rar"}:
        return [[exe, "x", "-y", archive_path, target_dir + os.sep]]
    return [
        [exe, "x", "-y", "-aoa", f"-o:{target_dir}", archive_path],
        [exe, "x", "-y", f"-o{target_dir}", archive_path],
        [exe, "x", "-y", archive_path, target_dir + os.sep],
    ]


class DragDropArea(QFrame):
    """拖拽区域组件"""
    fileDropped = Signal(str)  # 文件拖拽信号

    def __init__(self, parent=None):
        super().__init__(parent)
        self.browse_btn = None
        self.main_text = None
        self.sub_text = None
        self.browse_text = None
        self.setAcceptDrops(True)
        self.setupUI()

    def setupUI(self):
        """设置拖拽区域UI"""
        self.setMinimumHeight(190)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setStyleSheet("""
            DragDropArea {
                border: 2px dashed #C8CDD6;
                border-radius: 8px;
                background: #FAFBFD;
            }
            DragDropArea:hover {
                border-color: #00A6B3;
                background: #F5FBFC;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignCenter)

        # 拖拽图标
        icon_label = IconWidget(FluentIcon.FOLDER, self)
        icon_label.setFixedSize(42, 42)

        # 主要提示文字
        self.main_text = SubtitleLabel("", self)
        self.main_text.setAlignment(Qt.AlignCenter)
        self.main_text.setWordWrap(True)

        # 次要提示文字
        self.sub_text = BodyLabel("", self)
        self.sub_text.setAlignment(Qt.AlignCenter)
        self.sub_text.setWordWrap(True)

        # 额外提示文字
        self.browse_text = BodyLabel("", self)
        self.browse_text.setAlignment(Qt.AlignCenter)
        self.browse_text.setWordWrap(True)

        # 浏览文件按钮
        self.browse_btn = PushButton("", self)
        self.browse_btn.clicked.connect(self.browse_files)

        layout.addWidget(icon_label)
        layout.addWidget(self.main_text)
        layout.addWidget(self.sub_text)
        layout.addWidget(self.browse_text)
        layout.addWidget(self.browse_btn)
        self._install_drag_event_forwarders()
        self.retranslate_ui()

    def _install_drag_event_forwarders(self):
        for widget in self.findChildren(QWidget):
            if widget is self:
                continue
            widget.setAcceptDrops(True)
            widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.DragEnter:
            self.dragEnterEvent(event)
            return event.isAccepted()
        if event.type() == QEvent.Type.DragMove:
            self.dragMoveEvent(event)
            return event.isAccepted()
        if event.type() == QEvent.Type.Drop:
            self.dropEvent(event)
            return event.isAccepted()
        return super().eventFilter(obj, event)

    def retranslate_ui(self):
        self.main_text.setText(tr("preview.drag_main"))
        self.sub_text.setText(tr("preview.drag_sub"))
        self.browse_text.setText(tr("preview.drag_or_click"))
        self.browse_btn.setText(tr("preview.browse_files"))

    def dragEnterEvent(self, event: QDragEnterEvent):
        """拖拽进入事件"""
        mime_data = event.mimeData()
        if self._accepts_mime_data(mime_data):
            event.acceptProposedAction()
            self._set_drag_active_style()
            return
        event.ignore()

    def dragMoveEvent(self, event):
        if self._accepts_mime_data(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def _accepts_mime_data(self, mime_data) -> bool:
        if not mime_data.hasUrls():
            return False
        urls = mime_data.urls()
        if not urls:
            return False
        return any(
            _is_supported_preview_source(url.toLocalFile())
            for url in urls
            if url.toLocalFile()
        )

    def _set_drag_active_style(self):
        self.setStyleSheet("""
            DragDropArea {
                border: 2px solid #00A6B3;
                border-radius: 8px;
                background: #EFFBFC;
            }
            DragDropArea:hover {
                border-color: #00A6B3;
                background: #EFFBFC;
            }
        """)

    def dragLeaveEvent(self, event):
        """拖拽离开事件"""
        self.setStyleSheet("""
            DragDropArea {
                border: 2px dashed #C8CDD6;
                border-radius: 8px;
                background: #FAFBFD;
            }
            DragDropArea:hover {
                border-color: #00A6B3;
                background: #F5FBFC;
            }
        """)

    def dropEvent(self, event: QDropEvent):
        """文件拖拽事件"""
        mime_data = event.mimeData()
        urls = mime_data.urls()
        if urls:
            emitted = False
            for url in urls:
                file_path = url.toLocalFile()
                if _is_supported_preview_source(file_path) and os.path.exists(file_path):
                    self.fileDropped.emit(file_path)
                    emitted = True
                    break
            if emitted:
                event.acceptProposedAction()

        # 恢复样式
        self.dragLeaveEvent(event)

    def browse_files(self):
        """浏览文件对话框"""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            tr("dialog.select_live2d_model_file"),
            "",
            tr("dialog.filter_preview_sources")
        )

        if file_path and os.path.exists(file_path):
            if _is_supported_preview_source(file_path):
                self.fileDropped.emit(file_path)
            else:
                InfoBar.warning(
                    title=tr("preview.invalid_file_type_title"),
                    content=tr("preview.invalid_file_type_content"),
                    orient=Qt.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=2500,
                    parent=self
                )


class PreviewPathLineEdit(LineEdit):
    pathsDropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if self._accepts(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._accepts(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        for url in urls:
            path = url.toLocalFile()
            if path and os.path.exists(path) and _is_supported_preview_source(path):
                self.pathsDropped.emit(path)
                event.acceptProposedAction()
                return
        event.ignore()

    def _accepts(self, event) -> bool:
        mime = event.mimeData()
        if not mime.hasUrls():
            return False
        return any(
            path and os.path.exists(path) and _is_supported_preview_source(path)
            for path in (url.toLocalFile() for url in mime.urls())
        )


class UnityTexturePreviewThread(QThread):
    previewReady = Signal(list, str)
    failed = Signal(str, str)

    def __init__(self, source_path: str, temp_dir: str, parent=None):
        super().__init__(parent)
        self.source_path = source_path
        self.temp_dir = temp_dir

    def run(self):
        try:
            result = AssetStudioCLI().export_textures(self.source_path, self.temp_dir)
            images = [str(path) for path in result.exported_files]
            if not images:
                images = _collect_preview_images(self.temp_dir)
            self.previewReady.emit(images[:48], self.temp_dir)
        except Exception as exc:
            self.failed.emit(str(exc), self.temp_dir)


class ArchivePreviewImportThread(QThread):
    archiveReady = Signal(str, str)
    failed = Signal(str, str)

    def __init__(self, archive_path: str, temp_root: str, parent=None):
        super().__init__(parent)
        self.archive_path = archive_path
        self.temp_root = temp_root
        self.extractor_path = SettingsManager().get_archive_extractor_path()

    def run(self):
        temp_dir = ""
        try:
            os.makedirs(self.temp_root, exist_ok=True)
            temp_dir = tempfile.mkdtemp(prefix="lpk_preview_archive_", dir=self.temp_root)
            _safe_extract_archive(self.archive_path, temp_dir, self.extractor_path)
            self.archiveReady.emit(temp_dir, self.archive_path)
        except Exception as exc:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
            self.failed.emit(str(exc), self.archive_path)


class PreviewExportThread(QThread):
    exported = Signal(str, int)
    failed = Signal(str)

    def __init__(
        self,
        output_root: str,
        folder_name: str,
        source_dir: str | None = None,
        files: list[str] | None = None,
        image_only: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.output_root = output_root
        self.folder_name = folder_name
        self.source_dir = source_dir
        self.files = list(files or [])
        self.image_only = image_only

    def run(self):
        try:
            target_dir = _unique_dir(os.path.join(self.output_root, self.folder_name))
            if self.source_dir and os.path.isdir(self.source_dir):
                source_abs = os.path.abspath(self.source_dir)
                target_abs = os.path.abspath(target_dir)
                try:
                    if os.path.commonpath([source_abs, target_abs]) == source_abs:
                        raise RuntimeError("Export directory cannot be inside the preview source directory.")
                except ValueError:
                    pass
            os.makedirs(target_dir, exist_ok=True)
            copied = self._copy_to(target_dir)
            if copied <= 0:
                raise RuntimeError("No files were available for export.")
            self.exported.emit(target_dir, copied)
        except Exception as exc:
            self.failed.emit(str(exc))

    def _copy_to(self, target_dir: str) -> int:
        if self.source_dir and os.path.isdir(self.source_dir):
            return self._copy_tree(self.source_dir, target_dir)
        return self._copy_flat(target_dir)

    def _copy_tree(self, source_dir: str, target_dir: str) -> int:
        copied = 0
        source_dir_abs = os.path.abspath(source_dir)
        for root, _dirs, filenames in os.walk(source_dir_abs):
            for filename in filenames:
                source_file = os.path.join(root, filename)
                if self.image_only and os.path.splitext(filename)[1].lower() not in IMAGE_PREVIEW_EXTENSIONS:
                    continue
                rel = os.path.relpath(source_file, source_dir_abs)
                target_file = os.path.join(target_dir, rel)
                os.makedirs(os.path.dirname(target_file), exist_ok=True)
                shutil.copy2(source_file, _unique_path(target_file))
                copied += 1
        return copied

    def _copy_flat(self, target_dir: str) -> int:
        copied = 0
        for source_file in self.files:
            if not os.path.isfile(source_file):
                continue
            if self.image_only and os.path.splitext(source_file)[1].lower() not in IMAGE_PREVIEW_EXTENSIONS:
                continue
            target_file = os.path.join(target_dir, os.path.basename(source_file))
            shutil.copy2(source_file, _unique_path(target_file))
            copied += 1
        return copied


class ModelPreviewImportThread(QThread):
    modelReady = Signal(str, str, str)
    failed = Signal(str, str)

    def __init__(self, source_path: str, temp_root: str, parent=None):
        super().__init__(parent)
        self.source_path = source_path
        self.temp_root = temp_root

    def run(self):
        try:
            result = prepare_preview_import(self.source_path, self.temp_root)
            self.modelReady.emit(
                str(result.preview_model_json),
                str(result.temp_dir or ""),
                self.source_path,
            )
        except Exception as exc:
            self.failed.emit(str(exc), self.source_path)


class FolderPreviewScanThread(QThread):
    itemFound = Signal(dict)
    scanFinished = Signal(int, bool, str)
    failed = Signal(str, str)

    def __init__(self, source_path: str, temp_root: str, image_limit: int = 48, parent=None):
        super().__init__(parent)
        self.source_path = source_path
        self.temp_root = temp_root
        self.image_limit = max(1, int(image_limit or 48))
        self.extractor_path = SettingsManager().get_archive_extractor_path()
        self._image_count = 0
        self._limited = False
        self._emitted_paths = set()
        self._temp_dirs = []

    def run(self):
        try:
            source = os.path.abspath(self.source_path)
            os.makedirs(self.temp_root, exist_ok=True)
            count = 0

            for item in self._iter_direct_model_items(source):
                if self.isInterruptionRequested():
                    return
                self.itemFound.emit(item)
                count += 1

            for item in self._iter_folder_items(source):
                if self.isInterruptionRequested():
                    return
                self.itemFound.emit(item)
                count += 1

            self.scanFinished.emit(count, self._limited, source)
        except Exception as exc:
            self.failed.emit(str(exc), self.source_path)

    def _iter_direct_model_items(self, source: str):
        if os.path.isfile(source):
            candidates = [source] if _is_model_json(source) else []
        else:
            candidates = self._iter_model_json_files(source)
        for candidate in candidates:
            if self.isInterruptionRequested():
                return
            path = os.path.abspath(str(candidate))
            if path in self._emitted_paths:
                continue
            try:
                package = resolve_live2d_package(path)
            except Exception:
                continue
            self._emitted_paths.add(path)
            yield {
                "kind": "model",
                "path": path,
                "source_path": path,
                "source_dir": str(package.root_dir),
                "label": package.name,
                "detail": os.path.relpath(path, source),
            }

    def _iter_model_json_files(self, source: str):
        for root, dirs, filenames in os.walk(source):
            if self.isInterruptionRequested():
                return
            dirs.sort()
            for filename in sorted(filenames):
                if _is_model_json(filename):
                    yield os.path.join(root, filename)

    def _iter_folder_items(self, source: str):
        for root, dirs, filenames in os.walk(source):
            if self.isInterruptionRequested():
                return
            dirs.sort()
            for filename in sorted(filenames):
                if self.isInterruptionRequested():
                    return
                path = os.path.join(root, filename)
                suffix = os.path.splitext(filename)[1].lower()

                if suffix in IMAGE_PREVIEW_EXTENSIONS:
                    item = self._image_item(path, source)
                    if item:
                        yield item
                    continue

                if _is_model_json(path):
                    continue

                if suffix in PACKAGE_PREVIEW_EXTENSIONS:
                    for item in self._items_from_package(path, source):
                        yield item
                    continue

                if _is_archive_preview_source(path):
                    if self._image_count >= self.image_limit:
                        self._limited = True
                        continue
                    for item in self._items_from_archive(path, source):
                        yield item
                    continue

                if _is_unity_preview_source(path):
                    if self._image_count >= self.image_limit:
                        self._limited = True
                        continue
                    for item in self._items_from_unity(path, source):
                        yield item

    def _image_item(self, path: str, source: str):
        if self._image_count >= self.image_limit:
            self._limited = True
            return None
        abs_path = os.path.abspath(path)
        if abs_path in self._emitted_paths:
            return None
        self._emitted_paths.add(abs_path)
        self._image_count += 1
        if self._image_count >= self.image_limit:
            self._limited = True
        return {
            "kind": "image",
            "path": abs_path,
            "source_path": source,
            "source_dir": source,
            "label": os.path.basename(abs_path),
            "detail": os.path.relpath(abs_path, source),
        }

    def _items_from_package(self, path: str, source: str):
        try:
            result = prepare_preview_import(path, self.temp_root)
        except Exception:
            return
        temp_dir = str(result.temp_dir or "")
        if temp_dir:
            self._temp_dirs.append(temp_dir)
        preview_path = os.path.abspath(str(result.preview_model_json))
        if preview_path in self._emitted_paths:
            return
        self._emitted_paths.add(preview_path)
        yield {
            "kind": "model",
            "path": preview_path,
            "prepared_model_json": preview_path,
            "source_path": os.path.abspath(path),
            "source_dir": str(result.package.root_dir),
            "temp_dir": temp_dir,
            "label": result.package.name,
            "detail": os.path.relpath(path, source),
        }

    def _items_from_archive(self, path: str, source: str):
        temp_dir = tempfile.mkdtemp(prefix="lpk_preview_folder_archive_", dir=self.temp_root)
        self._temp_dirs.append(temp_dir)
        try:
            _safe_extract_archive(path, temp_dir, self.extractor_path)
        except Exception:
            return

        for item in self._iter_direct_model_items(temp_dir):
            item["source_path"] = item.get("path")
            item["temp_dir"] = temp_dir
            item["detail"] = os.path.relpath(path, source)
            yield item

        if self._image_count >= self.image_limit:
            self._limited = True
            return

        for image in _collect_preview_images(temp_dir, self.image_limit - self._image_count):
            item = self._image_item(image, temp_dir)
            if item:
                item["source_path"] = os.path.abspath(path)
                item["source_dir"] = temp_dir
                item["temp_dir"] = temp_dir
                item["detail"] = os.path.join(os.path.relpath(path, source), os.path.relpath(image, temp_dir))
                yield item

    def _items_from_unity(self, path: str, source: str):
        try:
            result = prepare_preview_import(path, self.temp_root)
            temp_dir = str(result.temp_dir or "")
            if temp_dir:
                self._temp_dirs.append(temp_dir)
            preview_path = os.path.abspath(str(result.preview_model_json))
            if preview_path not in self._emitted_paths:
                self._emitted_paths.add(preview_path)
                yield {
                    "kind": "model",
                    "path": preview_path,
                    "prepared_model_json": preview_path,
                    "source_path": os.path.abspath(path),
                    "source_dir": str(result.package.root_dir),
                    "temp_dir": temp_dir,
                    "label": result.package.name,
                    "detail": os.path.relpath(path, source),
                }
            return
        except Exception:
            pass

        if self._image_count >= self.image_limit:
            self._limited = True
            return
        temp_dir = tempfile.mkdtemp(prefix="lpk_preview_folder_unity_", dir=self.temp_root)
        self._temp_dirs.append(temp_dir)
        try:
            result = AssetStudioCLI().export_textures(path, temp_dir)
            images = [str(item) for item in result.exported_files] or _collect_preview_images(temp_dir)
        except Exception:
            return
        remaining = max(0, self.image_limit - self._image_count)
        for image in images[:remaining]:
            item = self._image_item(image, temp_dir)
            if item:
                item["source_path"] = os.path.abspath(path)
                item["source_dir"] = temp_dir
                item["temp_dir"] = temp_dir
                item["detail"] = os.path.join(os.path.relpath(path, source), os.path.basename(image))
                yield item

    @property
    def temp_dirs(self) -> list[str]:
        return list(self._temp_dirs)


class ImageZoomScrollArea(QScrollArea):
    zoomRequested = Signal(float, QPoint)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.horizontalScrollBar().installEventFilter(self)
        self.verticalScrollBar().installEventFilter(self)

    def wheelEvent(self, event):
        angle_delta = event.angleDelta().y()
        pixel_delta = event.pixelDelta().y()
        delta = angle_delta or pixel_delta
        if delta:
            steps = delta / (120.0 if angle_delta else 240.0)
            self.zoomRequested.emit(1.25 ** steps, event.position().toPoint())
        event.accept()

    def eventFilter(self, watched, event):
        if (
            event.type() == QEvent.Type.Wheel
            and watched in (self.horizontalScrollBar(), self.verticalScrollBar())
        ):
            event.accept()
            return True
        return super().eventFilter(watched, event)


class ImagePreviewPanel(QFrame):
    itemActivated = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image_paths = []
        self._preview_items = []
        self._current_index = 0
        self._thumb_size = 64
        self._fit_to_window = True
        self._zoom = 1.0
        self._current_pixmap = QPixmap()
        self._list_items = []
        self.title_label = BodyLabel("", self)
        self.main_image_label = QLabel(self)
        self.image_scroll = ImageZoomScrollArea(self)
        self.path_label = BodyLabel("", self)
        self.prev_btn = PushButton("", self)
        self.next_btn = PushButton("", self)
        self.fit_btn = PushButton("", self)
        self.actual_btn = PushButton("", self)
        self.zoom_out_btn = PushButton("", self)
        self.zoom_in_btn = PushButton("", self)
        self.list_title_label = BodyLabel("", self)
        self.limit_label = BodyLabel("", self)
        self.list_widget = QWidget(self)
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setContentsMargins(6, 6, 6, 6)
        self.list_layout.setSpacing(6)

        root_layout = QHBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(10)

        preview_layout = QVBoxLayout()
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(8)
        preview_layout.addWidget(self.title_label)

        self.main_image_label.setMinimumHeight(420)
        self.main_image_label.setAlignment(Qt.AlignCenter)
        self.main_image_label.setStyleSheet("background: transparent; color: #68707D;")
        self.image_scroll.setWidget(self.main_image_label)
        self.image_scroll.setWidgetResizable(False)
        self.image_scroll.setAlignment(Qt.AlignCenter)
        self.image_scroll.setFrameShape(QFrame.NoFrame)
        self.image_scroll.zoomRequested.connect(self.zoom_at_position)
        self.image_scroll.setStyleSheet("""
            QScrollArea {
                border: 1px solid #DDE2EA;
                border-radius: 8px;
                background: #FFFFFF;
            }
        """)
        preview_layout.addWidget(self.image_scroll, 1)

        nav_layout = QHBoxLayout()
        nav_layout.setSpacing(6)
        self.prev_btn.setIcon(FluentIcon.LEFT_ARROW)
        self.prev_btn.clicked.connect(self.show_previous)
        self.next_btn.setIcon(FluentIcon.RIGHT_ARROW)
        self.next_btn.clicked.connect(self.show_next)
        self.fit_btn.clicked.connect(self.fit_to_window)
        self.actual_btn.clicked.connect(self.show_actual_size)
        self.zoom_out_btn.clicked.connect(self.zoom_out)
        self.zoom_in_btn.clicked.connect(self.zoom_in)
        for button in (
            self.prev_btn,
            self.next_btn,
            self.fit_btn,
            self.actual_btn,
            self.zoom_out_btn,
            self.zoom_in_btn,
        ):
            button.setFixedHeight(32)
        self.path_label.setWordWrap(True)
        nav_layout.addWidget(self.prev_btn)
        nav_layout.addWidget(self.next_btn)
        nav_layout.addWidget(self.fit_btn)
        nav_layout.addWidget(self.actual_btn)
        nav_layout.addWidget(self.zoom_out_btn)
        nav_layout.addWidget(self.zoom_in_btn)
        nav_layout.addWidget(self.path_label, 1)
        preview_layout.addLayout(nav_layout)

        self.side_panel = QFrame(self)
        self.side_panel.setObjectName("imagePreviewSidePanel")
        self.side_panel.setMinimumWidth(210)
        self.side_panel.setMaximumWidth(270)
        self.side_panel.setStyleSheet("""
            QFrame#imagePreviewSidePanel {
                border: 1px solid #E3E6EA;
                border-radius: 8px;
                background: #FAFBFD;
            }
        """)
        side_layout = QVBoxLayout(self.side_panel)
        side_layout.setContentsMargins(10, 10, 10, 10)
        side_layout.setSpacing(8)
        self.limit_label.setWordWrap(True)
        self.limit_label.setStyleSheet("color: #68707D;")
        list_scroll = QScrollArea(self.side_panel)
        list_scroll.setWidgetResizable(True)
        list_scroll.setFrameShape(QFrame.NoFrame)
        list_scroll.setWidget(self.list_widget)
        side_layout.addWidget(self.list_title_label)
        side_layout.addWidget(self.limit_label)
        side_layout.addWidget(list_scroll, 1)

        root_layout.addLayout(preview_layout, 1)
        root_layout.addWidget(self.side_panel)
        self.setMinimumHeight(420)
        self.retranslate_ui()

    def retranslate_ui(self):
        self.fit_btn.setText(tr("preview.image_fit"))
        self.fit_btn.setToolTip(tr("preview.image_fit_tooltip"))
        self.actual_btn.setText(tr("preview.image_actual"))
        self.actual_btn.setToolTip(tr("preview.image_actual_tooltip"))
        self.zoom_out_btn.setText("-")
        self.zoom_out_btn.setToolTip(tr("preview.image_zoom_out_tooltip"))
        self.zoom_in_btn.setText("+")
        self.zoom_in_btn.setToolTip(tr("preview.image_zoom_in_tooltip"))
        self.prev_btn.setToolTip(tr("preview.image_previous_tooltip"))
        self.next_btn.setToolTip(tr("preview.image_next_tooltip"))
        self.list_title_label.setText(tr("preview.preview_item_list_title"))
        if self._preview_items:
            self.title_label.setText(tr("preview.preview_item_title", count=len(self._preview_items)))
            self.limit_label.setText(tr("preview.image_limited_note", count=len(self._image_paths)))
            self._show_current_item()

    def load_images(self, image_paths: list[str]):
        items = [
            {
                "kind": "image",
                "path": path,
                "source_path": path,
                "source_dir": os.path.dirname(path),
                "label": os.path.basename(path),
                "detail": path,
            }
            for path in image_paths
        ]
        self.load_items(items)

    def load_items(self, items: list[dict], current_index: int = 0):
        self._preview_items = [dict(item) for item in (items or [])]
        self._image_paths = [
            str(item.get("path", ""))
            for item in self._preview_items
            if item.get("kind") == "image"
        ]
        self._current_index = max(0, min(current_index, len(self._preview_items) - 1)) if self._preview_items else 0
        self._fit_to_window = True
        self._zoom = 1.0
        self.title_label.setText(tr("preview.preview_item_title", count=len(self._preview_items)))
        self.limit_label.setVisible(len(self._image_paths) >= 48)
        self.limit_label.setText(tr("preview.image_limited_note", count=len(self._image_paths)))
        self._populate_items()
        self._show_current_item()

    def append_item(self, item: dict):
        self._preview_items.append(dict(item))
        if item.get("kind") == "image":
            self._image_paths.append(str(item.get("path", "")))
        self.title_label.setText(tr("preview.preview_item_title", count=len(self._preview_items)))
        self.limit_label.setText(tr("preview.image_limited_note", count=len(self._image_paths)))
        self._add_item_widget(len(self._preview_items) - 1, self._preview_items[-1])
        self._update_list_styles()

    def current_item(self) -> dict | None:
        if not self._preview_items:
            return None
        if self._current_index < 0 or self._current_index >= len(self._preview_items):
            return None
        return self._preview_items[self._current_index]

    def show_model_placeholder(self, text: str = ""):
        self._current_pixmap = QPixmap()
        self.main_image_label.setPixmap(QPixmap())
        self.main_image_label.setText(text or tr("preview.model_stage_hint"))
        self.main_image_label.adjustSize()

    def preview_rect(self) -> dict | None:
        try:
            viewport = self.image_scroll.viewport()
            origin = viewport.mapToGlobal(QPoint(0, 0))
            rect = viewport.rect()
            return {
                "x": int(origin.x()),
                "y": int(origin.y()),
                "w": max(240, int(rect.width())),
                "h": max(240, int(rect.height())),
            }
        except Exception:
            return None

    def is_model_item_selected(self) -> bool:
        item = self.current_item()
        return bool(item and item.get("kind") == "model")

    def set_item_list_visible(self, visible: bool):
        self.side_panel.setVisible(bool(visible))

    def set_current_index(self, index: int, emit: bool = False):
        if not self._preview_items:
            return
        self._current_index = max(0, min(index, len(self._preview_items) - 1))
        self._fit_to_window = True
        self._zoom = 1.0
        self._show_current_item()
        if emit:
            self.itemActivated.emit(self._current_index)

    def _populate_items(self):
        self._clear_list()
        for index, item in enumerate(self._preview_items):
            self._add_item_widget(index, item)
        self.list_layout.addStretch(1)
        self._update_list_styles()

    def _add_item_widget(self, index: int, item: dict):
        frame = QFrame(self.list_widget)
        frame.setObjectName("imagePreviewListItem")
        frame.setToolTip(str(item.get("path") or item.get("source_path") or ""))
        item_layout = QHBoxLayout(frame)
        item_layout.setContentsMargins(6, 6, 6, 6)
        item_layout.setSpacing(8)

        thumb = QLabel(frame)
        thumb.setFixedSize(self._thumb_size, self._thumb_size)
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setStyleSheet("""
            QLabel {
                border: 1px solid #E3E6EA;
                border-radius: 6px;
                background: #FFFFFF;
                color: #68707D;
                font-weight: 600;
            }
        """)
        if item.get("kind") == "model":
            thumb.setText("L2D")
        else:
            image_path = str(item.get("path", ""))
            pixmap = QPixmap(image_path)
            if pixmap.isNull():
                thumb.setText(os.path.splitext(os.path.basename(image_path))[1].lstrip(".").upper() or "?")
            else:
                thumb.setPixmap(
                    pixmap.scaled(
                        self._thumb_size - 8,
                        self._thumb_size - 8,
                        Qt.KeepAspectRatio,
                        Qt.SmoothTransformation,
                    )
                )

        text_box = QWidget(frame)
        text_layout = QVBoxLayout(text_box)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)
        type_text = tr("preview.preview_item_model") if item.get("kind") == "model" else tr("preview.preview_item_image")
        type_label = BodyLabel(type_text, text_box)
        type_label.setStyleSheet("color: #00A6B3; font-weight: 600;")
        name_label = BodyLabel(str(item.get("label") or os.path.basename(str(item.get("path", "")))), text_box)
        name_label.setWordWrap(True)
        detail = str(item.get("detail") or "")
        detail_label = BodyLabel(detail, text_box)
        detail_label.setWordWrap(True)
        detail_label.setStyleSheet("color: #68707D;")
        text_layout.addWidget(type_label)
        text_layout.addWidget(name_label)
        if detail:
            text_layout.addWidget(detail_label)

        item_layout.addWidget(thumb)
        item_layout.addWidget(text_box, 1)
        frame.mousePressEvent = lambda _event, i=index: self.select_image(i)
        thumb.mousePressEvent = lambda _event, i=index: self.select_image(i)
        text_box.mousePressEvent = lambda _event, i=index: self.select_image(i)

        stretch_index = self.list_layout.count() - 1
        last_item = self.list_layout.itemAt(stretch_index) if stretch_index >= 0 else None
        if last_item and last_item.spacerItem():
            self.list_layout.insertWidget(stretch_index, frame)
        else:
            self.list_layout.addWidget(frame)
        self._list_items.append(frame)

    def _populate_images(self):
        self._populate_items()

    def clear(self):
        self._image_paths = []
        self._preview_items = []
        self._current_index = 0
        self._fit_to_window = True
        self._zoom = 1.0
        self._current_pixmap = QPixmap()
        self._clear_list()
        self.title_label.clear()
        self.path_label.clear()
        self.limit_label.clear()
        self.main_image_label.clear()

    def _clear_list(self):
        self._list_items = []
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        item = self.current_item()
        if item and item.get("kind") == "image" and self._fit_to_window:
            self._render_current_pixmap()

    def select_image(self, index: int):
        if not self._preview_items:
            return
        self._current_index = max(0, min(index, len(self._preview_items) - 1))
        self._fit_to_window = True
        self._zoom = 1.0
        self._show_current_item()
        self.itemActivated.emit(self._current_index)

    def show_previous(self):
        if not self._preview_items:
            return
        self.select_image((self._current_index - 1) % len(self._preview_items))

    def show_next(self):
        if not self._preview_items:
            return
        self.select_image((self._current_index + 1) % len(self._preview_items))

    def fit_to_window(self):
        if self._current_pixmap.isNull():
            return
        self._fit_to_window = True
        self._render_current_pixmap()

    def show_actual_size(self):
        if self._current_pixmap.isNull():
            return
        self._fit_to_window = False
        self._zoom = 1.0
        self._render_current_pixmap()

    def zoom_in(self):
        self._set_zoom(self._displayed_zoom() * 1.25)

    def zoom_out(self):
        self._set_zoom(self._displayed_zoom() / 1.25)

    def zoom_at_position(self, factor: float, viewport_pos: QPoint):
        if self._current_pixmap.isNull() or factor <= 0:
            return

        label_pos = self.main_image_label.mapFrom(self.image_scroll.viewport(), viewport_pos)
        old_width = max(1, self.main_image_label.width())
        old_height = max(1, self.main_image_label.height())
        rel_x = min(1.0, max(0.0, label_pos.x() / old_width))
        rel_y = min(1.0, max(0.0, label_pos.y() / old_height))

        self._set_zoom(self._displayed_zoom() * factor)

        new_width = max(1, self.main_image_label.width())
        new_height = max(1, self.main_image_label.height())
        self.image_scroll.horizontalScrollBar().setValue(int(rel_x * new_width - viewport_pos.x()))
        self.image_scroll.verticalScrollBar().setValue(int(rel_y * new_height - viewport_pos.y()))

    def _set_zoom(self, zoom: float):
        if self._current_pixmap.isNull():
            return
        self._fit_to_window = False
        self._zoom = max(0.1, min(6.0, zoom))
        self._render_current_pixmap()

    def _displayed_zoom(self) -> float:
        if self._current_pixmap.isNull():
            return self._zoom
        displayed_pixmap = self.main_image_label.pixmap()
        if self._fit_to_window and displayed_pixmap is not None and not displayed_pixmap.isNull():
            return displayed_pixmap.width() / max(1, self._current_pixmap.width())
        return self._zoom

    def _show_current_item(self):
        item = self.current_item()
        if not item:
            self.main_image_label.setText("")
            return
        if item.get("kind") == "model":
            self.set_item_list_visible(False)
            self.path_label.setText(
                tr(
                    "preview.model_current_details",
                    index=self._current_index + 1,
                    total=len(self._preview_items),
                    file=str(item.get("label") or os.path.basename(str(item.get("path", "")))),
                )
            )
            self.show_model_placeholder()
            self._update_list_styles()
            return
        self.set_item_list_visible(True)
        self._show_current_image()

    def _show_current_image(self):
        item = self.current_item()
        if not item or item.get("kind") != "image":
            self.main_image_label.setText("")
            return
        path = str(item.get("path", ""))
        filename = os.path.basename(path)
        self._current_pixmap = QPixmap(path)
        if self._current_pixmap.isNull():
            self.path_label.setText(
                tr(
                    "preview.image_current_invalid",
                    index=self._current_index + 1,
                    total=len(self._preview_items),
                    file=filename,
                )
            )
            self.main_image_label.setPixmap(QPixmap())
            self.main_image_label.setText(filename)
            self.main_image_label.adjustSize()
            self._update_list_styles()
            return

        self.path_label.setText(
            tr(
                "preview.image_current_details",
                index=self._current_index + 1,
                total=len(self._preview_items),
                width=self._current_pixmap.width(),
                height=self._current_pixmap.height(),
                file=filename,
            )
        )
        self._render_current_pixmap()
        self._update_list_styles()

    def _render_current_pixmap(self):
        if self._current_pixmap.isNull():
            return
        if self._fit_to_window:
            max_size = self.image_scroll.viewport().size()
            pixmap = self._current_pixmap.scaled(
                max(1, max_size.width() - 24),
                max(1, max_size.height() - 24),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        else:
            pixmap = self._current_pixmap.scaled(
                max(1, int(self._current_pixmap.width() * self._zoom)),
                max(1, int(self._current_pixmap.height() * self._zoom)),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        self.main_image_label.setPixmap(pixmap)
        self.main_image_label.resize(pixmap.size())

    def _update_list_styles(self):
        for index, item in enumerate(self._list_items):
            if index == self._current_index:
                item.setStyleSheet("""
                    QFrame#imagePreviewListItem {
                        border: 1px solid #00A6B3;
                        border-radius: 8px;
                        background: #EFFBFC;
                    }
                """)
            else:
                item.setStyleSheet("""
                    QFrame#imagePreviewListItem {
                        border: 1px solid transparent;
                        border-radius: 8px;
                        background: transparent;
                    }
                    QFrame#imagePreviewListItem:hover {
                        border-color: #D0D7E2;
                        background: #FFFFFF;
                    }
                """)


class Live2DSettingsPanel(QFrame):
    """Live2D设置面板"""

    settingsChanged = Signal(dict)
    requestRefreshParams = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

        self.preview_window = None

        self.width_spinbox = None
        self.height_spinbox = None
        self.opacity_label = None
        self.opacity_slider = None
        self.show_controls_check = None

        self.rotation_label = None
        self.rotation_slider = None
        self.position_x_spinbox = None
        self.position_y_spinbox = None
        self.bg_transparent_check = None
        # 按钮与颜色展示
        self.bg_color_btn = None
        self.bg_color_preview = None
        self.selected_bg_color = QColor(255, 255, 255)

        self.mouse_tracking_check = None
        self.auto_blink_check = None
        self.auto_breath_check = None
        self.sensitivity_label = None
        self.sensitivity_slider = None

        # 高级参数控件（动态）
        self.advanced_enable_check = None
        self.advanced_param_sliders = {}  # id -> (slider, label, scale)
        self.PARAM_SPECS = []
        self.param_specs_by_id = {}  # id -> spec dict
        self.advanced_group = None
        self.adv_params_container = None
        self.adv_params_container_layout = None

        self.window_group_title = None
        self.window_size_label = None
        self.width_label = None
        self.height_label = None
        self.opacity_text_label = None
        self.model_group_title = None
        self.rotation_text_label = None
        self.background_label = None
        self.interaction_group_title = None
        self.advanced_group_title = None
        self.refresh_adv_btn = None
        self.reset_adv_btn = None

        self.setupUI()

    def setupUI(self):
        """设置面板UI"""
        self.setMinimumWidth(300)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 0, 0, 0)
        layout.setSpacing(10)

        # 创建滚动区域
        scroll = SingleDirectionScrollArea(orient=Qt.Vertical)
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(10)

        # 窗口设置组
        window_group = self.create_window_settings_group()
        scroll_layout.addWidget(window_group)

        # 模型设置组
        model_group = self.create_model_settings_group()
        scroll_layout.addWidget(model_group)

        # 交互设置组
        interaction_group = self.create_interaction_settings_group()
        scroll_layout.addWidget(interaction_group)

        # 高级设置组（动态构建）
        advanced_group = self.create_advanced_settings_group()
        self.advanced_group = advanced_group
        scroll_layout.addWidget(advanced_group)

        # 添加弹性空间
        scroll_layout.addStretch()

        scroll.setWidget(scroll_widget)
        scroll.setWidgetResizable(True)
        scroll.enableTransparentBackground()
        layout.addWidget(scroll)
        self.retranslate_ui()

    def create_window_settings_group(self):
        """创建窗口设置组"""
        group = CardWidget(self)
        layout = QVBoxLayout(group)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # 组标题
        self.window_group_title = SubtitleLabel("", group)
        layout.addWidget(self.window_group_title)

        # 窗口大小设置
        size_layout = QGridLayout()
        size_layout.setHorizontalSpacing(8)
        size_layout.setVerticalSpacing(8)
        self.window_size_label = BodyLabel("", group)
        size_layout.addWidget(self.window_size_label, 0, 0, 1, 4)

        self.width_spinbox = SpinBox(group)
        self.width_spinbox.setRange(240, 2560)
        self.width_spinbox.setValue(720)
        self.width_spinbox.setSuffix(" px")
        self.width_spinbox.setMinimumWidth(112)

        self.width_label = BodyLabel("", group)
        size_layout.addWidget(self.width_label, 1, 0)
        size_layout.addWidget(self.width_spinbox, 1, 1)

        self.height_spinbox = SpinBox(group)
        self.height_spinbox.setRange(240, 1600)
        self.height_spinbox.setValue(720)
        self.height_spinbox.setSuffix(" px")
        self.height_spinbox.setMinimumWidth(112)

        self.height_label = BodyLabel("", group)
        size_layout.addWidget(self.height_label, 1, 2)
        size_layout.addWidget(self.height_spinbox, 1, 3)

        layout.addLayout(size_layout)
        for widget in (
            self.window_size_label,
            self.width_label,
            self.width_spinbox,
            self.height_label,
            self.height_spinbox,
        ):
            widget.setVisible(False)

        # 模型透明度
        opacity_layout = QHBoxLayout()
        self.opacity_text_label = BodyLabel("", group)
        opacity_layout.addWidget(self.opacity_text_label)

        self.opacity_slider = Slider(Qt.Horizontal, group)
        self.opacity_slider.setRange(10, 100)
        self.opacity_slider.setValue(100)

        self.opacity_label = BodyLabel("100%", group)
        self.opacity_label.setMinimumWidth(40)

        self.opacity_slider.valueChanged.connect(
            lambda v: self.opacity_label.setText(f"{v}%")
        )
        # 实时应用设置
        self.opacity_slider.valueChanged.connect(lambda _: self._emit_settings())

        opacity_layout.addWidget(self.opacity_slider)
        opacity_layout.addWidget(self.opacity_label)

        layout.addLayout(opacity_layout)

        self.show_controls_check = CheckBox("", group)
        layout.addWidget(self.show_controls_check)

        # 尺寸变化时也应用
        self.width_spinbox.valueChanged.connect(lambda _: self._emit_settings())
        self.height_spinbox.valueChanged.connect(lambda _: self._emit_settings())

        return group

    def create_model_settings_group(self):
        """创建模型设置组"""
        group = CardWidget(self)
        layout = QVBoxLayout(group)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # 组标题
        self.model_group_title = SubtitleLabel("", group)
        layout.addWidget(self.model_group_title)

        # 模型旋转
        rotation_layout = QHBoxLayout()
        self.rotation_text_label = BodyLabel("", group)
        rotation_layout.addWidget(self.rotation_text_label)

        self.rotation_slider = Slider(Qt.Horizontal, group)
        self.rotation_slider.setRange(0, 360)
        self.rotation_slider.setValue(0)

        self.rotation_label = BodyLabel("0°", group)
        self.rotation_label.setMinimumWidth(40)

        self.rotation_slider.valueChanged.connect(
            lambda v: self.rotation_label.setText(f"{v}°")
        )
        # 实时应用旋转
        self.rotation_slider.valueChanged.connect(lambda _: self._emit_settings())

        rotation_layout.addWidget(self.rotation_slider)
        rotation_layout.addWidget(self.rotation_label)

        layout.addLayout(rotation_layout)

        # 背景设置
        bg_layout = QHBoxLayout()
        self.background_label = BodyLabel("", group)
        bg_layout.addWidget(self.background_label)

        self.bg_transparent_check = CheckBox("", group)
        self.bg_transparent_check.setChecked(True)
        bg_layout.addWidget(self.bg_transparent_check)

        # 颜色选择按钮
        self.bg_color_btn = PushButton("", group)
        self.bg_color_btn.setEnabled(False)
        self.bg_color_btn.clicked.connect(self.open_color_dialog)
        bg_layout.addWidget(self.bg_color_btn)

        # 颜色预览块
        self.bg_color_preview = QFrame(group)
        self.bg_color_preview.setFixedSize(24, 24)
        self.bg_color_preview.setStyleSheet(
            f"QFrame{{border:1px solid #ccc; border-radius:4px; background:{self.selected_bg_color.name()};}}"
        )
        bg_layout.addWidget(self.bg_color_preview)

        # 连接透明背景选择框
        self.bg_transparent_check.toggled.connect(
            lambda checked: self.bg_color_btn.setEnabled(not checked)
        )
        # 透明背景切换时也应用设置
        self.bg_transparent_check.toggled.connect(lambda _: self._emit_settings())

        bg_layout.addStretch()
        layout.addLayout(bg_layout)

        return group

    def create_interaction_settings_group(self):
        """创建交互设置组"""
        group = CardWidget(self)
        layout = QVBoxLayout(group)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # 组标题
        self.interaction_group_title = SubtitleLabel("", group)
        layout.addWidget(self.interaction_group_title)

        # 交互选项
        self.mouse_tracking_check = CheckBox("", group)
        self.mouse_tracking_check.setChecked(True)
        self.mouse_tracking_check.clicked.connect(lambda _: self._emit_settings())
        layout.addWidget(self.mouse_tracking_check)

        self.auto_blink_check = CheckBox("", group)
        self.auto_blink_check.setChecked(True)
        self.auto_blink_check.clicked.connect(lambda _: self._emit_settings())
        layout.addWidget(self.auto_blink_check)

        self.auto_breath_check = CheckBox("", group)
        self.auto_breath_check.setChecked(True)
        self.auto_breath_check.clicked.connect(lambda _: self._emit_settings())
        layout.addWidget(self.auto_breath_check)

        return group

    def create_advanced_settings_group(self):
        """创建高级设置组的容器；具体参数根据模型动态生成"""
        group = CardWidget(self)
        layout = QVBoxLayout(group)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self.advanced_group_title = SubtitleLabel("", group)
        layout.addWidget(self.advanced_group_title)

        self.advanced_enable_check = CheckBox("", group)
        self.advanced_enable_check.setChecked(False)
        self.advanced_enable_check.toggled.connect(lambda _: self._emit_settings())
        layout.addWidget(self.advanced_enable_check)

        # 容器用于放置动态参数滑条
        self.adv_params_container = QWidget(group)
        self.adv_params_container_layout = QVBoxLayout(self.adv_params_container)
        self.adv_params_container_layout.setContentsMargins(0, 0, 0, 0)
        self.adv_params_container_layout.setSpacing(8)
        layout.addWidget(self.adv_params_container)

        # Buttons row (adv params)
        btns_layout = QHBoxLayout()
        self.refresh_adv_btn = PushButton("", group)
        self.refresh_adv_btn.clicked.connect(self.requestRefreshParams.emit)
        self.reset_adv_btn = PushButton("", group)
        self.reset_adv_btn.clicked.connect(self.reset_advanced_params)
        btns_layout.addWidget(self.refresh_adv_btn)
        btns_layout.addWidget(self.reset_adv_btn)
        btns_layout.addStretch()
        layout.addLayout(btns_layout)

        return group

    def retranslate_ui(self):
        if self.window_group_title:
            self.window_group_title.setText(tr("preview.window_settings"))
        if self.window_size_label:
            self.window_size_label.setText(tr("preview.window_size"))
        if self.width_label:
            self.width_label.setText(tr("preview.width_short"))
        if self.height_label:
            self.height_label.setText(tr("preview.height_short"))
        if self.opacity_text_label:
            self.opacity_text_label.setText(tr("preview.opacity"))
        if self.show_controls_check:
            self.show_controls_check.setText(tr("preview.show_control_panel"))

        if self.model_group_title:
            self.model_group_title.setText(tr("preview.model_display_settings"))
        if self.rotation_text_label:
            self.rotation_text_label.setText(tr("preview.model_rotation"))
        if self.background_label:
            self.background_label.setText(tr("preview.background"))
        if self.bg_transparent_check:
            self.bg_transparent_check.setText(tr("preview.transparent"))
        if self.bg_color_btn:
            self.bg_color_btn.setText(tr("preview.select_color"))

        if self.interaction_group_title:
            self.interaction_group_title.setText(tr("preview.interaction_settings"))
        if self.mouse_tracking_check:
            self.mouse_tracking_check.setText(tr("preview.enable_mouse_tracking"))
        if self.auto_blink_check:
            self.auto_blink_check.setText(tr("preview.enable_auto_blink"))
        if self.auto_breath_check:
            self.auto_breath_check.setText(tr("preview.enable_auto_breath"))

        if self.advanced_group_title:
            self.advanced_group_title.setText(tr("preview.advanced_settings"))
        if self.advanced_enable_check:
            self.advanced_enable_check.setText(tr("preview.enable_advanced_overrides"))
        if self.refresh_adv_btn:
            self.refresh_adv_btn.setText(tr("preview.refresh_current_model"))
        if self.reset_adv_btn:
            self.reset_adv_btn.setText(tr("preview.reset_advanced_params"))

    def _emit_settings(self):
        try:
            self.settingsChanged.emit(self.get_settings())
        except Exception:
            pass

    def reset_advanced_params(self):
        """将高级参数重置为当前模型的默认值"""
        for spec in self.PARAM_SPECS:
            sid = spec['id']
            if sid in self.advanced_param_sliders:
                slider, _label, scale = self.advanced_param_sliders[sid]
                dv = float(spec.get('default', 0.0))
                dv = max(spec.get('min', dv), min(spec.get('max', dv), dv))
                slider.setValue(int(round(dv * scale)))

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            child = item.layout()
            if child is not None:
                self._clear_layout(child)

    def rebuild_advanced_params(self, meta_list: list):
        """依据模型枚举到的参数元数据动态重建高级设置UI.
        meta: list of {id, type, value, min, max, default}
        尽可能保留用户当前已设定的值。
        """
        # 尽可能保留用户当前已设定的值
        prev_values = {}
        for pid, (slider, _lbl, scale) in self.advanced_param_sliders.items():
            prev_values[pid] = slider.value() / float(scale)

        # 清理旧控件
        self._clear_layout(self.adv_params_container_layout)
        self.advanced_param_sliders.clear()
        self.PARAM_SPECS = []
        self.param_specs_by_id.clear()

        # 缩放决定函数
        def decide_scale(vmin, vmax):
            rng = max(vmax, vmin) - min(vmax, vmin)
            if rng <= 2.0:
                return 100
            return 1

        # 构造控件，按id字母序排列以保持一致性
        for p in sorted(meta_list, key=lambda x: str(x.get('id', ''))):
            pid = str(p.get('id', ''))
            pmin = float(p.get('min', 0.0))
            pmax = float(p.get('max', 1.0))
            pdef = float(p.get('default', 0.0))+0.0
            pval = float(p.get('value', pdef))
            scale = decide_scale(pmin, pmax)
            spec = {
                'label': pid,
                'id': pid,
                'min': pmin,
                'max': pmax,
                'default': pdef,
                'scale': scale,
            }
            self.PARAM_SPECS.append(spec)
            self.param_specs_by_id[pid] = spec

            row = QHBoxLayout()
            name_label = BodyLabel(f"{pid}:", self.adv_params_container)
            row.addWidget(name_label)

            slider = Slider(Qt.Horizontal, self.adv_params_container)
            s_min = int(round(pmin * scale))
            s_max = int(round(pmax * scale))
            # Ensure s_min <= s_max
            if s_min > s_max:
                s_min, s_max = s_max, s_min
            slider.setRange(s_min, s_max)

            # 初始值：如果存在则保留之前的值，否则使用模型默认值/当前值
            init_val = prev_values.get(pid, pval)
            init_val = max(pmin, min(pmax, init_val))
            slider.setValue(int(round(init_val * scale)))

            val_label = BodyLabel(f"{init_val:.2f}" if scale != 1 else f"{int(round(init_val))}", self.adv_params_container)
            val_label.setMinimumWidth(80)

            def make_on_change(lbl, scale_factor):
                return lambda v: lbl.setText(f"{v/scale_factor:.2f}") if scale_factor != 1 else lbl.setText(f"{v}")

            slider.valueChanged.connect(make_on_change(val_label, scale))
            slider.valueChanged.connect(lambda _: self._emit_settings())

            row.addWidget(slider)
            row.addWidget(val_label)
            row.addStretch()

            self.adv_params_container_layout.addLayout(row)
            self.advanced_param_sliders[pid] = (slider, val_label, scale)

    def get_settings(self):
        """获取当前设置"""
        settings = {
            'window_size': (self.width_spinbox.value(), self.height_spinbox.value()),
            'opacity': self.opacity_slider.value() / 100.0,
            'show_controls': self.show_controls_check.isChecked(),
            'model_rotation': self.rotation_slider.value(),
            'transparent_bg': self.bg_transparent_check.isChecked(),
            'bg_color': self.selected_bg_color,
            'mouse_tracking': self.mouse_tracking_check.isChecked(),
            'auto_blink': self.auto_blink_check.isChecked(),
            'auto_breath': self.auto_breath_check.isChecked(),
        }
        # 高级参数
        adv_enabled = bool(self.advanced_enable_check.isChecked()) if self.advanced_enable_check else False
        settings['advanced_enabled'] = adv_enabled
        if adv_enabled:
            advanced_params = {}
            for pid, (slider, _label, scale) in self.advanced_param_sliders.items():
                spec = self.param_specs_by_id.get(pid, None)
                val = slider.value() / float(scale)
                if spec:
                    v = max(spec['min'], min(spec['max'], val))
                else:
                    v = val
                advanced_params[pid] = v
            settings['advanced_params'] = advanced_params
        else:
            settings['advanced_params'] = {}
        return settings

    def open_color_dialog(self):
        """使用 qfluentwidgets 的 ColorDialog 选择背景颜色，并实时应用"""
        current = self.selected_bg_color if isinstance(self.selected_bg_color, QColor) else QColor(255, 255, 255)
        try:
            dlg = ColorDialog(current, tr("dialog.choose_background_color"), self, enableAlpha=False)
        except TypeError:
            dlg = ColorDialog(current, tr("dialog.choose_background_color"), self)
        def on_color_changed(color: QColor):
            if isinstance(color, QColor) and color.isValid():
                self.selected_bg_color = color
                try:
                    self.bg_color_preview.setStyleSheet(
                        f"QFrame{{border:1px solid #ccc; border-radius:4px; background:{color.name()};}}"
                    )
                except Exception:
                    pass
                self._emit_settings()
        try:
            dlg.colorChanged.connect(on_color_changed)
        except Exception:
            pass
        try:
            dlg.exec()
        except Exception:
            pass

class PreviewPage(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings_panel = None
        self.preview_btn = None
        self.close_all_btn = None
        self.model_info_text_box = None
        self.image_preview_panel = None
        self.preview_stage_title = None
        self.preview_stage = None
        self.preview_stage_layout = None
        self.preview_stage_close_btn = None
        self.export_preview_btn = None
        self.preview_dock_area = None
        self.preview_dock_layout = None
        self.preview_placeholder = None
        self.motion_group_title = None
        self.motion_group = None
        self.motion_combo = None
        self.play_motion_btn = None
        self.motion_hint_label = None
        self._motion_items = []
        self.drag_drop_area = None
        self.source_label = None
        self.source_edit = None
        self.source_file_btn = None
        self.source_folder_btn = None
        self.image_limit_label = None
        self.image_limit_spinbox = None
        self.title_label = None
        self.main_layout = None
        self.current_model_path = None
        self.setObjectName('previewPage')
        self.i18n = get_i18n()
        self.settings_manager = SettingsManager()
        self.preview_process = None
        # 新增：预览按钮冷却
        self._preview_cooldown_timer = None
        self._preview_cooldown_ms = 1500  # 冷却时长（毫秒）
        self._preview_process_poll_timer = None
        self._preview_dock_timer = None
        self._last_preview_dock_rect = None
        # 新增：记录临时美化的 model json 文件（在新文件载入时清理）
        self._temp_model_json_path = None
        self._model_preview_thread = None
        self._model_preview_temp_dirs = []
        self._image_preview_thread = None
        self._image_preview_temp_dirs = []
        self._archive_preview_thread = None
        self._archive_preview_temp_dirs = []
        self._folder_preview_thread = None
        self._folder_preview_temp_dirs = []
        self._preview_items = []
        self._auto_selected_model_from_scan = False
        self._preview_export_thread = None
        self._preview_export_kind = None
        self._preview_export_source_dir = None
        self._preview_export_files = []
        self._preview_export_label = ""
        self._pending_unity_preview_source_path = None

        self.setupUI()
        self.retranslate_ui()
        self.i18n.languageChanged.connect(self.retranslate_ui)
        # 应用退出前做一次兜底清理，防止文件句柄未及时释放
        try:
            app = QCoreApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(self._terminate_preview_process)
                app.aboutToQuit.connect(self._cleanup_temp_model_json)
                app.aboutToQuit.connect(self._cleanup_model_preview_temp_dirs)
                app.aboutToQuit.connect(self._cleanup_image_preview_temp_dirs)
                app.aboutToQuit.connect(self._cleanup_archive_preview_temp_dirs)
                app.aboutToQuit.connect(self._cleanup_folder_preview_temp_dirs)
        except Exception:
            pass

    def setupUI(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(20, 18, 20, 20)
        self.main_layout.setSpacing(12)

        # 标题
        self.title_label = SubtitleLabel("", self)
        self.main_layout.addWidget(self.title_label)

        # 创建分割器
        splitter = QSplitter(Qt.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        splitter.setOpaqueResize(True)
        splitter.setHandleWidth(8)
        splitter.setStyleSheet("""
            QSplitter::handle {
                background: #E6EAF0;
                border-radius: 3px;
            }
            QSplitter::handle:hover {
                background: #BFC7D4;
            }
        """)

        # 左侧：导入、设置和控制按钮
        left_widget = QWidget()
        left_widget.setMinimumWidth(320)
        left_widget.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 10, 12, 0)
        left_layout.setSpacing(12)

        import_card = CardWidget(self)
        import_layout = QVBoxLayout(import_card)
        import_layout.setContentsMargins(12, 12, 12, 12)
        import_layout.setSpacing(8)

        import_label = BodyLabel("", import_card)
        import_label.setObjectName("previewSourceLabel")
        import_layout.addWidget(import_label)
        self.source_label = import_label

        self.source_edit = PreviewPathLineEdit(import_card)
        self.source_edit.setReadOnly(True)
        self.source_edit.pathsDropped.connect(self.on_file_dropped)
        self.source_edit.setMinimumHeight(34)
        import_layout.addWidget(self.source_edit)

        source_button_row = QHBoxLayout()
        source_button_row.setSpacing(8)
        self.source_file_btn = PushButton("", import_card)
        self.source_file_btn.setIcon(FluentIcon.FOLDER)
        self.source_file_btn.clicked.connect(self.browse_preview_file)
        self.source_folder_btn = PushButton("", import_card)
        self.source_folder_btn.setIcon(FluentIcon.FOLDER_ADD)
        self.source_folder_btn.clicked.connect(self.browse_preview_folder)
        source_button_row.addWidget(self.source_file_btn)
        source_button_row.addWidget(self.source_folder_btn)
        import_layout.addLayout(source_button_row)

        image_limit_row = QHBoxLayout()
        image_limit_row.setSpacing(8)
        self.image_limit_label = BodyLabel("", import_card)
        self.image_limit_spinbox = SpinBox(import_card)
        self.image_limit_spinbox.setRange(1, 500)
        self.image_limit_spinbox.setValue(int(self.settings_manager.get("preview.image_limit", 48) or 48))
        self.image_limit_spinbox.valueChanged.connect(self.on_preview_image_limit_changed)
        image_limit_row.addWidget(self.image_limit_label)
        image_limit_row.addWidget(self.image_limit_spinbox)
        image_limit_row.addStretch(1)
        import_layout.addLayout(image_limit_row)
        left_layout.addWidget(import_card)

        # 当前模型信息
        self.model_info_text_box = TextBrowser(self)
        self.model_info_text_box.setMinimumHeight(120)
        self.model_info_text_box.setMaximumHeight(170)
        self.model_info_text_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.model_info_text_box.setStyleSheet("""
            TextBrowser {
                border: 1px solid #E3E6EA;
                border-radius: 8px;
                background: #FFFFFF;
                padding: 8px;
            }
        """)

        left_layout.addWidget(self.model_info_text_box)

        self.motion_group = CardWidget(self)
        self.motion_group.setVisible(False)
        motion_layout = QVBoxLayout(self.motion_group)
        motion_layout.setContentsMargins(12, 12, 12, 12)
        motion_layout.setSpacing(8)
        self.motion_group_title = SubtitleLabel("", self.motion_group)
        motion_layout.addWidget(self.motion_group_title)
        motion_row = QHBoxLayout()
        motion_row.setSpacing(8)
        self.motion_combo = ComboBox(self.motion_group)
        self.motion_combo.setMinimumWidth(180)
        self.play_motion_btn = PushButton("", self.motion_group)
        self.play_motion_btn.setIcon(FluentIcon.PLAY)
        self.play_motion_btn.clicked.connect(self.play_selected_motion)
        motion_row.addWidget(self.motion_combo, 1)
        motion_row.addWidget(self.play_motion_btn)
        motion_layout.addLayout(motion_row)
        self.motion_hint_label = BodyLabel("", self.motion_group)
        self.motion_hint_label.setWordWrap(True)
        motion_layout.addWidget(self.motion_hint_label)
        left_layout.addWidget(self.motion_group)

        # 设置面板
        self.settings_panel = Live2DSettingsPanel(self)
        self.settings_panel.settingsChanged.connect(self.on_settings_changed)
        self.settings_panel.requestRefreshParams.connect(self.on_request_refresh_params)
        left_layout.addWidget(self.settings_panel, 1)

        # 控制按钮区域
        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)
        self.preview_btn = PushButton("", self)
        self.preview_btn.setIcon(FluentIcon.PLAY)
        self.preview_btn.setEnabled(False)
        # 修改：接入冷却逻辑
        self.preview_btn.clicked.connect(self._on_preview_clicked)

        self.close_all_btn = PushButton("", self)
        self.close_all_btn.setIcon(FluentIcon.CLOSE)
        self.close_all_btn.clicked.connect(self.close_preview_window)

        button_layout.addWidget(self.preview_btn, 1)
        button_layout.addWidget(self.close_all_btn, 1)

        left_layout.addLayout(button_layout)

        # 右侧：大预览舞台
        right_widget = QWidget()
        right_widget.setMinimumWidth(520)
        right_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(12, 10, 0, 0)
        right_layout.setSpacing(10)

        self.preview_stage_title = SubtitleLabel("", self)
        right_layout.addWidget(self.preview_stage_title)

        self.preview_stage = QFrame(self)
        self.preview_stage.setObjectName("previewStage")
        self.preview_stage.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview_stage.setStyleSheet("""
            QFrame#previewStage {
                border: 1px solid #DDE2EA;
                border-radius: 8px;
                background: #FAFBFD;
            }
        """)
        self.preview_stage_layout = QVBoxLayout(self.preview_stage)
        self.preview_stage_layout.setContentsMargins(12, 10, 12, 12)
        self.preview_stage_layout.setSpacing(8)

        stage_toolbar = QHBoxLayout()
        stage_toolbar.setContentsMargins(0, 0, 0, 0)
        stage_toolbar.setSpacing(8)
        stage_toolbar.addStretch(1)

        self.export_preview_btn = PushButton("", self.preview_stage)
        self.export_preview_btn.setIcon(FluentIcon.DOWNLOAD)
        self.export_preview_btn.setEnabled(False)
        self.export_preview_btn.clicked.connect(self.export_preview_resources)
        stage_toolbar.addWidget(self.export_preview_btn, 0, Qt.AlignRight)

        self.preview_stage_close_btn = PushButton("", self.preview_stage)
        self.preview_stage_close_btn.setText("X")
        self.preview_stage_close_btn.setFixedSize(34, 30)
        self.preview_stage_close_btn.setStyleSheet("""
            PushButton {
                border: 1px solid #D0D7E2;
                border-radius: 6px;
                background: #FFFFFF;
                color: #31363F;
                font-size: 18px;
                font-weight: 600;
            }
            PushButton:hover {
                border-color: #F1A7A7;
                background: #FDEBEC;
                color: #C42B1C;
            }
        """)
        self.preview_stage_close_btn.clicked.connect(self.close_preview_window)
        stage_toolbar.addWidget(self.preview_stage_close_btn, 0, Qt.AlignRight)
        self.preview_stage_layout.addLayout(stage_toolbar)

        self.preview_dock_area = QFrame(self.preview_stage)
        self.preview_dock_area.setObjectName("previewDockArea")
        self.preview_dock_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview_dock_area.setStyleSheet("""
            QFrame#previewDockArea {
                border: none;
                background: transparent;
            }
        """)
        self.preview_dock_layout = QVBoxLayout(self.preview_dock_area)
        self.preview_dock_layout.setContentsMargins(0, 0, 0, 0)
        self.preview_dock_layout.setSpacing(0)

        self.preview_placeholder = BodyLabel("", self.preview_dock_area)
        self.preview_placeholder.setAlignment(Qt.AlignCenter)
        self.preview_placeholder.setStyleSheet("color: #68707D;")
        self.preview_dock_layout.addWidget(self.preview_placeholder, 1)

        self.image_preview_panel = ImagePreviewPanel(self.preview_dock_area)
        self.image_preview_panel.setVisible(False)
        self.image_preview_panel.itemActivated.connect(self.on_preview_item_activated)
        self.preview_dock_layout.addWidget(self.image_preview_panel, 1)
        self.preview_stage_layout.addWidget(self.preview_dock_area, 1)

        right_layout.addWidget(self.preview_stage, 1)

        # 添加到分割器
        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([380, 1040])  # 设置初始比例

        self.main_layout.addWidget(splitter, 1)
        self.main_layout.setStretch(0, 0)
        self.main_layout.setStretch(1, 1)

        # 当前模型路径
        self.current_model_path = None

        # 新增：初始化冷却计时器
        self._preview_cooldown_timer = QTimer(self)
        self._preview_cooldown_timer.setSingleShot(True)
        self._preview_cooldown_timer.timeout.connect(self._on_preview_cooldown_end)

        self._preview_process_poll_timer = QTimer(self)
        self._preview_process_poll_timer.setInterval(1000)
        self._preview_process_poll_timer.timeout.connect(self._poll_preview_process)

        self._preview_dock_timer = QTimer(self)
        self._preview_dock_timer.setInterval(250)
        self._preview_dock_timer.timeout.connect(self._send_preview_dock_geometry)

    def retranslate_ui(self):
        self.title_label.setText(tr("preview.title"))
        if getattr(self, "source_label", None):
            self.source_label.setText(tr("preview.source_label"))
        if self.source_edit:
            self.source_edit.setPlaceholderText(tr("preview.source_placeholder"))
        if self.source_file_btn:
            self.source_file_btn.setText(tr("preview.browse_files"))
        if self.source_folder_btn:
            self.source_folder_btn.setText(tr("preview.browse_folder"))
        if self.image_limit_label:
            self.image_limit_label.setText(tr("preview.image_limit_label"))
        if self.preview_stage_title:
            self.preview_stage_title.setText(tr("preview.stage_title"))
        if self.preview_placeholder:
            self.preview_placeholder.setText(tr("preview.stage_empty"))
        self.preview_btn.setText(tr("preview.preview_model"))
        self.close_all_btn.setText(tr("preview.close_window"))
        if self.export_preview_btn:
            self.export_preview_btn.setText(tr("preview.export_preview"))
            self.export_preview_btn.setToolTip(tr("preview.export_preview_tooltip"))
        if self.preview_stage_close_btn:
            self.preview_stage_close_btn.setToolTip(tr("preview.close_window"))
        if self.motion_group_title:
            self.motion_group_title.setText(tr("preview.motion_debug"))
        if self.play_motion_btn:
            self.play_motion_btn.setText(tr("preview.play_motion"))
        if self.motion_hint_label:
            self.motion_hint_label.setText(tr("preview.motion_hint"))
        if self.image_preview_panel:
            self.image_preview_panel.retranslate_ui()

        if hasattr(self, "drag_drop_area") and self.drag_drop_area:
            self.drag_drop_area.retranslate_ui()
        if hasattr(self, "settings_panel") and self.settings_panel:
            self.settings_panel.retranslate_ui()

        if not self.current_model_path and not (
            self.image_preview_panel and self.image_preview_panel.isVisible()
        ):
            self.model_info_text_box.setMarkdown(tr("preview.model_info_empty"))
        if self.motion_combo and not self._motion_items:
            self.motion_combo.clear()
            self.motion_combo.addItem(tr("preview.motion_none"))
            self.motion_combo.setEnabled(False)
            self.play_motion_btn.setEnabled(False)

    def on_preview_image_limit_changed(self, value: int):
        self.settings_manager.set("preview.image_limit", int(value))

    def browse_preview_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            tr("dialog.select_live2d_model_file"),
            "",
            tr("dialog.filter_preview_sources"),
        )
        if file_path:
            self.on_file_dropped(file_path)

    def browse_preview_folder(self):
        folder_path = QFileDialog.getExistingDirectory(
            self,
            tr("dialog.select_package_folder"),
        )
        if folder_path:
            self.on_file_dropped(folder_path)

    # 新增：预览按钮点击（带冷却）
    def _on_preview_clicked(self):
        # 若处于冷却中，拦截点击并提示
        if self._preview_cooldown_timer and self._preview_cooldown_timer.isActive():
            InfoBar.warning(
                title=tr("preview.wait_title"),
                content=tr("preview.wait_content"),
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=1500,
                parent=self
            )
            return
        # 进入冷却：先禁用按钮
        if self.preview_btn is not None:
            self.preview_btn.setEnabled(False)
            try:
                self.preview_btn.setToolTip(tr("preview.tooltip_cooldown"))
            except Exception:
                pass
        # 开始冷却计时
        if self._preview_cooldown_timer:
            try:
                self._preview_cooldown_timer.start(self._preview_cooldown_ms)
            except Exception:
                # 兜底：若计时器异常，仍尝试在结束时恢复
                pass
        # 执行原有预览逻辑
        try:
            self.preview_current_model()
        except Exception:
            # 忽略异常，等待冷却结束再恢复按钮
            pass

    # 新增：冷却结束处理
    def _on_preview_cooldown_end(self):
        if self.preview_btn is not None:
            # 冷却结束，仅当存在可预览模型时才启用
            self.preview_btn.setEnabled(bool(self.current_model_path))
            try:
                self.preview_btn.setToolTip("")
            except Exception:
                pass

    @staticmethod
    def _load_motions_from_model_json(model_json_path: str) -> list[dict]:
        return load_live2d_motions(model_json_path)

    @staticmethod
    def _serialize_preview_settings(settings: dict) -> dict:
        serialized = dict(settings or {})
        serialized["fit_to_dock"] = True
        bg_color = serialized.get("bg_color")
        if isinstance(bg_color, QColor):
            serialized["bg_color"] = bg_color.name()
        elif bg_color is not None:
            serialized["bg_color"] = str(bg_color)
        if "window_size" in serialized:
            try:
                w, h = serialized["window_size"]
                serialized["window_size"] = [int(w), int(h)]
            except Exception:
                serialized.pop("window_size", None)
        return serialized

    def _preview_command_args(self) -> list[str]:
        language = getattr(self.i18n, "language", "en_US")
        if getattr(sys, "frozen", False):
            return [
                sys.executable,
                "--preview-process",
                "--model",
                self.current_model_path,
                "--language",
                language,
            ]
        return [
            sys.executable,
            "-m",
            "app.preview_process",
            "--model",
            self.current_model_path,
            "--language",
            language,
        ]

    def _send_preview_command(self, payload: dict) -> bool:
        process = self.preview_process
        if process is None or process.poll() is not None or process.stdin is None:
            self.preview_process = None
            return False
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
            return True
        except Exception:
            self.preview_process = None
            return False

    def _preview_dock_rect(self) -> dict | None:
        if (
            self.image_preview_panel is not None
            and self.image_preview_panel.isVisible()
            and self.image_preview_panel.is_model_item_selected()
        ):
            rect = self.image_preview_panel.preview_rect()
            if rect:
                return rect
        if self.preview_dock_area is None or not self.preview_dock_area.isVisible():
            return None
        try:
            origin = self.preview_dock_area.mapToGlobal(QPoint(0, 0))
            rect = self.preview_dock_area.rect()
            margin = 0
            return {
                "x": int(origin.x() + margin),
                "y": int(origin.y() + margin),
                "w": max(240, int(rect.width() - margin * 2)),
                "h": max(240, int(rect.height() - margin * 2)),
            }
        except Exception:
            return None

    def _send_preview_dock_geometry(self):
        rect = self._preview_dock_rect()
        if not rect:
            return
        if rect == self._last_preview_dock_rect:
            return
        if self._send_preview_command({"type": "dock", "rect": rect}):
            self._last_preview_dock_rect = rect

    def _terminate_preview_process(self):
        process = self.preview_process
        self.preview_process = None
        if self._preview_process_poll_timer is not None:
            self._preview_process_poll_timer.stop()
        if self._preview_dock_timer is not None:
            self._preview_dock_timer.stop()
        self._last_preview_dock_rect = None
        self._set_motion_debug_visible(False)
        if process is None:
            return
        try:
            if process.poll() is None and process.stdin is not None:
                process.stdin.write(json.dumps({"type": "close"}, ensure_ascii=False) + "\n")
                process.stdin.flush()
        except Exception:
            pass
        try:
            if process.poll() is None:
                process.terminate()
        except Exception:
            pass

    def _poll_preview_process(self):
        process = self.preview_process
        if process is None:
            if self._preview_process_poll_timer is not None:
                self._preview_process_poll_timer.stop()
            if self._preview_dock_timer is not None:
                self._preview_dock_timer.stop()
            return
        if process.poll() is None:
            self._send_preview_dock_geometry()
            return
        self.preview_process = None
        if self._preview_process_poll_timer is not None:
            self._preview_process_poll_timer.stop()
        if self._preview_dock_timer is not None:
            self._preview_dock_timer.stop()
        self._last_preview_dock_rect = None
        self._set_motion_debug_visible(False)

    def _show_stage_placeholder(self, text: str | None = None):
        if self.image_preview_panel:
            self.image_preview_panel.setVisible(False)
        if self.preview_placeholder:
            self.preview_placeholder.setText(text or tr("preview.stage_empty"))
            self.preview_placeholder.setVisible(True)

    def _show_image_stage(self):
        if self.preview_placeholder:
            self.preview_placeholder.setVisible(False)
        if self.image_preview_panel:
            self.image_preview_panel.setVisible(True)

    def _set_preview_export_payload(
        self,
        kind: str | None,
        source_dir: str | None = None,
        files: list[str] | None = None,
        label: str | None = None,
    ):
        self._preview_export_kind = kind
        self._preview_export_source_dir = os.path.abspath(source_dir) if source_dir else None
        self._preview_export_files = [os.path.abspath(path) for path in (files or [])]
        self._preview_export_label = label or source_dir or (self._preview_export_files[0] if self._preview_export_files else "")
        enabled = bool(kind and (self._preview_export_source_dir or self._preview_export_files))
        if self.export_preview_btn:
            self.export_preview_btn.setEnabled(enabled)

    def _clear_preview_export_payload(self):
        self._set_preview_export_payload(None)

    def export_preview_resources(self):
        if self._preview_export_thread is not None and self._preview_export_thread.isRunning():
            return
        if not self._preview_export_kind:
            self.show_error(tr("common.error"), tr("preview.export_no_resources"))
            return

        default_type = "textures" if self._preview_export_kind == "textures" else "live2d"
        default_dir = self.settings_manager.get_output_dir(default_type)
        output_dir = QFileDialog.getExistingDirectory(
            self,
            tr("preview.export_select_directory"),
            default_dir,
        )
        if not output_dir:
            return

        image_only = self._preview_export_kind == "textures"
        folder_suffix = "textures" if image_only else "live2d"
        folder_name = f"{_safe_export_name(self._preview_export_label, 'preview_export')}_{folder_suffix}"

        self.export_preview_btn.setEnabled(False)
        self._preview_export_thread = PreviewExportThread(
            output_dir,
            folder_name,
            source_dir=self._preview_export_source_dir,
            files=self._preview_export_files,
            image_only=image_only,
            parent=self,
        )
        self._preview_export_thread.exported.connect(self.on_preview_export_finished)
        self._preview_export_thread.failed.connect(self.on_preview_export_failed)
        self._preview_export_thread.start()

    def on_preview_export_finished(self, output_dir: str, count: int):
        if self.export_preview_btn:
            self.export_preview_btn.setEnabled(bool(self._preview_export_kind))
        self._preview_export_thread = None
        InfoBar.success(
            title=tr("common.success"),
            content=tr("preview.export_success_content", count=count, output=output_dir),
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=5000,
            parent=self,
        )

    def on_preview_export_failed(self, error: str):
        if self.export_preview_btn:
            self.export_preview_btn.setEnabled(bool(self._preview_export_kind))
        self._preview_export_thread = None
        self.show_error(
            tr("preview.export_failed_title"),
            tr("preview.export_failed_content", error=error),
        )

    def _populate_motion_controls(self, motions: list[dict]):
        self._motion_items = list(motions or [])
        if not self.motion_combo or not self.play_motion_btn:
            return
        self.motion_combo.clear()
        if not self._motion_items:
            self.motion_combo.addItem(tr("preview.motion_none"))
            self.motion_combo.setEnabled(False)
            self.play_motion_btn.setEnabled(False)
            return
        for motion in self._motion_items:
            self.motion_combo.addItem(str(motion.get("display") or motion.get("group") or "motion"))
        self.motion_combo.setEnabled(True)
        self.play_motion_btn.setEnabled(True)

    def _set_motion_debug_visible(self, visible: bool):
        if self.motion_group:
            self.motion_group.setVisible(bool(visible))

    def play_selected_motion(self):
        if not self._motion_items or not self.motion_combo:
            return
        index = self.motion_combo.currentIndex()
        if index < 0 or index >= len(self._motion_items):
            index = 0
        motion = self._motion_items[index]
        self._send_preview_command({
            "type": "play_motion",
            "group": str(motion.get("group", "")),
            "index": int(motion.get("index", 0)),
        })

    def _cleanup_temp_model_json(self):
        """删除上一次创建的临时美化 model json（若存在）。"""
        try:
            if self._temp_model_json_path and os.path.isfile(self._temp_model_json_path):
                os.remove(self._temp_model_json_path)
        except Exception:
            pass

    def _cleanup_image_preview_temp_dirs(self):
        for temp_dir in list(self._image_preview_temp_dirs):
            try:
                if temp_dir and os.path.isdir(temp_dir):
                    shutil.rmtree(temp_dir)
            except Exception:
                pass
        self._image_preview_temp_dirs = []
        if self._preview_export_kind == "textures":
            self._clear_preview_export_payload()

    def _cleanup_model_preview_temp_dirs(self):
        for temp_dir in list(self._model_preview_temp_dirs):
            try:
                if temp_dir and os.path.isdir(temp_dir):
                    shutil.rmtree(temp_dir)
            except Exception:
                pass
        self._model_preview_temp_dirs = []
        if self._preview_export_kind == "live2d":
            self._clear_preview_export_payload()

    def _cleanup_archive_preview_temp_dirs(self):
        for temp_dir in list(self._archive_preview_temp_dirs):
            try:
                if temp_dir and os.path.isdir(temp_dir):
                    shutil.rmtree(temp_dir)
            except Exception:
                pass
        self._archive_preview_temp_dirs = []

    def _cleanup_folder_preview_temp_dirs(self):
        for temp_dir in list(self._folder_preview_temp_dirs):
            try:
                if temp_dir and os.path.isdir(temp_dir):
                    shutil.rmtree(temp_dir)
            except Exception:
                pass
        self._folder_preview_temp_dirs = []

    def on_file_dropped(self, file_path):
        """处理文件拖拽"""
        if self.source_edit:
            self.source_edit.setText(file_path)
            self.source_edit.setCursorPosition(0)
        if not os.path.exists(file_path):
            self.show_error(
                tr("preview.file_not_found_title"),
                tr("preview.file_not_found_content", file=file_path)
            )
            return

        if _is_spine_preview_source(file_path):
            self.show_error(
                tr("preview.spine_not_supported_title"),
                tr("preview.spine_not_supported_content"),
            )
            return

        if _is_archive_preview_source(file_path):
            self.start_archive_preview_import(file_path)
            return

        if os.path.isdir(file_path):
            self.start_folder_preview_scan(file_path)
            return

        if _is_model_json(file_path):
            try:
                resolve_live2d_package(file_path)
                self.start_model_preview_import(file_path)
                return
            except Exception:
                pass

        suffix = os.path.splitext(file_path)[1].lower()
        if suffix in PACKAGE_PREVIEW_EXTENSIONS or (
            _is_unity_preview_source(file_path) and not os.path.isdir(file_path)
        ):
            self.start_model_preview_import(file_path)
            return

        if _is_image_file(file_path):
            local_images = _collect_preview_images(file_path)
            if local_images:
                self.load_image_preview(local_images, file_path, temporary=False)
                return

        if _is_unity_preview_source(file_path) and not _is_model_json(file_path):
            self.start_unity_image_preview(file_path)
            return

        self.show_error(
            tr("preview.invalid_file_type_title"),
            tr("preview.invalid_file_type_content")
        )

    def start_folder_preview_scan(self, folder_path: str):
        if self._folder_preview_thread is not None and self._folder_preview_thread.isRunning():
            self._folder_preview_thread.requestInterruption()
            self._folder_preview_thread.wait(300)
        self.close_preview_window()
        self._cleanup_temp_model_json()
        self._cleanup_model_preview_temp_dirs()
        self._cleanup_image_preview_temp_dirs()
        self._cleanup_archive_preview_temp_dirs()
        self._cleanup_folder_preview_temp_dirs()
        self.current_model_path = None
        self._preview_items = []
        self._auto_selected_model_from_scan = False
        self.preview_btn.setEnabled(False)
        self._set_motion_debug_visible(False)
        if self.image_preview_panel:
            self.image_preview_panel.clear()
            self.image_preview_panel.setVisible(True)
        if self.preview_placeholder:
            self.preview_placeholder.setVisible(False)
        self.model_info_text_box.setMarkdown(
            tr(
                "preview.folder_scan_loading",
                source=folder_path,
                limit=self.current_preview_image_limit(),
            )
        )
        self._folder_preview_thread = FolderPreviewScanThread(
            folder_path,
            self.settings_manager.get_temp_dir(),
            self.current_preview_image_limit(),
            self,
        )
        self._folder_preview_thread.itemFound.connect(self.on_folder_preview_item_found)
        self._folder_preview_thread.scanFinished.connect(self.on_folder_preview_scan_finished)
        self._folder_preview_thread.failed.connect(self.on_folder_preview_scan_failed)
        self._folder_preview_thread.start()

    def current_preview_image_limit(self) -> int:
        if self.image_limit_spinbox:
            return int(self.image_limit_spinbox.value())
        return int(self.settings_manager.get("preview.image_limit", 48) or 48)

    def on_folder_preview_item_found(self, item: dict):
        self._preview_items.append(dict(item))
        temp_dir = item.get("temp_dir")
        if temp_dir and temp_dir not in self._folder_preview_temp_dirs:
            self._folder_preview_temp_dirs.append(temp_dir)
        if self.image_preview_panel:
            self.image_preview_panel.append_item(item)
            self.image_preview_panel.setVisible(True)

        index = len(self._preview_items) - 1
        if item.get("kind") == "model" and not self._auto_selected_model_from_scan:
            self._auto_selected_model_from_scan = True
            self.activate_preview_item(index)
        elif item.get("kind") == "image" and not self._auto_selected_model_from_scan and len(self._preview_items) == 1:
            self.activate_preview_item(index)

    def on_folder_preview_scan_finished(self, count: int, limited: bool, source_path: str):
        if self._folder_preview_thread is not None:
            for temp_dir in self._folder_preview_thread.temp_dirs:
                if temp_dir and temp_dir not in self._folder_preview_temp_dirs:
                    self._folder_preview_temp_dirs.append(temp_dir)
        self._folder_preview_thread = None
        if count <= 0:
            self._show_stage_placeholder(tr("preview.no_preview_items"))
            self.model_info_text_box.setMarkdown(
                tr("preview.folder_scan_empty", source=source_path)
            )
            return
        note_key = "preview.folder_scan_limited" if limited else "preview.folder_scan_finished"
        self.model_info_text_box.setMarkdown(
            tr(
                note_key,
                source=source_path,
                count=count,
                limit=self.current_preview_image_limit(),
            )
        )

    def on_folder_preview_scan_failed(self, error: str, source_path: str):
        self._folder_preview_thread = None
        self.show_error(
            tr("preview.folder_scan_failed_title"),
            tr("preview.folder_scan_failed_content", error=error),
        )

    def on_preview_item_activated(self, index: int):
        self.activate_preview_item(index)

    def activate_preview_item(self, index: int):
        if index < 0 or index >= len(self._preview_items):
            return
        item = self._preview_items[index]
        if self.image_preview_panel:
            self.image_preview_panel.set_current_index(index, emit=False)
            self.image_preview_panel.setVisible(True)
        if self.preview_placeholder:
            self.preview_placeholder.setVisible(False)
        if item.get("kind") == "model":
            self.activate_model_preview_item(item)
        elif item.get("kind") == "image":
            self.activate_image_preview_item(item)

    def activate_model_preview_item(self, item: dict):
        prepared = item.get("prepared_model_json")
        if prepared and os.path.isfile(str(prepared)):
            self.load_model_preview(str(prepared), str(item.get("source_path") or prepared))
            return
        source_path = str(item.get("source_path") or item.get("path") or "")
        if source_path:
            self.start_model_preview_import(source_path)

    def activate_image_preview_item(self, item: dict):
        self._terminate_preview_process()
        self.current_model_path = None
        self._set_motion_debug_visible(False)
        self.preview_btn.setEnabled(False)
        if self.preview_placeholder:
            self.preview_placeholder.setVisible(False)
        if self.image_preview_panel:
            self.image_preview_panel.setVisible(True)
        image_files = [
            str(preview_item.get("path"))
            for preview_item in self._preview_items
            if preview_item.get("kind") == "image" and preview_item.get("path")
        ]
        source_dir = str(item.get("source_dir") or "")
        self._set_preview_export_payload(
            "textures",
            source_dir=source_dir if source_dir and os.path.isdir(source_dir) else None,
            files=image_files,
            label=str(item.get("source_path") or item.get("path") or ""),
        )

    def start_archive_preview_import(self, archive_path: str):
        self.close_preview_window()
        self._cleanup_temp_model_json()
        self._cleanup_model_preview_temp_dirs()
        self._cleanup_image_preview_temp_dirs()
        self._cleanup_archive_preview_temp_dirs()
        self.current_model_path = None
        self.preview_btn.setEnabled(False)
        self._set_motion_debug_visible(False)
        if self.image_preview_panel:
            self.image_preview_panel.clear()
        self._show_stage_placeholder(tr("preview.stage_loading"))
        self.model_info_text_box.setMarkdown(
            tr("preview.archive_import_loading", source=archive_path)
        )
        self._archive_preview_thread = ArchivePreviewImportThread(
            archive_path,
            self.settings_manager.get_temp_dir(),
            self,
        )
        self._archive_preview_thread.archiveReady.connect(self.on_archive_preview_ready)
        self._archive_preview_thread.failed.connect(self.on_archive_preview_failed)
        self._archive_preview_thread.start()

    def on_archive_preview_ready(self, temp_dir: str, source_path: str):
        self._archive_preview_temp_dirs.append(temp_dir)
        self._preview_from_temp_directory(temp_dir, source_path)

    def _preview_from_temp_directory(self, temp_dir: str, source_path: str):

        try:
            result = prepare_preview_import(temp_dir, self.settings_manager.get_temp_dir())
            if result.temp_dir:
                self._model_preview_temp_dirs.append(str(result.temp_dir))
            self.load_model_preview(str(result.preview_model_json), source_path)
            return
        except Exception:
            pass

        local_images = _collect_preview_images(temp_dir)
        if local_images:
            self._pending_unity_preview_source_path = source_path
            self.load_image_preview(local_images, temp_dir, temporary=True)
            self._pending_unity_preview_source_path = None
            return

        self.start_unity_image_preview(temp_dir, display_source=source_path, keep_archive_dirs=True)

    def on_archive_preview_failed(self, error: str, source_path: str):
        self.show_error(
            tr("preview.archive_import_failed_title"),
            tr("preview.archive_import_failed_content", error=error),
        )

    def start_model_preview_import(self, source_path: str):
        self.close_preview_window()
        self._cleanup_temp_model_json()
        self._cleanup_model_preview_temp_dirs()
        self._cleanup_image_preview_temp_dirs()
        self._cleanup_archive_preview_temp_dirs()
        preserve_items = any(
            os.path.abspath(str(item.get("source_path") or item.get("path") or "")) == os.path.abspath(source_path)
            for item in self._preview_items
        )
        if not preserve_items:
            self._preview_items = []
            if self.image_preview_panel:
                self.image_preview_panel.clear()
        self._show_stage_placeholder(tr("preview.stage_loading"))
        self.current_model_path = None
        self._set_motion_debug_visible(False)
        self.preview_btn.setEnabled(False)
        self.model_info_text_box.setMarkdown(
            tr("preview.model_import_loading", source=source_path)
        )
        self._model_preview_thread = ModelPreviewImportThread(
            source_path,
            self.settings_manager.get_temp_dir(),
            self,
        )
        self._model_preview_thread.modelReady.connect(self.on_model_preview_import_ready)
        self._model_preview_thread.failed.connect(self.on_model_preview_import_failed)
        self._model_preview_thread.start()

    def on_model_preview_import_ready(self, model_json_path: str, temp_dir: str, source_path: str):
        if temp_dir:
            self._model_preview_temp_dirs.append(temp_dir)
        self.load_model_preview(model_json_path, source_path)

    def on_model_preview_import_failed(self, error: str, source_path: str):
        if _is_unity_preview_source(source_path):
            self.start_unity_image_preview(source_path)
            return
        self.show_error(
            tr("preview.model_import_failed_title"),
            tr("preview.model_import_failed_content", error=error),
        )

    def load_model_preview(self, model_json_path: str, source_path: str | None = None):
        self.current_model_path = os.path.abspath(model_json_path)
        self._temp_model_json_path = self.current_model_path
        self._cleanup_image_preview_temp_dirs()

        model_name = os.path.basename(self.current_model_path)
        model_dir = os.path.dirname(self.current_model_path)
        if not self._preview_items:
            self._preview_items = [{
                "kind": "model",
                "path": self.current_model_path,
                "prepared_model_json": self.current_model_path,
                "source_path": source_path or self.current_model_path,
                "source_dir": model_dir,
                "label": model_name,
                "detail": model_dir,
            }]
            if self.image_preview_panel:
                self.image_preview_panel.load_items(self._preview_items)
        if self.image_preview_panel:
            self.image_preview_panel.setVisible(True)
            self.image_preview_panel.show_model_placeholder(tr("preview.stage_loading"))
        if self.preview_placeholder:
            self.preview_placeholder.setVisible(False)
        self.model_info_text_box.setMarkdown(
            tr("preview.model_info_loaded", file=model_name, directory=model_dir)
        )
        self._set_preview_export_payload(
            "live2d",
            source_dir=model_dir,
            label=source_path or model_dir,
        )
        QTimer.singleShot(50, self.preview_current_model)
        if not (self._preview_cooldown_timer and self._preview_cooldown_timer.isActive()):
            self.preview_btn.setEnabled(True)

        InfoBar.success(
            title=tr("preview.model_loaded_title"),
            content=tr("preview.model_loaded_content", model=model_name),
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2000,
            parent=self,
        )

    def load_image_preview(self, image_paths: list[str], source_path: str, temporary: bool):
        self.close_preview_window()
        self._cleanup_temp_model_json()
        if not temporary:
            self._cleanup_image_preview_temp_dirs()
            self._cleanup_archive_preview_temp_dirs()
        self.current_model_path = None
        self._set_motion_debug_visible(False)
        self.preview_btn.setEnabled(False)
        self._preview_items = [
            {
                "kind": "image",
                "path": path,
                "source_path": source_path,
                "source_dir": source_path if os.path.isdir(source_path) else os.path.dirname(path),
                "label": os.path.basename(path),
                "detail": os.path.relpath(path, source_path) if os.path.isdir(source_path) else path,
            }
            for path in image_paths
        ]
        if self.image_preview_panel:
            self.image_preview_panel.load_items(self._preview_items)
        self._show_image_stage()
        export_label = self._pending_unity_preview_source_path or source_path
        self._set_preview_export_payload(
            "textures",
            source_dir=source_path if os.path.isdir(source_path) else None,
            files=image_paths,
            label=export_label,
        )
        self.model_info_text_box.setMarkdown(
            tr(
                "preview.image_info_loaded",
                count=len(image_paths),
                source=source_path,
            )
        )
        InfoBar.success(
            title=tr("preview.image_loaded_title"),
            content=tr("preview.image_loaded_content", count=len(image_paths)),
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2000,
            parent=self,
        )

    def start_unity_image_preview(
        self,
        source_path: str,
        display_source: str | None = None,
        keep_archive_dirs: bool = False,
    ):
        self.close_preview_window()
        self._cleanup_temp_model_json()
        self._cleanup_image_preview_temp_dirs()
        if not keep_archive_dirs:
            self._cleanup_archive_preview_temp_dirs()
        self.current_model_path = None
        self._set_motion_debug_visible(False)
        self.preview_btn.setEnabled(False)
        if self.image_preview_panel:
            self.image_preview_panel.clear()
        self._show_stage_placeholder(tr("preview.stage_loading"))
        self.model_info_text_box.setMarkdown(
            tr("preview.unity_preview_loading", source=source_path)
        )
        self._pending_unity_preview_source_path = display_source or source_path
        temp_dir = tempfile.mkdtemp(
            prefix="lpk_preview_unity_",
            dir=self.settings_manager.get_temp_dir(),
        )
        self._image_preview_thread = UnityTexturePreviewThread(source_path, temp_dir, self)
        self._image_preview_thread.previewReady.connect(self.on_unity_image_preview_ready)
        self._image_preview_thread.failed.connect(self.on_unity_image_preview_failed)
        self._image_preview_thread.start()

    def on_unity_image_preview_ready(self, image_paths: list[str], temp_dir: str):
        if not image_paths:
            shutil.rmtree(temp_dir, ignore_errors=True)
            self.show_error(
                tr("preview.no_images_title"),
                tr("preview.no_images_content"),
            )
            return
        self._image_preview_temp_dirs.append(temp_dir)
        self.load_image_preview(image_paths, temp_dir, temporary=True)
        self._pending_unity_preview_source_path = None

    def on_unity_image_preview_failed(self, error: str, temp_dir: str):
        self._pending_unity_preview_source_path = None
        shutil.rmtree(temp_dir, ignore_errors=True)
        self.show_error(
            tr("preview.unity_preview_failed_title"),
            tr("preview.unity_preview_failed_content", error=error),
        )

    def preview_current_model(self):
        """在独立进程中预览当前模型，避免 Cubism/OpenGL 卡住主 GUI。"""
        if not self.current_model_path:
            self.show_error(
                tr("preview.no_model_selected_title"),
                tr("preview.no_model_selected_content")
            )
            return False

        try:
            self._terminate_preview_process()
            motions = self._load_motions_from_model_json(self.current_model_path)
            self._populate_motion_controls(motions)

            creationflags = 0
            if os.name == "nt" and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            self.preview_process = subprocess.Popen(
                self._preview_command_args(),
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
            self._send_preview_dock_geometry()
            self._send_preview_command({
                "type": "settings",
                "settings": self._serialize_preview_settings(self.settings_panel.get_settings()),
            })
            if self._preview_process_poll_timer is not None:
                self._preview_process_poll_timer.start()
            if self._preview_dock_timer is not None:
                self._preview_dock_timer.start()
            self._set_motion_debug_visible(True)
            if self.preview_placeholder:
                self.preview_placeholder.setVisible(False)
            if self.image_preview_panel:
                self.image_preview_panel.setVisible(True)
                self.image_preview_panel.show_model_placeholder(tr("preview.external_running"))
            return True
        except Exception as exc:
            self._terminate_preview_process()
            self._set_motion_debug_visible(False)
            self._show_stage_placeholder(tr("preview.stage_empty"))
            self.show_error(
                tr("common.error"),
                tr("preview_window.error_model_load_failed", error_type=type(exc).__name__, error=exc),
            )
            return False

    def on_preview_window_closed(self, window):
        """预览窗口关闭处理"""
        if self.preview_process is not None and self.preview_process.poll() is not None:
            self.preview_process = None

    def close_preview_window(self):
        """关闭独立预览进程"""
        self._terminate_preview_process()
        self._populate_motion_controls([])
        self._set_motion_debug_visible(False)
        self._clear_preview_export_payload()
        self._show_stage_placeholder()

    def show_error(self, title, message):
        """显示错误信息"""
        InfoBar.error(
            title=title,
            content=message,
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=3000,
            parent=self
        )

    def on_settings_changed(self, settings: dict):
        """实时发送设置到独立预览进程"""
        self._send_preview_command({
            "type": "settings",
            "settings": self._serialize_preview_settings(settings),
        })

    def on_request_refresh_params(self):
        """主进程不再加载 Cubism；这里只刷新 model3.json 内的动作列表。"""
        if self.current_model_path:
            self._populate_motion_controls(self._load_motions_from_model_json(self.current_model_path))
        else:
            self._populate_motion_controls([])
