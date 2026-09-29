"""Native Spine preview canvas.

The preview stage is deliberately a Qt/OpenGL surface. Official spine-cpp
does the versioned skeleton parsing, animation evaluation, mesh generation,
and clipping; this widget uploads the resulting atlas pages and draws the
native batches directly. No browser, HTML, or local HTTP server is involved.
"""

from __future__ import annotations

import ctypes
import math
import os
import time
from typing import Any

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QColor, QOpenGLContext, QSurfaceFormat
from PySide6.QtWidgets import QLabel, QFrame, QSizePolicy, QVBoxLayout, QWidget

try:
    from PySide6.QtOpenGL import QOpenGLWindow
    import OpenGL.GL as GL
    import numpy as np
    from PIL import Image

    OPENGL_AVAILABLE = True
except (ImportError, OSError):  # keep non-GUI imports and diagnostics usable
    QOpenGLWindow = None  # type: ignore[assignment,misc]
    GL = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment,misc]
    OPENGL_AVAILABLE = False

from app.core.spine_native import SpineNativeError, SpineNativeModel, find_native_library


def _gl_id(value: Any) -> int:
    if value is None:
        return 0
    try:
        if hasattr(value, "reshape"):
            value = value.reshape(-1)[0]
    except Exception:
        pass
    return int(value)


if OPENGL_AVAILABLE:

    class _SpineOpenGLWindow(QOpenGLWindow):
        """The native surface embedded by QWidget.createWindowContainer."""

        def __init__(self, owner: "SpinePreviewWidget"):
            fmt = QSurfaceFormat()
            fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
            fmt.setDepthBufferSize(24)
            fmt.setStencilBufferSize(8)
            fmt.setSamples(4)
            # QOpenGLWindow's direct swap path is required for
            # ``grabFramebuffer()`` on the NVIDIA/Windows driver used by the
            # application.  The blit-backed update mode leaves that readback
            # buffer black even though the native child is visible.
            super().__init__(QOpenGLWindow.UpdateBehavior.NoPartialUpdate)
            self.setFormat(fmt)
            self._owner = owner
            self._program = 0
            self._vao = 0
            self._vbo = 0
            self._ebo = 0
            self._textures: dict[int, int] = {}
            self._initialized = False
            self._vbo_capacity_bytes = 0
            self._transform_location = -1
            self._opacity_location = -1
            self._texture_location = -1
            self._pma_location = -1

        def initializeGL(self):  # noqa: N802
            try:
                vertex = """#version 330 core
                    layout(location = 0) in vec2 a_position;
                    layout(location = 1) in vec2 a_uv;
                    layout(location = 2) in vec4 a_color;
                    out vec2 v_uv;
                    out vec4 v_color;
                    uniform float opacity;
                    uniform mat4 model_transform;
                    void main() { gl_Position = model_transform * vec4(a_position, 0.0, 1.0); v_uv = a_uv; v_color = a_color * vec4(1.0, 1.0, 1.0, opacity); }
                """
                fragment = """#version 330 core
                    in vec2 v_uv;
                    in vec4 v_color;
                    uniform sampler2D page_texture;
                    uniform float premultiplied;
                    out vec4 frag_color;
                    void main() {
                        vec4 color = texture(page_texture, v_uv) * v_color;
                        // A PMA atlas already contains texture-alpha in RGB,
                        // but the slot/global tint alpha still has to be
                        // applied to RGB before ONE-based blending.
                        if (premultiplied > 0.5) color.rgb *= v_color.a;
                        frag_color = color;
                    }
                """
                self._program = _gl_id(GL.glCreateProgram())
                vs = _gl_id(GL.glCreateShader(GL.GL_VERTEX_SHADER))
                fs = _gl_id(GL.glCreateShader(GL.GL_FRAGMENT_SHADER))
                GL.glShaderSource(vs, vertex)
                GL.glShaderSource(fs, fragment)
                GL.glCompileShader(vs)
                GL.glCompileShader(fs)
                if not GL.glGetShaderiv(vs, GL.GL_COMPILE_STATUS):
                    raise RuntimeError(GL.glGetShaderInfoLog(vs).decode("utf-8", "replace"))
                if not GL.glGetShaderiv(fs, GL.GL_COMPILE_STATUS):
                    raise RuntimeError(GL.glGetShaderInfoLog(fs).decode("utf-8", "replace"))
                GL.glAttachShader(self._program, vs)
                GL.glAttachShader(self._program, fs)
                GL.glLinkProgram(self._program)
                if not GL.glGetProgramiv(self._program, GL.GL_LINK_STATUS):
                    raise RuntimeError(GL.glGetProgramInfoLog(self._program).decode("utf-8", "replace"))
                GL.glDeleteShader(vs)
                GL.glDeleteShader(fs)
                self._vbo = _gl_id(GL.glGenBuffers(1))
                self._ebo = _gl_id(GL.glGenBuffers(1))
                self._vao = _gl_id(GL.glGenVertexArrays(1))
                # The bridge's POD vertex is always eight contiguous float32
                # values.  Record the attribute layout once in the VAO; each
                # frame only updates the VBO contents.
                GL.glBindVertexArray(self._vao)
                GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._vbo)
                stride = 8 * ctypes.sizeof(ctypes.c_float)
                for location, count, offset in ((0, 2, 0), (1, 2, 2), (2, 4, 4)):
                    GL.glEnableVertexAttribArray(location)
                    GL.glVertexAttribPointer(
                        location, count, GL.GL_FLOAT, False, stride,
                        ctypes.c_void_p(offset * ctypes.sizeof(ctypes.c_float)),
                    )
                GL.glBindVertexArray(0)
                GL.glBindBuffer(GL.GL_ARRAY_BUFFER, 0)
                self._transform_location = GL.glGetUniformLocation(self._program, "model_transform")
                self._opacity_location = GL.glGetUniformLocation(self._program, "opacity")
                self._texture_location = GL.glGetUniformLocation(self._program, "page_texture")
                self._pma_location = GL.glGetUniformLocation(self._program, "premultiplied")
                self._initialized = True
                self._owner._upload_native_textures(self)
                self._owner._native_gl_error = ""
            except Exception as exc:
                self._owner._native_gl_error = str(exc)
                self._owner._report_error(f"Spine OpenGL initialization failed: {exc}")

        def paintGL(self):  # noqa: N802
            if not self._initialized:
                return
            try:
                dpr = float(self.devicePixelRatio())
                GL.glViewport(0, 0, max(1, int(self.width() * dpr)), max(1, int(self.height() * dpr)))
                self._owner._paint_native(self)
            except Exception as exc:
                self._owner._native_gl_error = str(exc)
                self._owner._report_error(f"Spine OpenGL rendering failed: {exc}")

        def grabFramebuffer(self):  # noqa: N802
            """Read a frame after synchronously drawing the current model.

            QOpenGLWindow's update request is asynchronous.  A caller can
            therefore observe the same swap buffer twice immediately after a
            timer tick even though the native model has advanced.  This is
            especially visible on a maximized, HiDPI child window.  Render
            once into the current surface and fence the GPU before delegating
            the actual readback to Qt's implementation.
            """
            if self._initialized and GL is not None and self.isExposed():
                previous_context = QOpenGLContext.currentContext()
                previous_surface = previous_context.surface() if previous_context is not None else None
                try:
                    self.makeCurrent()
                    dpr = float(self.devicePixelRatio())
                    GL.glViewport(0, 0, max(1, int(self.width() * dpr)), max(1, int(self.height() * dpr)))
                    self._owner._paint_native(self)
                    GL.glFinish()
                except Exception as exc:
                    self._owner._native_gl_error = str(exc)
                    self._owner._report_error(f"Spine OpenGL framebuffer capture failed: {exc}")
                finally:
                    try:
                        self.doneCurrent()
                    except Exception:
                        pass
                    if previous_context is not None and previous_surface is not None:
                        try:
                            previous_context.makeCurrent(previous_surface)
                        except Exception:
                            pass
            return super().grabFramebuffer()

        def resizeGL(self, width: int, height: int):  # noqa: N802
            # ``paintGL`` owns the viewport.  Qt can deliver a late resize
            # callback while a QOpenGLWindow is being closed, when no context
            # is current; issuing glViewport here then produces
            # GL_INVALID_OPERATION and, on some drivers, an access violation
            # during application shutdown.
            return None

        def release(self):
            if not self._initialized or GL is None:
                return
            previous_context = QOpenGLContext.currentContext()
            previous_surface = previous_context.surface() if previous_context is not None else None
            try:
                self.makeCurrent()
                for texture in self._textures.values():
                    GL.glDeleteTextures([texture])
                self._textures.clear()
                if self._vbo:
                    GL.glDeleteBuffers(1, [self._vbo])
                    self._vbo = 0
                if self._ebo:
                    GL.glDeleteBuffers(1, [self._ebo])
                    self._ebo = 0
                if self._vao:
                    GL.glDeleteVertexArrays(1, [self._vao])
                    self._vao = 0
                if self._program:
                    GL.glDeleteProgram(self._program)
                    self._program = 0
            except Exception:
                pass
            try:
                self.doneCurrent()
            except Exception:
                pass
            if previous_context is not None and previous_surface is not None and previous_context is not self.context():
                try:
                    previous_context.makeCurrent(previous_surface)
                except Exception:
                    pass
            self._initialized = False
            self._vbo_capacity_bytes = 0
            self._transform_location = -1
            self._opacity_location = -1
            self._texture_location = -1
            self._pma_location = -1

        def clear_textures(self):
            """Drop atlas textures while keeping the reusable GL program alive."""
            if not self._initialized or GL is None:
                self._textures.clear()
                return
            previous_context = QOpenGLContext.currentContext()
            previous_surface = previous_context.surface() if previous_context is not None else None
            try:
                self.makeCurrent()
                for texture in self._textures.values():
                    GL.glDeleteTextures([texture])
                self._textures.clear()
            except Exception:
                self._textures.clear()
            finally:
                try:
                    self.doneCurrent()
                except Exception:
                    pass
                if previous_context is not None and previous_surface is not None and previous_context is not self.context():
                    try:
                        previous_context.makeCurrent(previous_surface)
                    except Exception:
                        pass


else:

    class _SpineOpenGLWindow:  # type: ignore[no-redef]
        def __init__(self, owner: "SpinePreviewWidget"):
            self._owner = owner

        def update(self):
            return None

        def release(self):
            return None


class SpinePreviewWidget(QFrame):
    """Qt-native Spine renderer with the historical preview control API."""

    previewStarted = Signal(str)
    documentLoaded = Signal(str)
    previewReady = Signal(str)
    previewFailed = Signal(str)
    statusChanged = Signal(str, str)
    stateChanged = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("spinePreviewWidget")
        self._model: SpineNativeModel | None = None
        self._plan = None
        self._url = ""
        self._last_state: dict[str, Any] = {}
        self._reported_error = False
        self._native_gl_error = ""
        self._renderer_ready = False
        self._ready_emitted = False
        self._loading_deadline = 0.0
        self._base_bounds = (0.0, 0.0, 1.0, 1.0)
        self._settings: dict[str, Any] = {
            "opacity": 1.0, "model_rotation": 0.0, "model_scale": 1.0,
            "model_offset_x": 0.0, "model_offset_y": 0.0,
            "transparent_bg": True, "bg_color": "#20242b", "antialias": True,
        }
        self._last_tick = time.monotonic()
        self._textures_uploaded = False
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(16)
        self._status_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._status_timer.timeout.connect(self._tick)

        self.canvas = None
        self.view = None  # compatibility alias; no QWebEngineView is used
        self._fallback_label = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._native_surface_enabled = OPENGL_AVAILABLE and os.environ.get("LPK_DISABLE_NATIVE_PREVIEW", "0") != "1"
        if self._native_surface_enabled:
            self.canvas = _SpineOpenGLWindow(self)
            self.view = self.canvas
            self._container = QWidget.createWindowContainer(self.canvas, self)
            self._container.setObjectName("spineNativeCanvasContainer")
            self._container.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self._container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            layout.addWidget(self._container, 1)
        else:
            reason = "disabled by LPK_DISABLE_NATIVE_PREVIEW" if OPENGL_AVAILABLE else "PySide6 OpenGL/Pillow is unavailable"
            self._fallback_label = QLabel(f"Native Spine preview {reason}.", self)
            self._fallback_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._fallback_label.setWordWrap(True)
            layout.addWidget(self._fallback_label, 1)

    @property
    def is_available(self) -> bool:
        return bool(self._native_surface_enabled and self.canvas is not None)

    @property
    def current_url(self) -> str:
        return self._url

    @property
    def last_state(self) -> dict:
        return dict(self._last_state)

    @property
    def model(self) -> SpineNativeModel | None:
        return self._model

    def open_plan(self, plan: Any) -> None:
        """Load one prepared SpinePreviewPlan in the native surface."""
        self.clear_preview(keep_visible=True)
        self._plan = plan
        asset = getattr(plan, "asset", None)
        skeleton = getattr(asset, "skeleton_path", None)
        atlas_paths = tuple(getattr(asset, "atlas_paths", ()) or ())
        if skeleton is None or not atlas_paths:
            self._report_error("Spine native preview needs a skeleton and atlas.")
            return
        family = getattr(asset, "family", None) or getattr(getattr(plan, "runtime", None), "family", None)
        runtime_root = getattr(getattr(plan, "runtime", None), "root_dir", None)
        library = find_native_library(runtime_root, family)
        if library is None and runtime_root is None:
            library = find_native_library(None, family)
        if library is None:
            self._report_error(f"No official Spine native bridge is installed for family {family or 'unknown'}.")
            return
        target = str(skeleton.resolve() if hasattr(skeleton, "resolve") else skeleton)
        self._url = target
        self._reported_error = False
        self._renderer_ready = False
        self._ready_emitted = False
        self._loading_deadline = time.monotonic() + 15.0
        self._last_tick = time.monotonic()
        self.previewStarted.emit(target)
        try:
            self._model = SpineNativeModel(library, skeleton, atlas_paths[0], getattr(asset, "skeleton_format", None))
            self._base_bounds = self._model.bounds()
            self._textures_uploaded = False
            self._last_state = self._make_state()
            self.documentLoaded.emit(target)
            self.show()
            self._request_canvas_update()
            self._status_timer.start()
            self._emit_state()
        except (SpineNativeError, OSError, ValueError) as exc:
            self._model = None
            self._report_error(str(exc))

    def open_url(self, url: str) -> None:
        """Compatibility shim; native mode accepts a prepared plan only."""
        self._report_error("Native Spine preview requires a SpinePreviewPlan, not a web URL.")

    def clear_preview(self, keep_visible: bool = False) -> None:
        self._status_timer.stop()
        if self.canvas is not None and hasattr(self.canvas, "clear_textures"):
            self.canvas.clear_textures()
        if self._model is not None:
            self._model.close()
        self._model = None
        self._plan = None
        self._url = ""
        self._last_state = {}
        self._textures_uploaded = False
        self._renderer_ready = False
        self._ready_emitted = False
        self._loading_deadline = 0.0
        if not keep_visible:
            self.hide()

    def shutdown(self) -> None:
        self.clear_preview()
        if self.canvas is not None:
            if hasattr(self.canvas, "release"):
                self.canvas.release()
            self.canvas.close()

    def set_skin(self, name: str) -> None:
        if self._model and not self._model.set_skin(str(name or "")):
            self._report_error(f"Spine skin is unavailable: {name}")
        self._refresh_canvas()

    def set_animation(self, name: str, loop: bool | None = None) -> None:
        if self._model and not self._model.set_animation(str(name or ""), loop):
            self._report_error(f"Spine animation is unavailable: {name}")
        self._refresh_canvas()

    def set_paused(self, paused: bool) -> None:
        if self._model:
            self._model.set_paused(bool(paused))
        self._emit_state()

    def set_loop(self, loop: bool) -> None:
        if self._model:
            self._model.set_loop(bool(loop))
        self._emit_state()

    def set_time(self, value: float) -> None:
        if self._model:
            self._model.set_time(float(value))
        self._refresh_canvas()

    def reset_pose(self) -> None:
        if self._model:
            self._model.reset_pose()
        self._refresh_canvas()

    def export_pose_psd(self) -> None:
        self.statusChanged.emit("info", "Native Spine pose export is not available in this canvas.")

    def set_view_settings(self, settings: dict) -> None:
        if not isinstance(settings, dict):
            return
        self._settings.update({
            key: settings[key] for key in (
                "opacity", "model_rotation", "model_scale", "model_offset_x", "model_offset_y",
                "transparent_bg", "bg_color", "antialias",
            ) if key in settings
        })
        self._refresh_canvas()

    def _refresh_canvas(self) -> None:
        self._request_canvas_update()
        self._emit_state()

    def _request_canvas_update(self) -> None:
        if self.canvas is None:
            return
        request_update = getattr(self.canvas, "requestUpdate", None)
        if callable(request_update):
            request_update()
        else:
            self.canvas.update()

    def _report_error(self, message: str) -> None:
        text = str(message or "Spine native preview failed.")
        if self._reported_error and self._last_state.get("previewError") == text:
            return
        self._reported_error = True
        self._last_state = dict(self._last_state)
        self._last_state.update({"previewState": "error", "previewError": text, "runtimeLoaded": False, "skeletonLoaded": False})
        self._status_timer.stop()
        self.statusChanged.emit("error", text)
        self.stateChanged.emit(dict(self._last_state))
        self.previewFailed.emit(text)

    def _make_state(self) -> dict[str, Any]:
        model = self._model
        if model is None:
            return {"previewState": "loading", "runtimeLoaded": False, "skeletonLoaded": False}
        return {
            "previewState": "ready" if self._renderer_ready else "loading", "previewError": "", "mode": "native",
            "runtimeLoaded": True, "skeletonLoaded": True, "nativeVersion": model.version,
            "skinOptions": [{"value": name, "text": name, "disabled": False} for name in model.skins],
            "animationOptions": [{"value": name, "text": name, "disabled": False} for name, _ in model.animations],
            "selectedSkin": model.selected_skin, "selectedAnimation": model.selected_animation,
            "paused": bool(model.paused), "loop": bool(model.loop),
            "time": model.time, "timeMax": model.duration,
            "exportSupported": False,
            "nativeLimitations": [
                "Spine two-color (dark tint) slots use their primary tint in the native canvas.",
                "Pose PSD export is unavailable in native mode.",
            ],
        }

    def _emit_state(self) -> None:
        if self._model is not None:
            self._last_state = self._make_state()
        self.stateChanged.emit(dict(self._last_state))
        state = self._last_state
        self.statusChanged.emit(str(state.get("previewState", "")), str(state.get("previewError", "")))

    def _tick(self) -> None:
        if self._model is None:
            self._status_timer.stop()
            return
        now = time.monotonic()
        # Keep the real elapsed interval.  A busy window may miss timer ticks,
        # but the animation clock must not silently lose that elapsed time.
        delta = max(0.0, now - self._last_tick)
        self._last_tick = now
        if not self._model.paused:
            self._model.update(delta)
            self._request_canvas_update()
            self._emit_state()
        if not self._renderer_ready and self._loading_deadline and now > self._loading_deadline:
            self._report_error("Spine native canvas did not render its first frame.")
            return

    def _mark_renderer_ready(self) -> None:
        if self._renderer_ready:
            return
        self._renderer_ready = True
        self._loading_deadline = 0.0
        self._last_state = self._make_state()
        if not self._ready_emitted:
            self._ready_emitted = True
            self.previewReady.emit(self._url)
        self._emit_state()

    def _upload_native_textures(self, canvas: _SpineOpenGLWindow) -> None:
        if self._model is None or self._textures_uploaded:
            return
        for index, page in enumerate(self._model.pages):
            if not page.path.is_file():
                raise SpineNativeError(f"Spine atlas texture is missing: {page.path}")
            # PIL exposes rows from the image's top edge while OpenGL's
            # texture origin is the lower-left.  spine-cpp flips atlas V
            # coordinates for the same OpenGL convention, so the pixel rows
            # must be flipped exactly once at upload time as well.
            image = Image.open(page.path).convert("RGBA").transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            raw = image.tobytes("raw", "RGBA")
            texture = _gl_id(GL.glGenTextures(1))
            GL.glBindTexture(GL.GL_TEXTURE_2D, texture)
            GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA, image.width, image.height, 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, raw)
            canvas._textures[index] = texture
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        self._textures_uploaded = True

    def _paint_native(self, canvas: _SpineOpenGLWindow) -> None:
        if self._model is None:
            color = QColor(self._settings.get("bg_color", "#20242b"))
            GL.glClearColor(color.redF(), color.greenF(), color.blueF(), 1.0)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT)
            return
        self._upload_native_textures(canvas)
        color = QColor(self._settings.get("bg_color", "#20242b"))
        transparent = bool(self._settings.get("transparent_bg", True))
        # A QOpenGLWindow inside QWidget.createWindowContainer is a native
        # child window.  Windows cannot alpha-composite that child into the
        # QWidget hierarchy, so an alpha-zero clear is presented as a large
        # black rectangle.  Keep the on-screen RGB stage in the configured
        # theme color for both modes; an off-screen exporter can still bind a
        # transparent target explicitly without changing the native preview.
        if transparent and not canvas.isExposed():
            # A hidden/off-screen surface can retain a real transparent
            # framebuffer for callers that capture it explicitly.
            GL.glClearColor(0.0, 0.0, 0.0, 0.0)
        else:
            # Native child windows cannot alpha-composite into the QWidget
            # hierarchy, so exposed previews always get an opaque RGB stage.
            GL.glClearColor(color.redF(), color.greenF(), color.blueF(), 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        GL.glEnable(GL.GL_MULTISAMPLE) if bool(self._settings.get("antialias", True)) else GL.glDisable(GL.GL_MULTISAMPLE)
        render_result = self._model.render_into()
        vertex_buffer, vertex_count, _index_buffer, index_count, batch_buffer, batch_count = render_result
        if vertex_buffer is None or vertex_count <= 0 or index_count <= 0 or batch_buffer is None or batch_count <= 0:
            self._mark_renderer_ready()
            return
        width = max(1, int(canvas.width() * float(canvas.devicePixelRatio())))
        height = max(1, int(canvas.height() * float(canvas.devicePixelRatio())))
        bx, by, bw, bh = self._base_bounds
        center_x, center_y = bx + bw / 2.0, by + bh / 2.0
        fit = 0.9 * min(width / max(1.0, bw), height / max(1.0, bh))
        model_scale = max(0.01, float(self._settings.get("model_scale", 1.0)))
        rotation = math.radians(float(self._settings.get("model_rotation", 0.0)))
        cosine, sine = math.cos(rotation), math.sin(rotation)
        offset_x = float(self._settings.get("model_offset_x", 0.0)) * 2.0
        offset_y = -float(self._settings.get("model_offset_y", 0.0)) * 2.0
        # Keep skeleton-space positions in the VBO and perform fit/scale/
        # rotation/offset in the vertex shader.  ``render_into`` exposes the
        # native POD buffer as a zero-copy float view, so the 32k vertices do
        # not pass through a Python tuple or per-vertex loop each frame.
        vertex_array = np.ctypeslib.as_array(vertex_buffer)[:vertex_count].view(np.float32).reshape(vertex_count, 8)
        transform = np.asarray((
            fit * 2.0 / width * model_scale * cosine,
            fit * 2.0 / height * model_scale * sine,
            0.0, 0.0,
            -fit * 2.0 / width * model_scale * sine,
            fit * 2.0 / height * model_scale * cosine,
            0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            offset_x - fit * 2.0 / width * model_scale * (cosine * center_x - sine * center_y),
            offset_y - fit * 2.0 / height * model_scale * (sine * center_x + cosine * center_y),
            0.0, 1.0,
        ), dtype=np.float32)
        # The bridge expands each triangle index into a contiguous vertex so
        # clipping remains a plain batch operation.  DrawArrays avoids relying
        # on an index-pointer ABI across desktop OpenGL drivers.
        GL.glUseProgram(canvas._program)
        if canvas._vao:
            GL.glBindVertexArray(canvas._vao)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, canvas._vbo)
        if vertex_array.nbytes > canvas._vbo_capacity_bytes:
            GL.glBufferData(GL.GL_ARRAY_BUFFER, vertex_array.nbytes, None, GL.GL_DYNAMIC_DRAW)
            canvas._vbo_capacity_bytes = vertex_array.nbytes
        GL.glBufferSubData(GL.GL_ARRAY_BUFFER, 0, vertex_array.nbytes, vertex_array)
        GL.glUniformMatrix4fv(canvas._transform_location, 1, False, transform)
        GL.glUniform1f(canvas._opacity_location, max(0.0, min(1.0, float(self._settings.get("opacity", 1.0)))))
        GL.glUniform1i(canvas._texture_location, 0)
        GL.glEnable(GL.GL_BLEND)
        GL.glBlendEquation(GL.GL_FUNC_ADD)
        for index in range(batch_count):
            batch = batch_buffer[index]
            texture = canvas._textures.get(int(batch.page))
            if not texture:
                continue
            pma_page = int(batch.page) < len(self._model.pages) and self._model.pages[int(batch.page)].pma
            GL.glUniform1f(canvas._pma_location, 1.0 if pma_page else 0.0)
            if batch.blend == 1:
                if pma_page:
                    GL.glBlendFunc(GL.GL_ONE, GL.GL_ONE)
                else:
                    GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE)
            elif batch.blend == 2:
                GL.glBlendFunc(GL.GL_DST_COLOR, GL.GL_ONE_MINUS_SRC_ALPHA)
            elif batch.blend == 3:
                GL.glBlendFunc(GL.GL_ONE, GL.GL_ONE_MINUS_SRC_COLOR)
            elif batch.blend == 0 and int(batch.page) < len(self._model.pages) and self._model.pages[int(batch.page)].pma:
                GL.glBlendFuncSeparate(GL.GL_ONE, GL.GL_ONE_MINUS_SRC_ALPHA, GL.GL_ONE, GL.GL_ONE_MINUS_SRC_ALPHA)
            else:
                GL.glBlendFuncSeparate(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA, GL.GL_ONE, GL.GL_ONE_MINUS_SRC_ALPHA)
            GL.glActiveTexture(GL.GL_TEXTURE0)
            GL.glBindTexture(GL.GL_TEXTURE_2D, texture)
            GL.glDrawArrays(GL.GL_TRIANGLES, int(batch.vertex_offset), int(batch.vertex_count))
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        GL.glDisable(GL.GL_BLEND)
        if canvas._vao:
            GL.glBindVertexArray(0)
        gl_error = GL.glGetError()
        self._native_gl_error = "" if not gl_error else f"OpenGL error {gl_error}"
        self._mark_renderer_ready()


__all__ = ["OPENGL_AVAILABLE", "SpinePreviewWidget"]
