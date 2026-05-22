import os
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QApplication, QLabel, QSizeGrip)
from PySide6.QtCore import Signal, QPoint, QRect, Qt, QEvent, QTimer, QUrl
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QCursor
from qfluentwidgets import (PushButton, SubtitleLabel, BodyLabel)
from qfluentwidgets import CardWidget
from qfluentwidgets import ComboBox
from qfluentwidgets import InfoBar, InfoBarPosition
from app.i18n import get_i18n, tr

from app.gui.Live2DCanvas import Live2DCanvas

try:
    from PySide6.QtMultimedia import QSoundEffect
except Exception:
    QSoundEffect = None


class HitAreaOverlay(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._regions = []
        self._active_region_id = None
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.hide()

    def set_regions(self, regions: list[dict]):
        self._regions = list(regions or [])
        self.update()

    def regions(self) -> list[dict]:
        return list(self._regions)

    def set_active_region(self, region_id: str | None):
        self._active_region_id = region_id
        self.update()

    def region_at(self, pos: QPoint) -> dict | None:
        width = max(1, self.width())
        height = max(1, self.height())
        for region in reversed(self._regions):
            x, y, w, h = region.get("rect", (0, 0, 0, 0))
            rect = QRect(
                int(x * width),
                int(y * height),
                max(1, int(w * width)),
                max(1, int(h * height)),
            )
            if rect.contains(pos):
                return region
        return None

    def paintEvent(self, event):
        if not self._regions:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        width = max(1, self.width())
        height = max(1, self.height())
        for region in self._regions:
            x, y, w, h = region.get("rect", (0, 0, 0, 0))
            rect = QRect(
                int(x * width),
                int(y * height),
                max(1, int(w * width)),
                max(1, int(h * height)),
            )
            active = str(region.get("id") or region.get("name") or "") == str(self._active_region_id or "")
            fill = QColor(0, 166, 179, 72 if active else 42)
            border = QPen(QColor(0, 166, 179, 230 if active else 170), 3 if active else 2)
            text_color = QColor(0, 96, 104, 240)
            painter.setPen(border)
            painter.setBrush(QBrush(fill))
            painter.drawRoundedRect(rect, 8, 8)
            painter.setPen(text_color)
            painter.drawText(rect.adjusted(8, 6, -8, -6), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, str(region.get("name", "")))


class Live2DPreviewWindow(QWidget):
    """无边框的Live2D模型预览窗口"""

    closed = Signal()  # 窗口关闭信号

    def __init__(self, model_path=None):
        super().__init__()
        self.model_path = model_path
        self.i18n = get_i18n()
        self.live2d_canvas = None
        self.hit_area_overlay = None
        self.hit_area_toggle_btn = None
        self.control_panel = None
        self.dragging = False
        self.drag_position = QPoint()
        self._selected_motion = None
        self._motion_items = []
        self._dock_rect = None
        self._requested_canvas_size = (400, 300)
        self._show_hit_areas = True
        self._resize_margin = 12
        self._resizing = False
        self._resize_edges = set()
        self._resize_start_global = QPoint()
        self._resize_start_geometry = QRect()
        self._sound_effect = None
        self._last_bubble_motion = None
        self.bubble_label = None
        self.bubble_timer = QTimer(self)
        self.bubble_timer.setSingleShot(True)
        self.bubble_timer.timeout.connect(self._hide_bubble)
        self.controls_title = None
        self.motion_label = None
        self.toggle_controls_btn = None
        self.close_btn = None

        # 设置无边框窗口
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        # 设置窗口大小和位置
        self.resize(400, 300)
        self.move_to_screen_center()

        # 初始化UI
        self.setup_ui()
        self.i18n.languageChanged.connect(self.retranslate_ui)

    # 新增：统一的错误提示
    def _show_error_infobar(self, content: str, title: str = None):
        try:
            # 优先将 InfoBar 挂到主窗口/其他顶层窗口上，避免当前预览窗口关闭后看不到提示
            parent = QApplication.activeWindow()
            if parent is None or parent is self:
                for w in QApplication.topLevelWidgets():
                    if w is not self and w.isVisible():
                        parent = w
                        break
            InfoBar.error(
                title=title or tr("common.error"),
                content=content,
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=parent if parent is not None else self
            )
        except Exception:
            print(f"[Error] {title}: {content}")

    def setup_ui(self):
        """设置用户界面"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 统一确定模型路径
        if not self.model_path or not os.path.exists(self.model_path):
            self._show_error_infobar(tr("preview_window.error_model_missing", path=self.model_path))
            self.close()
            return

        # 创建Live2D显示区域并捕获异常
        try:
            self.live2d_canvas = Live2DCanvas(self.model_path)
        except Exception as e:
            # 显示错误并关闭窗口
            self._show_error_infobar(
                tr("preview_window.error_model_load_failed", error_type=type(e).__name__, error=e)
            )
            self.close()
            return
        # 设置Live2D widget样式
        self.live2d_canvas.setStyleSheet("""
            Live2DCanvas {
                background: transparent;
                border: 2px solid rgba(255, 255, 255, 0.3);
                border-radius: 10px;
            }
            Live2DCanvas:hover {
                border: 2px solid rgba(255, 255, 255, 0.5);
            }
        """)

        # 监听canvas右键
        self.live2d_canvas.setMouseTracking(True)
        self.live2d_canvas.installEventFilter(self)
        layout.addWidget(self.live2d_canvas)
        self.hit_area_overlay = HitAreaOverlay(self.live2d_canvas)
        self.hit_area_overlay.setGeometry(self.live2d_canvas.rect())
        self.hit_area_overlay.set_regions(self._load_hit_regions())
        self.hit_area_overlay.setVisible(self._show_hit_areas and bool(self.hit_area_overlay.regions()))
        self.hit_area_overlay.raise_()
        # 创建控制面板（可隐藏）
        self.control_panel = self.create_control_panel()
        self.control_panel.setVisible(False)  # 默认隐藏
        layout.addWidget(self.control_panel)
        self._create_bubble_label()

    def create_control_panel(self):
        """创建控制面板"""
        panel = CardWidget(self)
        panel.setFixedHeight(180)
        panel.setStyleSheet("""
            CardWidget {
                background: rgba(30, 30, 30, 0.9);
                border-radius: 10px;
            }
        """)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)

        # 标题
        self.controls_title = SubtitleLabel("", panel)
        self.controls_title.setStyleSheet("color: white;")
        layout.addWidget(self.controls_title)

        # 动作选择
        row = QHBoxLayout()
        self.motion_label = BodyLabel("", panel)
        self.motion_label.setStyleSheet("color: white;")
        row.addWidget(self.motion_label)
        self.motion_combo = ComboBox(panel)
        self.motion_combo.setMinimumWidth(220)
        # 填充动作列表
        self._populate_motion_combo()
        # 保存选择
        self.motion_combo.currentIndexChanged.connect(self._on_motion_changed)
        row.addWidget(self.motion_combo, 1)
        layout.addLayout(row)

        # 控制按钮行
        button_layout = QHBoxLayout()

        # 切换控制面板按钮
        self.toggle_controls_btn = PushButton("", panel)
        self.toggle_controls_btn.clicked.connect(self.toggle_control_panel)
        button_layout.addWidget(self.toggle_controls_btn)

        self.hit_area_toggle_btn = PushButton("", panel)
        self.hit_area_toggle_btn.clicked.connect(self._toggle_hit_area_overlay)
        button_layout.addWidget(self.hit_area_toggle_btn)

        # 关闭按钮
        self.close_btn = PushButton("", panel)
        self.close_btn.clicked.connect(self.close)
        button_layout.addWidget(self.close_btn)

        button_layout.addStretch()
        button_layout.addWidget(QSizeGrip(panel))

        layout.addLayout(button_layout)

        self.retranslate_ui()
        return panel

    def _populate_motion_combo(self):
        """读取model*.json中的动作并填充到下拉框"""
        import json
        self.motion_combo.clear()
        self._motion_items = []
        self._selected_motion = None
        if not self.model_path or not os.path.exists(self.model_path):
            return
        base_dir = os.path.dirname(self.model_path)
        try:
            with open(self.model_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            refs = (data or {}).get('FileReferences') or {}
            groups = refs.get('Motions') or {}
            for g, items in groups.items():
                if not isinstance(items, list):
                    continue
                for idx, it in enumerate(items):
                    rel = (it or {}).get('File') or ''
                    sound_rel = (it or {}).get('Sound') or ''
                    display = f"{g}[{idx}] - {os.path.basename(rel) if rel else ''}"
                    self._motion_items.append({
                        "group": str(g),
                        "index": int(idx),
                        "display": display,
                        "rel": rel,
                        "sound": os.path.normpath(os.path.join(base_dir, sound_rel)) if sound_rel else "",
                        "sound_rel": sound_rel,
                    })
        except Exception:
            self._motion_items = []
        if not self._motion_items:
            self.motion_combo.addItem(tr("preview_window.no_motions"))
            self.motion_combo.setEnabled(False)
            return
        self.motion_combo.setEnabled(True)
        for motion in self._motion_items:
            self.motion_combo.addItem(str(motion.get("display") or motion.get("group") or "motion"))
        # 默认选中第一个
        self.motion_combo.setCurrentIndex(0)
        self._on_motion_changed(0)

    def _on_motion_changed(self, i: int):
        if 0 <= i < len(self._motion_items):
            self._selected_motion = self._motion_items[i]
        else:
            self._selected_motion = None

    def _load_hit_regions(self) -> list[dict]:
        import json

        hit_areas = []
        if self.model_path and os.path.exists(self.model_path):
            try:
                with open(self.model_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                hit_areas = (data or {}).get("HitAreas") or []
            except Exception:
                hit_areas = []

        regions = []
        seen = set()
        for item in hit_areas:
            if not isinstance(item, dict):
                continue
            hit_id = str(item.get("Id") or item.get("id") or "")
            name = str(item.get("Name") or item.get("name") or hit_id or "HitArea")
            key = (hit_id.lower(), name.lower())
            if key in seen:
                continue
            seen.add(key)
            regions.append(self._hit_region_spec(name, hit_id))

        if regions:
            return regions
        return [
            {"id": "HitAreaHead", "name": "Head", "rect": (0.28, 0.05, 0.44, 0.28), "words": ("head", "face", "hair", "eye")},
            {"id": "HitAreaBody", "name": "Body", "rect": (0.20, 0.30, 0.60, 0.42), "words": ("body", "chest", "breast", "arm", "hand")},
            {"id": "HitAreaLower", "name": "Lower", "rect": (0.25, 0.70, 0.50, 0.25), "words": ("leg", "foot", "skirt")},
        ]

    def _hit_region_spec(self, name: str, hit_id: str) -> dict:
        label = name or hit_id or "HitArea"
        text = f"{hit_id} {name}".lower().replace("_", " ").replace("-", " ")
        if any(word in text for word in ("head", "face", "hair", "eye", "口", "目", "顔", "頭", "脸", "头")):
            rect = (0.28, 0.05, 0.44, 0.28)
            words = ("head", "face", "hair", "eye")
        elif any(word in text for word in ("left", "左")) and any(word in text for word in ("hand", "arm", "手", "腕")):
            rect = (0.02, 0.32, 0.24, 0.42)
            words = ("left", "hand", "arm", "body")
        elif any(word in text for word in ("right", "右")) and any(word in text for word in ("hand", "arm", "手", "腕")):
            rect = (0.74, 0.32, 0.24, 0.42)
            words = ("right", "hand", "arm", "body")
        elif any(word in text for word in ("body", "torso", "chest", "breast", "bust", "身体", "体", "胸")):
            rect = (0.20, 0.30, 0.60, 0.42)
            words = ("body", "chest", "breast", "arm", "hand")
        elif any(word in text for word in ("leg", "foot", "skirt", "feet", "脚", "足", "裙")):
            rect = (0.25, 0.70, 0.50, 0.25)
            words = ("leg", "foot", "skirt")
        else:
            rect = (0.25, 0.25, 0.50, 0.50)
            words = tuple(part for part in text.split() if part) + ("tap", "touch")
        return {"id": hit_id or label, "name": label, "rect": rect, "words": words}

    def eventFilter(self, obj, event):
        if obj is self.live2d_canvas:
            try:
                pos = event.position().toPoint() if hasattr(event, "position") else event.pos()

                if event.type() == QEvent.Type.MouseMove:
                    if self._resizing:
                        self._resize_from_global(self._event_global_pos(event))
                        return True
                    if not (event.buttons() & Qt.MouseButton.LeftButton):
                        edges = self._resize_edges_at_pos(pos)
                        self.live2d_canvas.setCursor(self._cursor_for_edges(edges))
                    return False

                if event.type() == QEvent.Type.MouseButtonRelease and self._resizing:
                    self._resizing = False
                    self._resize_edges = set()
                    self.live2d_canvas.unsetCursor()
                    return True

                if event.type() == QEvent.Type.MouseButtonPress:
                    if event.button() == Qt.MouseButton.LeftButton:
                        edges = self._resize_edges_at_pos(pos)
                        if edges:
                            self._begin_resize(edges, self._event_global_pos(event))
                            return True
                        w = max(1, self.live2d_canvas.width())
                        h = max(1, self.live2d_canvas.height())
                        region = self._hit_region_at_canvas_pos(pos)
                        words = tuple(region.get("words", ())) if region else None
                        name = str(region.get("name") or "") if region else None
                        if self.hit_area_overlay:
                            active_id = str(region.get("id") or region.get("name") or "") if region else None
                            self.hit_area_overlay.set_active_region(active_id)
                        motion = self.live2d_canvas.playInteractiveMotion(pos.x() / w, pos.y() / h, words, name)
                        self._after_motion_triggered(motion, pos)
                        return True
                    if event.button() == Qt.MouseButton.RightButton and self._selected_motion:
                        self._play_motion_item(self._selected_motion, pos)
                        return True
            except Exception:
                pass
        return super().eventFilter(obj, event)

    def _hit_region_at_canvas_pos(self, pos: QPoint) -> dict | None:
        if self.hit_area_overlay is None:
            return None
        return self.hit_area_overlay.region_at(pos)

    def _sync_overlay_geometry(self):
        if not self.hit_area_overlay or not self.live2d_canvas:
            return
        self.hit_area_overlay.setGeometry(self.live2d_canvas.rect())
        self.hit_area_overlay.setVisible(self._show_hit_areas and bool(self.hit_area_overlay.regions()))
        self.hit_area_overlay.raise_()

    def _toggle_hit_area_overlay(self):
        self._show_hit_areas = not self._show_hit_areas
        self._sync_overlay_geometry()
        self.retranslate_ui()

    def _resize_edges_at_pos(self, pos: QPoint) -> set[str]:
        if not self.live2d_canvas:
            return set()
        width = max(1, self.live2d_canvas.width())
        height = max(1, self.live2d_canvas.height())
        margin = self._resize_margin
        edges = set()
        if pos.x() <= margin:
            edges.add("left")
        elif pos.x() >= width - margin:
            edges.add("right")
        if pos.y() <= margin:
            edges.add("top")
        elif pos.y() >= height - margin:
            edges.add("bottom")
        return edges

    def _cursor_for_edges(self, edges: set[str]) -> QCursor:
        if edges in ({"left", "top"}, {"right", "bottom"}):
            return QCursor(Qt.CursorShape.SizeFDiagCursor)
        if edges in ({"right", "top"}, {"left", "bottom"}):
            return QCursor(Qt.CursorShape.SizeBDiagCursor)
        if edges & {"left", "right"}:
            return QCursor(Qt.CursorShape.SizeHorCursor)
        if edges & {"top", "bottom"}:
            return QCursor(Qt.CursorShape.SizeVerCursor)
        return QCursor(Qt.CursorShape.ArrowCursor)

    def _begin_resize(self, edges: set[str], global_pos: QPoint):
        self._resizing = True
        self.dragging = False
        self._resize_edges = set(edges)
        self._resize_start_global = QPoint(global_pos)
        self._resize_start_geometry = QRect(self.geometry())
        self.live2d_canvas.setCursor(self._cursor_for_edges(edges))

    def _resize_from_global(self, global_pos: QPoint):
        delta = global_pos - self._resize_start_global
        geometry = QRect(self._resize_start_geometry)
        min_w = 240
        min_h = 240

        if "left" in self._resize_edges:
            new_left = geometry.left() + delta.x()
            if geometry.right() - new_left + 1 >= min_w:
                geometry.setLeft(new_left)
        if "right" in self._resize_edges:
            geometry.setRight(max(geometry.left() + min_w - 1, geometry.right() + delta.x()))
        if "top" in self._resize_edges:
            new_top = geometry.top() + delta.y()
            if geometry.bottom() - new_top + 1 >= min_h:
                geometry.setTop(new_top)
        if "bottom" in self._resize_edges:
            geometry.setBottom(max(geometry.top() + min_h - 1, geometry.bottom() + delta.y()))

        geometry = self._clamp_resize_geometry(geometry)
        self.setGeometry(geometry)
        self._sync_overlay_geometry()

    def _clamp_resize_geometry(self, geometry: QRect) -> QRect:
        if not self._dock_rect:
            return geometry
        dock = QRect(
            int(self._dock_rect.get("x", geometry.x())),
            int(self._dock_rect.get("y", geometry.y())),
            int(self._dock_rect.get("w", geometry.width())),
            int(self._dock_rect.get("h", geometry.height())),
        )
        geometry.setWidth(min(geometry.width(), dock.width()))
        geometry.setHeight(min(geometry.height(), dock.height()))
        if geometry.left() < dock.left():
            geometry.moveLeft(dock.left())
        if geometry.top() < dock.top():
            geometry.moveTop(dock.top())
        if geometry.right() > dock.right():
            geometry.moveRight(dock.right())
        if geometry.bottom() > dock.bottom():
            geometry.moveBottom(dock.bottom())
        return geometry

    def _create_bubble_label(self):
        self.bubble_label = QLabel(self)
        self.bubble_label.setWordWrap(True)
        self.bubble_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bubble_label.setStyleSheet("""
            QLabel {
                color: white;
                background: rgba(30, 30, 30, 185);
                border-radius: 10px;
                padding: 10px 14px;
            }
        """)
        self.bubble_label.hide()

    def _hide_bubble(self):
        if self.bubble_label:
            self.bubble_label.hide()

    def _bubble_text(self, motion):
        if not motion:
            return tr("preview_window.bubble_tap")
        name = os.path.basename(str(motion.get("sound") or motion.get("rel") or motion.get("display") or ""))
        if not name:
            name = str(motion.get("group") or "motion")
        return tr("preview_window.bubble_triggered", motion=name)

    def _show_bubble(self, motion, pos=None):
        if not self.bubble_label:
            return
        if motion is not None:
            self._last_bubble_motion = motion
        motion = motion if motion is not None else self._last_bubble_motion
        self.bubble_label.setText(self._bubble_text(motion))
        self.bubble_label.setMaximumWidth(max(220, min(420, self.width() - 40)))
        self.bubble_label.adjustSize()
        x = (self.width() - self.bubble_label.width()) // 2
        y = 24
        if pos is not None:
            x = max(16, min(pos.x() - self.bubble_label.width() // 2, self.width() - self.bubble_label.width() - 16))
            y = max(16, min(pos.y() - self.bubble_label.height() - 18, self.height() - self.bubble_label.height() - 16))
        self.bubble_label.move(x, y)
        self.bubble_label.raise_()
        self.bubble_label.show()
        self.bubble_timer.start(2600)

    def _play_motion_sound(self, motion):
        if not motion or QSoundEffect is None:
            return
        sound_path = str(motion.get("sound") or "")
        if not sound_path or not os.path.isfile(sound_path):
            return
        try:
            if self._sound_effect is not None:
                self._sound_effect.stop()
                self._sound_effect.deleteLater()
            self._sound_effect = QSoundEffect(self)
            self._sound_effect.setSource(QUrl.fromLocalFile(sound_path))
            self._sound_effect.setVolume(0.85)
            self._sound_effect.play()
        except Exception:
            pass

    def _after_motion_triggered(self, motion, pos=None):
        if motion:
            self._play_motion_sound(motion)
        self._show_bubble(motion, pos)

    def _play_motion_item(self, motion, pos=None):
        if not self.live2d_canvas or not motion:
            return
        played = self.live2d_canvas.playMotionItem(motion)
        self._after_motion_triggered(played, pos)

    def play_motion(self, group: str, index: int):
        if not self.live2d_canvas:
            return
        motion = self.live2d_canvas.findMotion(group, index)
        if motion is None:
            for item in self._motion_items:
                if str(item.get("group", "")) == str(group) and int(item.get("index", -1)) == int(index):
                    motion = item
                    break
        self._play_motion_item(motion, None)

    def move_to_screen_center(self):
        """将窗口移动到屏幕中央"""
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        screen = screen.availableGeometry()
        size = self.geometry()
        x = screen.x() + (screen.width() - size.width()) // 2
        y = screen.y() + (screen.height() - size.height()) // 2
        self.move(x, y)

    def apply_settings(self, settings):
        """应用设置到预览窗口和Live2D模型"""
        if not settings:
            return

        if 'show_controls' in settings and self.control_panel:
            should_show = bool(settings.get('show_controls'))
            if self.control_panel.isVisible() != should_show:
                self.toggle_control_panel()

        # 应用窗口设置
        if 'window_size' in settings:
            w, h = settings['window_size']
            self._requested_canvas_size = (int(w), int(h))
            # 当控制面板可见时，窗口总高度 = 目标画布高度 + 控制面板高度
            extra_h = 0
            try:
                if self.control_panel is not None and self.control_panel.isVisible():
                    extra_h = int(self.control_panel.height())
            except Exception:
                extra_h = 0
            total_h = int(h) + extra_h
            try:
                self._resize_clamped(int(w), int(total_h))
            except Exception:
                self._show_error_infobar("Failed to resize preview window.")

        # 画布透明度（模型不透明度）
        if 'opacity' in settings and self.live2d_canvas:
            self.live2d_canvas.setCanvasOpacity(settings['opacity'])

        # 模型旋转
        if 'model_rotation' in settings and self.live2d_canvas:
            self.live2d_canvas.setRotationAngle(settings['model_rotation'])

        # 背景透明/颜色
        if self.live2d_canvas and ('transparent_bg' in settings or 'bg_color' in settings):
            transparent = bool(settings.get('transparent_bg', True))
            qcolor = settings.get('bg_color')
            self.live2d_canvas.setBackground(transparent, qcolor)

        # 鼠标跟踪
        if 'mouse_tracking' in settings and self.live2d_canvas:
            self.live2d_canvas.setMouseTracking(bool(settings['mouse_tracking']))

        # 自动眨眼/呼吸
        if 'auto_blink' in settings and self.live2d_canvas:
            self.live2d_canvas.setAutoBlinkEnable(bool(settings['auto_blink']))
        if 'auto_breath' in settings and self.live2d_canvas:
            self.live2d_canvas.setAutoBreathEnable(bool(settings['auto_breath']))

        # 高级参数
        if self.live2d_canvas and ('advanced_enabled' in settings or 'advanced_params' in settings):
            enabled = bool(settings.get('advanced_enabled', False))
            params = settings.get('advanced_params', {}) or {}
            self.live2d_canvas.setAdvancedParams(enabled, params)

    def toggle_control_panel(self):
        """切换控制面板显示/隐藏"""
        if self.control_panel.isVisible():
            self.control_panel.setVisible(False)
            self.toggle_controls_btn.setText(tr("preview_window.show_controls"))
            # 调整窗口大小
            self._resize_clamped(self.width(), self.height() - self.control_panel.height())
        else:
            self.control_panel.setVisible(True)
            self.toggle_controls_btn.setText(tr("preview_window.hide_controls"))
            # 调整窗口大小
            self._resize_clamped(self.width(), self.height() + self.control_panel.height())

    def _resize_clamped(self, width: int, height: int):
        max_w = width
        max_h = height
        if self._dock_rect:
            max_w = int(self._dock_rect.get("w", width))
            max_h = int(self._dock_rect.get("h", height))
        self.resize(max(240, min(int(width), max_w)), max(240, min(int(height), max_h)))

    def apply_dock_geometry(self, rect: dict):
        if not rect:
            return
        try:
            x = int(rect.get("x", self.x()))
            y = int(rect.get("y", self.y()))
            w = max(240, int(rect.get("w", self.width())))
            h = max(240, int(rect.get("h", self.height())))
        except Exception:
            return
        self._dock_rect = {"x": x, "y": y, "w": w, "h": h}
        self.setMaximumSize(w, h)
        self.move(x, y)
        target_w = min(self.width(), w)
        target_h = min(self.height(), h)
        if target_w != self.width() or target_h != self.height():
            self.resize(target_w, target_h)
        self._sync_overlay_geometry()
        self.raise_()

    def mousePressEvent(self, event):
        """鼠标按下事件 - 用于拖拽窗口"""
        if event.button() == Qt.MouseButton.LeftButton:
            # 检查是否点击在控制面板区域
            if self.control_panel.isVisible():
                control_rect = self.control_panel.geometry()
                if control_rect.contains(event.pos()):
                    return  # 在控制面板区域，不启动拖拽

            self.dragging = True
            self.drag_position = self._event_global_pos(event) - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        """鼠标移动事件 - 拖拽窗口"""
        if (event.buttons() & Qt.MouseButton.LeftButton) and self.dragging:
            self.move(self._event_global_pos(event) - self.drag_position)
            event.accept()
        else:
            pass

    def mouseReleaseEvent(self, event):
        """鼠标释放事件"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.dragging = False

    def mouseDoubleClickEvent(self, event):
        """双击事件 - 切换控制面板"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_control_panel()

    def keyPressEvent(self, event):
        """键盘事件处理"""
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        elif event.key() == Qt.Key.Key_Space:
            self.toggle_control_panel()
        super().keyPressEvent(event)

    def _event_global_pos(self, event):
        if hasattr(event, "globalPosition"):
            return event.globalPosition().toPoint()
        return event.globalPos()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_overlay_geometry()
        if self.bubble_label and self.bubble_label.isVisible():
            self._show_bubble(None)

    def closeEvent(self, event):
        """窗口关闭事件"""
        if self.live2d_canvas:
            self.live2d_canvas.release()
        self.closed.emit()
        super().closeEvent(event)

    def contextMenuEvent(self, event):
        """右键菜单事件"""
        pass

    def retranslate_ui(self):
        if self.controls_title:
            self.controls_title.setText(tr("preview_window.controls_title"))
        if self.motion_label:
            self.motion_label.setText(tr("preview_window.right_click_action"))
        if self.toggle_controls_btn:
            if self.control_panel and self.control_panel.isVisible():
                self.toggle_controls_btn.setText(tr("preview_window.hide_controls"))
            else:
                self.toggle_controls_btn.setText(tr("preview_window.show_controls"))
        if self.hit_area_toggle_btn:
            if self._show_hit_areas:
                self.hit_area_toggle_btn.setText(tr("preview_window.hide_hit_areas"))
            else:
                self.hit_area_toggle_btn.setText(tr("preview_window.show_hit_areas"))
        if self.close_btn:
            self.close_btn.setText(tr("common.close"))

        if self.motion_combo and not self._motion_items and self.motion_combo.count() > 0:
            self.motion_combo.setItemText(0, tr("preview_window.no_motions"))
