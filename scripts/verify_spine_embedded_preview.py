"""Exercise the real PreviewPage -> QWebEngine Spine path.

Examples (run from the repository root with pixi):

    pixi run python scripts/verify_spine_embedded_preview.py \
        --source path/to/spine-4.0-model --runtime path/to/spine-runtimes
    pixi run python scripts/verify_spine_embedded_preview.py \
        --source path/to/spine-3.8-model --switch-source path/to/atlas-only \
        --screenshot runtime/spine-check.png --report runtime/spine-check.json

The script is intentionally diagnostic rather than a unit test.  It starts a
real Qt event loop, reads the embedded page DOM and canvas state through
``runJavaScript``, exercises the animation controls, optionally captures the
full three-column PreviewPage (left settings, central canvas, right animation
controls), and checks that close/switch clears its URL.  Use
``--webview-screenshot`` when only the central WebEngine is desired.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path


# Allow ``python scripts/verify_spine_embedded_preview.py`` from the repository
# root as well as ``pixi run python ...``.  GPU/platform settings are left to
# the caller: Windows desktop verification should exercise its real WebGL path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication, Qt

if hasattr(Qt.ApplicationAttribute, "AA_ShareOpenGLContexts"):
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

import importlib

_preview_page_module = importlib.import_module("app.gui.PreviewPage")
PreviewPage = _preview_page_module.PreviewPage
from app.gui.SpinePreviewWidget import WEBENGINE_AVAILABLE


class _SandboxSettings:
    """Small in-memory SettingsManager replacement for diagnostics.

    The verification script must never rewrite the user's runtime/setting.json
    while it temporarily selects a 3.8 or 4.0 runtime.
    """

    def __init__(self, root: Path):
        self.root = root
        self.settings = {
            "preview": {
                "image_limit": 48,
                "spine_runtime_dir": "",
                "ui_state": {
                    "left_sidebar_visible": True,
                    "right_sidebar_visible": True,
                    "transparent_bg": True,
                    "mouse_tracking": True,
                    "auto_blink": True,
                    "auto_breath": True,
                },
            }
        }

    def get(self, key, default=None):
        value = self.settings
        try:
            for part in str(key).split("."):
                value = value[part]
            return value
        except (KeyError, TypeError):
            return default

    def set(self, key, value):
        target = self.settings
        parts = str(key).split(".")
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value

    def get_temp_dir(self):
        path = self.root / "temp"
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def get_output_dir(self, output_type="live2d"):
        path = self.root / "output" / str(output_type)
        path.mkdir(parents=True, exist_ok=True)
        return str(path)


def _pump(app: QApplication, seconds: float = 0.05) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


def _wait_for(app: QApplication, predicate, timeout: float, description: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    raise RuntimeError(f"Timed out waiting for {description}")


def _run_js(app: QApplication, page, script: str, timeout: float = 10.0):
    values = []
    page.runJavaScript(script, values.append)
    _wait_for(app, lambda: bool(values), timeout, "JavaScript result")
    return values[0]


def _dom_state(app: QApplication, widget):
    if widget.view is None:
        return {}
    result = _run_js(
        app,
        widget.view.page(),
        """JSON.stringify((function () {
            const status = document.getElementById('status');
            const canvas = document.getElementById('canvas');
            const atlas = document.getElementById('atlas');
            const host = window.lpkSpineHost && typeof window.lpkSpineHost.state === 'function'
                ? window.lpkSpineHost.state() : {};
            let canvasPixel = null;
            if (canvas && canvas.width && canvas.height) {
                try {
                    const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
                    if (gl) {
                        const pixel = new Uint8Array(4);
                        gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixel);
                        canvasPixel = Array.from(pixel);
                    }
                } catch (_) {}
            }
            return {
                previewState: String(document.body && document.body.dataset.previewState || ''),
                previewError: String(document.body && document.body.dataset.previewError || ''),
                hostStateAvailable: !!(window.lpkSpineHost && typeof window.lpkSpineHost.state === 'function'),
                statusClass: status ? String(status.className || '') : '',
                status: status ? String(status.textContent || '') : '',
                canvasVisible: !!canvas && !canvas.classList.contains('hidden'),
                canvasWidth: canvas ? Number(canvas.width || 0) : 0,
                canvasHeight: canvas ? Number(canvas.height || 0) : 0,
                canvasPixel: canvasPixel,
                atlasVisible: !!atlas && !atlas.classList.contains('hidden'),
                atlasImages: atlas ? atlas.querySelectorAll('img').length : 0,
                skinOptions: Array.from(document.getElementById('skin')?.options || []).map(function (item) {
                    return {value: String(item.value || ''), text: String(item.textContent || ''), disabled: !!item.disabled};
                }),
                animationOptions: Array.from(document.getElementById('animation')?.options || []).map(function (item) {
                    return {value: String(item.value || ''), text: String(item.textContent || ''), disabled: !!item.disabled};
                }),
                selectedSkin: String(host.selectedSkin !== undefined
                    ? host.selectedSkin : (document.getElementById('skin') || {}).value || ''),
                selectedAnimation: String(host.selectedAnimation !== undefined
                    ? host.selectedAnimation : (document.getElementById('animation') || {}).value || ''),
                paused: host.paused !== undefined
                    ? !!host.paused : !!(document.getElementById('pause') && document.getElementById('pause').checked),
                loop: host.loop !== undefined
                    ? !!host.loop : !!(document.getElementById('loop') && document.getElementById('loop').checked),
                time: Number(host.time !== undefined
                    ? host.time : (document.getElementById('time') || {}).value || 0),
                timeMax: Number(host.timeMax !== undefined
                    ? host.timeMax : (document.getElementById('time') || {}).max || 0)
            };
        })());""",
    )
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return {"status": result}
    return result if isinstance(result, dict) else {}


def _dispatch_control(app: QApplication, widget, script: str):
    """Run one explicit DOM control action and return its JSON result."""

    result = _run_js(
        app,
        widget.view.page(),
        f"JSON.stringify((function () {{ {script} }})());",
    )
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return {"value": result}
    return result if isinstance(result, dict) else {}


def _exercise_runtime_controls(app: QApplication, widget, timeout: float):
    """Exercise the page's skin/animation/pause/time controls through the DOM."""

    state = _dom_state(app, widget)
    if not state or str(state.get("previewState", "")).lower() != "ready":
        raise RuntimeError("Cannot exercise Spine controls before the page is ready")

    controls = {
        "initial": state,
        "skin": {"status": "skipped", "reason": "no skin options"},
        "animation": {"status": "skipped", "reason": "no animation options"},
    }

    skin_options = [item for item in state.get("skinOptions", []) if not item.get("disabled") and item.get("value")]
    if skin_options:
        target = next((item for item in skin_options if item["value"] == "default"), skin_options[0])
        selected = _dispatch_control(
            app,
            widget,
            """
            const select = document.getElementById('skin');
            const target = %s;
            select.value = target;
            select.dispatchEvent(new Event('change', {bubbles: true}));
            return {selectedSkin: String(select.value || '')};
            """ % json.dumps(target["value"]),
        )
        _pump(app, 0.12)
        after_skin = _dom_state(app, widget)
        if str(after_skin.get("previewState", "")).lower() != "ready":
            raise RuntimeError(f"Selecting Spine skin failed: {after_skin}")
        controls["skin"] = {
            "status": "passed",
            "requested": target["value"],
            "selected": selected.get("selectedSkin", ""),
            "state": after_skin,
        }

    animation_options = [
        item for item in state.get("animationOptions", [])
        if not item.get("disabled") and item.get("value")
    ]
    if not animation_options:
        return controls

    target_animation = next(
        (
            item for item in animation_options
            if item["value"].lower() in {"normal", "idle"}
        ),
        animation_options[0],
    )
    selected = _dispatch_control(
        app,
        widget,
        """
        const select = document.getElementById('animation');
        const target = %s;
        select.value = target;
        select.dispatchEvent(new Event('change', {bubbles: true}));
        return {selectedAnimation: String(select.value || '')};
        """ % json.dumps(target_animation["value"]),
    )
    _pump(app, 0.12)
    samples = [_dom_state(app, widget)]
    for _ in range(3):
        _pump(app, 0.12)
        samples.append(_dom_state(app, widget))
    before = samples[0]
    advanced = samples[-1]
    if str(advanced.get("previewState", "")).lower() != "ready":
        raise RuntimeError(f"Animation selection failed: {advanced}")
    duration = float(advanced.get("timeMax") or before.get("timeMax") or 0.0)
    sample_times = [float(item.get("time", 0.0)) for item in samples]
    time_advanced = duration > 0 and (max(sample_times) - min(sample_times)) > 0.001
    if not time_advanced:
        raise RuntimeError(
            f"Spine animation time did not advance ({before.get('time')} -> {advanced.get('time')}, max={duration})"
        )

    _dispatch_control(
        app,
        widget,
        """
        const pause = document.getElementById('pause');
        pause.checked = true;
        pause.dispatchEvent(new Event('change', {bubbles: true}));
        return {paused: !!pause.checked};
        """,
    )
    _pump(app, 0.1)
    paused_before = _dom_state(app, widget)
    _pump(app, 0.3)
    paused_after = _dom_state(app, widget)
    pause_delta = abs(float(paused_after.get("time", 0.0)) - float(paused_before.get("time", 0.0)))
    if not paused_after.get("paused") or pause_delta > max(0.01, duration * 0.01):
        raise RuntimeError(f"Spine pause did not hold time ({paused_before} -> {paused_after})")

    target_time = max(0.0, min(duration * 0.5, max(0.0, duration - 0.001)))
    _dispatch_control(
        app,
        widget,
        """
        const time = document.getElementById('time');
        time.value = %s;
        time.dispatchEvent(new Event('input', {bubbles: true}));
        return {time: Number(time.value || 0)};
        """ % target_time,
    )
    _pump(app, 0.12)
    positioned = _dom_state(app, widget)
    position_delta = abs(float(positioned.get("time", 0.0)) - target_time)
    if position_delta > max(0.03, duration * 0.05):
        raise RuntimeError(f"Spine time seek did not hold ({positioned}, target={target_time})")

    # A non-looping track must remain at its final pose instead of wrapping to
    # the first frame.  Seeking to the end keeps this check fast even for long
    # source animations.
    _dispatch_control(
        app,
        widget,
        """
        const loop = document.getElementById('loop');
        loop.checked = false;
        loop.dispatchEvent(new Event('change', {bubbles: true}));
        const time = document.getElementById('time');
        time.value = Number(time.max || 0);
        time.dispatchEvent(new Event('input', {bubbles: true}));
        return {loop: !!loop.checked, time: Number(time.value || 0)};
        """,
    )
    _pump(app, 0.25)
    stopped = _dom_state(app, widget)
    stopped_time = float(stopped.get("time", 0.0) or 0.0)
    if stopped.get("loop") or duration > 0 and stopped_time < duration * 0.9:
        raise RuntimeError(f"Non-looping Spine animation did not stop at its end pose: {stopped}")

    _dispatch_control(
        app,
        widget,
        """
        const pause = document.getElementById('pause');
        pause.checked = false;
        pause.dispatchEvent(new Event('change', {bubbles: true}));
        return {paused: !!pause.checked};
        """,
    )
    _pump(app, 0.1)
    final_state = _dom_state(app, widget)
    if str(final_state.get("previewState", "")).lower() != "ready":
        raise RuntimeError(f"Spine controls left the page in an error state: {final_state}")
    controls["animation"] = {
        "status": "passed",
        "requested": target_animation["value"],
        "selected": selected.get("selectedAnimation", ""),
        "duration": duration,
        "time_advanced": time_advanced,
        "pause_delta": pause_delta,
        "seek_target": target_time,
        "seek_delta": position_delta,
        "loop_stop": {"status": "passed", "state": stopped},
        "state": final_state,
    }
    return controls


def _native_host_state(app: QApplication, page: PreviewPage):
    """Return the renderer state after a Qt-side control action."""

    if page is None or page.spine_preview is None:
        return {}
    return _dom_state(app, page.spine_preview)


def _embedded_chrome_state(app: QApplication, page: PreviewPage):
    """Read the page chrome and view transform exposed by the real WebEngine."""

    if page is None or page.spine_preview is None or page.spine_preview.view is None:
        return {}
    result = _run_js(
        app,
        page.spine_preview.view.page(),
        """JSON.stringify((function () {
            const canvas = document.getElementById('canvas');
            const toolbar = document.getElementById('toolbar');
            const attachments = document.getElementById('attachments');
            const root = document.documentElement;
            const body = document.body;
            const canvasStyle = canvas ? getComputedStyle(canvas) : null;
            const rootStyle = root ? getComputedStyle(root) : null;
            const bodyStyle = body ? getComputedStyle(body) : null;
            return {
                embeddedHost: !!body && body.classList.contains('lpk-embedded-host'),
                toolbarDisplay: toolbar ? getComputedStyle(toolbar).display : '',
                attachmentsDisplay: attachments ? getComputedStyle(attachments).display : '',
                canvasTransform: canvasStyle ? String(canvasStyle.transform || '') : '',
                canvasOpacity: canvasStyle ? String(canvasStyle.opacity || '') : '',
                rootBackground: rootStyle ? String(rootStyle.backgroundColor || '') : '',
                bodyBackground: bodyStyle ? String(bodyStyle.backgroundColor || '') : ''
            };
        })());""",
    )
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return {"raw": result}
    return result if isinstance(result, dict) else {}


def _exercise_native_controls(app: QApplication, page: PreviewPage, timeout: float):
    """Exercise the actual Qt right sidebar and left display settings.

    The DOM checks above prove that the page controls work.  This companion
    check deliberately drives the production Qt widgets so a disconnected
    signal or stale bridge cannot pass the diagnostic by itself.
    """

    controls = getattr(page, "spine_controls", None)
    if controls is None or page.spine_preview is None:
        raise RuntimeError("PreviewPage has no native Spine controls")
    _wait_for(
        app,
        lambda: bool(controls._ready and controls.animation_combo.count()),
        timeout,
        "native Spine animation controls",
    )

    report = {"status": "passed"}
    state = _native_host_state(app, page)
    if not state.get("hostStateAvailable"):
        raise RuntimeError(f"Embedded Spine host state bridge is unavailable: {state}")
    if str(state.get("previewState", "")).lower() != "ready":
        raise RuntimeError(f"Native controls started before renderer ready: {state}")

    skin_values = [controls.skin_combo.itemText(i) for i in range(controls.skin_combo.count())]
    if skin_values:
        requested_skin = "default" if "default" in skin_values else skin_values[0]
        controls.skin_combo.setCurrentText(requested_skin)
        _pump(app, 0.35)
        after_skin = _native_host_state(app, page)
        if after_skin.get("selectedSkin") != requested_skin:
            raise RuntimeError(f"Native skin selection did not reach the renderer: {after_skin}")
        report["skin"] = {"requested": requested_skin, "state": after_skin}
    else:
        report["skin"] = {"status": "skipped", "reason": "no skin options"}

    animation_values = [
        controls.animation_combo.itemText(i)
        for i in range(controls.animation_combo.count())
        if controls.animation_combo.itemText(i)
    ]
    if not animation_values:
        raise RuntimeError("Native Spine controls exposed no animation options")
    # Prefer the conventional long-running ``normal`` track.  If a source
    # has no such name, probe the available Qt options and retain the one with
    # the longest reported duration so the subsequent time-slider assertion is
    # meaningful instead of accidentally selecting a 1 ms placeholder track.
    requested_animation = next(
        (name for name in animation_values if name.lower() in {"normal", "idle"}),
        animation_values[0],
    )
    controls.animation_combo.setCurrentText(requested_animation)
    _pump(app, 0.35)
    after_animation = _native_host_state(app, page)
    if float(after_animation.get("timeMax") or 0.0) <= 0.001:
        best = (float(after_animation.get("timeMax") or 0.0), requested_animation, after_animation)
        for candidate in animation_values:
            controls.animation_combo.setCurrentText(candidate)
            _pump(app, 0.22)
            candidate_state = _native_host_state(app, page)
            candidate_duration = float(candidate_state.get("timeMax") or 0.0)
            if candidate_duration > best[0]:
                best = (candidate_duration, candidate, candidate_state)
        requested_animation, after_animation = best[1], best[2]
        controls.animation_combo.setCurrentText(requested_animation)
        _pump(app, 0.25)
        after_animation = _native_host_state(app, page)
    if after_animation.get("selectedAnimation") != requested_animation:
        raise RuntimeError(f"Native animation selection did not reach the renderer: {after_animation}")
    report["animation"] = {"requested": requested_animation, "state": after_animation}

    # Click the native pause button and verify the host state, then restore the
    # running state for the remaining checks.
    initial_paused = bool(after_animation.get("paused"))
    controls.play_pause_btn.click()
    _pump(app, 0.35)
    paused_state = _native_host_state(app, page)
    if bool(paused_state.get("paused")) == initial_paused:
        raise RuntimeError(f"Native play/pause click was not forwarded: {paused_state}")
    controls.play_pause_btn.click()
    _pump(app, 0.25)
    resumed_state = _native_host_state(app, page)
    report["play_pause"] = {"paused": paused_state, "resumed": resumed_state}

    initial_loop = bool(resumed_state.get("loop"))
    controls.loop_check.click()
    _pump(app, 0.35)
    loop_state = _native_host_state(app, page)
    if bool(loop_state.get("loop")) == initial_loop:
        raise RuntimeError(f"Native loop click was not forwarded: {loop_state}")
    controls.loop_check.click()
    _pump(app, 0.25)
    restored_loop_state = _native_host_state(app, page)
    report["loop"] = {"toggled": loop_state, "restored": restored_loop_state}

    duration = float(restored_loop_state.get("timeMax") or 0.0)
    if duration <= 0:
        raise RuntimeError(f"Native time slider has no animation duration: {restored_loop_state}")
    target_value = max(1, min(999, int(round(0.37 * 1000))))
    controls.time_slider.setValue(target_value)
    _pump(app, 0.35)
    seek_state = _native_host_state(app, page)
    target_time = duration * target_value / 1000.0
    if abs(float(seek_state.get("time", 0.0)) - target_time) > max(0.08, duration * 0.08):
        raise RuntimeError(f"Native time slider was not forwarded: {seek_state}, target={target_time}")
    report["time"] = {"requested": target_time, "state": seek_state}

    # Reset while paused so a rAF tick cannot make a reset-to-zero assertion
    # flaky.  Restore the previous play state after reading the reset result.
    was_paused = bool(seek_state.get("paused"))
    if not was_paused:
        controls.play_pause_btn.click()
        _pump(app, 0.25)
    controls.reset_btn.click()
    _pump(app, 0.25)
    reset_state = _native_host_state(app, page)
    if reset_state.get("selectedAnimation") != requested_animation:
        raise RuntimeError(f"Native reset changed the selected animation: {reset_state}")
    if float(reset_state.get("time", 0.0) or 0.0) > max(0.15, duration * 0.08):
        raise RuntimeError(f"Native reset did not return to the first pose: {reset_state}")
    if not was_paused:
        controls.play_pause_btn.click()
        _pump(app, 0.25)
    report["reset"] = reset_state

    settings = page.settings_panel
    if settings is None:
        raise RuntimeError("PreviewPage has no display settings panel")
    original_scale = settings.scale_slider.value()
    original_opacity = settings.opacity_slider.value()
    original_transparent = settings.bg_transparent_check.isChecked()
    try:
        settings.scale_slider.setValue(max(25, min(300, original_scale + 25)))
        settings.opacity_slider.setValue(max(10, min(100, original_opacity - 10)))
        settings.bg_transparent_check.setChecked(False)
        _pump(app, 0.35)
        chrome = _embedded_chrome_state(app, page)
        if not chrome.get("embeddedHost"):
            raise RuntimeError(f"Embedded host marker is missing: {chrome}")
        if chrome.get("toolbarDisplay") != "none" or chrome.get("attachmentsDisplay") != "none":
            raise RuntimeError(f"Embedded Spine webpage chrome is still visible: {chrome}")
        if not chrome.get("canvasTransform") or chrome.get("canvasTransform") == "none":
            raise RuntimeError(f"Native scale/offset settings did not reach canvas CSS: {chrome}")
        if chrome.get("canvasOpacity") in {"", "1"}:
            raise RuntimeError(f"Native opacity setting did not reach canvas CSS: {chrome}")
        transparent_values = {"", "rgba(0, 0, 0, 0)", "transparent"}
        if chrome.get("bodyBackground") in transparent_values or chrome.get("rootBackground") in transparent_values:
            raise RuntimeError(f"Native background setting did not reach html/body CSS: {chrome}")
        settings.bg_transparent_check.setChecked(True)
        _pump(app, 0.35)
        transparent_chrome = _embedded_chrome_state(app, page)
        if (
            transparent_chrome.get("bodyBackground") not in transparent_values
            or transparent_chrome.get("rootBackground") not in transparent_values
        ):
            raise RuntimeError(f"Transparent background did not reach html/body CSS: {transparent_chrome}")
        report["view_settings"] = {"opaque": chrome, "transparent": transparent_chrome}
    finally:
        settings.scale_slider.setValue(original_scale)
        settings.opacity_slider.setValue(original_opacity)
        settings.bg_transparent_check.setChecked(original_transparent)
        _pump(app, 0.15)
    return report


def _capture_screenshot(page, path: Path):
    """Capture the complete visible PreviewPage and return metadata."""

    if page is None:
        raise RuntimeError("Cannot capture a Spine screenshot without PreviewPage")
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    pixmap = page.grab()
    if pixmap.isNull() or not pixmap.save(str(path)):
        raise RuntimeError(f"Could not save Spine screenshot: {path}")
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    sample_colors = set()
    sample_count = 0
    for y in range(0, image.height(), max(1, image.height() // 24)):
        for x in range(0, image.width(), max(1, image.width() // 24)):
            color = image.pixelColor(x, y)
            sample_colors.add((color.red(), color.green(), color.blue(), color.alpha()))
            sample_count += 1
    return {
        "path": str(path),
        "width": image.width(),
        "height": image.height(),
        "sample_count": sample_count,
        "unique_sample_colors": len(sample_colors),
    }


def _capture_webview_screenshot(widget, path: Path):
    """Capture only the central WebEngine for low-level renderer debugging."""

    if widget is None or widget.view is None:
        raise RuntimeError("Cannot capture a Spine screenshot without QWebEngineView")
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    pixmap = widget.view.grab()
    if pixmap.isNull() or not pixmap.save(str(path)):
        raise RuntimeError(f"Could not save Spine WebEngine screenshot: {path}")
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    return {"path": str(path), "width": image.width(), "height": image.height()}


def _wait_for_case(
    app,
    page: PreviewPage,
    source: Path,
    runtime: Path | None,
    timeout: float,
    expect_error: bool = False,
):
    settings = page.settings_manager.settings
    settings.setdefault("preview", {})["spine_runtime_dir"] = str(runtime or "")
    page.start_spine_preview_import(str(source))
    loading_text = page.preview_placeholder.text() if page.preview_placeholder else ""
    _wait_for(
        app,
        lambda: bool(page._active_spine_preview_url) or (
            page.preview_placeholder is not None
            and page.preview_placeholder.isVisible()
            and page.preview_placeholder.text() != loading_text
        ),
        timeout,
        f"Spine page or error for {source}",
    )

    # A worker/import error is expected to leave a concrete placeholder.  It
    # must never be reported as a successful central preview.
    if not page._active_spine_preview_url:
        placeholder = page.preview_placeholder.text() if page.preview_placeholder else ""
        result = {
            "source": str(source),
            "mode": "error",
            "stage_visible": False,
            "state": {},
            "placeholder": placeholder,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not expect_error:
            raise RuntimeError(f"Spine preview failed before the WebEngine page opened: {placeholder}")
        return result

    _wait_for(
        app,
        lambda: bool(page.spine_preview and page.spine_preview.isVisible())
        or (
            not page._active_spine_preview_url
            and page.preview_placeholder is not None
            and page.preview_placeholder.isVisible()
            and page.preview_placeholder.text() != loading_text
        ),
        timeout,
        "central Spine stage",
    )

    if not page._active_spine_preview_url:
        placeholder = page.preview_placeholder.text() if page.preview_placeholder else ""
        result = {
            "source": str(source),
            "mode": "error",
            "stage_visible": False,
            "state": {},
            "placeholder": placeholder,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not expect_error:
            raise RuntimeError(f"Spine page failed before becoming visible: {placeholder}")
        return result

    state = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if not page._active_spine_preview_url:
            # The Qt error signal has replaced the loading page with an error
            # placeholder.  Keep that evidence for --expect-error.
            break
        if page.spine_preview and page.spine_preview.isVisible():
            state = _dom_state(app, page.spine_preview)
            if str(state.get("previewState", "")).lower() in {"ready", "error"}:
                break
        time.sleep(0.05)
    else:
        raise RuntimeError(f"Timed out waiting for renderer ready/error state for {source}")

    plan = page._active_spine_plan
    result = {
        "source": str(source),
        "mode": getattr(plan, "mode", "error"),
        "stage_visible": bool(page.spine_preview and page.spine_preview.isVisible()),
        "state": state,
        "placeholder": page.preview_placeholder.text() if page.preview_placeholder else "",
    }
    if result["stage_visible"] and str(state.get("previewState", "")).lower() == "error":
        raise RuntimeError(f"Spine page reported an error: {state.get('status')}")
    if result["stage_visible"] and result["mode"] == "atlas" and not state.get("atlasImages"):
        raise RuntimeError("Atlas page reached ready state without loading a texture image")
    if result["stage_visible"] and result["mode"] == "runtime":
        if not state.get("canvasWidth") or not state.get("canvasHeight"):
            raise RuntimeError("Spine runtime reached ready state without a canvas size")
        if not isinstance(state.get("canvasPixel"), list) or len(state["canvasPixel"]) != 4:
            raise RuntimeError("Spine runtime canvas pixel readback was unavailable")
        animation_values = [
            str(item.get("value") or "")
            for item in state.get("animationOptions", [])
            if isinstance(item, dict) and item.get("value")
        ]
        preferred = next(
            (name for name in animation_values if name.lower() in {"normal", "idle"}),
            "",
        )
        initial_animation = str(state.get("selectedAnimation") or "")
        if preferred and initial_animation != preferred:
            raise RuntimeError(
                "Spine initial animation did not prefer normal/idle "
                f"({initial_animation!r} != {preferred!r})"
            )
        result["initial_animation"] = {
            "selected": initial_animation,
            "preferred": preferred,
            "options": animation_values,
        }
        result["controls"] = _exercise_runtime_controls(app, page.spine_preview, timeout)
        result["native_controls"] = _exercise_native_controls(app, page, timeout)
    else:
        result["controls"] = {"status": "skipped", "reason": "atlas mode"}
        result["native_controls"] = {"status": "skipped", "reason": "atlas mode"}
    if not result["stage_visible"] and not expect_error:
        raise RuntimeError(f"Spine page closed with an error: {result['placeholder']}")
    if expect_error and result["stage_visible"]:
        raise RuntimeError("Expected a Spine error, but central preview became ready")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Spine source directory/file")
    parser.add_argument("--runtime", type=Path, help="Root containing matching spine-ts runtime(s)")
    parser.add_argument("--switch-source", type=Path, help="Second source used to verify switch cleanup")
    parser.add_argument("--switch-runtime", type=Path, help="Runtime root for --switch-source")
    parser.add_argument("--expect-mode", choices=("runtime", "atlas"), help="Assert the selected plan mode")
    parser.add_argument("--expect-error", action="store_true", help="Treat a concrete preparation/page error as the expected result")
    parser.add_argument("--screenshot", type=Path, help="Save a full PreviewPage screenshot of the ready first case")
    parser.add_argument("--webview-screenshot", type=Path, help="Save only the central QWebEngineView screenshot")
    parser.add_argument("--width", type=int, default=1280, help="PreviewPage width for the full-page capture")
    parser.add_argument("--height", type=int, default=800, help="PreviewPage height for the full-page capture")
    parser.add_argument("--report", type=Path, help="Write the structured verification result as JSON")
    parser.add_argument("--timeout", type=float, default=45.0)
    args = parser.parse_args()

    if not WEBENGINE_AVAILABLE:
        print("QtWebEngine is unavailable; run this script with `pixi run python`.", file=sys.stderr)
        return 2
    if not args.source.exists() and not args.expect_error:
        print(f"Source does not exist: {args.source}", file=sys.stderr)
        return 2

    app = QApplication.instance() or QApplication([])
    sandbox_root = Path(tempfile.mkdtemp(prefix="lpk-spine-preview-check-"))
    original_settings_manager = _preview_page_module.SettingsManager
    _preview_page_module.SettingsManager = lambda: _SandboxSettings(sandbox_root)
    page = None
    report = {
        "source": str(args.source),
        "runtime": str(args.runtime) if args.runtime else "",
        "switch_source": str(args.switch_source) if args.switch_source else "",
        "ok": False,
    }
    try:
        page = PreviewPage()
        page.resize(max(640, args.width), max(480, args.height))
        page.show()
        first = _wait_for_case(
            app,
            page,
            args.source,
            args.runtime,
            args.timeout,
            args.expect_error,
        )
        report["first"] = first
        if args.expect_mode and first["mode"] != args.expect_mode:
            raise RuntimeError(f"Expected mode {args.expect_mode}, got {first['mode']}")
        if args.screenshot and first.get("stage_visible"):
            _pump(app, 0.2)
            report["screenshot"] = _capture_screenshot(page, args.screenshot)
        if args.webview_screenshot and first.get("stage_visible"):
            _pump(app, 0.2)
            report["webview_screenshot"] = _capture_webview_screenshot(page.spine_preview, args.webview_screenshot)

        if args.switch_source:
            if not args.switch_source.exists():
                raise RuntimeError(f"Switch source does not exist: {args.switch_source}")
            old_url = page.spine_preview.current_url
            second = _wait_for_case(
                app,
                page,
                args.switch_source,
                args.switch_runtime or args.runtime,
                args.timeout,
            )
            report["switch"] = second
            if page.spine_preview.current_url == old_url:
                raise RuntimeError("Switch did not replace the embedded WebEngine URL")
            if args.screenshot and second.get("stage_visible"):
                _pump(app, 0.2)
                switch_path = args.screenshot.with_name(
                    f"{args.screenshot.stem}_switch{args.screenshot.suffix}"
                )
                report["switch_screenshot"] = _capture_screenshot(page, switch_path)
            if args.webview_screenshot and second.get("stage_visible"):
                switch_path = args.webview_screenshot.with_name(
                    f"{args.webview_screenshot.stem}_switch{args.webview_screenshot.suffix}"
                )
                report["switch_webview_screenshot"] = _capture_webview_screenshot(page.spine_preview, switch_path)
            print(json.dumps({"switch": second["source"], "old_url_replaced": True}, ensure_ascii=False))

        page.close_preview_window()
        _pump(app, 0.3)
        cleanup = {
            "url_after_close": page.spine_preview.current_url if page.spine_preview else "",
            "stage_visible_after_close": bool(page.spine_preview and page.spine_preview.isVisible()),
            "spine_mode_after_close": bool(page._spine_mode),
            "placeholder": page.preview_placeholder.text() if page.preview_placeholder else "",
        }
        print(json.dumps({"cleanup": cleanup}, ensure_ascii=False, indent=2))
        if any((cleanup["url_after_close"], cleanup["stage_visible_after_close"], cleanup["spine_mode_after_close"])):
            raise RuntimeError("Spine WebEngine state remained active after close")
        report["cleanup"] = cleanup
        report["ok"] = True
        return 0
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _preview_page_module.SettingsManager = original_settings_manager
        if page is not None:
            page.close()
            page.deleteLater()
            _pump(app, 0.2)
        shutil.rmtree(sandbox_root, ignore_errors=True)
        if args.report:
            report_path = args.report.resolve()
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )


if __name__ == "__main__":
    raise SystemExit(main())
