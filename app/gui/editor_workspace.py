"""Shared Fluent editor panels and a recoverable, resizable workspace.

Layout preferences live in SettingsManager; no model or project files are touched.
The permanent layout bar remains available even with every panel collapsed.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QPointF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QSplitter, QSplitterHandle, QStackedWidget, QVBoxLayout, QWidget, QSizePolicy
from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, FluentIcon, Pivot, TransparentToolButton,
    TransparentToggleToolButton, PushButton, ComboBox,
    ScrollArea, setFont, isDarkTheme,
)

from app.i18n import tr
from app.gui.theme import transparent_scroll_area
from app.core.settings_manager import SettingsManager


WORKSPACE_TEXT = {
    "editor.workspace.preview": "预览",
    "editor.workspace.details": "编辑面板",
    "editor.workspace.timeline": "时间轴",
    "editor.workspace.layout": "布局",
    "editor.workspace.reset": "重置布局",
    "editor.workspace.hide": "收起{panel}",
    "editor.workspace.show": "恢复{panel}",
    "editor.workspace.empty": "面板已收起，可在上方恢复预览、编辑面板或时间轴",
    "editor.workspace.resize": "拖动调整面板宽度或高度；双击重置布局",
    "editor.workspace.zoom_in": "放大",
    "editor.workspace.zoom_out": "缩小",
    "editor.workspace.zoom_reset": "重置视图",
    "editor.workspace.zoom_hint": "Ctrl+滚轮在光标处缩放；按住鼠标中键拖动平移",
}


def _text(key: str, **values) -> str:
    return tr(key, WORKSPACE_TEXT[key], **values)


class EditorComboBox(ComboBox):
    """Real Fluent combo with Qt-compatible positional userData arguments."""

    def addItem(self, text, userData=None, *, icon=None):  # noqa: N802
        super().addItem(text, icon=icon, userData=userData)

    def minimumSizeHint(self):  # noqa: N802
        # A selected long path/track should be clipped by the Fluent button,
        # while its popup and tooltip retain the complete name.
        return QSize(max(60, self.minimumWidth()), super().minimumSizeHint().height())


class EditorViewportLayout(QVBoxLayout):
    """Scroll panes own their height; do not inflate the top-level minimum."""

    def hasHeightForWidth(self):
        return False

    def heightForWidth(self, _width):
        return -1

    def minimumHeightForWidth(self, _width):
        return -1


class _ViewportStack(QStackedWidget):
    def hasHeightForWidth(self):
        return False

    def heightForWidth(self, _width):
        return -1

    def minimumSizeHint(self):
        return QSize(100, 70)


class EditorTabs(QWidget):
    """Fluent Pivot + stack with the small QTabWidget API used by both pages."""

    currentChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pivot = Pivot(self)
        self.pivot.setFixedHeight(34)
        self.tab_scroll = ScrollArea(self)
        self.tab_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.tab_scroll.setFixedHeight(39)
        self.tab_scroll.setWidget(self.pivot)
        transparent_scroll_area(self.tab_scroll)
        self.tab_scroll.setWidgetResizable(False)
        self.tab_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tab_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.back_button = TransparentToolButton(FluentIcon.LEFT_ARROW, self)
        self.forward_button = TransparentToolButton(FluentIcon.CHEVRON_RIGHT, self)
        for button in (self.back_button, self.forward_button):
            button.setFixedSize(24, 30)
            button.hide()
        tab_row = QHBoxLayout()
        self.tab_row = tab_row
        self._corner_space = 0
        tab_row.setContentsMargins(0, 0, 0, 0)
        tab_row.setSpacing(2)
        tab_row.addWidget(self.back_button)
        tab_row.addWidget(self.tab_scroll, 1)
        tab_row.addWidget(self.forward_button)
        self.stack = _ViewportStack(self)
        layout = EditorViewportLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(tab_row)
        layout.addWidget(self.stack, 1)
        self._keys = []
        self._tab_labels = []
        self._tab_fit_timer = QTimer(self)
        self._tab_fit_timer.setSingleShot(True)
        self._tab_fit_timer.timeout.connect(self._ensure_current_tab_visible)
        self.stack.currentChanged.connect(self._changed)
        self.back_button.clicked.connect(lambda: self._scroll_tabs(-120))
        self.forward_button.clicked.connect(lambda: self._scroll_tabs(120))
        self.tab_scroll.horizontalScrollBar().rangeChanged.connect(self._update_tab_arrows)

    def addTab(self, widget: QWidget, text: str) -> int:  # noqa: N802
        index = self.stack.addWidget(widget)
        key = f"editor-tab-{index}"
        self._keys.append(key)
        self._tab_labels.append(text)
        # PivotItem.itemClicked emits a checked bool.  Consume it explicitly so
        # True cannot replace the captured page index and select page 1.
        item = self.pivot.addItem(key, text, onClick=lambda _checked=False, i=index: self.setCurrentIndex(i))
        setFont(item, 14)
        item.setToolTip(text)
        self.pivot.adjustSize()
        if index == 0:
            self.pivot.setCurrentItem(key)
        self._fit_tab_items()
        return index

    def setTabText(self, index: int, text: str):  # noqa: N802
        self._tab_labels[index] = text
        self.pivot.setItemText(self._keys[index], text)
        self.pivot.widget(self._keys[index]).setToolTip(text)
        self.pivot.adjustSize()
        self._fit_tab_items()

    def tabText(self, index: int) -> str:  # noqa: N802
        return self._tab_labels[index]

    def setTabToolTip(self, index: int, text: str):  # noqa: N802
        self.pivot.widget(self._keys[index]).setToolTip(text)

    def currentIndex(self) -> int:  # noqa: N802
        return self.stack.currentIndex()

    def currentWidget(self) -> QWidget:  # noqa: N802
        return self.stack.currentWidget()

    def setCurrentWidget(self, widget: QWidget):  # noqa: N802
        self.stack.setCurrentWidget(widget)

    def setCurrentIndex(self, index: int):  # noqa: N802
        self.stack.setCurrentIndex(index)

    def widget(self, index: int) -> QWidget:
        return self.stack.widget(index)

    def count(self) -> int:
        return self.stack.count()

    def _changed(self, index: int):
        if 0 <= index < len(self._keys):
            self.pivot.setCurrentItem(self._keys[index])
            self._fit_tab_items()
            self.tab_scroll.ensureWidgetVisible(self.pivot.widget(self._keys[index]), 8, 0)
        self.currentChanged.emit(index)

    def _scroll_tabs(self, offset: int):
        bar = self.tab_scroll.horizontalScrollBar()
        bar.setValue(bar.value() + offset)

    def _update_tab_arrows(self, *args):
        overflow = self._natural_tab_width() > self.width() - self._corner_space
        self.back_button.setVisible(overflow)
        self.forward_button.setVisible(overflow)

    def _natural_tab_width(self):
        return sum(self.pivot.widget(key).fontMetrics().horizontalAdvance(label) + 24
                   for key, label in zip(self._keys, self._tab_labels))

    def _fit_tab_items(self):
        overflow = self._natural_tab_width() > self.width() - self._corner_space
        available = max(70, self.width() - self._corner_space - (52 if overflow else 0) - 24)
        for key, label in zip(self._keys, self._tab_labels):
            item = self.pivot.widget(key)
            item.setText(item.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight, available))
        self.pivot.adjustSize()
        self._update_tab_arrows()
        if hasattr(self, "_tab_fit_timer"):
            self._tab_fit_timer.start(0)

    def _ensure_current_tab_visible(self):
        index = self.currentIndex()
        if 0 <= index < len(self._keys):
            self.tab_scroll.ensureWidgetVisible(self.pivot.widget(self._keys[index]), 8, 0)

    def reserve_corner(self, width: int):
        self._corner_space = max(0, int(width))
        self.tab_row.setContentsMargins(0, 0, self._corner_space, 0)
        self._fit_tab_items()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._fit_tab_items()


class EditorSurface(CardWidget):
    """An opaque, palette-owned surface without nested translucent layers."""

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # Same fill and hairline as the Fluent cards on the other pages, so
        # switching pages does not change the surface colour.
        painter.setPen(QPen(QColor(255, 255, 255, 16) if isDarkTheme() else QColor(0, 0, 0, 38), 1))
        painter.setBrush(self.palette().base())
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 8, 8)


# Width a panel's top row must leave free for the floating hide button.
PANEL_CORNER_RESERVE = 28


class EditorPanel(EditorSurface):
    hideRequested = Signal()

    def __init__(self, content: QWidget, panel: str, parent=None):
        super().__init__(parent)
        self.panel = panel
        self.content = content
        self.setBorderRadius(8)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setMinimumSize(140, 90)
        layout = EditorViewportLayout(self)
        layout.setContentsMargins(10, 7, 10, 9)
        layout.setSpacing(0)
        self.title = BodyLabel(self)
        self.title.hide()
        self.hide_button = TransparentToolButton(FluentIcon.HIDE, self)
        self.hide_button.setFixedSize(24, 24)
        self.hide_button.clicked.connect(self.hideRequested)
        if isinstance(content, EditorTabs):
            content.reserve_corner(PANEL_CORNER_RESERVE)
        if panel == "timeline":
            self.scroll = ScrollArea(self)
            self.scroll.setFrameShape(QFrame.Shape.NoFrame)
            self.scroll.setWidgetResizable(True)
            self.scroll.setWidget(content)
            transparent_scroll_area(self.scroll)
            self.scroll.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
            self.scroll.setMinimumSize(0, 0)
            layout.addWidget(self.scroll, 1)
        else:
            layout.addWidget(content, 1)
        self.retranslate_ui()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self.hide_button.move(self.width() - self.hide_button.width() - 9, 9)
        self.hide_button.raise_()

    def retranslate_ui(self):
        name = _text(f"editor.workspace.{self.panel}")
        self.title.setText(name)
        self.setAccessibleName(name)
        self.hide_button.setAccessibleName(_text("editor.workspace.hide", panel=name))
        self.hide_button.setToolTip(_text("editor.workspace.hide", panel=name))


class _Grip(QSplitterHandle):
    resetRequested = Signal()

    def __init__(self, orientation, parent):
        super().__init__(orientation, parent)
        self.setToolTip(_text("editor.workspace.resize"))

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.palette().mid().color())
        x, y = self.width() / 2, self.height() / 2
        for offset in (-10, -5, 0, 5, 10):
            point = QPointF(x, y + offset) if self.orientation() == Qt.Orientation.Horizontal else QPointF(x + offset, y)
            painter.drawEllipse(point, 1.2, 1.2)

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        self.resetRequested.emit()
        event.accept()


class ViewZoomControls(QWidget):
    """Zoom out / percentage / zoom in / reset for a preview canvas.

    The canvas owns the actual view; this only requests changes and shows
    the factor it reports back through set_scale().
    """

    zoomInRequested = Signal()
    zoomOutRequested = Signal()
    resetRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.zoom_out_button = TransparentToolButton(FluentIcon.ZOOM_OUT, self)
        self.scale_label = CaptionLabel("100%", self)
        self.scale_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scale_label.setMinimumWidth(42)
        self.zoom_in_button = TransparentToolButton(FluentIcon.ZOOM_IN, self)
        self.reset_button = TransparentToolButton(FluentIcon.FIT_PAGE, self)
        for button in (self.zoom_out_button, self.zoom_in_button, self.reset_button):
            button.setFixedSize(28, 28)
        layout.addWidget(self.zoom_out_button)
        layout.addWidget(self.scale_label)
        layout.addWidget(self.zoom_in_button)
        layout.addWidget(self.reset_button)
        self.zoom_out_button.clicked.connect(self.zoomOutRequested)
        self.zoom_in_button.clicked.connect(self.zoomInRequested)
        self.reset_button.clicked.connect(self.resetRequested)
        self.retranslate_ui()

    def set_scale(self, scale: float):
        self.scale_label.setText(f"{round(float(scale) * 100)}%")

    def retranslate_ui(self):
        hint = _text("editor.workspace.zoom_hint")
        for button, key in ((self.zoom_out_button, "zoom_out"), (self.zoom_in_button, "zoom_in"),
                            (self.reset_button, "zoom_reset")):
            name = _text(f"editor.workspace.{key}")
            button.setToolTip(name + "\n" + hint)
            button.setAccessibleName(name)
        self.scale_label.setToolTip(hint)


class EditorSplitter(QSplitter):
    """A splitter inside a panel, with the same dotted grip as the workspace."""

    def createHandle(self):  # noqa: N802
        return _Grip(self.orientation(), self)


class _WorkspaceSplitter(QSplitter):
    resetRequested = Signal()

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setHandleWidth(10)
        self.setChildrenCollapsible(True)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

    def createHandle(self):  # noqa: N802
        handle = _Grip(self.orientation(), self)
        handle.resetRequested.connect(self.resetRequested)
        return handle

    def hasHeightForWidth(self):
        return False

    def heightForWidth(self, _width):
        return -1

    def minimumSizeHint(self):
        return QSize(280, 100)


class EditorWorkspace(QWidget):
    panelVisibilityChanged = Signal(str, bool)
    panelVisibilityEdited = Signal(str, bool)

    def __init__(self, preview: QWidget, details: QWidget, timeline: QWidget,
                 parent=None, settings_key: str | None = None, settings=None):
        super().__init__(parent)
        self.setObjectName("editorWorkspace")
        self._settings_key = settings_key
        self._restoring = False
        self._initial_show = False
        self._task_context = None
        self._task_timeline_default = True
        self._task_timeline_visibility = {}
        self._legacy_timeline_visibility = None
        self._visibility = {"preview": True, "details": True, "timeline": True}
        self._horizontal_sizes = [550, 450]
        self._vertical_sizes = [450, 195]
        self._settings = (settings or SettingsManager()) if settings_key and settings is not False else None
        layout = EditorViewportLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.layout_toolbar = QWidget(self)
        bar = QHBoxLayout(self.layout_toolbar)
        bar.setContentsMargins(2, 0, 2, 0)
        bar.setSpacing(5)
        self.layout_label = CaptionLabel(self)
        self.layout_label.hide()
        self.preview_toggle = TransparentToggleToolButton(FluentIcon.VIEW, self)
        self.details_toggle = TransparentToggleToolButton(FluentIcon.EDIT, self)
        self.timeline_toggle = TransparentToggleToolButton(FluentIcon.HISTORY, self)
        self._toggles = {"preview": self.preview_toggle, "details": self.details_toggle, "timeline": self.timeline_toggle}
        for panel, button in self._toggles.items():
            button.setChecked(True)
            button.setFixedSize(30, 30)
            button.clicked.connect(lambda checked, name=panel: self.set_panel_visible(name, checked, user=True))
            bar.addWidget(button)
        self.reset_button = TransparentToolButton(FluentIcon.SYNC, self)
        self.reset_button.setFixedSize(30, 30)
        self.reset_button.clicked.connect(self.reset_layout)
        bar.addWidget(self.reset_button)
        layout.addWidget(self.layout_toolbar)
        self._body = _ViewportStack(self)
        self.horizontal_splitter = _WorkspaceSplitter(Qt.Orientation.Horizontal, self)
        self.vertical_splitter = _WorkspaceSplitter(Qt.Orientation.Vertical, self.horizontal_splitter)
        self.preview_panel = EditorPanel(preview, "preview", self.vertical_splitter)
        self.details_panel = EditorPanel(details, "details", self.horizontal_splitter)
        self.timeline_panel = EditorPanel(timeline, "timeline", self.vertical_splitter)
        self.vertical_splitter.addWidget(self.preview_panel)
        self.vertical_splitter.addWidget(self.timeline_panel)
        self.horizontal_splitter.addWidget(self.vertical_splitter)
        self.horizontal_splitter.addWidget(self.details_panel)
        self.horizontal_splitter.setStretchFactor(0, 11)
        self.horizontal_splitter.setStretchFactor(1, 9)
        self.vertical_splitter.setStretchFactor(0, 1)
        self.vertical_splitter.setStretchFactor(1, 0)
        self.horizontal_splitter.setSizes(self._horizontal_sizes)
        self.vertical_splitter.setSizes(self._vertical_sizes)
        self._body.addWidget(self.horizontal_splitter)
        empty = QWidget(self)
        empty_layout = QVBoxLayout(empty)
        empty_layout.addStretch(1)
        self.empty_label = BodyLabel(self)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        empty_layout.addWidget(self.empty_label)
        self.empty_reset_button = PushButton(FluentIcon.SYNC, "", self)
        empty_layout.addWidget(self.empty_reset_button, 0, Qt.AlignmentFlag.AlignHCenter)
        empty_layout.addStretch(1)
        self.empty_reset_button.clicked.connect(self.reset_layout)
        self._body.addWidget(empty)
        layout.addWidget(self._body, 1)
        self._panels = {"preview": self.preview_panel, "details": self.details_panel, "timeline": self.timeline_panel}
        for name, panel in self._panels.items():
            panel.hideRequested.connect(lambda name=name: self.set_panel_visible(name, False, user=True))
        self.horizontal_splitter.splitterMoved.connect(self._splitter_moved)
        self.vertical_splitter.splitterMoved.connect(self._splitter_moved)
        self.horizontal_splitter.resetRequested.connect(self.reset_layout)
        self.vertical_splitter.resetRequested.connect(self.reset_layout)
        self.retranslate_ui()
        self._restore_timer = QTimer(self)
        self._restore_timer.setSingleShot(True)
        self._restore_timer.timeout.connect(self.restore_layout)
        self._restore_timer.start(0)

    def is_panel_visible(self, panel: str) -> bool:
        return self._visibility[panel]

    def toggle_panel(self, panel: str):
        self.set_panel_visible(panel, not self.is_panel_visible(panel), user=True)

    def set_task_context(self, context: str, *, timeline_visible=True):
        """Apply a task default while preserving a user's choice for that task."""
        if (self._task_context is None and self._legacy_timeline_visibility is not None
                and str(context) not in self._task_timeline_visibility):
            self._task_timeline_visibility[str(context)] = self._legacy_timeline_visibility
        self._task_context = str(context)
        self._task_timeline_default = bool(timeline_visible)
        visible = self._task_timeline_visibility.get(self._task_context, self._task_timeline_default)
        self.set_panel_visible("timeline", visible)

    def move_toolbar_to(self, layout: QHBoxLayout):
        """Place the permanent visibility controls in the page's header."""
        self.layout().removeWidget(self.layout_toolbar)
        layout.addWidget(self.layout_toolbar)

    def set_panel_visible(self, panel: str, visible: bool, *, user=False):
        if panel not in self._panels:
            raise ValueError(f"Unknown editor panel: {panel}")
        visible = bool(visible)
        if user and not self._restoring:
            if panel == "timeline" and self._task_context is not None:
                self._task_timeline_visibility[self._task_context] = visible
            self.panelVisibilityEdited.emit(panel, visible)
        if self._visibility[panel] == visible:
            if user:
                self.save_layout()
            return
        self._remember_sizes()
        self._visibility[panel] = visible
        self._panels[panel].setVisible(visible)
        self._toggles[panel].setChecked(visible)
        self.vertical_splitter.setVisible(self._visibility["preview"] or self._visibility["timeline"])
        self._body.setCurrentIndex(0 if any(self._visibility.values()) else 1)
        if visible:
            self.horizontal_splitter.setSizes(self._horizontal_sizes)
            self.vertical_splitter.setSizes(self._vertical_sizes)
        self.retranslate_ui()
        self.panelVisibilityChanged.emit(panel, visible)
        self.save_layout()

    def _remember_sizes(self):
        horizontal = self.horizontal_splitter.sizes()
        vertical = self.vertical_splitter.sizes()
        # A remaining pane expands when its neighbour is hidden. Remember the
        # split only while both panes are present, so repeated collapse/restore
        # operations recover the user's proportions instead of drifting.
        left_visible = self._visibility["preview"] or self._visibility["timeline"]
        if left_visible and self._visibility["details"] and all(horizontal):
            self._horizontal_sizes = horizontal
        if self._visibility["preview"] and self._visibility["timeline"] and all(vertical):
            self._vertical_sizes = vertical

    def _splitter_moved(self, position, index):
        if self._restoring:
            return
        self._remember_sizes()
        horizontal = self.horizontal_splitter.sizes()
        vertical = self.vertical_splitter.sizes()
        updates = {"preview": vertical[0] > 0, "details": horizontal[1] > 0, "timeline": vertical[1] > 0}
        # Collapsing the complete left column hides preview and timeline.
        if horizontal[0] == 0:
            updates["preview"] = updates["timeline"] = False
        for panel, visible in updates.items():
            if self._visibility[panel] != visible:
                self.set_panel_visible(panel, visible, user=True)
        self.save_layout()

    def reset_layout(self):
        self._restoring = True
        self._task_timeline_visibility.clear()
        for name, panel in self._panels.items():
            self._visibility[name] = True
            panel.show()
            self._toggles[name].setChecked(True)
            self.panelVisibilityChanged.emit(name, True)
        self.vertical_splitter.show()
        self._body.setCurrentIndex(0)
        self._horizontal_sizes = [550, 450]
        self._vertical_sizes = self._default_vertical_sizes()
        self.horizontal_splitter.setSizes(self._horizontal_sizes)
        self.vertical_splitter.setSizes(self._vertical_sizes)
        self._restoring = False
        if self._task_context is not None:
            self._task_timeline_visibility[self._task_context] = True
        self.retranslate_ui()
        self.save_layout()

    def save_layout(self):
        if not self._settings or self._restoring or not self._initial_show:
            return
        state = {"version": 2, "layout": "left-preview-timeline",
                 "horizontal": bytes(self.horizontal_splitter.saveState()).hex(),
                 "vertical": bytes(self.vertical_splitter.saveState()).hex(),
                 "sizes-h": self._horizontal_sizes, "sizes-v": self._vertical_sizes,
                 "visible": dict(self._visibility),
                 "task-timeline": dict(self._task_timeline_visibility)}
        self._settings.set(f"editor_layouts.{self._settings_key}", state)

    def restore_layout(self):
        if not self._settings:
            self._vertical_sizes = self._default_vertical_sizes()
            self.vertical_splitter.setSizes(self._vertical_sizes)
            return
        self._restoring = True
        saved = self._settings.get(f"editor_layouts.{self._settings_key}", {})
        saved = saved if isinstance(saved, dict) else {}
        task_visibility = saved.get("task-timeline", {})
        self._task_timeline_visibility = ({str(key): bool(value) for key, value in task_visibility.items()}
                                          if isinstance(task_visibility, dict) else {})
        # The old vertical-over-horizontal tree cannot be restored into this
        # layout. Migrate to the new visible default rather than reinterpret
        # its byte states or keep a hidden, unexpectedly narrow workspace.
        migrated = bool(saved) and (saved.get("version") != 2 or saved.get("layout") != "left-preview-timeline")
        if migrated:
            saved = {}
            self._task_timeline_visibility.clear()
        if not saved:
            self._horizontal_sizes = [550, 450]
            self.horizontal_splitter.setSizes(self._horizontal_sizes)
            self._vertical_sizes = self._default_vertical_sizes()
            self.vertical_splitter.setSizes(self._vertical_sizes)
        visibility = saved.get("visible", {})
        visibility = visibility if isinstance(visibility, dict) else {}
        self._legacy_timeline_visibility = (visibility["timeline"] if saved and "task-timeline" not in saved
                                            and isinstance(visibility.get("timeline"), bool) else None)
        if (self._task_context is not None and saved and "task-timeline" not in saved
                and isinstance(visibility.get("timeline"), bool)):
            # A valid version-2 layout predates task preferences. Preserve the
            # user's saved choice for the initially restored task only.
            self._task_timeline_visibility[self._task_context] = visibility["timeline"]
        for key, splitter in (("horizontal", self.horizontal_splitter), ("vertical", self.vertical_splitter)):
            state = saved.get(key)
            if isinstance(state, str):
                splitter.restoreState(QByteArray.fromHex(state.encode("ascii", errors="ignore")))
        for key, attr in (("sizes-h", "_horizontal_sizes"), ("sizes-v", "_vertical_sizes")):
            values = saved.get(key)
            if isinstance(values, (list, tuple)) and len(values) == 2:
                try:
                    sizes = [max(90, int(value)) for value in values]
                    setattr(self, attr, sizes)
                except (TypeError, ValueError):
                    pass
        for name in self._panels:
            visible = bool(visibility.get(name, True))
            if name == "timeline" and self._task_context is not None:
                visible = self._task_timeline_visibility.get(self._task_context, self._task_timeline_default)
            self._visibility[name] = visible
            self._panels[name].setVisible(visible)
            self._toggles[name].setChecked(visible)
            self.panelVisibilityChanged.emit(name, visible)
        self.vertical_splitter.setVisible(self._visibility["preview"] or self._visibility["timeline"])
        self._body.setCurrentIndex(0 if any(self._visibility.values()) else 1)
        self.horizontal_splitter.setSizes(self._horizontal_sizes)
        self.vertical_splitter.setSizes(self._vertical_sizes)
        self._restoring = False
        self.retranslate_ui()
        if migrated:
            self.save_layout()

    def _default_vertical_sizes(self):
        available = self.vertical_splitter.height() - self.vertical_splitter.handleWidth()
        bottom = 195
        return [max(160, available - bottom), bottom]

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if not self._initial_show:
            self._initial_show = True
            # Recompute the first layout after the enclosing page has its
            # real height. The bottom toolbar fits even on a shorter window.
            self._restore_timer.start(0)

    def retranslate_ui(self):
        self.layout_label.setText(_text("editor.workspace.layout"))
        for name, button in self._toggles.items():
            title = _text(f"editor.workspace.{name}")
            button.setText("")
            button.setAccessibleName(title)
            button.setToolTip(_text("editor.workspace.hide" if self._visibility[name] else "editor.workspace.show", panel=title))
            self._panels[name].retranslate_ui()
        self.reset_button.setToolTip(_text("editor.workspace.reset"))
        self.empty_reset_button.setText(_text("editor.workspace.reset"))
        self.empty_label.setText(_text("editor.workspace.empty"))


FluentEditorTabs = EditorTabs
EditorSplitter = _WorkspaceSplitter

__all__ = ["EditorWorkspace", "EditorPanel", "EditorTabs", "FluentEditorTabs", "EditorComboBox", "EditorSplitter", "EditorViewportLayout", "WORKSPACE_TEXT"]
