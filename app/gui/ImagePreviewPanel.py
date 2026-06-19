from __future__ import annotations

import os

from PySide6.QtCore import QEvent, QPoint, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, FluentIcon, PushButton

from app.i18n import tr


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


