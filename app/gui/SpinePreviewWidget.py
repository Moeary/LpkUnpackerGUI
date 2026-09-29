"""Embedded WebEngine host for the local Spine preview page.

The Spine renderer is intentionally kept in ``assets/spine/preview.html``.  This
widget only owns the browser page and translates navigation/JavaScript failures
into Qt signals so the surrounding preview page can leave its loading state.
"""

from __future__ import annotations

import json
import time
from typing import Any

from PySide6.QtCore import QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QLabel, QFrame, QVBoxLayout


try:  # QtWebEngine is provided by the PySide6 Addons package in pixi.
    from PySide6.QtWebEngineCore import QWebEnginePage
    from PySide6.QtWebEngineWidgets import QWebEngineView

    WEBENGINE_AVAILABLE = True
except (ImportError, OSError):  # Keep lightweight/system-Python imports usable.
    QWebEnginePage = None  # type: ignore[assignment,misc]
    QWebEngineView = None  # type: ignore[assignment,misc]
    WEBENGINE_AVAILABLE = False


if WEBENGINE_AVAILABLE:

    class _SpineWebPage(QWebEnginePage):
        consoleMessage = Signal(str)

        def javaScriptConsoleMessage(self, level, message, line_number, source_id):  # noqa: N802
            self.consoleMessage.emit(str(message))
            super().javaScriptConsoleMessage(level, message, line_number, source_id)


class SpinePreviewWidget(QFrame):
    """Display one disposable Spine preview URL inside the Qt stage.

    ``preview.html`` reports asynchronous runtime/texture failures in its
    ``#status`` element.  A short DOM poll turns that browser-side error into
    ``previewFailed`` so the parent page can replace ``正在准备预览...`` with a
    concrete error message.
    """

    previewStarted = Signal(str)
    documentLoaded = Signal(str)
    previewReady = Signal(str)
    previewFailed = Signal(str)
    statusChanged = Signal(str, str)
    # A structured snapshot is intentionally emitted separately from the
    # legacy status text.  PreviewPage uses it to drive native Qt controls;
    # standalone browser users still only see the page's own toolbar.
    stateChanged = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("spinePreviewWidget")
        self._url = ""
        self._navigation_generation = 0
        self._active_generation = 0
        self._ignore_navigation = False
        self._reported_error = False
        self._reported_ready = False
        self._last_state = {}
        self._status_deadline = 0.0
        self._status_poll = QTimer(self)
        self._status_poll.setInterval(250)
        self._status_poll.timeout.connect(self._poll_status)

        self.view = None
        self._fallback_label = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        if WEBENGINE_AVAILABLE:
            self.view = QWebEngineView(self)
            self.view.setObjectName("spineWebEngineView")
            self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
            page = _SpineWebPage(self.view)
            # The Spine canvas uses alpha when the left display settings select
            # a transparent background.  Keep the browser viewport transparent
            # too, otherwise Chromium's default page surface shows through.
            page.setBackgroundColor(QColor(0, 0, 0, 0))
            self.view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.view.setStyleSheet("background: transparent;")
            page.consoleMessage.connect(self._on_console_message)
            self.view.setPage(page)
            self.view.loadStarted.connect(self._on_load_started)
            self.view.loadFinished.connect(self._on_load_finished)
            layout.addWidget(self.view, 1)
        else:
            self._fallback_label = QLabel(
                "QtWebEngine is unavailable in this Python environment.", self
            )
            self._fallback_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._fallback_label.setWordWrap(True)
            layout.addWidget(self._fallback_label, 1)

    @property
    def is_available(self) -> bool:
        return bool(WEBENGINE_AVAILABLE and self.view is not None)

    @property
    def current_url(self) -> str:
        return self._url

    def open_url(self, url: str) -> None:
        if not self.is_available:
            raise RuntimeError(
                "QtWebEngine is unavailable; install the PySide6 WebEngine Addons package."
            )
        target = str(url or "").strip()
        if not target:
            raise ValueError("Spine preview URL is empty.")
        self._ignore_navigation = False
        self._reported_error = False
        self._reported_ready = False
        self._last_state = {}
        self._status_deadline = time.monotonic() + 65.0
        self._url = target
        self._navigation_generation += 1
        self._active_generation = self._navigation_generation
        self.previewStarted.emit(target)
        self.view.setUrl(QUrl(target))

    def clear_preview(self) -> None:
        """Stop the current page and navigate to a blank document."""

        self._status_poll.stop()
        self._reported_error = False
        self._reported_ready = False
        self._last_state = {}
        self._status_deadline = 0.0
        self._ignore_navigation = True
        self._url = ""
        self._navigation_generation += 1
        self._active_generation = 0
        if self.view is not None:
            self.view.stop()
            self.view.setUrl(QUrl("about:blank"))
        self.hide()

    def shutdown(self) -> None:
        """Release page activity before the parent window is destroyed."""

        self.clear_preview()
        if self.view is not None:
            self.view.page().deleteLater()

    def _on_load_started(self) -> None:
        if not self._is_current_navigation():
            return
        self._reported_error = False

    def _on_load_finished(self, ok: bool) -> None:
        if not self._is_current_navigation():
            return
        if not ok:
            self._report_error(f"Spine preview page failed to load: {self._url}")
            return
        # Show the view as soon as its document exists.  WebGL requestAnimationFrame
        # can be throttled while the widget is hidden, so documentLoaded is
        # deliberately separate from the renderer's eventual previewReady state.
        self._install_error_bridge()
        self._install_embedded_host_class()
        self.documentLoaded.emit(self._url)
        self._status_poll.start()
        # The atlas fallback has no asynchronous runtime setup.  One immediate
        # check still catches malformed manifests before the timer's first tick.
        self._poll_status()

    def _on_console_message(self, message: str) -> None:
        # Console output is useful for diagnostics.  The page's explicit state
        # remains authoritative, with uncaught JavaScript errors as a fallback
        # for renderFrame failures that occur outside the page's promise chain.
        if message:
            self.statusChanged.emit("console", message)
            lowered = message.lower()
            if any(
                marker in lowered
                for marker in ("uncaught", "typeerror", "referenceerror", "syntaxerror")
            ):
                self._report_error(message)

    def _poll_status(self) -> None:
        if not self._is_current_navigation() or self.view is None:
            self._status_poll.stop()
            return
        if self._status_deadline and time.monotonic() > self._status_deadline:
            self._report_error("Spine preview did not reach a ready or error state.")
            return
        page = self.view.page()
        if page is None:
            return
        script = """JSON.stringify((function () {
            const node = document.getElementById('status');
            const atlas = document.getElementById('atlas');
            const exportButton = document.getElementById('exportPsd');
            function values(id) {
                const select = document.getElementById(id);
                return select ? Array.from(select.options || []).map(function (item) {
                    return {value: String(item.value || ''), text: String(item.textContent || ''), disabled: !!item.disabled};
                }) : [];
            }
            const track = window.lpkSpineHost && typeof window.lpkSpineHost.state === 'function'
                ? window.lpkSpineHost.state() : {};
            return {
                previewState: String(document.body && document.body.dataset.previewState || ''),
                previewError: String(document.body && document.body.dataset.previewError || ''),
                className: node ? String(node.className || '') : '',
                text: node ? String(node.textContent || '') : '',
                atlasReady: !!atlas && !atlas.classList.contains('hidden'),
                runtimeReady: !!exportButton && !exportButton.classList.contains('hidden'),
                mode: String(track.mode || ''),
                skinOptions: values('skin'),
                animationOptions: values('animation'),
                selectedSkin: String(track.selectedSkin || (document.getElementById('skin') || {}).value || ''),
                selectedAnimation: String(track.selectedAnimation || (document.getElementById('animation') || {}).value || ''),
                paused: !!track.paused,
                loop: !!track.loop,
                time: Number(track.time || 0),
                timeMax: Number(track.timeMax || (document.getElementById('time') || {}).max || 0),
                runtimeLoaded: !!track.runtimeLoaded,
                skeletonLoaded: !!track.skeletonLoaded
            };
        })());"""
        try:
            page.runJavaScript(script, self._on_status_result)
        except Exception as exc:
            self._report_error(f"Could not inspect Spine preview page: {exc}")

    def _on_status_result(self, result: Any) -> None:
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                result = {}
        if not self._is_current_navigation() or not isinstance(result, dict):
            return
        class_name = str(result.get("className") or "")
        text = str(result.get("text") or "").strip()
        self._last_state = dict(result)
        self.stateChanged.emit(dict(result))
        self.statusChanged.emit(class_name, text)
        state = str(result.get("previewState") or "").lower()
        if state == "error":
            self._report_error(
                str(result.get("previewError") or text or "Spine preview reported an unknown error.")
            )
            return
        if state == "ready":
            if not self._reported_ready:
                self._reported_ready = True
                self.previewReady.emit(self._url)
            # Keep polling after the first frame: the native animation panel
            # mirrors skin/animation/time changes and late texture failures
            # must still reach the page.  The loading deadline only applies
            # until the first explicit ready state.
            self._status_deadline = 0.0
            if not self._status_poll.isActive():
                self._status_poll.start()

    @property
    def last_state(self) -> dict:
        return dict(self._last_state)

    def _run_host_command(self, name: str, value: Any = None) -> None:
        """Call one of the page's small, JSON-safe native-host commands."""

        if self.view is None or not self._is_current_navigation():
            return
        argument = "" if value is None else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        script = "(function(){var host=window.lpkSpineHost;" \
            "if(host&&typeof host[%s]==='function'){host[%s](%s);return true;}return false;})();" % (
                json.dumps(str(name)), json.dumps(str(name)), argument
            )
        try:
            self.view.page().runJavaScript(script)
        except Exception as exc:
            self.statusChanged.emit("error", str(exc))

    def set_skin(self, name: str) -> None:
        self._run_host_command("setSkin", str(name or ""))

    def set_animation(self, name: str, loop: bool | None = None) -> None:
        payload = {"name": str(name or "")}
        if loop is not None:
            payload["loop"] = bool(loop)
        self._run_host_command("setAnimation", payload)

    def set_paused(self, paused: bool) -> None:
        self._run_host_command("setPaused", bool(paused))

    def set_loop(self, loop: bool) -> None:
        self._run_host_command("setLoop", bool(loop))

    def set_time(self, value: float) -> None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return
        self._run_host_command("setTime", number)

    def reset_pose(self) -> None:
        self._run_host_command("resetPose")

    def export_pose_psd(self) -> None:
        self._run_host_command("exportPosePsd")

    def set_view_settings(self, settings: dict) -> None:
        """Forward display-only settings to the embedded page.

        The settings panel remains shared with Live2D, but the bridge is kept
        explicit so a Spine preview never calls Live2D methods by accident.
        """

        if not isinstance(settings, dict):
            return
        payload = {
            "opacity": float(settings.get("opacity", 1.0)),
            "rotation": float(settings.get("model_rotation", 0.0)),
            "scale": float(settings.get("model_scale", 1.0)),
            "offsetX": float(settings.get("model_offset_x", 0.0)),
            "offsetY": float(settings.get("model_offset_y", 0.0)),
            "transparent": bool(settings.get("transparent_bg", True)),
            "background": (settings.get("bg_color").name()
                           if hasattr(settings.get("bg_color"), "name")
                           else str(settings.get("bg_color", "#20242b"))),
        }
        self._run_host_command("setViewSettings", payload)

    def _report_error(self, message: str) -> None:
        if self._reported_error:
            return
        self._reported_error = True
        self._status_poll.stop()
        self.previewFailed.emit(str(message or "Spine preview failed."))

    def _install_error_bridge(self) -> None:
        """Turn uncaught rAF/render errors into the page's explicit error state."""

        if self.view is None or self._ignore_navigation:
            return
        script = """(function () {
            if (window.__lpkSpineHostErrorBridge) return true;
            function report(error) {
                const message = String(error || 'Unknown Spine renderer error');
                if (document.body) {
                    document.body.dataset.previewState = 'error';
                    document.body.dataset.previewError = message;
                }
                const status = document.getElementById('status');
                if (status) { status.className = 'error'; status.textContent = message; }
            }
            window.addEventListener('error', function (event) {
                report(event && (event.error || event.message));
            });
            window.addEventListener('unhandledrejection', function (event) {
                report(event && event.reason);
            });
            window.__lpkSpineHostErrorBridge = true;
            return true;
        })();"""
        try:
            self.view.page().runJavaScript(script)
        except Exception as exc:
            self._report_error(f"Could not install Spine error bridge: {exc}")

    def _install_embedded_host_class(self) -> None:
        """Hide standalone page chrome only in the central Qt preview stage."""

        if self.view is None or self._ignore_navigation:
            return
        script = "(function(){if(document.body){document.body.classList.add('lpk-embedded-host');}return true;})();"
        try:
            self.view.page().runJavaScript(script)
        except Exception as exc:
            self.statusChanged.emit("error", str(exc))

    def _is_current_navigation(self) -> bool:
        return bool(
            not self._ignore_navigation
            and self.view is not None
            and self._url
            and self._active_generation == self._navigation_generation
        )


__all__ = ["SpinePreviewWidget", "WEBENGINE_AVAILABLE"]
