from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from typing import Any

from PySide6.QtCore import QObject, QCoreApplication, Qt, Signal
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import QApplication

from app.core.app_logging import setup_runtime_logging
from app.i18n import get_i18n, normalize_language_code
from app.paths import APP_ICON


logger = logging.getLogger(__name__)


class PreviewCommandBridge(QObject):
    commandReceived = Signal(dict)


def _decode_settings(settings: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(settings or {})
    bg_color = decoded.get("bg_color")
    if isinstance(bg_color, str):
        color = QColor(bg_color)
        if color.isValid():
            decoded["bg_color"] = color
        else:
            decoded.pop("bg_color", None)
    return decoded


def _start_command_reader(bridge: PreviewCommandBridge) -> None:
    def read_loop():
        stream = sys.stdin
        if stream is None:
            return
        while True:
            line = stream.readline()
            if not line:
                bridge.commandReceived.emit({"type": "stdin_closed"})
                return
            try:
                command = json.loads(line)
            except Exception:
                logger.exception("Invalid preview command: %r", line)
                continue
            if isinstance(command, dict):
                bridge.commandReceived.emit(command)

    thread = threading.Thread(target=read_loop, name="preview-command-reader", daemon=True)
    thread.start()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LpkUnpacker Live2D preview process")
    parser.add_argument("--model", required=True, help="Path to model3.json/model.json")
    parser.add_argument("--language", default="en_US", help="UI language code")
    parser.add_argument("--settings-json", default="", help="Initial preview settings JSON")
    return parser.parse_args(argv)


def run_preview_process(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    log_path = setup_runtime_logging()
    print(f"Preview process runtime log: {log_path}")

    if hasattr(Qt.ApplicationAttribute, "AA_ShareOpenGLContexts"):
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication([sys.argv[0], *([] if argv is None else argv)])
    app.setQuitOnLastWindowClosed(True)
    app.setWindowIcon(QIcon(str(APP_ICON)))

    font = app.font()
    font.setPointSize(10)
    app.setFont(font)

    get_i18n().set_language(normalize_language_code(args.language))

    from app.gui.Live2DPreviewWindow import Live2DPreviewWindow

    window = Live2DPreviewWindow(args.model)
    if window.live2d_canvas is None:
        return 1
    window.closed.connect(app.quit)
    window.show()

    if args.settings_json:
        try:
            window.apply_settings(_decode_settings(json.loads(args.settings_json)))
        except Exception:
            logger.exception("Failed to apply initial preview settings")

    bridge = PreviewCommandBridge()

    def handle_command(command: dict):
        command_type = str(command.get("type") or "")
        if command_type in {"close", "stdin_closed"}:
            window.close()
            return
        if command_type == "settings":
            window.apply_settings(_decode_settings(command.get("settings") or {}))
            return
        if command_type == "dock":
            window.apply_dock_geometry(command.get("rect") or {})
            return
        if command_type == "play_motion" and window.live2d_canvas:
            try:
                window.play_motion(
                    str(command.get("group") or ""),
                    int(command.get("index") or 0),
                )
            except Exception:
                logger.exception("Failed to play motion")
            return
        if command_type == "set_selected_motion" and window.live2d_canvas:
            try:
                window.set_selected_motion(
                    str(command.get("group") or ""),
                    int(command.get("index") or 0),
                )
            except Exception:
                logger.exception("Failed to select motion")

    bridge.commandReceived.connect(handle_command)
    _start_command_reader(bridge)

    return app.exec()


if __name__ == "__main__":
    sys.exit(run_preview_process())
