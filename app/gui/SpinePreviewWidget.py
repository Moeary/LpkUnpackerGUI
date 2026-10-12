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
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPointF, QTimer, Qt, Signal
from PySide6.QtGui import (
    QBrush, QColor, QGuiApplication, QOpenGLContext, QPainter, QPainterPath, QPalette, QPen, QPolygonF,
    QSurfaceFormat,
)
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
from app.core.spine_pose import parent_of, pick_bone, rotate_bone, translate_bone
from app.gui.view_zoom import ZOOM_STEP as ZOOM_KEY_STEP, zoom_key_action


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
                self._owner._paint_bone_overlay(self)
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
                    self._owner._paint_bone_overlay(self)
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

        # View interaction lives on the owner; the native child window only
        # forwards its input there, falling back to Qt's default handling.
        def wheelEvent(self, event):  # noqa: N802
            if not self._owner._canvas_wheel(event):
                super().wheelEvent(event)

        def mousePressEvent(self, event):  # noqa: N802
            if not self._owner._canvas_press(event):
                super().mousePressEvent(event)

        def mouseMoveEvent(self, event):  # noqa: N802
            if not self._owner._canvas_move(event):
                super().mouseMoveEvent(event)

        def mouseReleaseEvent(self, event):  # noqa: N802
            if not self._owner._canvas_release(event):
                super().mouseReleaseEvent(event)

        def keyPressEvent(self, event):  # noqa: N802
            if not self._owner._canvas_key(event):
                super().keyPressEvent(event)

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
    # Emitted with the new zoom factor (1.0 = fitted) after a view change.
    viewScaleChanged = Signal(float)
    # Emitted with a PickResult (or None) when a click selects a part.
    regionPicked = Signal(object)
    # Bone mode: a bone was clicked, and a drag finished posing a bone with
    # new local values ({"rotation"} or {"x", "y"}).
    bonePicked = Signal(str)
    bonePosed = Signal(str, dict)

    # Bone overlay colours, chosen to read on light and dark stages alike.
    BONE_COLORS = {
        "fk": QColor(236, 236, 236), "ik": QColor(255, 159, 28), "target": QColor(255, 77, 109),
        "constrained": QColor(61, 220, 151), "selected": QColor(0, 183, 255),
    }

    # Same 25%-800% range as Live2D and the preview page's scale slider.
    ZOOM_STEP = ZOOM_KEY_STEP
    MIN_ZOOM = 0.25
    MAX_ZOOM = 8.0

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
            "transparent_bg": True, "bg_color": "#2b2b2b", "antialias": True,
        }
        self._last_tick = time.monotonic()
        self._textures_uploaded = False
        # Ctrl+wheel / Ctrl+= Ctrl+- Ctrl+0 zoom and middle-drag pan everywhere;
        # click picking is editor-only (set_pick_enabled).
        self._view_interaction = True
        self._pick_enabled = False
        self._press_pos = None
        self._pan_last = None
        self._pick_regions: dict[int, list] = {}
        self._pick_page_sizes: dict[int, tuple[int, int]] = {}
        self._pick_alpha: dict[int, Any] = {}
        self._bone_mode = False
        self._bone_tool = "rotate"
        self._bone_filter = "all"
        self._bone_names = False
        self._selected_bone = ""
        self._bone_drag: dict[str, Any] | None = None
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
            self._load_pick_regions(atlas_paths[0])
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
        self._pick_regions, self._pick_page_sizes, self._pick_alpha = {}, {}, {}
        self._bone_drag = None
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

    def show_setup_pose(self) -> None:
        """Show the bare setup pose (no animation) where the bridge supports it."""
        if self._model:
            self._model.show_setup_pose()
        self._refresh_canvas()

    # Bone mode ------------------------------------------------------------
    @property
    def bones_supported(self) -> bool:
        return bool(self._model is not None and getattr(self._model, "supports_bones", False))

    def bone_mode(self) -> bool:
        return self._bone_mode

    def set_bone_mode(self, enabled: bool) -> None:
        """Draw the skeleton, pick bones before parts and pose them by dragging."""
        self._bone_mode = bool(enabled)
        self._bone_drag = None
        self._request_canvas_update()

    def bone_tool(self) -> str:
        return self._bone_tool

    def set_bone_tool(self, tool: str) -> None:
        if tool not in {"rotate", "translate"}:
            raise ValueError("Bone tool must be rotate or translate")
        self._bone_tool = tool

    BONE_FILTERS = ("all", "fk", "ik", "constrained")

    def bone_filter(self) -> str:
        return self._bone_filter

    def set_bone_filter(self, kind: str) -> None:
        """Show (and pick) all bones, or only FK, IK (chains and targets) or constrained ones."""
        if kind not in self.BONE_FILTERS:
            raise ValueError(f"Bone filter must be one of {self.BONE_FILTERS}")
        self._bone_filter = kind
        self._request_canvas_update()

    def bone_names_visible(self) -> bool:
        return self._bone_names

    def set_bone_names_visible(self, visible: bool) -> None:
        self._bone_names = bool(visible)
        self._request_canvas_update()

    @staticmethod
    def bone_kind(bone) -> str:
        if bone.is_ik_target or bone.is_ik:
            return "ik"
        if bone.is_constrained:
            return "constrained"
        return "fk"

    def _bone_shown(self, bone) -> bool:
        return bone.active and (self._bone_filter == "all" or self.bone_kind(bone) == self._bone_filter)

    @property
    def selected_bone(self) -> str:
        return self._selected_bone

    def set_selected_bone(self, name: str | None) -> None:
        name = str(name or "")
        if name != self._selected_bone:
            self._selected_bone = name
            if self._bone_mode:
                self._request_canvas_update()

    def clear_bone_overrides(self) -> None:
        if self._model is not None:
            self._model.clear_bone_overrides()
        self._refresh_canvas()

    def skeleton_to_view(self, x: float, y: float) -> tuple[float, float]:
        """Map skeleton coordinates to a canvas pixel (inverse of view_to_skeleton)."""
        width, height = self._canvas_size()
        kx, ky, cosine, sine, offset_x, offset_y, centre_x, centre_y = self._view_parameters(width, height)
        u, v = x - centre_x, y - centre_y
        ndc_x = kx * (cosine * u - sine * v) + offset_x
        ndc_y = ky * (sine * u + cosine * v) + offset_y
        return (ndc_x + 1.0) * width / 2.0, (1.0 - ndc_y) * height / 2.0

    def bone_at(self, x: float, y: float):
        if not self.bones_supported:
            return None
        return pick_bone([bone for bone in self._model.bones() if self._bone_shown(bone)], (x, y), self.skeleton_to_view)

    def _bone_press(self, event) -> bool:
        if not (self._bone_mode and self.bones_supported and event.button() == Qt.MouseButton.LeftButton):
            return False
        position = event.position()
        bones = self._model.bones()
        bone = pick_bone([item for item in bones if self._bone_shown(item)], (position.x(), position.y()),
                         self.skeleton_to_view)
        if bone is None:
            return False  # an empty spot still picks the part there
        self.set_selected_bone(bone.name)
        self.bonePicked.emit(bone.name)
        translate = (self._bone_tool == "translate" or bone.is_ik_target
                     or bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
        self._bone_drag = {
            "bone": bone, "parent": parent_of(bone, bones), "mode": "translate" if translate else "rotate",
            "press": self.view_to_skeleton(position.x(), position.y()), "view": (position.x(), position.y()),
            "values": None,
        }
        event.accept()
        return True

    def _bone_move(self, event) -> bool:
        drag = self._bone_drag
        if drag is None:
            return False
        position = event.position()
        if drag["values"] is None and abs(position.x() - drag["view"][0]) + abs(position.y() - drag["view"][1]) < 3:
            event.accept()
            return True
        bone, parent = drag["bone"], drag["parent"]
        current = self.view_to_skeleton(position.x(), position.y())
        x, y, rotation = bone.x, bone.y, bone.rotation
        if drag["mode"] == "rotate":
            rotation = rotate_bone(bone, parent, drag["press"], current)
            drag["values"] = {"rotation": rotation}
        else:
            x, y = translate_bone(bone, parent, drag["press"], current)
            drag["values"] = {"x": x, "y": y}
        if self._model is not None and self._model.set_bone_override(bone.index, x, y, rotation, bone.scale_x, bone.scale_y):
            self._request_canvas_update()
        event.accept()
        return True

    def _bone_release(self, event) -> bool:
        drag = self._bone_drag
        if drag is None or event.button() != Qt.MouseButton.LeftButton:
            return False
        self._bone_drag = None
        if drag["values"]:
            self.bonePosed.emit(drag["bone"].name, dict(drag["values"]))
        event.accept()
        return True

    def _paint_bone_overlay(self, canvas) -> None:
        """Draw bones like the Spine editor: FK, IK chain, IK targets, constrained."""
        if not (self._bone_mode and self.bones_supported):
            return
        bones = self._model.bones()
        if not bones:
            return
        GL.glUseProgram(0)
        GL.glBindVertexArray(0)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, 0)
        painter = QPainter(canvas)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            outline = QPen(QColor(0, 0, 0, 170), 1.2)
            by_index = {bone.index: bone for bone in bones}
            to_view = self.skeleton_to_view
            # IK constraints: dashed line from the chain end to its target.
            for ik in self._model.ik_constraints() if self._bone_filter in {"all", "ik"} else ():
                target = by_index.get(ik.target)
                chain = [by_index[index] for index in ik.bones if index in by_index]
                if target is None or not chain or ik.mix <= 0 or not target.active:
                    continue
                painter.setPen(QPen(self.BONE_COLORS["ik"], 1.4, Qt.PenStyle.DashLine))
                painter.drawLine(QPointF(*to_view(*chain[-1].tip)), QPointF(*to_view(*target.origin)))
            selected = None
            shown = []
            for bone in bones:
                if bone.active and bone.name == self._selected_bone:
                    selected = bone  # stays visible even when filtered out
                    continue
                if not self._bone_shown(bone):
                    continue
                shown.append(bone)
                self._draw_bone(painter, bone, outline, self._bone_color(bone))
            if selected is not None:
                self._draw_bone(painter, selected, QPen(QColor(255, 255, 255), 1.6), self.BONE_COLORS["selected"])
            # Vector paths, not drawText: the GL engine's glyph cache does not
            # survive the native renderer's texture state.
            labels = QPainterPath()
            for bone in (shown if self._bone_names else []) + ([selected] if selected is not None else []):
                x, y = to_view(*bone.origin)
                labels.addText(QPointF(x + 9, y - 9), painter.font(), bone.name)
            if not labels.isEmpty():
                painter.strokePath(labels, QPen(QColor(0, 0, 0, 200), 3.0))
                painter.fillPath(labels, QBrush(QColor(255, 255, 255)))
        finally:
            painter.end()

    def _bone_color(self, bone) -> QColor:
        if bone.is_ik_target:
            return self.BONE_COLORS["target"]
        if bone.is_ik:
            return self.BONE_COLORS["ik"]
        if bone.is_constrained:
            return self.BONE_COLORS["constrained"]
        return self.BONE_COLORS["fk"]

    def _draw_bone(self, painter, bone, outline: QPen, color: QColor) -> None:
        origin = QPointF(*self.skeleton_to_view(*bone.origin))
        tip = QPointF(*self.skeleton_to_view(*bone.tip))
        dx, dy = tip.x() - origin.x(), tip.y() - origin.y()
        length = math.hypot(dx, dy)
        if bone.is_ik_target:
            # A target is a handle, drawn as a crosshair ring.
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(color, 2.0))
            painter.drawEllipse(origin, 7.0, 7.0)
            painter.drawLine(QPointF(origin.x() - 11, origin.y()), QPointF(origin.x() + 11, origin.y()))
            painter.drawLine(QPointF(origin.x(), origin.y() - 11), QPointF(origin.x(), origin.y() + 11))
            return
        fill = QColor(color)
        fill.setAlpha(150)
        painter.setPen(outline)
        painter.setBrush(QBrush(fill))
        if length >= 6.0:
            ux, uy = dx / length, dy / length
            width = min(6.0, max(2.5, length * 0.12))
            shoulder = QPointF(origin.x() + dx * 0.18, origin.y() + dy * 0.18)
            painter.drawPolygon(QPolygonF([
                origin,
                QPointF(shoulder.x() - uy * width, shoulder.y() + ux * width),
                tip,
                QPointF(shoulder.x() + uy * width, shoulder.y() - ux * width),
            ]))
        painter.setBrush(QBrush(color))
        painter.drawEllipse(origin, 3.2, 3.2)

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

    def view_settings(self) -> dict:
        """Current view transform, in the units set_view_settings() takes."""
        return {key: float(self._settings.get(key, default)) for key, default in (
            ("model_scale", 1.0), ("model_rotation", 0.0), ("model_offset_x", 0.0), ("model_offset_y", 0.0))}

    # View interaction ---------------------------------------------------
    def set_view_interaction(self, enabled: bool) -> None:
        """Allow Ctrl+wheel / keyboard zoom and middle-button pan (default on)."""
        self._view_interaction = bool(enabled)

    def set_pick_enabled(self, enabled: bool) -> None:
        """Let a left click pick the atlas part under the cursor."""
        self._pick_enabled = bool(enabled)

    def view_scale(self) -> float:
        return float(self._settings.get("model_scale", 1.0))

    def zoom_by(self, factor: float, anchor: tuple[float, float] | None = None) -> None:
        """Zoom around ``anchor`` (canvas pixels; default: the centre).

        The skeleton point under the anchor stays where it is, so zooming
        towards the cursor inspects the detail the user points at.
        """
        scale = self.view_scale()
        new_scale = min(self.MAX_ZOOM, max(self.MIN_ZOOM, scale * float(factor)))
        if math.isclose(new_scale, scale):
            return
        width, height = self._canvas_size()
        ax, ay = anchor if anchor is not None else (width / 2.0, height / 2.0)
        ndc_x, ndc_y = 2.0 * ax / width - 1.0, 1.0 - 2.0 * ay / height
        ratio = new_scale / scale
        offset_x = float(self._settings.get("model_offset_x", 0.0)) * 2.0
        offset_y = -float(self._settings.get("model_offset_y", 0.0)) * 2.0
        offset_x = ndc_x - (ndc_x - offset_x) * ratio
        offset_y = ndc_y - (ndc_y - offset_y) * ratio
        self._settings.update(model_scale=new_scale, model_offset_x=offset_x / 2.0, model_offset_y=-offset_y / 2.0)
        self._view_changed()

    def pan_by(self, dx: float, dy: float) -> None:
        width, height = self._canvas_size()
        self._settings["model_offset_x"] = float(self._settings.get("model_offset_x", 0.0)) + dx / width
        self._settings["model_offset_y"] = float(self._settings.get("model_offset_y", 0.0)) + dy / height
        self._view_changed()

    def reset_view(self) -> None:
        self._settings.update(model_scale=1.0, model_offset_x=0.0, model_offset_y=0.0)
        self._view_changed()

    def _view_changed(self) -> None:
        self._request_canvas_update()
        self.viewScaleChanged.emit(self.view_scale())

    def _canvas_size(self) -> tuple[float, float]:
        surface = self.canvas if self.canvas is not None and hasattr(self.canvas, "width") else self
        return max(1.0, float(surface.width())), max(1.0, float(surface.height()))

    def _view_parameters(self, width: float, height: float) -> tuple[float, ...]:
        """(kx, ky, cos, sin, offset_x, offset_y, centre_x, centre_y) of the view.

        NDC = (kx, ky) * R(rotation) * (skeleton - centre) + offset.  The
        ratios are size-independent, so logical and device pixels agree.
        """
        bx, by, bw, bh = self._base_bounds
        fit = 0.9 * min(width / max(1.0, bw), height / max(1.0, bh))
        model_scale = max(0.01, float(self._settings.get("model_scale", 1.0)))
        rotation = math.radians(float(self._settings.get("model_rotation", 0.0)))
        return (fit * 2.0 / width * model_scale, fit * 2.0 / height * model_scale,
                math.cos(rotation), math.sin(rotation),
                float(self._settings.get("model_offset_x", 0.0)) * 2.0,
                -float(self._settings.get("model_offset_y", 0.0)) * 2.0,
                bx + bw / 2.0, by + bh / 2.0)

    def view_to_skeleton(self, x: float, y: float) -> tuple[float, float]:
        """Map a canvas pixel to skeleton coordinates (inverse of the shader)."""
        width, height = self._canvas_size()
        kx, ky, cosine, sine, offset_x, offset_y, centre_x, centre_y = self._view_parameters(width, height)
        u = (2.0 * x / width - 1.0 - offset_x) / kx
        v = (1.0 - 2.0 * y / height - offset_y) / ky
        return centre_x + cosine * u + sine * v, centre_y - sine * u + cosine * v

    def pick_at(self, x: float, y: float):
        """Return the topmost visible atlas part at a canvas pixel, or None."""
        if self._model is None or not self._pick_regions or np is None:
            return None
        from app.core.spine_pick import pick_region
        result = self._model.render_into()
        vertex_buffer, vertex_count, _ib, _ic, batch_buffer, batch_count = result
        if vertex_buffer is None or vertex_count <= 0 or batch_buffer is None:
            return None
        vertices = np.ctypeslib.as_array(vertex_buffer)[:vertex_count].view(np.float32).reshape(vertex_count, 8)
        batches = [(int(batch_buffer[i].vertex_offset), int(batch_buffer[i].vertex_count), int(batch_buffer[i].page))
                   for i in range(batch_count)]
        return pick_region(vertices, batches, self.view_to_skeleton(x, y), self._pick_page_sizes,
                           self._pick_regions, alpha_at=self._texel_alpha)

    def _load_pick_regions(self, atlas_path) -> None:
        """Index atlas regions by native page so a click can name its part."""
        from app.core.spine_atlas import parse_atlas
        from app.core.spine_pick import PickRegion, regions_by_page
        self._pick_regions, self._pick_page_sizes, self._pick_alpha = {}, {}, {}
        try:
            atlas = parse_atlas(atlas_path)
        except Exception:
            return  # picking is optional; the preview itself still works
        pages = {Path(page.path).name: index for index, page in enumerate(self._model.pages)} if self._model else {}
        for page in atlas.pages:
            index = pages.get(Path(page.name).name, page.index)
            native = self._model.pages[index] if self._model and index < len(self._model.pages) else None
            self._pick_page_sizes[index] = (int(native.width if native and native.width else page.width or 0),
                                            int(native.height if native and native.height else page.height or 0))
        self._pick_regions = regions_by_page(
            PickRegion(region.region_id, region.name,
                       pages.get(Path(atlas.pages[region.page_index].name).name, region.page_index),
                       region.x, region.y, *region.physical_size)
            for region in atlas.regions)

    def _texel_alpha(self, page: int, x: int, y: int) -> int:
        if page not in self._pick_alpha:
            alpha = None
            try:
                with Image.open(self._model.pages[page].path) as image:
                    alpha = image.convert("RGBA").getchannel("A")
            except Exception:
                pass
            self._pick_alpha[page] = alpha
        alpha = self._pick_alpha[page]
        if alpha is None or not (0 <= x < alpha.width and 0 <= y < alpha.height):
            return 255  # unknown alpha: trust the geometry
        return int(alpha.getpixel((x, y)))

    # Canvas input (forwarded by the native child window) ----------------
    def _canvas_wheel(self, event) -> bool:
        if not (self._view_interaction and self._model is not None
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            return False
        steps = event.angleDelta().y() / 120.0
        if steps:
            position = event.position()
            self.zoom_by(self.ZOOM_STEP ** steps, (position.x(), position.y()))
        event.accept()
        return True

    def _canvas_key(self, event) -> bool:
        action = zoom_key_action(event) if self._view_interaction and self._model is not None else None
        if action is None:
            return False
        if action == "reset":
            self.reset_view()
        else:
            self.zoom_by(self.ZOOM_STEP if action == "in" else 1 / self.ZOOM_STEP)
        event.accept()
        return True

    def keyPressEvent(self, event):  # noqa: N802
        if not self._canvas_key(event):
            super().keyPressEvent(event)

    def _canvas_press(self, event) -> bool:
        if self._model is None:
            return False
        if self._bone_press(event):
            return True
        position = event.position()
        if self._view_interaction and event.button() == Qt.MouseButton.MiddleButton:
            self._pan_last = (position.x(), position.y())
            if self.canvas is not None and hasattr(self.canvas, "setCursor"):
                self.canvas.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return True
        if self._pick_enabled and event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = (position.x(), position.y())
            event.accept()
            return True
        return False

    def _canvas_move(self, event) -> bool:
        if self._bone_move(event):
            return True
        if self._pan_last is None:
            return False
        position = event.position()
        self.pan_by(position.x() - self._pan_last[0], position.y() - self._pan_last[1])
        self._pan_last = (position.x(), position.y())
        event.accept()
        return True

    def _canvas_release(self, event) -> bool:
        if self._bone_release(event):
            return True
        position = event.position()
        if self._pan_last is not None and event.button() == Qt.MouseButton.MiddleButton:
            self._pan_last = None
            if self.canvas is not None and hasattr(self.canvas, "unsetCursor"):
                self.canvas.unsetCursor()
            event.accept()
            return True
        if self._press_pos is not None and event.button() == Qt.MouseButton.LeftButton:
            start, self._press_pos = self._press_pos, None
            if abs(position.x() - start[0]) + abs(position.y() - start[1]) < 4:
                try:
                    self.regionPicked.emit(self.pick_at(position.x(), position.y()))
                except Exception as exc:  # a pick must never break the preview
                    self.statusChanged.emit("info", f"Spine part pick failed: {exc}")
            event.accept()
            return True
        return False

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

    def _background_color(self) -> QColor:
        """Return the stage color for the current preview settings.

        Transparent mode is rendered as an opaque stage for the native child
        window on Windows.  Its visible color must therefore follow the
        active Qt palette instead of the color picker default, which may have
        been created under the light theme before the application switches to
        dark mode.
        """

        app = QGuiApplication.instance()
        if bool(self._settings.get("transparent_bg", True)):
            if app is not None:
                return app.palette().color(QPalette.ColorRole.Base)
            return self.palette().color(QPalette.ColorRole.Base)

        value = self._settings.get("bg_color", "#2b2b2b")
        color = value if isinstance(value, QColor) else QColor(str(value))
        if color.isValid():
            return color

        if app is not None:
            return app.palette().color(QPalette.ColorRole.Base)
        return QColor("#2b2b2b")

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
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 4)  # the default other GL users expect
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        self._textures_uploaded = True

    def _paint_native(self, canvas: _SpineOpenGLWindow) -> None:
        color = self._background_color()
        if self._model is None:
            GL.glClearColor(color.redF(), color.greenF(), color.blueF(), 1.0)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT)
            return
        self._upload_native_textures(canvas)
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
        # The bone overlay's QPainter may leave clipping state enabled.
        GL.glDisable(GL.GL_SCISSOR_TEST)
        GL.glDisable(GL.GL_STENCIL_TEST)
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glEnable(GL.GL_MULTISAMPLE) if bool(self._settings.get("antialias", True)) else GL.glDisable(GL.GL_MULTISAMPLE)
        render_result = self._model.render_into()
        vertex_buffer, vertex_count, _index_buffer, index_count, batch_buffer, batch_count = render_result
        if vertex_buffer is None or vertex_count <= 0 or index_count <= 0 or batch_buffer is None or batch_count <= 0:
            self._mark_renderer_ready()
            return
        width = max(1, int(canvas.width() * float(canvas.devicePixelRatio())))
        height = max(1, int(canvas.height() * float(canvas.devicePixelRatio())))
        # Shared with view_to_skeleton(), so a click maps to what is drawn.
        kx, ky, cosine, sine, offset_x, offset_y, center_x, center_y = self._view_parameters(width, height)
        # Keep skeleton-space positions in the VBO and perform fit/scale/
        # rotation/offset in the vertex shader.  ``render_into`` exposes the
        # native POD buffer as a zero-copy float view, so the 32k vertices do
        # not pass through a Python tuple or per-vertex loop each frame.
        vertex_array = np.ctypeslib.as_array(vertex_buffer)[:vertex_count].view(np.float32).reshape(vertex_count, 8)
        transform = np.asarray((
            kx * cosine, ky * sine, 0.0, 0.0,
            -kx * sine, ky * cosine, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            offset_x - kx * (cosine * center_x - sine * center_y),
            offset_y - ky * (sine * center_x + cosine * center_y),
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
