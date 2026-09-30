import os
import json
import math
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path

from PySide6.QtWidgets import (
    QFrame,
    QVBoxLayout,
    QHBoxLayout,
    QFileDialog,
    QWidget,
    QMessageBox,
    QSplitter,
    QGridLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QInputDialog,
    QCompleter,
    QStackedWidget,
)
from PySide6.QtCore import (
    Qt,
    Signal,
    QTimer,
    QCoreApplication,
    QThread,
    QPoint,
    QEvent,
    QStringListModel,
    QSignalBlocker,
)
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QColor, QPixmap
from qfluentwidgets import (SubtitleLabel, BodyLabel, CaptionLabel, PushButton, Slider, CheckBox, SpinBox, InfoBar, InfoBarPosition,
                           CardWidget, SingleDirectionScrollArea, TextBrowser, ColorDialog, FluentIcon, IconWidget,
                           ComboBox, EditableComboBox, LineEdit, TransparentToolButton)

from app.core.assetstudio_cli import AssetStudioCLI
from app.core.model import resolve_live2d_package
from app.core.model.motions import load_live2d_motions
from app.core.preview import prepare_preview_import, prepare_spine_preview_import, prepare_package_preview_import
from app.core.spine_preview import (
    SpinePreviewPlan,
    SpineRuntimePolicy,
    SPINE_COMPATIBILITY_VERSION,
    SPINE_RUNTIME_SUPPORTED_VERSIONS,
    discover_spine_runtime,
)
from app.core.spine_converter import discover_native_converter
from app.core.preview.sources import (
    IMAGE_PREVIEW_EXTENSIONS,
    PACKAGE_PREVIEW_EXTENSIONS,
    collect_preview_images as _collect_preview_images,
    is_archive_preview_source as _is_archive_preview_source,
    is_image_file as _is_image_file,
    is_model_json as _is_model_json,
    is_spine_preview_source as _is_spine_preview_source,
    is_supported_preview_source as _is_supported_preview_source,
    is_unity_preview_source as _is_unity_preview_source,
    safe_export_name as _safe_export_name,
)
from app.core.settings_manager import SettingsManager
from app.gui.SettingsPage import ToolchainInstallWorker
from app.gui.ImagePreviewPanel import ImagePreviewPanel
from app.gui.Live2DPreviewWindow import Live2DPreviewWindow
from app.gui.SpinePreviewWidget import SpinePreviewWidget
from app.gui.PreviewArtMeshPanel import PreviewArtMeshPanel
from app.gui.editor_workspace import EditorTabs, EditorViewportLayout, EditorComboBox
from app.i18n import get_i18n, tr
from app.paths import PROJECT_ROOT


PREVIEW_LAYOUT_TEXT = {
    "preview.layout.animation_parameters": "动画与参数",
    "preview.layout.display_interaction": "显示与交互",
    "preview.layout.animation": "动画",
    "preview.layout.display": "显示",
    "preview.layout.artmesh": "ArtMesh 部件",
    "preview.layout.empty": "载入资源后显示对应的预览控制。",
}


def _layout_text(key):
    return tr(key, PREVIEW_LAYOUT_TEXT[key])

try:
    from shiboken6 import isValid as _is_qt_object_valid
except ImportError:  # pragma: no cover - bundled with PySide6 in normal builds
    def _is_qt_object_valid(obj) -> bool:
        return obj is not None


def _spine_thread_is_running(thread) -> bool:
    """Read a worker state only while its wrapped C++ QThread is alive."""

    return bool(thread is not None and _is_qt_object_valid(thread) and thread.isRunning())


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
                border: 2px dashed palette(mid);
                border-radius: 8px;
                background: palette(alternate-base);
            }
            DragDropArea:hover {
                border-color: #00A6B3;
                background: palette(base);
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
                background: palette(base);
            }
            DragDropArea:hover {
                border-color: #00A6B3;
                background: palette(base);
            }
        """)

    def dragLeaveEvent(self, event):
        """拖拽离开事件"""
        self.setStyleSheet("""
            DragDropArea {
                border: 2px dashed palette(mid);
                border-radius: 8px;
                background: palette(alternate-base);
            }
            DragDropArea:hover {
                border-color: #00A6B3;
                background: palette(base);
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
            self.editor_model_json = str(result.package.model_json)
            self.modelReady.emit(
                str(result.preview_model_json),
                str(result.temp_dir or ""),
                self.source_path,
            )
        except Exception as exc:
            self.failed.emit(str(exc), self.source_path)


class SpinePreviewImportThread(QThread):
    """Prepare a disposable Spine asset without blocking the Qt event loop."""

    previewReady = Signal(object, str)
    failed = Signal(str, str)

    def __init__(self, source_path: str, temp_root: str, runtime_root: str = "", parent=None):
        super().__init__(parent)
        self.source_path = source_path
        self.temp_root = temp_root
        self.runtime_root = runtime_root or None
        # Captured by PreviewPage on the GUI thread before ``start``.  Keeping
        # this value on the worker avoids observing a settings-page edit while
        # an import is already in progress.
        self._unify_version = False
        self._runtime_policy = None
        self.result = None

    def run(self):
        try:
            if getattr(self, "_package_fallback", False):
                result = prepare_package_preview_import(
                    self.source_path, self.temp_root, self.runtime_root,
                    should_continue=lambda: not self.isInterruptionRequested(),
                    unify_version=self._unify_version,
                    runtime_policy=self._runtime_policy,
                )
            else:
                result = prepare_spine_preview_import(
                    self.source_path, self.temp_root, self.runtime_root,
                    unify_version=self._unify_version,
                    runtime_policy=self._runtime_policy,
                )
            self.result = result
            self.previewReady.emit(result, self.source_path)
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
            "editor_model_json": str(result.package.model_json),
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
                    "editor_model_json": str(result.package.model_json),
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


class Live2DSettingsPanel(QFrame):
    """Live2D设置面板"""

    settingsChanged = Signal(dict)
    requestRefreshParams = Signal()

    def __init__(self, parent=None, mode: str = "display"):
        super().__init__(parent)

        self.mode = mode if mode in {"display", "parameters"} else "display"
        self.preview_window = None

        self.width_spinbox = None
        self.height_spinbox = None
        self.opacity_label = None
        self.opacity_slider = None
        self.show_controls_check = None

        self.rotation_label = None
        self.rotation_slider = None
        self.scale_text_label = None
        self.scale_value_label = None
        self.scale_slider = None
        self.antialias_check = None
        self.fit_model_btn = None
        self.offset_text_label = None
        self.position_x_label = None
        self.position_y_label = None
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
        self.window_group = None
        self.model_group = None
        self.interaction_group = None

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

        window_group = self.create_window_settings_group()
        model_group = self.create_model_settings_group()
        interaction_group = self.create_interaction_settings_group()
        advanced_group = self.create_advanced_settings_group()
        self.window_group = window_group
        self.model_group = model_group
        self.interaction_group = interaction_group
        self.advanced_group = advanced_group
        if self.mode == "parameters":
            window_group.hide()
            model_group.hide()
            interaction_group.hide()
            scroll_layout.addWidget(advanced_group)
        else:
            advanced_group.hide()
            scroll_layout.addWidget(window_group)
            scroll_layout.addWidget(model_group)
            scroll_layout.addWidget(interaction_group)

        # 添加弹性空间
        scroll_layout.addStretch()

        scroll.setWidget(scroll_widget)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
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
        self.show_controls_check.setChecked(False)
        self.show_controls_check.hide()
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

        scale_layout = QHBoxLayout()
        self.scale_text_label = BodyLabel("", group)
        scale_layout.addWidget(self.scale_text_label)
        self.scale_slider = Slider(Qt.Horizontal, group)
        self.scale_slider.setRange(25, 400)
        self.scale_slider.setValue(100)
        self.scale_value_label = BodyLabel("100%", group)
        self.scale_value_label.setMinimumWidth(48)
        self.scale_slider.valueChanged.connect(
            lambda value: self.scale_value_label.setText(f"{value}%")
        )
        self.scale_slider.valueChanged.connect(lambda _: self._emit_settings())
        scale_layout.addWidget(self.scale_slider, 1)
        scale_layout.addWidget(self.scale_value_label)
        layout.addLayout(scale_layout)
        self.antialias_check = CheckBox("", group)
        self.antialias_check.setChecked(True)
        self.antialias_check.toggled.connect(lambda _: self._emit_settings())
        layout.addWidget(self.antialias_check)
        self.fit_model_btn = PushButton("", group)
        self.fit_model_btn.clicked.connect(self.fit_model_to_view)
        layout.addWidget(self.fit_model_btn)


        offset_layout = QGridLayout()
        offset_layout.setHorizontalSpacing(8)
        offset_layout.setVerticalSpacing(6)
        self.offset_text_label = BodyLabel("", group)
        self.offset_text_label.setWordWrap(True)
        offset_layout.addWidget(self.offset_text_label, 0, 0, 1, 4)
        self.position_x_label = BodyLabel("X", group)
        self.position_x_spinbox = SpinBox(group)
        self.position_x_spinbox.setRange(-100, 100)
        self.position_x_spinbox.setValue(0)
        self.position_x_spinbox.setSuffix(" %")
        self.position_x_spinbox.setFixedWidth(105)
        self.position_x_spinbox.valueChanged.connect(lambda _: self._emit_settings())
        self.position_y_label = BodyLabel("Y", group)
        self.position_y_spinbox = SpinBox(group)
        self.position_y_spinbox.setRange(-100, 100)
        self.position_y_spinbox.setValue(0)
        self.position_y_spinbox.setSuffix(" %")
        self.position_y_spinbox.setFixedWidth(105)
        self.position_y_spinbox.valueChanged.connect(lambda _: self._emit_settings())
        offset_layout.addWidget(self.position_x_label, 1, 0)
        offset_layout.addWidget(self.position_x_spinbox, 1, 1)
        offset_layout.addWidget(self.position_y_label, 1, 2)
        offset_layout.addWidget(self.position_y_spinbox, 1, 3)
        layout.addLayout(offset_layout)

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
            f"QFrame{{border:1px solid palette(mid); border-radius:4px; background:{self.selected_bg_color.name()};}}"
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

    def fit_model_to_view(self):
        """Reset only the viewport transform, retaining animation and pose."""
        for control, value in ((self.scale_slider, 100), (self.rotation_slider, 0),
                               (self.position_x_spinbox, 0), (self.position_y_spinbox, 0)):
            control.blockSignals(True)
            control.setValue(value)
            control.blockSignals(False)
        self.scale_value_label.setText("100%")
        self.rotation_label.setText("0°")
        self._emit_settings()

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
        self.adv_params_container_layout = QGridLayout(self.adv_params_container)
        self.adv_params_container_layout.setContentsMargins(0, 0, 0, 0)
        self.adv_params_container_layout.setHorizontalSpacing(10)
        self.adv_params_container_layout.setVerticalSpacing(8)
        self.adv_params_container_layout.setColumnStretch(0, 1)
        self.adv_params_container_layout.setColumnStretch(1, 0)
        self.adv_params_container_layout.setColumnStretch(2, 0)
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
        if self.antialias_check:
            self.antialias_check.setText(tr("preview.antialias"))
        if self.fit_model_btn:
            self.fit_model_btn.setText(tr("preview.fit_model"))
        if self.scale_text_label:
            self.scale_text_label.setText(tr("preview.model_scale"))
        if self.offset_text_label:
            self.offset_text_label.setText(tr("preview.model_offset"))
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

    def set_spine_mode(self, active: bool):
        """Show only display settings that have a meaning for Spine."""

        active = bool(active)
        if self.mode != "display":
            return
        if self.window_group:
            # Window size controls are already hidden in this card; its
            # opacity slider remains a valid Spine display setting.
            self.window_group.setVisible(True)
        if self.model_group:
            self.model_group.setVisible(True)
        if self.interaction_group:
            self.interaction_group.setVisible(not active)

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
            if rng <= 200.0:
                return 100
            if rng <= 2000.0:
                return 10
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

            name_label = BodyLabel(f"{pid}:", self.adv_params_container)
            name_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            name_label.setToolTip(pid)
            name_label.setMinimumWidth(0)
            name_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

            slider = Slider(Qt.Horizontal, self.adv_params_container)
            slider.setFixedWidth(120)
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
            val_label.setFixedWidth(58)
            val_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            def make_on_change(lbl, scale_factor):
                return lambda v: lbl.setText(f"{v/scale_factor:.2f}") if scale_factor != 1 else lbl.setText(f"{v}")

            slider.valueChanged.connect(make_on_change(val_label, scale))
            slider.valueChanged.connect(lambda _: self._emit_settings())

            row_index = len(self.advanced_param_sliders)
            self.adv_params_container_layout.addWidget(
                name_label,
                row_index,
                0,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            )
            self.adv_params_container_layout.addWidget(
                slider,
                row_index,
                1,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            )
            self.adv_params_container_layout.addWidget(
                val_label,
                row_index,
                2,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            )
            self.advanced_param_sliders[pid] = (slider, val_label, scale)

    def sync_advanced_param_values(self, meta_list: list):
        """Mirror current model values without emitting override changes."""
        current_values = {
            str(item.get("id", "")): float(item.get("value", 0.0))
            for item in meta_list
            if item.get("id")
        }
        for pid, (slider, value_label, scale) in self.advanced_param_sliders.items():
            if pid not in current_values or slider.isSliderDown():
                continue
            spec = self.param_specs_by_id.get(pid)
            value = current_values[pid]
            if spec:
                value = max(spec["min"], min(spec["max"], value))
            slider.blockSignals(True)
            slider.setValue(int(round(value * scale)))
            slider.blockSignals(False)
            value_label.setText(
                f"{value:.2f}" if scale != 1 else f"{int(round(value))}"
            )

    def set_advanced_param_values(self, values: dict[str, float]):
        """Update visible parameter controls from a frozen motion timeline."""
        for parameter_id, value in values.items():
            item = self.advanced_param_sliders.get(parameter_id)
            if item is None:
                continue
            slider, _label, scale = item
            slider.setValue(int(round(float(value) * scale)))

    def get_settings(self):
        """获取当前设置"""
        settings = {
            'window_size': (self.width_spinbox.value(), self.height_spinbox.value()),
            'opacity': self.opacity_slider.value() / 100.0,
            'show_controls': bool(self.show_controls_check and self.show_controls_check.isVisible() and self.show_controls_check.isChecked()),
            'model_rotation': self.rotation_slider.value(),
            'model_scale': self.scale_slider.value() / 100.0,
            'antialias': self.antialias_check.isChecked(),
            'model_offset_x': self.position_x_spinbox.value() / 100.0,
            'model_offset_y': self.position_y_spinbox.value() / 100.0,
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

    def get_advanced_settings(self):
        settings = self.get_settings()
        return {
            "advanced_enabled": settings.get("advanced_enabled", False),
            "advanced_params": settings.get("advanced_params", {}),
        }

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
                        f"QFrame{{border:1px solid palette(mid); border-radius:4px; background:{color.name()};}}"
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

class SpineAnimationControls(CardWidget):
    """Native controls for the Spine renderer.

    Live2D's motion widgets carry model-specific semantics and must not be
    reused for Spine.  This card talks only to ``SpinePreviewWidget``'s small
    native rendering API and keeps its own state when the preview is switched.
    """

    skinChanged = Signal(str)
    animationChanged = Signal(str)
    pausedChanged = Signal(bool)
    loopChanged = Signal(bool)
    timeChanged = Signal(float)
    resetRequested = Signal()
    exportRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("spineAnimationControls")
        self.setMinimumWidth(250)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.title = SubtitleLabel("", self)
        layout.addWidget(self.title)
        self.status = CaptionLabel("", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        scroll = SingleDirectionScrollArea(orient=Qt.Vertical)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.enableTransparentBackground()
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(0, 0, 0, 0)
        inner_layout.setSpacing(8)

        self.skin_label = BodyLabel("", inner)
        inner_layout.addWidget(self.skin_label)
        self.skin_combo = ComboBox(inner)
        self.skin_combo.currentTextChanged.connect(self.skinChanged.emit)
        inner_layout.addWidget(self.skin_combo)

        self.animation_label = BodyLabel("", inner)
        inner_layout.addWidget(self.animation_label)
        self.animation_combo = ComboBox(inner)
        self.animation_combo.currentTextChanged.connect(self.animationChanged.emit)
        inner_layout.addWidget(self.animation_combo)

        action_row = QHBoxLayout()
        self.play_pause_btn = PushButton("", inner)
        self.play_pause_btn.setCheckable(True)
        self.play_pause_btn.toggled.connect(self.pausedChanged.emit)
        action_row.addWidget(self.play_pause_btn, 1)
        self.reset_btn = PushButton("", inner)
        self.reset_btn.clicked.connect(self.resetRequested.emit)
        action_row.addWidget(self.reset_btn, 1)
        inner_layout.addLayout(action_row)

        self.export_pose_btn = PushButton("", inner)
        self.export_pose_btn.clicked.connect(self.exportRequested.emit)
        inner_layout.addWidget(self.export_pose_btn)

        self.loop_check = CheckBox("", inner)
        self.loop_check.setChecked(True)
        self.loop_check.toggled.connect(self.loopChanged.emit)
        inner_layout.addWidget(self.loop_check)

        time_row = QHBoxLayout()
        self.time_label = BodyLabel("", inner)
        time_row.addWidget(self.time_label)
        self.time_value = CaptionLabel("0.00 / 0.00", inner)
        time_row.addWidget(self.time_value, 1, Qt.AlignmentFlag.AlignRight)
        inner_layout.addLayout(time_row)
        self.time_slider = Slider(Qt.Horizontal, inner)
        self.time_slider.setRange(0, 1000)
        self.time_slider.valueChanged.connect(self._emit_time)
        inner_layout.addWidget(self.time_slider)
        inner_layout.addStretch(1)

        scroll.setWidget(inner)
        layout.addWidget(scroll, 1)
        self._time_max = 0.0
        self._ready = False
        self.retranslate_ui()
        self.clear()

    def retranslate_ui(self):
        self.title.setText(tr("preview.spine_animation_title"))
        self.skin_label.setText(tr("preview.spine_skin"))
        self.animation_label.setText(tr("preview.spine_animation"))
        self.play_pause_btn.setText(
            tr("preview.spine_play") if self.play_pause_btn.isChecked()
            else tr("preview.spine_pause")
        )
        self.reset_btn.setText(tr("preview.spine_reset_pose"))
        self.export_pose_btn.setText(tr("preview.spine_export_pose"))
        self.loop_check.setText(tr("preview.spine_loop"))
        self.time_label.setText(tr("preview.spine_time"))

    def _emit_time(self, value: int):
        if not self._ready or self._time_max <= 0:
            return
        self.timeChanged.emit(float(value) / 1000.0 * self._time_max)

    def _set_combo_items(self, combo, values, selected=""):
        names = [str(value.get("value", "") if isinstance(value, dict) else value or "")
                 for value in values or []]
        names = [name for name in names if name]
        combo.blockSignals(True)
        # Animation time updates must not rebuild the skin/animation menus.
        if names != [combo.itemText(index) for index in range(combo.count())]:
            combo.clear()
            for name in names:
                combo.addItem(name)
        if combo.count():
            index = combo.findText(str(selected or ""))
            combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)

    def set_state(self, state: dict | None):
        if not isinstance(state, dict):
            self.clear()
            return
        preview_state = str(state.get("previewState") or "").lower()
        mode = str(state.get("mode") or "").lower()
        ready = preview_state == "ready"
        self._ready = ready and mode in {"native", "runtime"}
        if mode == "atlas":
            self.status.setText(tr("preview.spine_atlas_controls_hint"))
        elif preview_state == "error":
            self.status.setText(str(state.get("previewError") or state.get("text") or ""))
        elif ready:
            self.status.setText(tr("preview.spine_controls_ready"))
        else:
            self.status.setText(tr("preview.spine_controls_loading"))
        self._set_combo_items(self.skin_combo, state.get("skinOptions", []), state.get("selectedSkin", ""))
        self._set_combo_items(self.animation_combo, state.get("animationOptions", []), state.get("selectedAnimation", ""))
        self.skin_combo.setEnabled(self._ready and self.skin_combo.count() > 0)
        self.animation_combo.setEnabled(self._ready and self.animation_combo.count() > 0)
        self.play_pause_btn.setEnabled(self._ready and self.animation_combo.count() > 0)
        self.loop_check.setEnabled(self._ready and self.animation_combo.count() > 0)
        self.reset_btn.setEnabled(self._ready)
        self.export_pose_btn.setEnabled(self._ready and bool(state.get("exportSupported", False)))
        self.export_pose_btn.setToolTip(
            "" if state.get("exportSupported") else tr("preview.spine_native_pose_unavailable")
        )
        self.play_pause_btn.blockSignals(True)
        self.play_pause_btn.setChecked(bool(state.get("paused", False)))
        self.play_pause_btn.blockSignals(False)
        self.retranslate_ui()
        self.loop_check.blockSignals(True)
        self.loop_check.setChecked(bool(state.get("loop", True)))
        self.loop_check.blockSignals(False)
        try:
            current = max(0.0, float(state.get("time", 0.0) or 0.0))
            self._time_max = max(0.0, float(state.get("timeMax", 0.0) or 0.0))
        except (TypeError, ValueError):
            current, self._time_max = 0.0, 0.0
        if not math.isfinite(current) or not math.isfinite(self._time_max):
            current, self._time_max = 0.0, 0.0
        if self._time_max > 0:
            current = current % self._time_max if state.get("loop", True) else min(current, self._time_max)
        else:
            current = 0.0
        self.time_slider.setEnabled(self._ready and self._time_max > 0)
        if not self.time_slider.isSliderDown():
            self.time_slider.blockSignals(True)
            ratio = current / self._time_max if self._time_max > 0 else 0.0
            self.time_slider.setValue(max(0, min(1000, int(round(ratio * 1000)))))
            self.time_slider.blockSignals(False)
        self.time_value.setText(f"{current:.2f} / {self._time_max:.2f}")

    def clear(self):
        self._ready = False
        self._time_max = 0.0
        self._set_combo_items(self.skin_combo, [])
        self._set_combo_items(self.animation_combo, [])
        for widget in (self.skin_combo, self.animation_combo, self.play_pause_btn,
                       self.loop_check, self.time_slider, self.reset_btn, self.export_pose_btn):
            widget.setEnabled(False)
        self.play_pause_btn.blockSignals(True)
        self.play_pause_btn.setChecked(False)
        self.play_pause_btn.blockSignals(False)
        self.loop_check.blockSignals(True)
        self.loop_check.setChecked(True)
        self.loop_check.blockSignals(False)
        self.time_slider.blockSignals(True)
        self.time_slider.setValue(0)
        self.time_slider.blockSignals(False)
        self.time_value.setText("0.00 / 0.00")
        self.status.setText(tr("preview.spine_controls_loading"))
        self.retranslate_ui()


class PreviewPage(QFrame):
    poseSchemeRequested = Signal(dict)
    editorRequested = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings_panel = None
        self.advanced_panel = None
        self.live2d_preview = None
        self.preview_btn = None
        self.close_all_btn = None
        self.model_info_text_box = None
        self.image_preview_panel = None
        self.preview_stage_title = None
        self.preview_stage = None
        self.preview_stage_layout = None
        self.preview_stage_close_btn = None
        self.export_preview_btn = None
        self.open_editor_btn = None
        self._editor_source = None
        self._editor_source_leases = {}
        self._editor_leased_dirs = {}
        self._deferred_preview_temp_dirs = set()
        self._preview_page_active = None
        self._native_playback_suspended = False
        self._resume_spine_paused = False
        self.preview_dock_area = None
        self.preview_dock_layout = None
        self.preview_placeholder = None
        self.spine_preview = None
        self.spine_controls = None
        self.motion_group_title = None
        self.motion_group = None
        self.motion_combo = None
        self.play_motion_btn = None
        self.motion_hint_label = None
        self.freeze_motion_check = None
        self.loop_motion_check = None
        self.auto_play_motion_check = None
        self.save_pose_scheme_btn = None
        self.pose_controls_card = None
        self.pose_controls_title = None
        self.refresh_pose_params_btn = None
        self.reset_pose_params_btn = None
        self.motion_timeline_frame = None
        self.motion_timeline = None
        self.motion_time_spin = None
        self.motion_time_label = None
        self._timeline_sync = False
        self._freeze_syncing = False
        self.left_sidebar_btn = None
        self.right_sidebar_btn = None
        self.preview_splitter = None
        self.left_sidebar = None
        self.right_sidebar = None
        self._motion_items = []
        self.drag_drop_area = None
        self.source_label = None
        self.source_edit = None
        self.source_file_btn = None
        self.source_folder_btn = None
        self.spine_runtime_label = None
        self.spine_runtime_edit = None
        self.spine_runtime_btn = None
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
        self._spine_preview_thread = None
        self._spine_preview_workers = []
        self._spine_preview_generation = 0
        self._spine_preview_temp_dirs = []
        self._spine_runtime_install_worker = None
        self._spine_runtime_retry_source = ""
        self._spine_runtime_retry_package_fallback = False
        self._spine_runtime_retry_compatibility = False
        self._spine_runtime_retry_generation = 0
        self._spine_runtime_prompt_enabled = True
        self._active_spine_plan = None
        self._active_spine_preview_key = ""
        self._spine_mode = False
        self._spine_sidebar_state = None
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
        self._parameter_sync_timer = None
        self._advanced_enabled_before_freeze = False
        self._psd_project_context = None
        self._pending_psd_project_context = None
        self._resource_mode = "empty"
        self._artmesh_loaded_for = None
        self._live2d_load_timer = QTimer(self)
        self._live2d_load_timer.setSingleShot(True)
        self._live2d_load_timer.setInterval(50)
        self._live2d_load_timer.timeout.connect(self.preview_current_model)
        self._parameter_refresh_retries = 0
        self._parameter_refresh_timer = QTimer(self)
        self._parameter_refresh_timer.setSingleShot(True)
        self._parameter_refresh_timer.setInterval(120)
        self._parameter_refresh_timer.timeout.connect(lambda: self._refresh_parameter_controls(self._parameter_refresh_retries))

        self.setupUI()
        self.retranslate_ui()
        self.i18n.languageChanged.connect(self.retranslate_ui)
        # 应用退出前做一次兜底清理，防止文件句柄未及时释放
        try:
            app = QCoreApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(self._terminate_preview_process)
                app.aboutToQuit.connect(self._destroy_embedded_live2d)
                app.aboutToQuit.connect(self._destroy_embedded_spine)
                app.aboutToQuit.connect(self._cleanup_temp_model_json)
                app.aboutToQuit.connect(self._cleanup_model_preview_temp_dirs)
                app.aboutToQuit.connect(self._cleanup_spine_preview_temp_dirs)
                app.aboutToQuit.connect(self._cancel_spine_runtime_install)
                app.aboutToQuit.connect(self._cleanup_image_preview_temp_dirs)
                app.aboutToQuit.connect(self._cleanup_archive_preview_temp_dirs)
                app.aboutToQuit.connect(self._cleanup_folder_preview_temp_dirs)
        except Exception:
            pass

    def setupUI(self):
        self.main_layout = EditorViewportLayout(self)
        self.main_layout.setContentsMargins(20, 18, 20, 20)
        self.main_layout.setSpacing(12)

        # 标题与侧栏开关始终位于三栏布局之外，侧栏隐藏后仍可恢复。
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self.title_label = SubtitleLabel("", self)
        title_row.addWidget(self.title_label)
        title_row.addStretch(1)
        self.left_sidebar_btn = PushButton("", self)
        self.left_sidebar_btn.clicked.connect(lambda: self._toggle_sidebar("left"))
        title_row.addWidget(self.left_sidebar_btn)
        self.right_sidebar_btn = PushButton("", self)
        self.right_sidebar_btn.clicked.connect(lambda: self._toggle_sidebar("right"))
        title_row.addWidget(self.right_sidebar_btn)
        self.main_layout.addLayout(title_row)

        # 创建分割器
        splitter = QSplitter(Qt.Horizontal, self)
        self.preview_splitter = splitter
        splitter.setChildrenCollapsible(False)
        splitter.setOpaqueResize(True)
        splitter.setHandleWidth(8)
        splitter.setStyleSheet("""
            QSplitter::handle {
                background: palette(alternate-base);
                border-radius: 3px;
            }
            QSplitter::handle:hover {
                background: palette(mid);
            }
        """)

        # 左侧：导入、设置和控制按钮
        left_widget = QWidget()
        self.left_sidebar = left_widget
        left_widget.setMinimumWidth(200)
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

        source_button_row = QVBoxLayout()
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

        spine_runtime_row = QHBoxLayout()
        self.spine_runtime_label = BodyLabel("Spine runtime", import_card)
        self.spine_runtime_edit = LineEdit(import_card)
        self.spine_runtime_edit.setReadOnly(True)
        self.spine_runtime_edit.setPlaceholderText("Official spine-ts core/webgl directory (optional)")
        self.spine_runtime_btn = PushButton("Choose", import_card)
        self.spine_runtime_btn.setIcon(FluentIcon.FOLDER)
        self.spine_runtime_btn.clicked.connect(self.browse_spine_runtime)
        spine_runtime_row.addWidget(self.spine_runtime_label)
        spine_runtime_row.addWidget(self.spine_runtime_edit, 1)
        spine_runtime_row.addWidget(self.spine_runtime_btn)
        import_layout.addLayout(spine_runtime_row)
        # Runtime selection belongs to Settings > Runtime.  Keep the preview
        # page focused on the source and show only the resolved version/status
        # in the model information card below.
        for _widget in (self.spine_runtime_label, self.spine_runtime_edit, self.spine_runtime_btn):
            _widget.setVisible(False)

        image_limit_row = QVBoxLayout()
        image_limit_row.setSpacing(8)
        self.image_limit_label = BodyLabel("", import_card)
        self.image_limit_label.setWordWrap(True)
        self.image_limit_spinbox = SpinBox(import_card)
        self.image_limit_spinbox.setSymbolVisible(False)
        self.image_limit_spinbox.setFixedWidth(80)
        self.image_limit_spinbox.setRange(1, 500)
        self.image_limit_spinbox.setValue(int(self.settings_manager.get("preview.image_limit", 48) or 48))
        self.image_limit_spinbox.valueChanged.connect(self.on_preview_image_limit_changed)
        image_limit_row.addWidget(self.image_limit_label)
        image_limit_row.addWidget(self.image_limit_spinbox)
        import_layout.addLayout(image_limit_row)
        left_layout.addWidget(import_card)

        # 当前模型信息
        self.model_info_text_box = TextBrowser(self)
        self.model_info_text_box.setMinimumHeight(120)
        self.model_info_text_box.setMaximumHeight(170)
        self.model_info_text_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.model_info_text_box.setStyleSheet("""
            TextBrowser {
                border: 1px solid palette(mid);
                border-radius: 8px;
                background: palette(base);
                padding: 8px;
            }
        """)

        left_layout.addWidget(self.model_info_text_box)

        # Display and interaction controls move into the format-aware right tabs.
        self.settings_panel = Live2DSettingsPanel(self, mode="display")
        self.settings_panel.settingsChanged.connect(self.on_settings_changed)
        self.settings_panel.requestRefreshParams.connect(self.on_request_refresh_params)
        left_layout.addStretch(1)

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

        # 中间：统一的图片 / Live2D 预览舞台
        right_widget = QWidget()
        right_widget.setMinimumWidth(280)
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
                border: 1px solid palette(mid);
                border-radius: 8px;
                background: palette(alternate-base);
            }
        """)
        self.preview_stage_layout = QVBoxLayout(self.preview_stage)
        self.preview_stage_layout.setContentsMargins(12, 10, 12, 12)
        self.preview_stage_layout.setSpacing(8)

        stage_toolbar = QVBoxLayout()
        stage_toolbar.setContentsMargins(0, 0, 0, 0)
        stage_toolbar.setSpacing(8)
        editor_toolbar = QHBoxLayout()
        self.open_editor_btn = PushButton("", self.preview_stage)
        self.open_editor_btn.setIcon(FluentIcon.EDIT)
        self.open_editor_btn.clicked.connect(self._request_model_editor)
        self.open_editor_btn.hide()
        editor_toolbar.addWidget(self.open_editor_btn)
        editor_toolbar.addStretch(1)
        stage_toolbar.addLayout(editor_toolbar)
        resource_toolbar = editor_toolbar
        resource_toolbar.setSpacing(8)
        self.export_preview_btn = TransparentToolButton(FluentIcon.DOWNLOAD, self.preview_stage)
        self.export_preview_btn.setFixedSize(32, 32)
        self.export_preview_btn.setEnabled(False)
        self.export_preview_btn.clicked.connect(self.export_preview_resources)
        resource_toolbar.addWidget(self.export_preview_btn)

        self.preview_stage_close_btn = TransparentToolButton(FluentIcon.CLOSE, self.preview_stage)
        self.preview_stage_close_btn.setFixedSize(32, 32)
        self.preview_stage_close_btn.clicked.connect(self.close_preview_window)
        resource_toolbar.addWidget(self.preview_stage_close_btn)
        self.resource_combo = EditorComboBox(self.preview_stage)
        self.resource_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.resource_combo.currentIndexChanged.connect(self._resource_selected)
        self.resource_combo.hide()
        stage_toolbar.addWidget(self.resource_combo)
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
        self.preview_placeholder.setWordWrap(True)
        self.preview_placeholder.setStyleSheet("color: palette(placeholder-text);")
        self.preview_dock_layout.addWidget(self.preview_placeholder, 1)

        self.image_preview_panel = ImagePreviewPanel(self.preview_dock_area)
        self.image_preview_panel.setVisible(False)
        self.image_preview_panel.itemActivated.connect(self.on_preview_item_activated)
        self.image_preview_panel.itemsChanged.connect(self._sync_resource_selector)
        self.preview_dock_layout.addWidget(self.image_preview_panel, 1)
        self.image_item_list = self.image_preview_panel.take_item_list()

        self.spine_preview = SpinePreviewWidget(self.preview_dock_area)
        self.spine_preview.setVisible(False)
        self.spine_preview.documentLoaded.connect(self._on_spine_preview_document_loaded)
        self.spine_preview.previewReady.connect(self._on_spine_preview_ready)
        self.spine_preview.previewFailed.connect(self._on_spine_preview_error)
        self.spine_preview.stateChanged.connect(self._on_spine_preview_state)
        self.preview_dock_layout.addWidget(self.spine_preview, 1)
        self._ensure_embedded_live2d()
        self.preview_stage_layout.addWidget(self.preview_dock_area, 1)

        right_layout.addWidget(self.preview_stage, 1)

        # 右侧：触发动作与高级参数编辑
        action_widget = QWidget()
        self.right_sidebar = action_widget
        action_widget.setMinimumWidth(300)
        action_widget.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        action_layout = QVBoxLayout(action_widget)
        action_layout.setContentsMargins(12, 10, 0, 0)
        action_layout.setSpacing(10)
        self.resource_details_stack = QStackedWidget(action_widget)
        self.resource_details_stack.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        action_layout.addWidget(self.resource_details_stack, 1)
        self.empty_details = BodyLabel(action_widget)
        self.empty_details.setWordWrap(True)
        self.empty_details.setAlignment(Qt.AlignCenter)
        self.resource_details_stack.addWidget(self.empty_details)
        self.resource_details_stack.addWidget(self.image_item_list)
        self.live2d_details = EditorTabs(action_widget)
        self.spine_details = EditorTabs(action_widget)
        self.resource_details_stack.addWidget(self.live2d_details)
        self.resource_details_stack.addWidget(self.spine_details)
        self.live2d_animation_tab = QWidget(self.live2d_details)
        live2d_actions = EditorViewportLayout(self.live2d_animation_tab)
        live2d_actions.setContentsMargins(0, 0, 0, 0)
        live2d_actions.setSpacing(8)
        self.live2d_details.addTab(self.live2d_animation_tab, "")

        self.motion_group = CardWidget(action_widget)
        motion_layout = QVBoxLayout(self.motion_group)
        motion_layout.setContentsMargins(12, 12, 12, 12)
        motion_layout.setSpacing(8)
        self.motion_group_title = SubtitleLabel("", self.motion_group)
        motion_layout.addWidget(self.motion_group_title)
        self.motion_combo = EditableComboBox(self.motion_group)
        self.motion_combo.setClearButtonEnabled(True)
        self._motion_completer_model = QStringListModel(self.motion_combo)
        self._motion_completer = QCompleter(self._motion_completer_model, self.motion_combo)
        self._motion_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._motion_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._motion_completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._motion_completer.setMaxVisibleItems(14)
        self.motion_combo.setCompleter(self._motion_completer)
        self.motion_combo.currentIndexChanged.connect(self._on_motion_selection_changed)
        motion_layout.addWidget(self.motion_combo)
        motion_button_row = QHBoxLayout()
        self.play_motion_btn = PushButton("", self.motion_group)
        self.play_motion_btn.setIcon(FluentIcon.PLAY)
        self.play_motion_btn.clicked.connect(self.play_selected_motion)
        motion_button_row.addWidget(self.play_motion_btn, 1)
        motion_layout.addLayout(motion_button_row)

        motion_option_row = QVBoxLayout()
        self.loop_motion_check = CheckBox("", self.motion_group)
        self.loop_motion_check.toggled.connect(self.on_motion_loop_changed)
        motion_option_row.addWidget(self.loop_motion_check)
        self.auto_play_motion_check = CheckBox("", self.motion_group)
        self.auto_play_motion_check.setChecked(False)
        self.auto_play_motion_check.toggled.connect(self._save_preview_ui_state)
        motion_option_row.addWidget(self.auto_play_motion_check)
        motion_option_row.addStretch(1)
        motion_layout.addLayout(motion_option_row)
        self.save_pose_scheme_btn = PushButton("", self.motion_group)
        self.save_pose_scheme_btn.setIcon(FluentIcon.SAVE)
        self.save_pose_scheme_btn.clicked.connect(self.request_pose_scheme_save)
        self.save_pose_scheme_btn.setVisible(False)
        self.motion_hint_label = BodyLabel("", self.motion_group)
        self.motion_hint_label.setWordWrap(True)
        self.motion_hint_label.hide()
        live2d_actions.addWidget(self.motion_group)

        self.spine_controls = SpineAnimationControls(action_widget)
        self.spine_controls.skinChanged.connect(self._on_spine_skin_changed)
        self.spine_controls.animationChanged.connect(self._on_spine_animation_changed)
        self.spine_controls.pausedChanged.connect(self._on_spine_paused_changed)
        self.spine_controls.loopChanged.connect(self._on_spine_loop_changed)
        self.spine_controls.timeChanged.connect(self._on_spine_time_changed)
        self.spine_controls.resetRequested.connect(self._on_spine_reset_requested)
        self.spine_controls.exportRequested.connect(self._on_spine_export_requested)
        self.spine_controls.setVisible(False)
        self.spine_details.addTab(self.spine_controls, "")

        self.advanced_panel = Live2DSettingsPanel(action_widget, mode="parameters")
        self.advanced_panel.settingsChanged.connect(self.on_advanced_settings_changed)
        self.advanced_panel.requestRefreshParams.connect(self.on_request_refresh_params)
        # Keep pose actions above the scrollable parameter list: when a model
        # has many parameters, freeze/save/reset controls must stay reachable.
        self.advanced_panel.advanced_group_title.setVisible(False)
        self.advanced_panel.advanced_enable_check.setVisible(False)
        self.pose_controls_card = CardWidget(action_widget)
        pose_layout = QVBoxLayout(self.pose_controls_card)
        pose_layout.setContentsMargins(12, 12, 12, 12)
        pose_layout.setSpacing(8)
        self.pose_controls_title = SubtitleLabel("", self.pose_controls_card)
        pose_layout.addWidget(self.pose_controls_title)
        self.freeze_motion_check = CheckBox("", self.pose_controls_card)
        self.freeze_motion_check.toggled.connect(self._on_pose_freeze_toggled)
        pose_layout.addWidget(self.freeze_motion_check)
        pose_action_row = QHBoxLayout()
        self.refresh_pose_params_btn = PushButton("", self.pose_controls_card)
        self.refresh_pose_params_btn.clicked.connect(self.on_request_refresh_params)
        self.reset_pose_params_btn = PushButton("", self.pose_controls_card)
        self.reset_pose_params_btn.clicked.connect(self.advanced_panel.reset_advanced_params)
        pose_action_row.addWidget(self.refresh_pose_params_btn)
        pose_action_row.addWidget(self.reset_pose_params_btn)
        pose_action_row.addStretch(1)
        pose_layout.addLayout(pose_action_row)

        self.motion_timeline_frame = QFrame(self.pose_controls_card)
        timeline_layout = QVBoxLayout(self.motion_timeline_frame)
        timeline_layout.setContentsMargins(0, 0, 0, 0)
        timeline_layout.setSpacing(5)
        self.motion_timeline = Slider(Qt.Horizontal, self.motion_timeline_frame)
        self.motion_timeline.setRange(0, 1000)
        self.motion_timeline.valueChanged.connect(self._on_motion_timeline_slider_changed)
        timeline_layout.addWidget(self.motion_timeline)
        timeline_detail = QVBoxLayout()
        self.motion_time_spin = SpinBox(self.motion_timeline_frame)
        self.motion_time_spin.setSymbolVisible(False)
        self.motion_time_spin.setFixedWidth(126)
        self.motion_time_spin.setRange(0, 0)
        self.motion_time_spin.setSingleStep(33)
        self.motion_time_spin.setSuffix(" ms")
        self.motion_time_spin.valueChanged.connect(self._on_motion_timeline_spin_changed)
        self.motion_time_label = CaptionLabel("", self.motion_timeline_frame)
        self.motion_time_label.setWordWrap(True)
        self.motion_time_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        timeline_detail.addWidget(self.motion_time_spin)
        timeline_detail.addWidget(self.motion_time_label, 1)
        timeline_layout.addLayout(timeline_detail)
        self.motion_timeline_frame.setVisible(False)
        pose_layout.addWidget(self.motion_timeline_frame)
        self.save_pose_scheme_btn.setParent(self.pose_controls_card)
        pose_layout.addWidget(self.save_pose_scheme_btn)
        live2d_actions.addWidget(self.pose_controls_card)
        live2d_actions.addWidget(self.advanced_panel, 1)
        self._display_layouts = {}
        for kind, tabs in (("live2d", self.live2d_details), ("spine", self.spine_details)):
            host = QWidget(tabs)
            display_layout = EditorViewportLayout(host)
            display_layout.setContentsMargins(0, 0, 0, 0)
            self._display_layouts[kind] = display_layout
            tabs.addTab(host, "")
        self._display_layouts["live2d"].addWidget(self.settings_panel, 1)
        self.artmesh_panel = PreviewArtMeshPanel(self.live2d_details)
        self.artmesh_panel.overridesChanged.connect(self._preview_parts_changed)
        self.live2d_details.addTab(self.artmesh_panel, "")
        self.live2d_details.currentChanged.connect(self._on_preview_tab_changed)

        # 添加到分割器
        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.addWidget(action_widget)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([230, 540, 320])

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
        self._parameter_sync_timer = QTimer(self)
        self._parameter_sync_timer.setInterval(80)
        self._parameter_sync_timer.timeout.connect(self._sync_live_parameter_controls)
        self._set_motion_debug_visible(False)
        self._set_resource_mode("empty")
        self._restore_preview_ui_state()

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
        if self.spine_runtime_edit:
            self.spine_runtime_edit.setText(
                str(self.settings_manager.get("preview.spine_runtime_dir", "") or "")
            )
        if self.image_limit_label:
            self.image_limit_label.setText(tr("preview.image_limit_label"))
        if self.preview_stage_title:
            self.preview_stage_title.setText(tr("preview.stage_title"))
        if self.preview_placeholder:
            self.preview_placeholder.setText(tr("preview.stage_empty"))
        self.preview_btn.setText(tr("preview.preview_model"))
        self.close_all_btn.setText(tr("preview.close_window"))
        if self.export_preview_btn:
            self.export_preview_btn.setAccessibleName(tr("preview.export_preview"))
            self.export_preview_btn.setToolTip(tr("preview.export_preview_tooltip"))
        if self.preview_stage_close_btn:
            self.preview_stage_close_btn.setToolTip(tr("preview.close_window"))
            self.preview_stage_close_btn.setAccessibleName(tr("preview.close_window"))
        self.empty_details.setText(_layout_text("preview.layout.empty"))
        for tabs, keys in ((self.live2d_details, ("animation_parameters", "display_interaction", "artmesh")),
                           (self.spine_details, ("animation", "display"))):
            for index, key in enumerate(keys):
                tabs.setTabText(index, _layout_text(f"preview.layout.{key}"))
        self.resource_combo.setToolTip(tr("preview.preview_item_list_title"))
        self.artmesh_panel.retranslate_ui()
        if self.motion_group_title:
            self.motion_group_title.setText(tr("preview.trigger_motion"))
        if self.motion_combo:
            self.motion_combo.setPlaceholderText(tr("preview.motion_search_placeholder"))
        if self.play_motion_btn:
            self.play_motion_btn.setText(tr("preview.play_motion"))
        if self.loop_motion_check:
            self.loop_motion_check.setText(tr("preview.loop_motion"))
        if self.auto_play_motion_check:
            self.auto_play_motion_check.setText(tr("preview.auto_play_motion"))
        if self.save_pose_scheme_btn:
            self.save_pose_scheme_btn.setText(tr("preview.save_psd_pose_scheme"))
        if self.pose_controls_title:
            self.pose_controls_title.setText(tr("preview.advanced_settings"))
        if self.freeze_motion_check:
            self.freeze_motion_check.setText(tr("preview.enable_advanced_overrides"))
        if self.refresh_pose_params_btn:
            self.refresh_pose_params_btn.setText(tr("preview.refresh_current_model"))
        if self.reset_pose_params_btn:
            self.reset_pose_params_btn.setText(tr("preview.reset_advanced_params"))
        if self.motion_hint_label:
            self.motion_hint_label.setText(tr("preview.trigger_motion_hint"))
            self.play_motion_btn.setToolTip(tr("preview.trigger_motion_hint"))
        if self.image_preview_panel:
            self.image_preview_panel.retranslate_ui()

        if hasattr(self, "drag_drop_area") and self.drag_drop_area:
            self.drag_drop_area.retranslate_ui()
        if hasattr(self, "settings_panel") and self.settings_panel:
            self.settings_panel.retranslate_ui()
        if self.advanced_panel:
            self.advanced_panel.retranslate_ui()
        if self.spine_controls:
            self.spine_controls.retranslate_ui()
        self._update_sidebar_button_text()
        self._update_editor_button()

        if not self.current_model_path and not (
            self.image_preview_panel and self.image_preview_panel.isVisible()
        ):
            self.model_info_text_box.setMarkdown(tr("preview.model_info_empty"))
        if self.motion_combo and not self._motion_items:
            self.motion_combo.clear()
            self.motion_combo.addItem(tr("preview.motion_none"))
            self.motion_combo.setEnabled(False)
            self.play_motion_btn.setEnabled(False)

    def _set_editor_source(self, kind: str | None = None, path: str | None = None):
        self._editor_source = (kind, os.path.abspath(path)) if kind and path and os.path.isfile(path) else None
        self._update_editor_button()

    def _set_resource_mode(self, mode):
        mode = mode if mode in {"live2d", "spine", "image"} else "empty"
        if mode != self._resource_mode and mode != "live2d":
            self._clear_live2d_controls()
        self._resource_mode = mode
        pages = {"empty": self.empty_details, "image": self.image_item_list,
                 "live2d": self.live2d_details, "spine": self.spine_details}
        self.resource_details_stack.setCurrentWidget(pages[mode])
        for widget in (self.motion_group, self.pose_controls_card, self.advanced_panel):
            widget.setVisible(mode == "live2d")
        self.spine_controls.setVisible(mode == "spine")
        if mode in self._display_layouts:
            self._display_layouts[mode].addWidget(self.settings_panel, 1)
            self.settings_panel.show()
            self.settings_panel.set_spine_mode(mode == "spine")
        if mode == "image":
            self.image_item_list.show()
        self.image_limit_label.setVisible(mode != "spine")
        self.image_limit_spinbox.setVisible(mode != "spine")

    def _clear_live2d_controls(self):
        self._live2d_load_timer.stop()
        self._parameter_refresh_timer.stop()
        if self._parameter_sync_timer:
            self._parameter_sync_timer.stop()
        self._artmesh_loaded_for = None
        if hasattr(self, "artmesh_panel"):
            self.artmesh_panel.clear()
        if self.advanced_panel:
            self.advanced_panel.rebuild_advanced_params([])
            with QSignalBlocker(self.advanced_panel.advanced_enable_check):
                self.advanced_panel.advanced_enable_check.setChecked(False)
        if self.freeze_motion_check:
            with QSignalBlocker(self.freeze_motion_check):
                self.freeze_motion_check.setChecked(False)
        if self.motion_combo:
            self._populate_motion_controls([])
        self._update_motion_timeline_visibility()

    def _sync_resource_selector(self):
        panel = self.image_preview_panel
        if panel is None:
            return
        with QSignalBlocker(self.resource_combo):
            self.resource_combo.clear()
            for index, item in enumerate(panel._preview_items):
                self.resource_combo.addItem(str(item.get("label") or Path(item.get("path", "")).name), userData=index)
            self.resource_combo.setCurrentIndex(panel._current_index)
        self.resource_combo.setVisible(len(panel._preview_items) > 1)

    def _resource_selected(self, index):
        item_index = self.resource_combo.itemData(index)
        if item_index is not None:
            self.on_preview_item_activated(int(item_index))

    def _preview_parameters(self):
        meta = self.live2d_preview.get_parameter_meta_list() if self.live2d_preview else []
        return {str(item["id"]): float(item.get("value", 0)) for item in meta if item.get("id")}

    def _ensure_artmesh_loaded(self):
        if self._resource_mode != "live2d" or not self.current_model_path:
            return False
        if self._artmesh_loaded_for == self.current_model_path:
            return True
        latest = self._load_latest_preview_settings() or {}
        dll = (latest.get("tools") or {}).get("cubism_core_dll_path")
        if not dll:
            getter = getattr(self.settings_manager, "get_cubism_core_dll_path", None)
            dll = getter() if callable(getter) else None
        if self.artmesh_panel.load_source(self.current_model_path, dll):
            self._artmesh_loaded_for = self.current_model_path
            return True
        return False

    def _on_preview_tab_changed(self, index):
        if index == 2 and self._ensure_artmesh_loaded():
            self.artmesh_panel.refresh(self._preview_parameters())

    def _preview_parts_changed(self, values, defaults):
        setter = getattr(self.live2d_preview, "set_part_opacity_overrides", None)
        if callable(setter):
            setter(values, defaults)

    def _preview_drawable_clicked(self, drawable_id):
        if self._ensure_artmesh_loaded() and any(entry.drawable_id == str(drawable_id)
                                               for entry in self.artmesh_panel.inspector.entries):
            self.live2d_details.setCurrentIndex(2)
            self.artmesh_panel.select_drawable(drawable_id)

    def _preview_model_point_clicked(self, x, y):
        if self._ensure_artmesh_loaded():
            self.live2d_details.setCurrentIndex(2)
            self.artmesh_panel.select_model_point(x, y, self._preview_parameters())

    def _update_editor_button(self):
        button = self.open_editor_btn
        if button is None:
            return
        valid = self._editor_source is not None and os.path.isfile(self._editor_source[1])
        button.setVisible(valid)
        button.setEnabled(valid)
        if valid:
            button.setText(tr(f"preview.open_{self._editor_source[0]}_editor"))

    def current_editor_source(self):
        if self._editor_source and os.path.isfile(self._editor_source[1]):
            return self._editor_source
        return None

    def _request_model_editor(self):
        source = self.current_editor_source()
        if source is not None:
            self.editorRequested.emit(*source)
        else:
            self._update_editor_button()

    def acquire_editor_source(self, model_path: str):
        """Lease preview workspaces while an editor copies its own session."""
        source = Path(model_path).resolve()
        roots = set()
        for name in ("_model_preview_temp_dirs", "_spine_preview_temp_dirs", "_archive_preview_temp_dirs", "_folder_preview_temp_dirs"):
            for directory in getattr(self, name, ()):
                root = str(Path(directory).resolve())
                if source.is_relative_to(Path(root)):
                    roots.add(root)
        for root in self._editor_leased_dirs:
            if source.is_relative_to(Path(root)):
                roots.add(root)
        if not roots:
            return None
        token = uuid.uuid4().hex
        self._editor_source_leases[token] = roots
        for root in roots:
            self._editor_leased_dirs[root] = self._editor_leased_dirs.get(root, 0) + 1
        return token

    def release_editor_source(self, token):
        for root in self._editor_source_leases.pop(token, ()):
            count = self._editor_leased_dirs.get(root, 0) - 1
            if count > 0:
                self._editor_leased_dirs[root] = count
                continue
            self._editor_leased_dirs.pop(root, None)
            if root in self._deferred_preview_temp_dirs:
                self._deferred_preview_temp_dirs.discard(root)
                self._dispose_preview_temp_dir(root)

    def _dispose_preview_temp_dir(self, directory):
        root = str(Path(directory).resolve())
        if self._editor_leased_dirs.get(root, 0):
            self._deferred_preview_temp_dirs.add(root)
            return
        if os.path.isdir(root):
            shutil.rmtree(root, ignore_errors=True)

    def set_active(self, active: bool):
        active = bool(active)
        spine = self.spine_preview
        if spine is not None:
            timer = getattr(spine, "_status_timer", None)
            if not active:
                if not self._native_playback_suspended:
                    self._resume_spine_paused = bool(getattr(spine, "_last_state", {}).get("paused", False))
                spine.set_paused(True)
                if timer is not None:
                    timer.stop()
            elif self._native_playback_suspended:
                spine.set_paused(self._resume_spine_paused)
                if timer is not None and getattr(spine, "_model", None) is not None:
                    timer.start()
        canvas = getattr(self.live2d_preview, "live2d_canvas", None)
        activate_rendering = getattr(self.live2d_preview, "set_rendering_active", None)
        if callable(activate_rendering):
            activate_rendering(active)
        elif canvas is not None:
            timer_id = getattr(canvas, "_render_timer_id", None)
            if not active and timer_id is not None:
                canvas.killTimer(timer_id)
                canvas._render_timer_id = None
            elif active and timer_id is None:
                canvas._render_timer_id = canvas.startTimer(int(1000 / 60))
        if not active:
            for timer in (self._parameter_sync_timer, self._preview_dock_timer):
                if timer is not None:
                    timer.stop()
        elif self._resource_mode == "live2d" and self.advanced_panel.isVisible():
            self._parameter_sync_timer.start()
        self._native_playback_suspended = not active
        self._preview_page_active = active

    def showEvent(self, event):
        super().showEvent(event)
        self.set_active(True)

    def hideEvent(self, event):
        self.set_active(False)
        super().hideEvent(event)

    def prepare_shutdown(self):
        """Finish or cancel import workers before any page is destroyed."""
        workers = []
        for name in ("_model_preview_thread", "_image_preview_thread", "_archive_preview_thread", "_folder_preview_thread", "_preview_export_thread"):
            worker = getattr(self, name, None)
            if worker is not None and _is_qt_object_valid(worker) and worker.isRunning():
                workers.append(worker)
        workers.extend(worker for worker in self._spine_preview_workers if worker is not None and _is_qt_object_valid(worker) and worker.isRunning() and worker not in workers)
        for worker in workers:
            worker.requestInterruption()
        if any(not worker.wait(1500) for worker in workers):
            return False
        return True

    def shutdown(self):
        if not self.prepare_shutdown():
            return False
        self.set_active(False)
        self._live2d_load_timer.stop()
        self._parameter_refresh_timer.stop()
        self._terminate_preview_process()
        self._destroy_embedded_live2d()
        self._destroy_embedded_spine()
        self.artmesh_panel.shutdown()
        # MainWindow releases editor leases after editor shutdown. A copying
        # editor may still need the source even after this view is destroyed.
        for cleanup in (self._cleanup_model_preview_temp_dirs, self._cleanup_spine_preview_temp_dirs, self._cleanup_image_preview_temp_dirs, self._cleanup_archive_preview_temp_dirs, self._cleanup_folder_preview_temp_dirs):
            cleanup()
        return True

    def _toggle_sidebar(self, side: str):
        widget = self.left_sidebar if side == "left" else self.right_sidebar
        if widget is None:
            return
        widget.setVisible(not widget.isVisible())
        self._update_sidebar_button_text()
        self._save_preview_ui_state()

    def _update_sidebar_button_text(self):
        if self.left_sidebar_btn and self.left_sidebar:
            key = "preview.show_left_sidebar" if self.left_sidebar.isHidden() else "preview.hide_left_sidebar"
            self.left_sidebar_btn.setText(tr(key))
        if self.right_sidebar_btn and self.right_sidebar:
            key = "preview.show_right_sidebar" if self.right_sidebar.isHidden() else "preview.hide_right_sidebar"
            self.right_sidebar_btn.setText(tr(key))

    def _restore_preview_ui_state(self):
        state = dict(self.settings_manager.get("preview.ui_state", {}) or {})
        panel = self.settings_panel
        if panel:
            controls = (
                (panel.opacity_slider, int(state.get("opacity", 100))),
                (panel.rotation_slider, int(state.get("rotation", 0))),
                (panel.scale_slider, int(state.get("scale", 100))),
                (panel.antialias_check, bool(state.get("antialias", True))),
                (panel.position_x_spinbox, int(state.get("offset_x", 0))),
                (panel.position_y_spinbox, int(state.get("offset_y", 0))),
                (panel.bg_transparent_check, bool(state.get("transparent_bg", True))),
                (panel.mouse_tracking_check, bool(state.get("mouse_tracking", True))),
                (panel.auto_blink_check, bool(state.get("auto_blink", True))),
                (panel.auto_breath_check, bool(state.get("auto_breath", True))),
            )
            for control, value in controls:
                if control is None:
                    continue
                control.blockSignals(True)
                control.setChecked(value) if isinstance(value, bool) else control.setValue(value)
                control.blockSignals(False)
        if self.loop_motion_check:
            self.loop_motion_check.blockSignals(True)
            self.loop_motion_check.setChecked(bool(state.get("motion_loop", False)))
            self.loop_motion_check.blockSignals(False)
        if self.auto_play_motion_check:
            self.auto_play_motion_check.blockSignals(True)
            self.auto_play_motion_check.setChecked(bool(state.get("motion_auto_play", False)))
            self.auto_play_motion_check.blockSignals(False)
        if self.freeze_motion_check:
            freeze_pose = False
            self.freeze_motion_check.blockSignals(True)
            self.freeze_motion_check.setChecked(freeze_pose)
            self.freeze_motion_check.blockSignals(False)
            if self.advanced_panel and self.advanced_panel.advanced_enable_check:
                self.advanced_panel.advanced_enable_check.blockSignals(True)
                self.advanced_panel.advanced_enable_check.setChecked(freeze_pose)
                self.advanced_panel.advanced_enable_check.blockSignals(False)
        if self.left_sidebar and not bool(state.get("left_sidebar_visible", True)):
            self.left_sidebar.hide()
        if self.right_sidebar and not bool(state.get("right_sidebar_visible", True)):
            self.right_sidebar.hide()
        self._update_sidebar_button_text()

    def _save_preview_ui_state(self, *_args):
        panel = self.settings_panel
        if panel is None:
            return
        selected_motion = ""
        if self.motion_combo and self.motion_combo.currentIndex() >= 0:
            selected_motion = str(self.motion_combo.currentData() or "")
        ui_state = {
            "opacity": panel.opacity_slider.value(),
            "rotation": panel.rotation_slider.value(),
            "scale": panel.scale_slider.value(),
            "antialias": panel.antialias_check.isChecked(),
            "offset_x": panel.position_x_spinbox.value(),
            "offset_y": panel.position_y_spinbox.value(),
            "transparent_bg": panel.bg_transparent_check.isChecked(),
            "mouse_tracking": panel.mouse_tracking_check.isChecked(),
            "auto_blink": panel.auto_blink_check.isChecked(),
            "auto_breath": panel.auto_breath_check.isChecked(),
            "motion_loop": bool(self.loop_motion_check and self.loop_motion_check.isChecked()),
            "motion_auto_play": bool(
                self.auto_play_motion_check and self.auto_play_motion_check.isChecked()
            ),
            "freeze_pose": bool(self.freeze_motion_check and self.freeze_motion_check.isChecked()),
            "left_sidebar_visible": bool(self.left_sidebar and not self.left_sidebar.isHidden()),
            "right_sidebar_visible": bool(self.right_sidebar and not self.right_sidebar.isHidden()),
            "selected_motion": selected_motion,
        }
        # SettingsPage owns the same JSON file through a separate manager.  A
        # stale PreviewPage manager must not write its old runtime/tool values
        # back merely because a display slider changed.
        self._merge_latest_preview_settings(ui_state)
        self.settings_manager.set("preview.ui_state", ui_state)

    def _load_latest_preview_settings(self):
        """Read the current settings snapshot without replacing the page cache.

        PreviewPage and SettingsPage each have a SettingsManager instance.  A
        fresh manager with the same settings file is the safest way to read a
        runtime selected by SettingsPage without disturbing the live preview
        UI state.  Lightweight diagnostic/test settings objects may not expose
        ``settings_file`` or ``load_settings``; those continue to use their
        in-memory values.
        """

        manager = self.settings_manager
        settings_file = getattr(manager, "settings_file", None)
        if settings_file:
            try:
                latest_manager = SettingsManager(settings_file=settings_file)
                latest = getattr(latest_manager, "settings", None)
                if isinstance(latest, dict):
                    return latest
            except Exception:
                # A test/sandbox replacement may only support a no-argument
                # constructor.  Fall through to its own loader/cache.
                pass

        loader = getattr(manager, "load_settings", None)
        if callable(loader):
            try:
                latest = loader()
                if isinstance(latest, dict):
                    return latest
            except Exception:
                pass
        return None

    def _spine_runtime_dir_for_import(self) -> str:
        """Get the runtime selected in SettingsPage immediately before import."""

        manager = self.settings_manager
        settings_file = getattr(manager, "settings_file", None)
        latest = self._load_latest_preview_settings()
        if isinstance(latest, dict):
            preview = latest.get("preview")
            if isinstance(preview, dict) and "spine_runtime_dir" in preview:
                # Sandbox managers without a settings file are intentionally
                # kept in memory, so a diagnostic can select a runtime by
                # editing ``settings`` directly between imports.
                if not settings_file and callable(getattr(manager, "load_settings", None)):
                    current_ui = manager.get("preview.ui_state", {})
                    manager.settings = latest
                    if isinstance(current_ui, dict) and current_ui:
                        manager.settings.setdefault("preview", {}).setdefault(
                            "ui_state", {}
                        ).update(current_ui)
                return str(preview.get("spine_runtime_dir") or "").strip()
        return str(manager.get("preview.spine_runtime_dir", "") or "").strip()

    def _spine_preview_unify_for_import(self) -> bool:
        """Compatibility alias for older integrations and tests."""

        return self._spine_compatibility_mode_for_import()

    def _spine_compatibility_mode_for_import(self) -> bool:
        """Capture the unified Spine policy on the GUI thread."""

        manager = self.settings_manager
        latest = self._load_latest_preview_settings()
        if isinstance(latest, dict):
            section = latest.get("spine")
            if isinstance(section, dict) and "compatibility_mode" in section:
                return bool(section.get("compatibility_mode"))
        getter = getattr(manager, "get_spine_compatibility_mode", None)
        if callable(getter):
            return bool(getter())
        # A small compatibility fallback for test doubles created before the
        # consolidated setting existed.
        legacy = getattr(manager, "get_spine_preview_unify_version", None)
        if callable(legacy):
            return bool(legacy())
        return bool(manager.get("spine.compatibility_mode", manager.get("spine_preview.unify_version", True)))

    def _spine_selected_runtime_version_for_import(self) -> str:
        """Capture the preserved off-mode runtime choice on the GUI thread."""

        manager = self.settings_manager
        latest = self._load_latest_preview_settings()
        if isinstance(latest, dict):
            section = latest.get("spine")
            if isinstance(section, dict) and "runtime_version" in section:
                value = str(section.get("runtime_version") or SPINE_COMPATIBILITY_VERSION).strip()
                if value.startswith("4.0."):
                    return "4.0"
                return value
        getter = getattr(manager, "get_spine_runtime_version", None)
        if callable(getter):
            return str(getter() or SPINE_COMPATIBILITY_VERSION)
        value = str(manager.get("spine.runtime_version", SPINE_COMPATIBILITY_VERSION) or SPINE_COMPATIBILITY_VERSION)
        return "4.0" if value.startswith("4.0.") else value

    def _spine_runtime_policy_for_import(self, force_compatibility: bool | None = None) -> SpineRuntimePolicy:
        compatibility = (
            self._spine_compatibility_mode_for_import()
            if force_compatibility is None
            else bool(force_compatibility)
        )
        return SpineRuntimePolicy.from_values(
            compatibility_mode=compatibility,
            selected_version=self._spine_selected_runtime_version_for_import(),
            runtime_root=self._spine_runtime_dir_for_import() or None,
            prefer_installed=compatibility,
        )

    def _merge_latest_preview_settings(self, ui_state: dict):
        """Merge the current UI state into the newest persisted settings."""

        latest = self._load_latest_preview_settings()
        if not isinstance(latest, dict):
            return
        preview = latest.setdefault("preview", {})
        if not isinstance(preview, dict):
            preview = {}
            latest["preview"] = preview
        disk_ui_state = preview.get("ui_state")
        merged_ui_state = dict(disk_ui_state) if isinstance(disk_ui_state, dict) else {}
        merged_ui_state.update(ui_state)
        preview["ui_state"] = merged_ui_state
        self.settings_manager.settings = latest

    def on_preview_image_limit_changed(self, value: int):
        self.settings_manager.set("preview.image_limit", int(value))

    def browse_spine_runtime(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select official Spine runtime directory",
            str(self.settings_manager.get("preview.spine_runtime_dir", "") or ""),
        )
        if not folder:
            return
        self.settings_manager.set("preview.spine_runtime_dir", folder)
        if self.spine_runtime_edit:
            self.spine_runtime_edit.setText(folder)
        current = str(self.source_edit.text() or "") if self.source_edit else ""
        if current and (
            _is_spine_preview_source(current)
            or os.path.splitext(current)[1].lower() in PACKAGE_PREVIEW_EXTENSIONS
        ):
            self.start_spine_preview_import(
                current,
                package_fallback=os.path.splitext(current)[1].lower() in PACKAGE_PREVIEW_EXTENSIONS,
            )

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
        self._close_embedded_live2d()
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

    def _ensure_embedded_live2d(self):
        if self.live2d_preview is not None:
            return self.live2d_preview
        if self.preview_dock_area is None or self.preview_dock_layout is None:
            return None
        preview = Live2DPreviewWindow(
            None,
            parent=self.preview_dock_area,
            embedded=True,
        )
        if preview.live2d_canvas is None:
            preview.deleteLater()
            return None
        self.live2d_preview = preview
        self.preview_dock_layout.addWidget(preview, 1)
        preview.live2d_canvas.drawableClicked.connect(self._preview_drawable_clicked)
        preview.live2d_canvas.modelPointClicked.connect(self._preview_model_point_clicked)
        preview.hide()
        return preview

    def _close_embedded_live2d(self):
        preview = self.live2d_preview
        if preview is None:
            return
        try:
            preview.unload_model()
            preview.hide()
        except Exception:
            pass

    def _destroy_embedded_live2d(self):
        preview = self.live2d_preview
        self.live2d_preview = None
        if preview is None:
            return
        try:
            if self.preview_dock_layout:
                self.preview_dock_layout.removeWidget(preview)
            preview.close()
            preview.deleteLater()
        except Exception:
            pass

    def _destroy_embedded_spine(self):
        preview = self.spine_preview
        self.spine_preview = None
        if preview is None:
            return
        try:
            if self.preview_dock_layout:
                self.preview_dock_layout.removeWidget(preview)
            preview.shutdown()
            preview.close()
            preview.deleteLater()
        except Exception:
            pass

    def _clear_embedded_spine(self):
        preview = self.spine_preview
        self._active_spine_plan = None
        self._active_spine_preview_key = ""
        if self.spine_controls:
            self.spine_controls.clear()
        if preview is None:
            self._set_spine_mode(False)
            return
        try:
            preview.clear_preview()
        except Exception:
            pass
        self._set_spine_mode(False)

    def _set_spine_mode(self, active: bool):
        """Swap controls by resource type without changing sidebar preferences."""
        active = bool(active)
        self._spine_mode = active
        if active:
            self._set_motion_debug_visible(False)
            self._set_resource_mode("spine")
        elif self._resource_mode == "spine":
            self.spine_controls.clear()
            self._set_resource_mode("empty")
        self._spine_sidebar_state = None
        self._update_sidebar_button_text()

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

    def _show_stage_placeholder(self, text: str | None = None, *, keep_editor_source=False):
        if not keep_editor_source:
            self._set_editor_source()
        self._set_resource_mode("live2d" if keep_editor_source else "empty")
        if self.live2d_preview:
            self.live2d_preview.setVisible(False)
        if self.spine_preview:
            self.spine_preview.setVisible(False)
        if self.image_preview_panel:
            self.image_preview_panel.setVisible(False)
        if self.preview_placeholder:
            self.preview_placeholder.setText(text or tr("preview.stage_empty"))
            self.preview_placeholder.setVisible(True)

    def _show_image_stage(self):
        self._set_editor_source()
        self._set_resource_mode("image")
        if self.live2d_preview:
            self.live2d_preview.setVisible(False)
        if self.spine_preview:
            self.spine_preview.setVisible(False)
        if self.preview_placeholder:
            self.preview_placeholder.setVisible(False)
        if self.image_preview_panel:
            self.image_preview_panel.setVisible(True)

    def _show_spine_stage(self):
        if self.live2d_preview:
            self.live2d_preview.setVisible(False)
        if self.image_preview_panel:
            self.image_preview_panel.setVisible(False)
        if self.preview_placeholder:
            self.preview_placeholder.setVisible(False)
        if self.spine_preview:
            self.spine_preview.setVisible(True)
        self._set_spine_mode(True)

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
        previous = str(self.motion_combo.currentData() or "") if self.motion_combo else ""
        if not previous:
            previous = str(
                self.settings_manager.get("preview.ui_state.selected_motion", "") or ""
            )
        self._motion_items = list(motions or [])
        if not self.motion_combo or not self.play_motion_btn:
            return
        self.motion_combo.blockSignals(True)
        self.motion_combo.clear()
        if not self._motion_items:
            self.motion_combo.addItem(tr("preview.motion_none"))
            if hasattr(self, "_motion_completer_model"):
                self._motion_completer_model.setStringList([])
            self.motion_combo.setEnabled(False)
            self.play_motion_btn.setEnabled(False)
            self.motion_combo.blockSignals(False)
            return
        labels = []
        for motion in self._motion_items:
            label = str(motion.get("display") or motion.get("group") or "motion")
            key = f"{motion.get('group', '')}::{int(motion.get('index', 0))}"
            labels.append(label)
            self.motion_combo.addItem(label, userData=key)
        if hasattr(self, "_motion_completer_model"):
            self._motion_completer_model.setStringList(labels)
        self.motion_combo.setEnabled(True)
        self.play_motion_btn.setEnabled(True)
        selected_index = self.motion_combo.findData(previous) if previous else -1
        self.motion_combo.setCurrentIndex(selected_index if selected_index >= 0 else 0)
        self.motion_combo.blockSignals(False)
        self._on_motion_selection_changed(self.motion_combo.currentIndex())

    def _set_motion_debug_visible(self, visible: bool):
        if self.motion_group:
            self.motion_group.setEnabled(bool(visible))
        if self.pose_controls_card:
            self.pose_controls_card.setEnabled(bool(visible))
        if self.advanced_panel:
            self.advanced_panel.setEnabled(bool(visible))
        if self._parameter_sync_timer:
            if visible and self.advanced_panel and not self.advanced_panel.isHidden():
                self._parameter_sync_timer.start()
            else:
                self._parameter_sync_timer.stop()

    def play_selected_motion(self):
        if not self._motion_items or not self.motion_combo:
            return
        index = self.motion_combo.currentIndex()
        if index < 0 or index >= len(self._motion_items):
            query = self.motion_combo.currentText().strip().lower()
            index = next(
                (
                    item_index
                    for item_index, item in enumerate(self._motion_items)
                    if query
                    and query
                    in str(item.get("display") or item.get("group") or "").lower()
                ),
                -1,
            )
            if index < 0:
                return
            self.motion_combo.setCurrentIndex(index)
        motion = self._motion_items[index]
        if self.freeze_motion_check:
            self.freeze_motion_check.setChecked(False)
        if self.live2d_preview:
            self.live2d_preview.play_motion(
                str(motion.get("group", "")),
                int(motion.get("index", 0)),
            )

    def _on_motion_selection_changed(self, index: int):
        if not self.live2d_preview or index < 0 or index >= len(self._motion_items):
            return
        motion = self._motion_items[index]
        self.live2d_preview.set_selected_motion(
            str(motion.get("group", "")),
            int(motion.get("index", 0)),
        )
        if self.auto_play_motion_check and self.auto_play_motion_check.isChecked():
            self.play_selected_motion()
        self._update_motion_timeline_visibility()
        self._save_preview_ui_state()

    def _on_pose_freeze_toggled(self, frozen: bool):
        if self._freeze_syncing:
            return
        self._freeze_syncing = True
        try:
            if self.advanced_panel and self.advanced_panel.advanced_enable_check:
                control = self.advanced_panel.advanced_enable_check
                control.blockSignals(True)
                control.setChecked(bool(frozen))
                control.blockSignals(False)
            self.on_advanced_settings_changed(
                self.advanced_panel.get_settings() if self.advanced_panel else {}
            )
            self.on_motion_freeze_changed(bool(frozen))
        finally:
            self._freeze_syncing = False
        self._update_motion_timeline_visibility()
        self._save_preview_ui_state()

    def _selected_motion_item(self) -> dict | None:
        if not self.motion_combo:
            return None
        index = self.motion_combo.currentIndex()
        return self._motion_items[index] if 0 <= index < len(self._motion_items) else None

    def _update_motion_timeline_visibility(self):
        motion = self._selected_motion_item()
        duration = float((motion or {}).get("duration") or 0.0)
        visible = bool(
            self.freeze_motion_check
            and self.freeze_motion_check.isChecked()
            and motion
            and duration > 0.0
        )
        if not self.motion_timeline_frame:
            return
        self.motion_timeline_frame.setVisible(visible)
        if not visible:
            return
        maximum_ms = max(1, int(round(duration * 1000)))
        self._timeline_sync = True
        try:
            self.motion_time_spin.setRange(0, maximum_ms)
            current_ms = min(self.motion_time_spin.value(), maximum_ms)
            self.motion_time_spin.setValue(current_ms)
            self.motion_timeline.setValue(int(round(current_ms / maximum_ms * 1000)))
            self.motion_time_label.setText(
                tr("preview.motion_timeline_position", current=current_ms / 1000, total=duration)
            )
        finally:
            self._timeline_sync = False

    def _on_motion_timeline_slider_changed(self, value: int):
        if self._timeline_sync or not self.motion_time_spin:
            return
        maximum = max(1, self.motion_time_spin.maximum())
        self._set_motion_timeline_ms(int(round(value / 1000 * maximum)))

    def _on_motion_timeline_spin_changed(self, value: int):
        if self._timeline_sync:
            return
        self._set_motion_timeline_ms(int(value))

    def _set_motion_timeline_ms(self, milliseconds: int):
        motion = self._selected_motion_item()
        if not motion or not self.live2d_preview or not self.motion_time_spin:
            return
        maximum = max(1, self.motion_time_spin.maximum())
        target = min(max(0, int(milliseconds)), maximum)
        self._timeline_sync = True
        try:
            self.motion_time_spin.setValue(target)
            self.motion_timeline.setValue(int(round(target / maximum * 1000)))
            self.motion_time_label.setText(
                tr(
                    "preview.motion_timeline_position",
                    current=target / 1000,
                    total=float(motion.get("duration") or 0.0),
                )
            )
        finally:
            self._timeline_sync = False
        values = self.live2d_preview.set_motion_time(motion, target / 1000)
        if values and self.advanced_panel:
            self.advanced_panel.set_advanced_param_values(values)

    def on_motion_freeze_changed(self, frozen: bool):
        if not self.live2d_preview:
            return
        self.live2d_preview.set_motion_frozen(bool(frozen))
        if frozen:
            self._sync_live_parameter_controls(force=True)
        self._update_motion_timeline_visibility()

    def on_motion_loop_changed(self, enabled: bool):
        if self.live2d_preview:
            self.live2d_preview.set_motion_loop(bool(enabled))
        self._save_preview_ui_state()

    def on_advanced_settings_changed(self, settings: dict):
        if self.live2d_preview and self.advanced_panel:
            editing_pose = bool(settings.get("advanced_enabled", False))
            if self.freeze_motion_check and not self._freeze_syncing:
                self.freeze_motion_check.blockSignals(True)
                self.freeze_motion_check.setChecked(editing_pose)
                self.freeze_motion_check.blockSignals(False)
            self.on_motion_freeze_changed(editing_pose)
            self.live2d_preview.apply_settings(
                self.advanced_panel.get_advanced_settings()
            )

    def _sync_live_parameter_controls(self, force: bool = False):
        if (
            self.live2d_preview is None
            or self.advanced_panel is None
            or (self.freeze_motion_check and self.freeze_motion_check.isChecked() and not force)
        ):
            return
        meta = self.live2d_preview.get_parameter_meta_list()
        if not meta:
            return
        if not self.advanced_panel.advanced_param_sliders:
            self.advanced_panel.rebuild_advanced_params(meta)
        self.advanced_panel.sync_advanced_param_values(meta)

    def _cleanup_temp_model_json(self):
        """Forget the model reference; owned temporary directories clean it up."""
        # current_model_path can point to a user's original model or a MOD
        # export. Only directories tracked by the importer may be deleted.
        self._temp_model_json_path = None

    def _cleanup_image_preview_temp_dirs(self):
        for temp_dir in list(self._image_preview_temp_dirs):
            try:
                if temp_dir and os.path.isdir(temp_dir):
                    self._dispose_preview_temp_dir(temp_dir)
            except Exception:
                pass
        self._image_preview_temp_dirs = []
        if self._preview_export_kind == "textures":
            self._clear_preview_export_payload()

    def _cleanup_model_preview_temp_dirs(self):
        for temp_dir in list(self._model_preview_temp_dirs):
            try:
                if temp_dir and os.path.isdir(temp_dir):
                    self._dispose_preview_temp_dir(temp_dir)
            except Exception:
                pass
        self._model_preview_temp_dirs = []
        if self._preview_export_kind == "live2d":
            self._clear_preview_export_payload()

    def _cleanup_spine_preview_temp_dirs(self):
        """Cancel Spine imports and release their disposable workspaces.

        A finished ``QThread`` may already have had its C++ object destroyed
        while the Python wrapper is still referenced.  Always test validity
        before touching it and clear the active pointer before scheduling
        ``deleteLater()`` so a later import cannot call ``isRunning()`` on a
        dangling wrapper.
        """

        self._spine_preview_generation += 1
        running = []
        processed = set()
        for thread in list(self._spine_preview_workers):
            if not _is_qt_object_valid(thread):
                if thread is self._spine_preview_thread:
                    self._spine_preview_thread = None
                continue
            if _spine_thread_is_running(thread):
                thread.requestInterruption()
                running.append(thread)
            else:
                processed.add(id(thread))
                result = getattr(thread, "result", None)
                temp_dir = getattr(result, "temp_dir", None)
                if temp_dir:
                    self._dispose_preview_temp_dir(temp_dir)
                if thread is self._spine_preview_thread:
                    self._spine_preview_thread = None
                thread.deleteLater()
        self._spine_preview_workers = running
        current = self._spine_preview_thread
        if current is not None and id(current) not in processed:
            if not _is_qt_object_valid(current):
                self._spine_preview_thread = None
            elif _spine_thread_is_running(current):
                current.requestInterruption()
                if not any(current is item for item in running):
                    running.append(current)
                    self._spine_preview_workers = running
            else:
                result = getattr(current, "result", None)
                temp_dir = getattr(result, "temp_dir", None)
                if temp_dir:
                    self._dispose_preview_temp_dir(temp_dir)
                current.deleteLater()
                self._spine_preview_thread = None

        # A worker may still be writing a disposable extraction.  Leave its
        # directory alone until finished() instead of deleting live files.
        if running:
            return
        for temp_dir in list(self._spine_preview_temp_dirs):
            try:
                if temp_dir and os.path.isdir(temp_dir):
                    self._dispose_preview_temp_dir(temp_dir)
            except Exception:
                pass
        self._spine_preview_temp_dirs = []

    def _cleanup_finished_spine_thread(self):
        finished = []
        running = []
        for thread in list(self._spine_preview_workers):
            if not _is_qt_object_valid(thread):
                if thread is self._spine_preview_thread:
                    self._spine_preview_thread = None
                continue
            if _spine_thread_is_running(thread):
                running.append(thread)
            else:
                finished.append(thread)
        self._spine_preview_workers = running
        for thread in finished:
            result = getattr(thread, "result", None)
            temp_dir = getattr(result, "temp_dir", None)
            is_current_result = (
                getattr(thread, "_preview_generation", None) == self._spine_preview_generation
            )
            if temp_dir:
                temp_dir = str(temp_dir)
                if is_current_result:
                    if temp_dir not in self._spine_preview_temp_dirs:
                        self._spine_preview_temp_dirs.append(temp_dir)
                else:
                    self._dispose_preview_temp_dir(temp_dir)
                    try:
                        self._spine_preview_temp_dirs.remove(temp_dir)
                    except ValueError:
                        pass
            if thread is self._spine_preview_thread:
                # The active preview owns the result directory; the worker
                # wrapper must not remain the active handle after finished.
                self._spine_preview_thread = None
            thread.deleteLater()
        if self._spine_preview_workers:
            return
        # Active result directories belong to the embedded view and are
        # removed by an explicit close/new import, never by an unrelated
        # worker's late ``finished`` signal.

    def _cleanup_archive_preview_temp_dirs(self):
        for temp_dir in list(self._archive_preview_temp_dirs):
            try:
                if temp_dir and os.path.isdir(temp_dir):
                    self._dispose_preview_temp_dir(temp_dir)
            except Exception:
                pass
        self._archive_preview_temp_dirs = []

    def _cleanup_folder_preview_temp_dirs(self):
        for temp_dir in list(self._folder_preview_temp_dirs):
            try:
                if temp_dir and os.path.isdir(temp_dir):
                    self._dispose_preview_temp_dir(temp_dir)
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
            self.start_spine_preview_import(file_path)
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
        if suffix in PACKAGE_PREVIEW_EXTENSIONS:
            # Extract once in the worker, then route Spine or Live2D using
            # that same workspace instead of decrypting a Live2D LPK twice.
            self.start_spine_preview_import(file_path, package_fallback=True)
            return

        if (
            _is_unity_preview_source(file_path) and not os.path.isdir(file_path)
        ):
            self.start_spine_preview_import(file_path, package_fallback=True)
            return

        if _is_image_file(file_path):
            local_images = _collect_preview_images(file_path)
            if local_images:
                self.load_image_preview(local_images, file_path, temporary=False)
                return

        if _is_unity_preview_source(file_path) and not _is_model_json(file_path):
            self.start_spine_preview_import(file_path, package_fallback=True)
            return

        self.show_error(
            tr("preview.invalid_file_type_title"),
            tr("preview.invalid_file_type_content")
        )

    def start_spine_preview_import(
        self,
        source_path: str,
        package_fallback: bool = False,
        *,
        force_compatibility: bool | None = None,
        prompt_missing_runtime: bool = True,
    ):
        """Prepare a Spine source and open the local viewer in the stage."""
        # Closing first invalidates the previous generation and requests
        # interruption for an import that is still extracting.  A new
        # generation is assigned only after that cancellation, so a late
        # ready/failed signal from the old worker cannot reopen the stage.
        self.close_preview_window()
        self._spine_preview_generation += 1
        generation = self._spine_preview_generation
        self._cleanup_temp_model_json()
        self._cleanup_model_preview_temp_dirs()
        self._cleanup_image_preview_temp_dirs()
        self._cleanup_archive_preview_temp_dirs()
        self.current_model_path = None
        self.preview_btn.setEnabled(False)
        self._set_motion_debug_visible(False)
        self._set_spine_mode(True)
        if self.image_preview_panel:
            self.image_preview_panel.clear()
        self._show_stage_placeholder(tr("preview.stage_loading"))
        self.model_info_text_box.setMarkdown(
            tr("preview.spine_import_loading", source=source_path)
        )
        runtime_root = self._spine_runtime_dir_for_import()
        runtime_policy = self._spine_runtime_policy_for_import(force_compatibility)
        unify_version = bool(runtime_policy.compatibility_mode)
        self._spine_runtime_prompt_enabled = bool(prompt_missing_runtime)
        worker = SpinePreviewImportThread(
            source_path,
            self.settings_manager.get_temp_dir(),
            runtime_root,
            self,
        )
        worker._package_fallback = bool(package_fallback)
        worker._unify_version = bool(unify_version)
        worker._runtime_policy = runtime_policy
        worker._preview_generation = generation
        worker.previewReady.connect(
            lambda result, source, w=worker, g=generation: self.on_spine_preview_ready(result, source, g, w)
        )
        worker.failed.connect(
            lambda error, source, w=worker, g=generation: self.on_spine_preview_failed(error, source, g, w)
        )
        worker.finished.connect(self._cleanup_finished_spine_thread)
        self._spine_preview_workers.append(worker)
        self._spine_preview_thread = worker
        worker.start()

    def on_spine_preview_ready(self, result, source_path: str, generation=None, worker=None):
        if generation is not None and generation != self._spine_preview_generation:
            return
        temp_dir = getattr(result, "temp_dir", None)
        if temp_dir and str(temp_dir) not in self._spine_preview_temp_dirs:
            self._spine_preview_temp_dirs.append(str(temp_dir))
        try:
            plan = getattr(result, "plan", None)
            if (
                isinstance(plan, SpinePreviewPlan)
                and plan.runtime_missing
                and bool(getattr(plan.asset, "has_skeleton", False))
            ):
                if self._spine_runtime_prompt_enabled:
                    self.handle_missing_spine_runtime(
                        plan,
                        source_path,
                        package_fallback=bool(getattr(worker, "_package_fallback", False)),
                    )
                else:
                    self._on_spine_preview_error(plan.runtime_error or plan.reason)
                return
            preview_model = getattr(result, "preview_model_json", None)
            if preview_model:
                self.load_model_preview(str(preview_model), source_path,
                                        editor_model_json=str(result.package.model_json))
                return
            self._open_spine_native_preview(result.plan)
        except Exception as exc:
            self._on_spine_preview_error(str(exc))

    def _spine_runtime_choice_version(self, plan: SpinePreviewPlan) -> str:
        requested = str(
            plan.requested_runtime_version
            or getattr(plan.asset, "spine_version", "")
            or ""
        ).strip()
        if requested.startswith("3.8."):
            return SPINE_COMPATIBILITY_VERSION if requested == SPINE_COMPATIBILITY_VERSION else requested
        if requested == "4.0" or requested.startswith("4.0."):
            return "4.0"
        return requested

    def _has_verified_spine_runtime(self, version: str) -> bool:
        """Return whether the requested verified bridge is usable locally."""

        family = "3.8" if version.startswith("3.8.") else "4.0" if version == "4.0" else ""
        if not family:
            return False
        requested = version if family == "3.8" else "4.0"
        roots = []
        configured = self._spine_runtime_dir_for_import()
        if configured:
            roots.append(configured)
        # ``discover_spine_runtime(None, ...)`` scans the managed catalog root
        # and is also the fallback used by the core planner for a single
        # family directory selected in Settings.
        roots.append(None)
        for root in roots:
            try:
                discover_spine_runtime(
                    root,
                    requested_family=family,
                    requested_version=requested,
                )
            except Exception:
                continue
            return True
        return False

    def _spine_conversion_fallback_available(self, plan: SpinePreviewPlan) -> bool:
        """Check the verified converter and target bridge before offering Convert."""

        if not bool(getattr(plan.asset, "has_skeleton", False)):
            return False
        source_version = str(
            getattr(plan.asset, "spine_version", "") or plan.requested_runtime_version or ""
        ).strip()
        source_family = source_version.rsplit(".", 1)[0] if source_version.count(".") >= 2 else ""
        if source_family not in {"3.5", "3.6", "3.7", "3.8", "4.0", "4.1", "4.2"}:
            return False
        if source_version == SPINE_COMPATIBILITY_VERSION:
            return False
        try:
            converter = discover_native_converter()
        except Exception:
            converter = None
        return bool(converter and self._has_verified_spine_runtime(SPINE_COMPATIBILITY_VERSION))

    def handle_missing_spine_runtime(
        self,
        plan: SpinePreviewPlan,
        source_path: str,
        *,
        package_fallback: bool = False,
    ) -> str:
        """Show the explicit missing-runtime choices and dispatch one branch.

        Return values are stable for tests and callers: ``download``,
        ``convert`` or ``cancel``.  Download/build work is delegated to the
        same QThread worker used by SettingsPage, so the modal decision never
        performs network or CMake work on the GUI thread.
        """

        version = self._spine_runtime_choice_version(plan)
        download_id = {
            "3.8.75": "spine_native",
            "4.0": "spine_native_4_0",
        }.get(version)

        box = QMessageBox(self)
        box.setWindowTitle(tr("preview.spine_runtime_missing_title"))
        box.setText(
            tr(
                "preview.spine_runtime_missing_content",
                version=version,
                error=plan.runtime_error or plan.reason,
            )
        )
        download_button = box.addButton(
            tr("preview.spine_runtime_download"), QMessageBox.ButtonRole.AcceptRole
        )
        convert_button = box.addButton(
            tr("preview.spine_runtime_convert"), QMessageBox.ButtonRole.ActionRole
        )
        cancel_button = box.addButton(
            tr("common.cancel"), QMessageBox.ButtonRole.RejectRole
        )
        # An unsupported family (for example 4.1) has no catalog download,
        # but can still use the verified converter when its 3.8.75 bridge is
        # already installed.  Conversely, a 3.8.75 source cannot be made
        # playable by converting it again when that same bridge is missing.
        download_button.setEnabled(download_id is not None)
        convert_button.setEnabled(self._spine_conversion_fallback_available(plan))
        box.exec()
        clicked = box.clickedButton()
        if clicked is download_button:
            started = self._start_spine_runtime_install(
                download_id,
                retry_source=str(source_path),
                retry_package_fallback=bool(package_fallback),
                retry_compatibility=self._spine_compatibility_mode_for_import(),
                retry_generation=self._spine_preview_generation,
            )
            return "download" if started else "cancel"
        if clicked is convert_button:
            self._spine_runtime_retry_source = str(source_path)
            self._spine_runtime_retry_package_fallback = bool(package_fallback)
            self._spine_runtime_retry_compatibility = True
            self._spine_runtime_retry_generation = self._spine_preview_generation
            # This is a one-import override and does not mutate the global
            # compatibility toggle in SettingsManager.
            self.start_spine_preview_import(
                source_path,
                package_fallback=package_fallback,
                force_compatibility=True,
                prompt_missing_runtime=False,
            )
            return "convert"

        self._spine_runtime_retry_source = ""
        self._spine_runtime_retry_package_fallback = False
        self._spine_runtime_retry_compatibility = False
        self._spine_runtime_retry_generation = 0
        self._show_stage_placeholder(tr("preview.spine_runtime_cancelled"))
        self.model_info_text_box.setMarkdown(tr("preview.spine_runtime_cancelled"))
        return "cancel"

    def _start_spine_runtime_install(
        self,
        package_id: str,
        *,
        retry_source: str | None = None,
        retry_package_fallback: bool | None = None,
        retry_compatibility: bool | None = None,
        retry_generation: int | None = None,
    ) -> bool:
        worker = self._spine_runtime_install_worker
        if worker is not None and _spine_thread_is_running(worker):
            InfoBar.warning(
                title=tr("settings.tool_download_busy_title"),
                content=tr("settings.tool_download_busy"),
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=4000,
                parent=self,
            )
            return False
        # Capture retry context on this worker.  The page-level fields are
        # convenient for diagnostics, but late signals must be checked against
        # the immutable worker context so a new source cannot be paired with an
        # older download that happened to finish later.
        retry_generation = (
            self._spine_preview_generation
            if retry_generation is None
            else int(retry_generation)
        )
        retry_source = (
            self._spine_runtime_retry_source
            if retry_source is None
            else str(retry_source)
        )
        retry_package_fallback = (
            self._spine_runtime_retry_package_fallback
            if retry_package_fallback is None
            else bool(retry_package_fallback)
        )
        retry_compatibility = (
            self._spine_runtime_retry_compatibility
            if retry_compatibility is None
            else bool(retry_compatibility)
        )
        worker = ToolchainInstallWorker(package_id, self)
        worker._spine_retry_generation = retry_generation
        worker._spine_retry_source = retry_source
        worker._spine_retry_package_fallback = retry_package_fallback
        worker._spine_retry_compatibility = retry_compatibility
        self._spine_runtime_retry_source = retry_source
        self._spine_runtime_retry_package_fallback = retry_package_fallback
        self._spine_runtime_retry_compatibility = retry_compatibility
        self._spine_runtime_retry_generation = retry_generation
        self._spine_runtime_install_worker = worker
        worker.progressChanged.connect(
            lambda progress, w=worker: self._on_spine_runtime_install_progress(progress, w)
        )
        worker.resultReady.connect(
            lambda result, w=worker: self._on_spine_runtime_install_result(result, w)
        )
        worker.failed.connect(
            lambda message, w=worker: self._on_spine_runtime_install_failed(message, w)
        )
        worker.finished.connect(self._on_spine_runtime_install_finished)
        self._show_stage_placeholder(tr("preview.spine_runtime_installing", version=package_id))
        worker.start()
        return True

    def _on_spine_runtime_install_progress(self, progress, worker=None) -> None:
        if worker is not None and worker is not self._spine_runtime_install_worker:
            return
        if worker is not None and getattr(worker, "_spine_retry_generation", None) != self._spine_preview_generation:
            return
        message = str(getattr(progress, "message", "") or getattr(progress, "phase", ""))
        if message:
            self.model_info_text_box.setMarkdown(message)

    def _on_spine_runtime_install_result(self, result, worker=None) -> None:
        if worker is not None and worker is not self._spine_runtime_install_worker:
            return
        retry_generation = getattr(
            worker, "_spine_retry_generation", self._spine_runtime_retry_generation
        )
        if retry_generation != self._spine_preview_generation:
            return
        install_dir = Path(str(getattr(result, "install_dir", ""))).expanduser().resolve()
        if not install_dir.is_dir():
            self._on_spine_runtime_install_failed(
                "Installed runtime directory is missing.", worker
            )
            return
        # The preview resolver accepts either a family directory or the
        # common parent.  Persist the common parent so both verified families
        # remain discoverable after installing either package.
        common_root = install_dir.parent if install_dir.name in {"3.8.75", "4.0"} else install_dir
        setter = getattr(self.settings_manager, "set_spine_runtime_dir", None)
        if callable(setter):
            setter(str(common_root))
        else:
            self.settings_manager.set("preview.spine_runtime_dir", str(common_root))
        if self.spine_runtime_edit:
            self.spine_runtime_edit.setText(str(common_root))
        source = getattr(worker, "_spine_retry_source", self._spine_runtime_retry_source)
        if source:
            self.start_spine_preview_import(
                source,
                package_fallback=getattr(
                    worker,
                    "_spine_retry_package_fallback",
                    self._spine_runtime_retry_package_fallback,
                ),
                force_compatibility=getattr(
                    worker,
                    "_spine_retry_compatibility",
                    self._spine_runtime_retry_compatibility,
                ),
                prompt_missing_runtime=False,
            )

    def _on_spine_runtime_install_failed(self, message: str, worker=None) -> None:
        if worker is not None and worker is not self._spine_runtime_install_worker:
            return
        retry_generation = getattr(
            worker, "_spine_retry_generation", self._spine_runtime_retry_generation
        )
        if retry_generation != self._spine_preview_generation:
            return
        self._show_stage_placeholder(
            tr("preview.spine_runtime_install_failed", error=str(message))
        )
        self.model_info_text_box.setMarkdown(
            tr("preview.spine_runtime_install_failed", error=str(message))
        )
        self.show_error(
            tr("preview.spine_runtime_install_failed_title"),
            tr("preview.spine_runtime_install_failed", error=str(message)),
        )
        self._spine_runtime_retry_source = ""
        self._spine_runtime_retry_package_fallback = False
        self._spine_runtime_retry_compatibility = False
        self._spine_runtime_retry_generation = 0

    def _on_spine_runtime_install_finished(self) -> None:
        worker = self.sender()
        if worker is self._spine_runtime_install_worker:
            self._spine_runtime_install_worker = None
        if worker is not None:
            worker.deleteLater()

    def is_spine_runtime_install_running(self) -> bool:
        worker = self._spine_runtime_install_worker
        if worker is None:
            return False
        try:
            return bool(worker.isRunning())
        except RuntimeError:
            return False

    def notify_close_while_spine_runtime_installing(self) -> bool:
        if not self.is_spine_runtime_install_running():
            return False
        self._cancel_spine_runtime_install()
        InfoBar.warning(
            title=tr("settings.tool_download_busy_title"),
            content=tr("settings.tool_download_busy"),
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=5000,
            parent=self,
        )
        return True

    def _cancel_spine_runtime_install(self) -> None:
        # Invalidate both pending import results and a pending installer
        # retry.  A late result from the worker must never reopen a preview
        # after the user closed it or the application began to quit.
        self._spine_preview_generation += 1
        self._spine_runtime_retry_source = ""
        self._spine_runtime_retry_package_fallback = False
        self._spine_runtime_retry_compatibility = False
        self._spine_runtime_retry_generation = 0
        worker = self._spine_runtime_install_worker
        if worker is None:
            return
        try:
            if worker.isRunning():
                worker.requestInterruption()
        except RuntimeError:
            pass

    def on_spine_preview_failed(self, error: str, source_path: str, generation=None, worker=None):
        if generation is not None and generation != self._spine_preview_generation:
            return
        self._clear_embedded_spine()
        self._show_stage_placeholder(tr("preview.spine_import_failed_content", error=error))
        self.model_info_text_box.setMarkdown(
            tr("preview.spine_import_failed_content", error=error)
        )
        self.show_error(
            tr("preview.spine_import_failed_title"),
            tr("preview.spine_import_failed_content", error=error),
        )

    @staticmethod
    def _spine_plan_reason(plan: SpinePreviewPlan) -> str:
        """Render plan diagnostics, including any source-to-target warning."""

        reason = str(plan.reason or "")
        warnings = [str(item) for item in (plan.warnings or ()) if str(item)]
        if warnings:
            reason = "\n\n".join(part for part in (reason, "\n".join(f"- {item}" for item in warnings)) if part)
        return reason

    def _open_spine_native_preview(self, plan: SpinePreviewPlan):
        if self.spine_preview is None:
            raise RuntimeError("Native Spine preview widget is unavailable.")
        self._active_spine_plan = plan
        self._active_spine_preview_key = str(plan.asset.skeleton_path or plan.asset.root_dir)
        if self.spine_controls:
            self.spine_controls.clear()
        self.model_info_text_box.setMarkdown(
            tr(
                "preview.spine_preview_loading",
                version=plan.asset.spine_version or "unknown",
                mode=plan.mode,
                reason=self._spine_plan_reason(plan),
            )
        )
        self.spine_preview.open_plan(plan)
        editable_path = plan.asset.model_config_path or plan.asset.skeleton_path
        self._set_editor_source("spine", str(editable_path) if editable_path else None)
        if self._preview_page_active is False:
            self.set_active(False)

    def _on_spine_preview_document_loaded(self, url: str):
        if not self._active_spine_preview_key or str(url) != self._active_spine_preview_key:
            return
        plan = self._active_spine_plan
        if plan is None:
            return
        self._show_spine_stage()
        if self.settings_panel:
            self.spine_preview.set_view_settings(self.settings_panel.get_settings())
        self.model_info_text_box.setMarkdown(
            tr(
                "preview.spine_preview_loading",
                version=plan.asset.spine_version or "unknown",
                mode=plan.mode,
                reason=self._spine_plan_reason(plan),
            )
        )

    def _on_spine_preview_ready(self, url: str):
        if not self._active_spine_preview_key or str(url) != self._active_spine_preview_key:
            return
        plan = self._active_spine_plan
        if plan is None:
            return
        self._show_spine_stage()
        self.model_info_text_box.setMarkdown(
            tr(
                "preview.spine_preview_ready",
                version=plan.asset.spine_version or "unknown",
                mode=plan.mode,
                reason=self._spine_plan_reason(plan),
            )
        )

    def _on_spine_preview_state(self, state: dict):
        """Mirror the page's renderer state into the native right sidebar."""

        if not self._spine_mode or self._active_spine_plan is None:
            return
        if self.spine_controls:
            self.spine_controls.set_state(state)

    def _on_spine_skin_changed(self, name: str):
        if self._spine_mode and self.spine_preview:
            self.spine_preview.set_skin(name)

    def _on_spine_animation_changed(self, name: str):
        if self._spine_mode and self.spine_preview:
            loop = bool(self.spine_controls and self.spine_controls.loop_check.isChecked())
            self.spine_preview.set_animation(name, loop)

    def _on_spine_paused_changed(self, paused: bool):
        if self._spine_mode and self.spine_preview:
            self.spine_preview.set_paused(paused)
        if self.spine_controls:
            self.spine_controls.retranslate_ui()

    def _on_spine_loop_changed(self, loop: bool):
        if self._spine_mode and self.spine_preview:
            self.spine_preview.set_loop(loop)

    def _on_spine_time_changed(self, value: float):
        if self._spine_mode and self.spine_preview:
            self.spine_preview.set_time(value)

    def _on_spine_reset_requested(self):
        if self._spine_mode and self.spine_preview:
            self.spine_preview.reset_pose()

    def _on_spine_export_requested(self):
        if self._spine_mode and self.spine_preview:
            self.spine_preview.export_pose_psd()

    def _on_spine_preview_error(self, error: str):
        self._clear_embedded_spine()
        message = tr("preview.spine_web_error_content", error=str(error))
        self._show_stage_placeholder(message)
        self.model_info_text_box.setMarkdown(message)
        self.show_error(tr("preview.spine_web_error_title"), message)

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
            self._sync_resource_selector()
            self.image_preview_panel.setVisible(True)
        if self.preview_placeholder:
            self.preview_placeholder.setVisible(False)
        if item.get("kind") == "model":
            self.activate_model_preview_item(item)
        elif item.get("kind") == "image":
            self.activate_image_preview_item(item)

    def activate_model_preview_item(self, item: dict):
        source_path = str(item.get("source_path") or item.get("path") or "")
        # Folder scans may have prepared a Live2D candidate before the user
        # activates it. Unity sources still go through the unified importer so
        # a Spine TextAsset is not hidden behind the old Cubism-only path.
        if source_path and _is_unity_preview_source(source_path) and not os.path.isdir(source_path):
            self.start_spine_preview_import(source_path, package_fallback=True)
            return
        prepared = item.get("prepared_model_json")
        if prepared and os.path.isfile(str(prepared)):
            self.load_model_preview(str(prepared), source_path or str(prepared),
                                    editor_model_json=item.get("editor_model_json"))
            return
        if source_path:
            self.start_model_preview_import(source_path)

    def activate_image_preview_item(self, item: dict):
        self._terminate_preview_process()
        self._clear_embedded_spine()
        self._show_image_stage()
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
            self.load_model_preview(str(result.preview_model_json), source_path,
                                    editor_model_json=str(result.package.model_json))
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
        worker = self.sender() or self._model_preview_thread
        self.load_model_preview(model_json_path, source_path,
                                editor_model_json=getattr(worker, "editor_model_json", None))

    def on_model_preview_import_failed(self, error: str, source_path: str):
        if _is_unity_preview_source(source_path):
            self.start_unity_image_preview(source_path)
            return
        self.show_error(
            tr("preview.model_import_failed_title"),
            tr("preview.model_import_failed_content", error=error),
        )

    def load_model_preview(self, model_json_path: str, source_path: str | None = None,
                           *, editor_model_json: str | None = None):
        self._clear_embedded_spine()
        self._clear_live2d_controls()
        self._set_resource_mode("live2d")
        self._psd_project_context = self._pending_psd_project_context
        self._pending_psd_project_context = None
        if self.save_pose_scheme_btn:
            self.save_pose_scheme_btn.setVisible(bool(self._psd_project_context))
        self.current_model_path = os.path.abspath(model_json_path)
        self._set_editor_source("live2d", editor_model_json or self.current_model_path)
        self._temp_model_json_path = self.current_model_path
        self._cleanup_image_preview_temp_dirs()

        model_name = os.path.basename(self.current_model_path)
        model_dir = os.path.dirname(self.current_model_path)
        if not self._preview_items:
            self._preview_items = [{
                "kind": "model",
                "path": self.current_model_path,
                "prepared_model_json": self.current_model_path,
                "editor_model_json": editor_model_json or self.current_model_path,
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
        self._live2d_load_timer.start()
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
            self._dispose_preview_temp_dir(temp_dir)
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
        self._dispose_preview_temp_dir(temp_dir)
        self.show_error(
            tr("preview.unity_preview_failed_title"),
            tr("preview.unity_preview_failed_content", error=error),
        )

    def preview_current_model(self):
        """Render Live2D in an isolated native QOpenGLWindow child."""
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
            if self.preview_placeholder:
                self.preview_placeholder.setVisible(False)
            if self.image_preview_panel:
                self.image_preview_panel.setVisible(False)

            preview = self._ensure_embedded_live2d()
            if preview is None or preview.live2d_canvas is None:
                raise RuntimeError("Live2D OpenGL canvas could not be created.")
            preview.load_model(self.current_model_path)
            settings = self.settings_panel.get_settings()
            settings.update(self.advanced_panel.get_advanced_settings())
            settings.update({
                "fit_to_dock": True,
                "selected_motion_on_click": True,
                "motion_frozen": bool(
                    self.freeze_motion_check and self.freeze_motion_check.isChecked()
                ),
                "motion_loop": bool(self.loop_motion_check and self.loop_motion_check.isChecked()),
            })
            preview.apply_settings(settings)
            self._set_resource_mode("live2d")
            self._preview_parts_changed(self.artmesh_panel.part_overrides, self.artmesh_panel.part_defaults)
            preview.show()
            self._set_motion_debug_visible(True)
            self._on_motion_selection_changed(self.motion_combo.currentIndex())
            self._parameter_refresh_retries = 5
            self._parameter_refresh_timer.start()
            return True
        except Exception as exc:
            self._terminate_preview_process()
            self._set_motion_debug_visible(False)
            self._show_stage_placeholder(tr("preview.stage_empty"), keep_editor_source=True)
            self.show_error(
                tr("common.error"),
                tr("preview_window.error_model_load_failed", error_type=type(exc).__name__, error=exc),
            )
            return False

    def open_psd_project_preview(self, model_json_path: str, project_file: str):
        """Open a model with PSD provenance, enabling named pose export."""
        self._pending_psd_project_context = {
            "project_file": os.path.abspath(project_file),
            "model_json": os.path.abspath(model_json_path),
        }
        self.start_model_preview_import(model_json_path)

    def open_model_preview_source(self, model_json_path: str):
        """Open a model sent by another workspace page without PSD context."""
        self._pending_psd_project_context = None
        self._psd_project_context = None
        self.start_model_preview_import(model_json_path)

    def request_pose_scheme_save(self):
        context = dict(self._psd_project_context or {})
        if not context or not self.current_model_path or not self.live2d_preview:
            return
        if self.freeze_motion_check and not self.freeze_motion_check.isChecked():
            self.freeze_motion_check.setChecked(True)
        name, accepted = QInputDialog.getText(
            self,
            tr("preview.pose_scheme_dialog_title"),
            tr("preview.pose_scheme_name_label"),
        )
        name = str(name or "").strip()
        if not accepted or not name:
            return
        parameters = {
            str(item.get("id")): float(item.get("value", 0.0))
            for item in self.live2d_preview.get_parameter_meta_list()
            if item.get("id")
        }
        if not parameters:
            self.show_error(
                tr("common.warning"),
                tr("preview.parameter_preset_empty"),
            )
            return
        self.poseSchemeRequested.emit({
            **context,
            "name": name,
            "parameters": parameters,
        })

    def _refresh_parameter_controls(self, retries: int = 0):
        preview = self.live2d_preview
        if preview is None or self.advanced_panel is None:
            return
        meta = preview.get_parameter_meta_list()
        if meta:
            self.advanced_panel.rebuild_advanced_params(meta)
            self.advanced_panel.sync_advanced_param_values(meta)
            return
        if retries > 0:
            self._parameter_refresh_retries = retries - 1
            self._parameter_refresh_timer.start()

    def on_preview_window_closed(self, window):
        """预览窗口关闭处理"""
        if self.preview_process is not None and self.preview_process.poll() is not None:
            self.preview_process = None

    def close_preview_window(self):
        """Close the current image, Spine page, or embedded Live2D preview."""
        # Invalidate the active import before clearing the page.  Otherwise a
        # queued worker signal could recreate the Spine page after the user
        # pressed the close button.
        self._cleanup_spine_preview_temp_dirs()
        self._cancel_spine_runtime_install()
        self._terminate_preview_process()
        self._clear_embedded_spine()
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
        """Apply display settings directly to the embedded OpenGL widget."""
        if self._spine_mode and self.spine_preview:
            self.spine_preview.set_view_settings(settings)
        elif self.live2d_preview:
            self.live2d_preview.apply_settings(settings)
        self._save_preview_ui_state()

    def on_request_refresh_params(self):
        """Refresh motions and parameter metadata from the embedded model."""
        if self.current_model_path:
            self._populate_motion_controls(self._load_motions_from_model_json(self.current_model_path))
            self._refresh_parameter_controls(2)
        else:
            self._populate_motion_controls([])
