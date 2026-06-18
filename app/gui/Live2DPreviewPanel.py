from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QSizePolicy, QVBoxLayout
from qfluentwidgets import BodyLabel, SubtitleLabel

from app.core.model import prepare_model_json_for_preview
from app.gui.Live2DPreviewWindow import Live2DPreviewWindow
from app.gui.PreviewPage import ImagePreviewPanel
from app.i18n import get_i18n, tr


class Live2DPreviewPanel(QFrame):
    def __init__(self, parent=None, object_name: str = "sharedLive2DPreviewPanel"):
        super().__init__(parent)
        self.setObjectName(object_name)
        self.i18n = get_i18n()
        self.live2d_preview_window: Live2DPreviewWindow | None = None
        self._custom_title = ""
        self._custom_placeholder = ""

        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(16, 16, 16, 16)
        self.layout.setSpacing(10)

        self.title_label = SubtitleLabel("", self)
        self.layout.addWidget(self.title_label)

        self.placeholder_label = BodyLabel("", self)
        self.placeholder_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder_label.setWordWrap(True)
        self.placeholder_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.layout.addWidget(self.placeholder_label, 1)

        self.image_panel = ImagePreviewPanel(self)
        self.image_panel.setVisible(False)
        self.layout.addWidget(self.image_panel, 1)

        self.live2d_host = QFrame(self)
        self.live2d_host.setObjectName(f"{object_name}Host")
        self.live2d_layout = QVBoxLayout(self.live2d_host)
        self.live2d_layout.setContentsMargins(0, 0, 0, 0)
        self.live2d_layout.setSpacing(0)
        self.live2d_host.setVisible(False)
        self.layout.addWidget(self.live2d_host, 1)

        self.i18n.languageChanged.connect(self.retranslate_ui)
        self.retranslate_ui()
        self._apply_static_styles()

    def retranslate_ui(self):
        self.title_label.setText(self._custom_title or tr("preview_panel.title"))
        self.placeholder_label.setText(self._custom_placeholder or tr("preview_panel.placeholder"))
        self.image_panel.retranslate_ui()

    def set_title(self, text: str):
        self._custom_title = str(text or "")
        self.title_label.setText(self._custom_title or tr("preview_panel.title"))

    def set_placeholder(self, text: str):
        self._custom_placeholder = str(text or "")
        self.placeholder_label.setText(self._custom_placeholder or tr("preview_panel.placeholder"))

    def show_placeholder(self, text: str = "", title: str = ""):
        self.close_live2d_preview()
        self.image_panel.setVisible(False)
        self.live2d_host.setVisible(False)
        self.placeholder_label.setVisible(True)
        self.set_title(title or tr("preview_panel.title"))
        self.set_placeholder(text or tr("preview_panel.placeholder"))

    def show_images(self, image_paths: list[str | Path], title: str = ""):
        paths = [str(Path(path)) for path in image_paths if Path(path).is_file()]
        if not paths:
            self.show_placeholder(tr("preview_panel.no_images"), title)
            return
        self.close_live2d_preview()
        self.live2d_host.setVisible(False)
        self.placeholder_label.setVisible(False)
        self.image_panel.setVisible(True)
        self.image_panel.load_images(paths)
        self.set_title(title or tr("preview_panel.images_title", count=len(paths)))

    def show_live2d(self, model_json: str | Path, title: str = "", settings: dict[str, Any] | None = None):
        model_path = Path(model_json)
        if not model_path.is_file():
            self.show_placeholder(tr("preview_panel.no_model"), title)
            return

        self.close_live2d_preview()
        self.image_panel.setVisible(False)
        self.live2d_host.setVisible(False)
        self.placeholder_label.setVisible(True)
        self.set_title(title or tr("preview_panel.live2d_title"))
        self.set_placeholder(tr("preview_panel.loading_live2d"))

        preview_model_path = prepare_model_json_for_preview(model_path)
        preview_window = Live2DPreviewWindow(
            str(preview_model_path),
            parent=self.live2d_host,
            embedded=True,
        )
        self.live2d_preview_window = preview_window
        preview_window.apply_settings(settings or {"show_controls": True})
        self.live2d_layout.addWidget(preview_window, 1)
        self.placeholder_label.setVisible(False)
        self.live2d_host.setVisible(True)
        preview_window.show()

    def close_preview(self, text: str = ""):
        self.show_placeholder(text or tr("preview_panel.closed"))

    def close_live2d_preview(self):
        window = self.live2d_preview_window
        self.live2d_preview_window = None
        if window is None:
            return
        try:
            self.live2d_layout.removeWidget(window)
            window.close()
            window.deleteLater()
        except Exception:
            pass

    def _apply_static_styles(self):
        self.setStyleSheet(
            f"""
            QFrame#{self.objectName()} {{
                border: 1px solid #dde2ea;
                border-radius: 8px;
                background: #ffffff;
            }}
            """
        )
        self.live2d_host.setStyleSheet(
            """
            QFrame {
                border: 1px solid #dde2ea;
                border-radius: 8px;
                background: #ffffff;
            }
            """
        )
