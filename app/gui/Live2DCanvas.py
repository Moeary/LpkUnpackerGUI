import math
import json
import time
import weakref
from pathlib import Path
import numpy as np
from typing import Optional, List, Dict, Any

from PySide6.QtOpenGL import QOpenGLWindow
from PySide6.QtCore import QLineF, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPalette, QPen
import OpenGL.GL as GL
from abc import abstractmethod

from app.core.live2d_preview_native import load_preview_runtime, preview_native_status
live2d = load_preview_runtime()
from live2d.utils.canvas import Canvas

from app.core.model.motions import evaluate_motion_parameters, load_live2d_motions
from app.gui.live2d_content_fit import alpha_content_bounds, content_fit_transform
from app.gui.live2d_selection import ModelCoordinates, PreviewTransform, SelectionScene

live2d.init()


def _gl_object_id(value) -> int:
    """Normalize PyOpenGL scalar/array handles to a plain GLuint-compatible int."""
    if value is None:
        return 0
    try:
        array = np.asarray(value).reshape(-1)
        if array.size:
            return int(array[0])
    except Exception:
        pass
    return int(value)


def compile_shader(shader_src, shader_type):
    shader = GL.glCreateShader(shader_type)
    GL.glShaderSource(shader, shader_src)
    GL.glCompileShader(shader)
    status = GL.glGetShaderiv(shader, GL.GL_COMPILE_STATUS)
    if not status:
        msg = GL.glGetShaderInfoLog(shader)
        raise RuntimeError(msg)

    return shader


def create_program(vs, fs):
    vertex_shader = compile_shader(vs, GL.GL_VERTEX_SHADER)
    frag_shader = compile_shader(fs, GL.GL_FRAGMENT_SHADER)
    program = GL.glCreateProgram()
    GL.glAttachShader(program, vertex_shader)
    GL.glAttachShader(program, frag_shader)
    GL.glLinkProgram(program)
    status = GL.glGetProgramiv(program, GL.GL_LINK_STATUS)
    if not status:
        msg = GL.glGetProgramInfoLog(program)
        raise RuntimeError(msg)

    return program


def create_vao(v_pos, uv_coord):
    """创建 VAO/VBO"""

    vao = _gl_object_id(GL.glGenVertexArrays(1))
    vbo = _gl_object_id(GL.glGenBuffers(1))
    uvbo = _gl_object_id(GL.glGenBuffers(1))

    GL.glBindVertexArray(vao)

    # 顶点坐标缓冲
    GL.glBindBuffer(GL.GL_ARRAY_BUFFER, vbo)
    GL.glBufferData(
        GL.GL_ARRAY_BUFFER,
        v_pos.nbytes,
        v_pos,
        GL.GL_DYNAMIC_DRAW
    )
    GL.glVertexAttribPointer(0, 2, GL.GL_FLOAT, False, 0, None)
    GL.glEnableVertexAttribArray(0)

    # UV 坐标缓冲
    GL.glBindBuffer(GL.GL_ARRAY_BUFFER, uvbo)
    GL.glBufferData(
        GL.GL_ARRAY_BUFFER,
        uv_coord.nbytes,
        uv_coord,
        GL.GL_DYNAMIC_DRAW
    )
    GL.glVertexAttribPointer(1, 2, GL.GL_FLOAT, False, 0, None)
    GL.glEnableVertexAttribArray(1)

    GL.glBindBuffer(GL.GL_ARRAY_BUFFER, 0)
    GL.glBindVertexArray(0)
    return vao


def create_canvas_framebuffer(width, height):
    old_fbo = _gl_object_id(GL.glGetIntegerv(GL.GL_FRAMEBUFFER_BINDING))
    fbo = _gl_object_id(GL.glGenFramebuffers(1))

    GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)

    texture = _gl_object_id(GL.glGenTextures(1))
    GL.glBindTexture(GL.GL_TEXTURE_2D, texture)
    GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA,
                    width, height,
                    0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, None)
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
    GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_TEXTURE_2D, texture, 0)

    GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, old_fbo)
    return fbo, texture


class ADPOpenGLCanvas(QOpenGLWindow):
    fitModeChanged = Signal(bool)
    contentFitApplied = Signal(dict)

    def __init__(self):
        super().__init__()
        self.__canvas_opacity = 1.0
        self.__rotation_angle = 0.0
        self.__model_scale = 1.0
        self.__model_offset = (0.0, 0.0)
        # Background control
        self.__use_background = False
        self.__bg_color = (0.0, 0.0, 0.0, 0.0)
        self.__background_follows_palette = False
        app = QGuiApplication.instance()
        if app is not None and hasattr(app, "paletteChanged"):
            app.paletteChanged.connect(self._on_palette_changed)
        # High-DPI handling
        self._dpr = 1.0
        self._canvas_framebuffer = None
        self._canvas_texture = None
        self._fbo_width = 0
        self._fbo_height = 0
        self._source_aspect_ratio = 1.0
        self._antialias = True
        # Fitting is opt-in: resource preview enables it, editor cameras retain
        # their existing interaction. Only explicit requests read native pixels.
        self._content_fit_enabled = False
        self._content_fit_pending = False
        self._content_fit_bounds = None
        self._content_fit_fill = .96
        self._content_fit_source_aspect = 1.
        self._content_fit_viewport = None
        self._content_fit_readbacks = 0
        self._content_fit_attempts = 0
        self._content_fit_sample_size = None
        self._content_fit_empty = False
        self._content_fit_status = "manual"
        self._content_fit_range_limited = False

    def __create_program(self):
        vertex_shader = """#version 330 core
        layout(location = 0) in vec2 a_position;
        layout(location = 1) in vec2 a_texCoord;

        out vec2 v_texCoord;
        uniform float rotation_angle;
        uniform float model_scale;
        uniform vec2 model_offset;
        uniform vec2 stage_to_canvas_scale;

        void main() {
            gl_Position = vec4(a_position, 0.0, 1.0);

            float angle = radians(rotation_angle);
            mat2 rotationMatrix = mat2(cos(angle), -sin(angle),
                                       sin(angle), cos(angle));

            // Preserve the model's intrinsic canvas aspect ratio. The
            // offscreen buffer follows that ratio and the final pass uses
            // contain-style sampling, so no model edge is cropped.
            vec2 centeredTexCoord =
                (a_texCoord - vec2(0.5, 0.5)) * stage_to_canvas_scale;
            vec2 sourceCoord = (centeredTexCoord - model_offset) / max(model_scale, 0.01);
            v_texCoord = rotationMatrix * sourceCoord + vec2(0.5, 0.5);
        }
        """
        frag_shader = """#version 330 core
        in vec2 v_texCoord;
        uniform sampler2D canvas;
        uniform float opacity;
        uniform vec4 bg_color;
        uniform int use_bg; // 0 or 1

        void main() {
            bool outside = any(lessThan(v_texCoord, vec2(0.0))) ||
                           any(greaterThan(v_texCoord, vec2(1.0)));
            vec4 color = outside ? vec4(0.0) : texture(canvas, v_texCoord);
            color *= opacity;
            if (use_bg == 1) {
                // Alpha composite over solid background
                vec3 rgb = mix(bg_color.rgb, color.rgb, color.a);
                gl_FragColor = vec4(rgb, 1.0);
            } else {
                gl_FragColor = color;
            }
        }
        """
        self._program = create_program(vertex_shader, frag_shader)
        self._opacity_loc = GL.glGetUniformLocation(self._program, "opacity")
        self._rotation_angle_loc = GL.glGetUniformLocation(self._program, "rotation_angle")
        self._model_scale_loc = GL.glGetUniformLocation(self._program, "model_scale")
        self._model_offset_loc = GL.glGetUniformLocation(self._program, "model_offset")
        self._stage_to_canvas_scale_loc = GL.glGetUniformLocation(
            self._program, "stage_to_canvas_scale"
        )
        self._bg_color_loc = GL.glGetUniformLocation(self._program, "bg_color")
        self._use_bg_loc = GL.glGetUniformLocation(self._program, "use_bg")

    def __create_vao(self):
        vertices = np.array([
            # 位置
            -1.0, 1.0,
            -1.0, -1.0,
            1.0, -1.0,
            -1.0, 1.0,
            1.0, -1.0,
            1.0, 1.0,
        ], dtype=np.float32)
        uvs = np.array([
            # 纹理坐标
            0.0, 1.0,
            0.0, 0.0,
            1.0, 0.0,
            0.0, 1.0,
            1.0, 0.0,
            1.0, 1.0,
        ], dtype=np.float32)
        self._vao = create_vao(vertices, uvs)

    def __delete_canvas_framebuffer(self):
        if self._canvas_texture:
            try:
                GL.glDeleteTextures([self._canvas_texture])
            except Exception:
                pass
            self._canvas_texture = None
        if self._canvas_framebuffer:
            try:
                GL.glDeleteFramebuffers(1, [self._canvas_framebuffer])
            except Exception:
                pass
            self._canvas_framebuffer = None

    def _desired_canvas_size(self, width: int | None = None, height: int | None = None):
        logical_width = max(1, int(self.width() if width is None else width))
        logical_height = max(1, int(self.height() if height is None else height))
        # Match the longest host edge while retaining the model canvas aspect.
        # This gives Live2D enough projection space for panoramic and portrait
        # models instead of forcing them through a short-edge square.
        longest = max(
            1,
            int(math.ceil(max(logical_width, logical_height) * self._dpr
                          * max(1.0, self.__model_scale)
                          * (1.5 if self._antialias else 1.0))),
        )
        aspect = max(0.01, float(self._source_aspect_ratio))
        # Bound GPU allocation at 32 megapixels / 8192 per edge. Re-render at
        # zoom-aware resolution instead of magnifying a low-resolution FBO.
        longest = min(longest, 8192, int(math.sqrt(32 * 1024 * 1024 * max(aspect, 1 / aspect))))
        if aspect >= 1.0:
            return longest, max(1, int(round(longest / aspect)))
        return max(1, int(round(longest * aspect))), longest

    def setSourceAspectRatio(self, aspect_ratio: float):
        aspect = max(0.05, min(20.0, float(aspect_ratio or 1.0)))
        if math.isclose(aspect, self._source_aspect_ratio, rel_tol=1e-4):
            return
        self._source_aspect_ratio = aspect
        if getattr(self, "_content_fit_bounds", None) is not None:
            self._invalidate_content_fit()
        # Model-ready signals run outside paintGL and may arrive while a
        # different editor's GL context is current. Allocate lazily in paintGL.
        self.update()

    def __create_canvas_framebuffer(self, force: bool = False):
        # Determine device-pixel size for FBO
        self._dpr = float(self.devicePixelRatioF()) if hasattr(self, 'devicePixelRatioF') else float(self.devicePixelRatio())
        fb_w, fb_h = self._desired_canvas_size()
        # Recreate only if size changed
        if (
            not force
            and self._canvas_framebuffer
            and fb_w == self._fbo_width
            and fb_h == self._fbo_height
        ):
            return
        if force:
            # initializeGL() may run for a freshly-created context. Object IDs
            # from the previous context must not be reused or deleted there.
            self._canvas_framebuffer = None
            self._canvas_texture = None
        else:
            self.__delete_canvas_framebuffer()
        # Create new
        self._canvas_framebuffer, self._canvas_texture = create_canvas_framebuffer(fb_w, fb_h)
        self._fbo_width, self._fbo_height = fb_w, fb_h

    def __draw_on_canvas(self):
        framebuffer = _gl_object_id(self._canvas_framebuffer)
        if framebuffer <= 0:
            return
        # Draw model into offscreen FBO at device-pixel resolution
        old_fbo = _gl_object_id(GL.glGetIntegerv(GL.GL_FRAMEBUFFER_BINDING))
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, framebuffer)
        # Ensure viewport matches FBO size
        GL.glViewport(0, 0, int(self._fbo_width), int(self._fbo_height))
        # Keep FBO transparent so compositing in second pass works
        GL.glClearColor(0.0, 0.0, 0.0, 0.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        try:
            if self.on_draw():
                self.on_canvas_rendered()
        finally:
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, old_fbo)

    def initializeGL(self):
        self.__create_program()
        self.__create_vao()
        self.__create_canvas_framebuffer(force=True)
        self.on_init()
        # Ensure model has correct initial size in pixels
        self.on_resize(self._fbo_width, self._fbo_height)

    def resizeGL(self, w, h):
        # Qt may issue a final resize while a native surface is being hidden
        # or closed. Only paintGL owns framebuffer deletion/allocation.
        self._dpr = float(self.devicePixelRatioF()) if hasattr(self, 'devicePixelRatioF') else float(self.devicePixelRatio())
        self.update()

    def paintGL(self):
        # Only allocate/delete GL resources while this window's context is current.
        if self._content_fit_enabled and self._content_fit_bounds is not None:
            viewport = (self.width(), self.height())
            if viewport != self._content_fit_viewport:
                self._apply_cached_content_fit()
        wanted = self._desired_canvas_size()
        if wanted != (self._fbo_width, self._fbo_height):
            self.__create_canvas_framebuffer()
            self.on_resize(self._fbo_width, self._fbo_height)
        if not self._canvas_framebuffer or not self._canvas_texture:
            return
        # First render to offscreen canvas
        self.__draw_on_canvas()
        # Then draw the canvas texture to the widget's default framebuffer
        # Clear to transparent; background color compositing is handled in shader
        GL.glClearColor(0.0, 0.0, 0.0, 0.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)

        # Ensure viewport matches default framebuffer size in pixels (HiDPI safe)
        dpr = float(self.devicePixelRatioF()) if hasattr(self, 'devicePixelRatioF') else float(self.devicePixelRatio())
        vp_w = max(1, int(round(self.width() * dpr)))
        vp_h = max(1, int(round(self.height() * dpr)))
        GL.glViewport(0, 0, vp_w, vp_h)

        GL.glBindVertexArray(self._vao)
        GL.glUseProgram(self._program)

        GL.glProgramUniform1f(self._program, self._opacity_loc, self.__canvas_opacity)
        GL.glProgramUniform1f(self._program, self._rotation_angle_loc, self.__rotation_angle)
        GL.glProgramUniform1f(self._program, self._model_scale_loc, self.__model_scale)
        GL.glProgramUniform2f(self._program, self._model_offset_loc, *self.__model_offset)
        viewport_aspect = float(vp_w) / float(vp_h)
        canvas_aspect = float(self._fbo_width) / float(max(1, self._fbo_height))
        if viewport_aspect >= canvas_aspect:
            stage_to_canvas = (viewport_aspect / canvas_aspect, 1.0)
        else:
            stage_to_canvas = (1.0, canvas_aspect / viewport_aspect)
        GL.glProgramUniform2f(
            self._program,
            self._stage_to_canvas_scale_loc,
            *stage_to_canvas,
        )
        GL.glProgramUniform4f(self._program, self._bg_color_loc, *self.__bg_color)
        GL.glProgramUniform1i(self._program, self._use_bg_loc, 1 if self.__use_background else 0)

        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, _gl_object_id(self._canvas_texture))
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 6)

        GL.glBindVertexArray(0)

        self.draw_overlay(vp_w, vp_h, dpr)

    def draw_overlay(self, _width, _height, _dpr):
        """Native-window overlays must be painted after the composition pass."""

    def on_canvas_rendered(self):
        """Runs with this canvas's transparent native FBO current after Draw."""
        if not self._content_fit_pending:
            return
        self._content_fit_readbacks += 1
        self._content_fit_attempts += 1
        try:
            pixels = self._read_content_fit_pixels()
            bounds = alpha_content_bounds(pixels)
        except Exception as error:
            self._content_fit_pending = False
            self._content_fit_status = "readback-error"
            state = self.getContentFitState()
            state["error"] = str(error)
            self.contentFitApplied.emit(state)
            return
        if bounds is None and self._content_fit_attempts < 3:
            # A newly initialized model can have an empty first pose. Retry at
            # most twice; an invisible model must never cause a readback loop.
            self._content_fit_status = "pending-empty-frame"
            self.update()
            return
        self._content_fit_bounds = bounds or (0., 0., 1., 1.)
        self._content_fit_sample_size = (self._fbo_width, self._fbo_height)
        self._content_fit_source_aspect = self._fbo_width / max(1, self._fbo_height)
        self._content_fit_pending = False
        self._content_fit_empty = bounds is None
        self._content_fit_status = "fit" if bounds is not None else "empty-fallback"
        self._apply_cached_content_fit()

    def _read_content_fit_pixels(self):
        """Read the transparent source, never the background-composited screen."""
        old_read_fbo = _gl_object_id(GL.glGetIntegerv(GL.GL_READ_FRAMEBUFFER_BINDING))
        old_alignment = int(GL.glGetIntegerv(GL.GL_PACK_ALIGNMENT))
        GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, _gl_object_id(self._canvas_framebuffer))
        GL.glPixelStorei(GL.GL_PACK_ALIGNMENT, 1)
        try:
            raw = GL.glReadPixels(0, 0, self._fbo_width, self._fbo_height,
                                  GL.GL_RGBA, GL.GL_UNSIGNED_BYTE)
            pixels = np.frombuffer(raw, dtype=np.uint8) if isinstance(raw, (bytes, bytearray, memoryview)) else np.asarray(raw, dtype=np.uint8)
            return pixels.reshape(self._fbo_height, self._fbo_width, 4)
        finally:
            GL.glPixelStorei(GL.GL_PACK_ALIGNMENT, old_alignment)
            GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, old_read_fbo)

    def _apply_cached_content_fit(self):
        if self._content_fit_bounds is None or self.width() < 1 or self.height() < 1:
            return
        fitted = content_fit_transform(self._content_fit_bounds, (self.width(), self.height()),
                                       self._content_fit_source_aspect, self._content_fit_fill)
        self.__model_scale = fitted.scale
        self.__model_offset = (fitted.offset_x, fitted.offset_y)
        self.__rotation_angle = 0.
        self._content_fit_range_limited = fitted.range_limited
        self._content_fit_viewport = (self.width(), self.height())
        if not self._content_fit_pending:
            self._content_fit_status = "empty-fallback" if self._content_fit_empty else "fit"
        self.contentFitApplied.emit(self.getContentFitState())
        self.update()

    def _invalidate_content_fit(self):
        self._content_fit_bounds = None
        self._content_fit_viewport = None
        self._content_fit_readbacks = 0
        self._content_fit_attempts = 0
        self._content_fit_sample_size = None
        self._content_fit_empty = False
        self._content_fit_range_limited = False
        self._content_fit_pending = self._content_fit_enabled or self._content_fit_pending
        self._content_fit_status = "pending" if self._content_fit_pending else "manual"

    def fitToContent(self, fill: float = .96, *, auto_resize: bool = True, refresh: bool = True) -> bool:
        """Queue a one-shot native-alpha fit; no GL read occurs in this setter.

        With auto_resize, future viewport resizes only recompute the cached
        bounds. Explicit fitting also resets rotation, like a camera reset.
        """
        fill = float(fill)
        if not math.isfinite(fill) or not 0 < fill <= 1:
            raise ValueError("Content fill must be between 0 and 1")
        if getattr(self, "model", None) is None and not getattr(self, "model_path", None):
            return False
        was_enabled = self._content_fit_enabled
        self._content_fit_enabled = bool(auto_resize)
        if was_enabled != self._content_fit_enabled:
            self.fitModeChanged.emit(self._content_fit_enabled)
        self._content_fit_fill = fill
        self.__rotation_angle = 0.
        if refresh or self._content_fit_bounds is None:
            self._content_fit_pending = True
            self._content_fit_attempts = 0
            self._content_fit_status = "pending"
        else:
            self._apply_cached_content_fit()
        self.update()
        return True

    def setContentFitEnabled(self, enabled: bool):
        enabled = bool(enabled)
        changed = enabled != self._content_fit_enabled
        self._content_fit_enabled = enabled
        if not enabled:
            self._content_fit_pending = False
            self._content_fit_status = "manual"
        elif self._content_fit_bounds is not None:
            self._apply_cached_content_fit()
        else:
            self._content_fit_pending = True
            self._content_fit_attempts = 0
            self._content_fit_status = "pending"
        if changed:
            self.fitModeChanged.emit(enabled)
        self.update()

    def getContentFitState(self) -> dict:
        return {"enabled": self._content_fit_enabled, "pending": self._content_fit_pending,
                "status": self._content_fit_status,
                "bounds": list(self._content_fit_bounds) if self._content_fit_bounds is not None else None,
                "fill": self._content_fit_fill, "scale": self.__model_scale,
                "offset_x": self.__model_offset[0], "offset_y": -self.__model_offset[1],
                "rotation": self.__rotation_angle, "range_limited": self._content_fit_range_limited,
                "readback_count": self._content_fit_readbacks,
                "sample_size": list(self._content_fit_sample_size) if self._content_fit_sample_size else None,
                "model_generation": getattr(self, "_model_load_generation", 0)}

    def setCanvasOpacity(self, value):
        self.__canvas_opacity = value
        self.update()

    def setRotationAngle(self, angle):
        self.setContentFitEnabled(False)
        self.__rotation_angle = float(angle)
        self.update()

    def setModelTransform(self, scale: float, offset_x: float, offset_y: float):
        """Apply responsive model scale and offsets at the final composition pass."""
        self.setContentFitEnabled(False)
        self.__model_scale = max(0.25, min(4.0, float(scale)))
        self.__model_offset = (
            max(-1.0, min(1.0, float(offset_x))),
            max(-1.0, min(1.0, -float(offset_y))),
        )
        self.update()

    def windowPointToModel(self, x: float, y: float) -> tuple[float, float]:
        """Legacy screen-to-FBO mapping; these are not Cubism canvas pixels."""
        width, height = max(1, self.width()), max(1, self.height())
        canvas_width, canvas_height = max(1, self._fbo_width), max(1, self._fbo_height)
        viewport_aspect, canvas_aspect = width / height, canvas_width / canvas_height
        sx, sy = ((viewport_aspect / canvas_aspect, 1.0) if viewport_aspect >= canvas_aspect
                  else (1.0, canvas_aspect / viewport_aspect))
        cx = ((float(x) / width - .5) * sx - self.__model_offset[0]) / self.__model_scale
        cy = ((.5 - float(y) / height) * sy - self.__model_offset[1]) / self.__model_scale
        angle = math.radians(self.__rotation_angle)
        cosine, sine = math.cos(angle), math.sin(angle)
        source_x = cosine * cx + sine * cy + .5
        source_y = -sine * cx + cosine * cy + .5
        return source_x * canvas_width, (1 - source_y) * canvas_height

    def previewTransform(self) -> PreviewTransform:
        return PreviewTransform(self.width(), self.height(), self._fbo_width, self._fbo_height,
                                self.__model_scale, *self.__model_offset, self.__rotation_angle)

    def setAntialias(self, enabled: bool):
        self._antialias = bool(enabled)
        self.update()

    def setBackground(self, transparent: bool, qcolor):
        """Configure background compositing.
        If transparent is True, the widget remains transparent.
        Otherwise, fill with the provided QColor.
        """
        # A createWindowContainer() child is an opaque native window on
        # Windows. Use the preview-stage color instead of exposing a black
        # native surface when the UI requests "transparent".
        embedded_transparent = bool(transparent) and bool(
            getattr(self, "_embedded", False)
        )
        self.__use_background = not bool(transparent) or embedded_transparent
        if embedded_transparent:
            self.__background_follows_palette = True
            self._set_palette_background()
            self.update()
            return
        self.__background_follows_palette = False
        if qcolor is not None:
            # QColor -> normalized RGBA
            r, g, b, a = qcolor.redF(), qcolor.greenF(), qcolor.blueF(), 1.0
            self.__bg_color = (r, g, b, a)
        else:
            self.__bg_color = (0.0, 0.0, 0.0, 1.0)
        self.update()

    def _set_palette_background(self) -> None:
        color = QGuiApplication.palette().color(QPalette.ColorRole.Window)
        self.__bg_color = (color.redF(), color.greenF(), color.blueF(), 1.0)

    def _on_palette_changed(self, *_args) -> None:
        if self.__background_follows_palette:
            self._set_palette_background()
            self.update()

    @abstractmethod
    def on_init(self):
        pass

    @abstractmethod
    def on_draw(self):
        pass

    @abstractmethod
    def on_resize(self, width: int, height: int):
        pass



class Live2DCanvas(ADPOpenGLCanvas):
    modelLoaded = Signal()
    modelFrameReady = Signal(str, int)
    drawableClicked = Signal(str)
    modelPointClicked = Signal(float, float)
    drawablesPicked = Signal(list)
    regionPicked = Signal(list, dict)
    drawablesPickedWithMode = Signal(list, str)
    regionPickedWithMode = Signal(list, dict, str)
    selectionFailed = Signal(str)
    selectionCancelled = Signal()
    def __init__(self, model_path=None, embedded: bool = False):
        super().__init__()
        self.model_path = model_path
        self._embedded = bool(embedded)
        self.model: Optional[live2d.LAppModel] = None
        # tool for controlling model opacity
        self.canvas: Optional[Canvas] = None
        if hasattr(self, "setTitle"):
            self.setTitle("Live2DCanvas")
        elif hasattr(self, "setWindowTitle"):
            self.setWindowTitle("Live2DCanvas")
        if self._embedded:
            self.setBackground(True, None)
        self.radius_per_frame = math.pi * 0.5 / 120
        self.total_radius = 0
        # Mouse follow control
        self._mouse_follow_enabled = False
        self._auto_blink_enabled = True
        self._auto_breath_enabled = True
        # Advanced parameter overrides
        self._advanced_enabled = False
        self._advanced_params = {}
        self._motion_frozen = False
        self._motion_loop_enabled = False
        self._last_played_motion: tuple[str, int] | None = None
        self._motion_playback = None
        self._motion_scrub = None
        self._motion_request_serial = 0
        self._motion_started_during_update = False
        self._updating_motion = False
        self._render_timer_id = None
        self._rendering_active = True
        self._editor_interaction = False
        self._selection_mode = "none"
        self._selection_scene_provider = None
        self._highlight_selected = set()
        self._highlight_related = set()
        self._highlight_edges = {}
        self._selection_alpha_cache = {}
        self._selection_drag = None
        self._selection_shift = False
        self._selection_moved = False
        self._parameter_value_model_id = None
        self._parameter_value_indices = {}
        self._last_selection_region = None
        self._selection_region_canvas = None
        self._part_opacity_overrides = {}
        self._part_opacity_defaults = {}
        self._part_indices = {}
        self._drawable_opacity_overrides = {}
        self._drawable_indices = {}
        self._gl_initialized = False
        self._model_load_generation = 1 if model_path else 0
        self._active_model_generation = 0
        self._model_frame_ready_generation = -1
        self._active_model_path = None
        # Cached motions metadata
        self._motions: List[Dict[str, Any]] = []

    def on_init(self):
        live2d.glInit()
        # must be created after opengl context is configured
        self.canvas = Canvas()
        self._gl_initialized = True
        if self.model_path:
            self._load_model_with_current_context(self.model_path)
        if self._rendering_active and self._render_timer_id is None:
            self._render_timer_id = self.startTimer(int(1000 / 60))

    def _load_model_with_current_context(self, model_path: str):
        model = live2d.LAppModel()
        model.LoadModelJson(model_path)
        try:
            canvas_size = model.GetCanvasSizePixel()
            canvas_width = float(canvas_size[0])
            canvas_height = float(canvas_size[1])
            if canvas_width > 0 and canvas_height > 0:
                self.setSourceAspectRatio(canvas_width / canvas_height)
        except Exception:
            try:
                canvas_size = model.GetCanvasSize()
                canvas_width = float(canvas_size[0])
                canvas_height = float(canvas_size[1])
                if canvas_width > 0 and canvas_height > 0:
                    self.setSourceAspectRatio(canvas_width / canvas_height)
            except Exception:
                pass
        if self._fbo_width > 0 and self._fbo_height > 0:
            model.Resize(self._fbo_width, self._fbo_height)
        self.model = model
        self._active_model_generation = self._model_load_generation
        self._model_frame_ready_generation = -1
        self._active_model_path = str(model_path)
        self._invalidate_content_fit()
        self._parameter_value_model_id = None
        self._parameter_value_indices.clear()
        self.model_path = model_path
        self._selection_alpha_cache.clear()
        self._selection_drag = None
        self._last_selection_region = None
        try:
            model.SetAutoBlinkEnable(self._auto_blink_enabled)
            model.SetAutoBreathEnable(self._auto_breath_enabled)
        except Exception:
            pass
        self._motion_frozen = False
        self._last_played_motion = None
        self._clear_motion_clock()
        self._advanced_params = {}
        self._part_opacity_overrides = {}
        self._part_opacity_defaults = {}
        self._drawable_opacity_overrides = {}
        self._drawable_indices = {str(identifier): index for index, identifier in enumerate(model.GetDrawableIds())}
        try:
            self._part_indices = {str(part_id): index for index, part_id in enumerate(model.GetPartIds())}
        except Exception:
            self._part_indices = {}
        try:
            self._motions = self._load_motions_from_model_json(model_path)
        except Exception:
            self._motions = []
        self.modelLoaded.emit()

    def loadModel(self, model_path: str):
        """Load a model into the already-created OpenGL widget."""
        if not model_path:
            raise ValueError("A Live2D model path is required.")
        self._model_load_generation += 1
        self.model_path = str(model_path)
        if not self._gl_initialized or self.context() is None or not self.isValid():
            self.update()
            return
        self.makeCurrent()
        try:
            self._load_model_with_current_context(self.model_path)
        finally:
            self.doneCurrent()
        self.update()

    def unloadModel(self):
        """Drop the active model while retaining the warmed OpenGL context."""
        context_current = False
        try:
            if self._gl_initialized and self.context() is not None and self.isValid():
                self.makeCurrent()
                context_current = True
            self.model = None
            self._parameter_value_model_id = None
            self._parameter_value_indices.clear()
        finally:
            if context_current:
                self.doneCurrent()
        self.model_path = None
        self._model_load_generation += 1
        self._active_model_path = None
        self._model_frame_ready_generation = -1
        self._invalidate_content_fit()
        self._motions = []
        self._last_played_motion = None
        self._motion_frozen = False
        self._clear_motion_clock()
        self._advanced_params = {}
        self._part_opacity_overrides = {}
        self._part_opacity_defaults = {}
        self._part_indices = {}
        self._drawable_opacity_overrides = {}
        self._drawable_indices = {}
        self._selection_alpha_cache.clear()
        self._selection_drag = None
        self._last_selection_region = None
        self.update()

    def timerEvent(self, a0):
        self.update()

    def on_draw(self):
        live2d.clearBuffer()
        if self.model is None:
            return False
        if not self._motion_frozen:
            self._update_model_motion()
            self._restart_loop_motion_if_finished()
        # Apply advanced parameter overrides each frame if enabled
        if self._advanced_enabled:
            try:
                self._apply_advanced_params()
            except Exception:
                pass
        if self._part_opacity_overrides:
            for part_id, value in self._part_opacity_overrides.items():
                index = self._part_indices.get(part_id)
                if index is not None:
                    self.model.SetPartOpacity(index, float(value))
        # live2d-py's native Draw recomputes Cubism Core vertices before drawing.
        # Calling SDK Update(0) here would also run motion/physics/pose, changing
        # an otherwise frozen frame and unrelated parameters during edits.
        # https://github.com/EasyLive2D/live2d-py/blob/v0.7.0.2/Live2D/V3/Main/src/Model.cpp#L1018
        self.model.Draw()
        return True

    def modelLoadGeneration(self) -> int:
        """Identity of the latest load request, including deferred native loads."""
        return self._model_load_generation

    def on_canvas_rendered(self):
        if self.model is None:
            return
        if self._model_frame_ready_generation != self._active_model_generation:
            self._model_frame_ready_generation = self._active_model_generation
            self.modelFrameReady.emit(str(self._active_model_path or self.model_path or ""),
                                      self._active_model_generation)
        super().on_canvas_rendered()

    def on_resize(self, width: int, height: int):
        if self.model is not None:
            self.model.Resize(width, height)

    # --- Mouse tracking and follow implementation ---
    def setMouseTracking(self, enable: bool) -> None:  # type: ignore[override]
        # QWindow receives pointer move events directly; keep this flag for
        # Live2D's optional gaze-follow behaviour.
        self._mouse_follow_enabled = bool(enable)

    def mouseMoveEvent(self, event):
        if self._effective_selection_mode() != "none":
            if self._selection_drag is not None:
                self._selection_drag[1] = QPointF(event.position())
                self._selection_moved |= (self._selection_drag[1] - self._selection_drag[0]).manhattanLength() >= 4
                if self._effective_selection_mode() == "rectangle":
                    self.update()
            event.accept()
            return
        if not self._mouse_follow_enabled or self.model is None:
            return super().mouseMoveEvent(event)
        w = max(1, self.width())
        h = max(1, self.height())
        px = event.pos().x()
        py = event.pos().y()
        # Normalize to [-1, 1], origin center, +x right, +y up
        nx = (px / w - 0.5) * 2.0
        ny = (0.5 - py / h) * 2.0
        self._apply_mouse_follow(nx, ny)
        self.update()
        return super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._effective_selection_mode() != "none":
                if self.model is not None:
                    self._selection_drag = [QPointF(event.position()), QPointF(event.position())]
                    self._selection_shift = bool(event.modifiers() & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier))
                    self._selection_moved = False
                    self._last_selection_region = None
                    self.update()
                event.accept()
                return
            try:
                self.playDefaultTapMotion()
            except Exception:
                pass
        return super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._selection_drag is not None:
            first, _ = self._selection_drag
            last = QPointF(event.position())
            self._selection_drag = None
            try:
                moved = self._selection_moved or (last - first).manhattanLength() >= 4
                if not moved:
                    self.pickDrawablesAt(last.x(), last.y(), "toggle" if self._selection_shift else "replace")
                elif self._effective_selection_mode() == "rectangle":
                    self.pickDrawablesInRect(first, last, "add" if self._selection_shift else "replace")
            except Exception as exc:
                self.selectionFailed.emit(str(exc))
            self.update()
            event.accept()
            return
        return super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if (not getattr(self, "external_selection_shortcuts", False)
                and event.key() == Qt.Key.Key_Escape and self._selection_mode != "none"):
            self._selection_drag = None
            self._last_selection_region = None
            self.update()
            self.selectionCancelled.emit()
            event.accept()
            return
        return super().keyPressEvent(event)

    def leaveEvent(self, event):
        # Reset follow when cursor leaves
        if self._selection_mode == "none" and self._mouse_follow_enabled and self.model is not None:
            try:
                self._apply_mouse_follow(0.0, 0.0)
            except Exception:
                pass
        return super().leaveEvent(event)

    def _apply_mouse_follow(self, nx: float, ny: float):
        """Try multiple strategies to apply mouse-follow to the model.
        nx/ny: normalized [-1, 1].
        Priority: SetDrag/SetDragging; fallback to parameter setting.
        """
        if self.model is None:
            return
        # Fallback: set common parameter IDs
        angle_x = float(max(-1.0, min(1.0, nx))) * 30.0
        angle_y = float(max(-1.0, min(1.0, ny))) * 30.0
        body_x = float(max(-1.0, min(1.0, nx))) * 10.0
        eye_x = float(max(-1.0, min(1.0, nx)))
        eye_y = float(max(-1.0, min(1.0, ny)))

        def try_set(param_id: str, value: float):
            fn = getattr(self.model, "SetParameterValue", None)
            if callable(fn):
                try:
                    fn(param_id, float(value))
                    return True
                except Exception:
                    pass
            return False
        # Apply a few representative parameters (best-effort)
        try_set("ParamAngleX", angle_x)
        try_set("ParamAngleY", angle_y)
        try_set("ParamBodyAngleX", body_x)
        try_set("ParamEyeBallX", eye_x)
        try_set("ParamEyeBallY", eye_y)

    def setAutoBlinkEnable(self, enabled: bool):
        self._auto_blink_enabled = bool(enabled)
        try:
            if self.model:
                self.model.SetAutoBlinkEnable(self._auto_blink_enabled)
        except Exception:
            pass

    def setAutoBreathEnable(self, enabled: bool):
        self._auto_breath_enabled = bool(enabled)
        try:
            if self.model:
                self.model.SetAutoBreathEnable(self._auto_breath_enabled)
        except Exception:
            pass

    def release(self):
        """Release the current model and GL resources"""
        if self._render_timer_id is not None:
            try:
                self.killTimer(self._render_timer_id)
            except Exception:
                pass
            self._render_timer_id = None

        context_current = False
        try:
            if self.context() is not None and self.isValid():
                self.makeCurrent()
                context_current = True
        except Exception:
            pass
        try:
            self.model = None
            self.canvas = None
            self._ADPOpenGLCanvas__delete_canvas_framebuffer()
            self._gl_initialized = False
        except Exception as e:
            print(f"Error releasing Live2D resources: {e}")
        finally:
            if context_current:
                try:
                    self.doneCurrent()
                except Exception:
                    pass

    def getParameterMetaList(self):
        """Return a list of parameter metadata from the loaded model.
        Each item: { 'id': str, 'type': any, 'value': float, 'min': float, 'max': float, 'default': float }
        Returns empty list if model is not ready or API not available.
        """
        meta = []
        try:
            if self.model is None:
                return meta
            try:
                n = self.model.GetParameterCount()
            except Exception:
                n = 0
            for i in range(n):
                try:
                    p = self.model.GetParameter(i)
                    pid = p.id
                    ptype = int(p.type)
                    pval = float(p.value)
                    pmax = float(p.max)
                    pmin = float(p.min)
                    pdef = float(p.default)
                    if pid is None:
                        continue
                    meta.append({
                        'id': str(pid),
                        'type': ptype,
                        'value': pval,
                        'min': pmin,
                        'max': pmax,
                        'default': pdef,
                    })
                except Exception:
                    continue
        except Exception:
            return []
        return meta

    # Advanced params API
    def setAdvancedParams(self, enabled: bool, params: dict):
        self._advanced_enabled = bool(enabled)
        self._advanced_params = dict(params) if params else {}
        # Apply immediately using SetParameterValue when available
        if self._advanced_enabled and self._advanced_params and self.model is not None:
            set_val = getattr(self.model, "SetParameterValue", None)
            if callable(set_val):
                for pid, v in self._advanced_params.items():
                    try:
                        set_val(pid, float(v))
                    except Exception:
                        continue
        self.update()

    def setMotionFrozen(self, frozen: bool):
        """Pause model updates while keeping rendering and parameter editing active."""
        was_frozen = self._motion_frozen
        self._motion_frozen = bool(frozen)
        if was_frozen and not frozen:
            self._reset_update_timestamp()
            scrub = self._motion_scrub
            self._motion_scrub = None
            # The SDK has no native seek.  A manually scrubbed Parameter pose
            # can only return to motion playback by explicitly restarting it.
            if scrub:
                self.playMotion(scrub["group"], scrub["index"])
        self.update()

    def setRenderingActive(self, active: bool):
        """Suspend the frame timer for an editor tab that is not visible."""
        self._rendering_active = bool(active)
        if not self._rendering_active and self._render_timer_id is not None:
            self.killTimer(self._render_timer_id)
            self._render_timer_id = None
        elif self._rendering_active and self._gl_initialized and self._render_timer_id is None:
            self._reset_update_timestamp()
            self._render_timer_id = self.startTimer(int(1000 / 60))
            self.update()

    def setEditorInteraction(self, enabled: bool):
        self._editor_interaction = bool(enabled)
        self.setSelectionMode("point" if enabled else "none")
        if enabled:
            self.setMouseTracking(False)

    def setSelectionMode(self, mode: str):
        if mode not in ("none", "point", "rectangle"):
            raise ValueError("Selection mode must be none, point or rectangle")
        self._selection_mode = mode
        self._selection_drag = None
        self._last_selection_region = None
        self.setCursor(Qt.CursorShape.CrossCursor if mode != "none" else Qt.CursorShape.ArrowCursor)
        self.update()

    def selectionMode(self) -> str:
        return self._selection_mode

    def _effective_selection_mode(self):
        # An editor's neutral mode still inspects the model. The resource
        # preview retains its existing tap interactions when no tool is active.
        return "point" if self._editor_interaction and self._selection_mode == "none" else self._selection_mode

    def setSelectionSceneProvider(self, provider):
        """Set an on-demand read-only provider returning snapshot/texture_paths.

        A provider returns ``{'snapshot': full_snapshot, 'texture_paths': paths}``.
        It may cache by getSelectionPose(); no metadata file is needed for picks.
        """
        self._selection_scene_provider = provider
        self._selection_alpha_cache.clear()

    def getSelectionPose(self) -> dict:
        if self.model is None:
            return {"parameters": {}, "parts": {}, "drawables": {}, "parts_complete": False}
        parameters = self.getParameterValues(self._parameter_indices())
        parts = dict(self._part_opacity_defaults)
        parts.update(self._part_opacity_overrides)
        # live2d-py exposes SetPartOpacity but no opacity getter. Do not invent
        # SDK Pose-controller values; only the overrides actually applied here.
        return {"parameters": parameters, "parts": parts, "drawables": dict(self._drawable_opacity_overrides),
                "parts_complete": False}

    def _parameter_indices(self):
        model_id = id(self.model) if self.model is not None else None
        if model_id != self._parameter_value_model_id:
            # A failed read from a newly loaded model must not leave the old
            # model's ID map reachable or mark that failed map as initialized.
            self._parameter_value_model_id = None
            self._parameter_value_indices = {}
            self._parameter_value_indices = ({str(identifier): index for index, identifier in enumerate(self.model.GetParamIds())}
                                             if self.model is not None else {})
            self._parameter_value_model_id = model_id
        return self._parameter_value_indices

    def getParameterValues(self, parameter_ids) -> dict[str, float]:
        """Read only requested native values without Update, clamping or metadata.

        The static ID/index map is rebuilt when the native model object changes.
        Unknown IDs are omitted rather than filled with invented default values.
        """
        indices = self._parameter_indices()
        if self.model is None:
            return {}
        values = {}
        getter = getattr(self.model, "GetParameterValue", None)
        for identifier in dict.fromkeys([parameter_ids] if isinstance(parameter_ids, str) else parameter_ids):
            index = indices.get(str(identifier))
            if index is None:
                continue
            value = getter(index) if callable(getter) else self.model.GetParameter(index).value
            values[str(identifier)] = float(value)
        return values

    def getSelectionScene(self) -> SelectionScene:
        if self._selection_scene_provider is None:
            raise RuntimeError("A current-pose ArtMesh selection provider is required")
        result = self._selection_scene_provider()
        if isinstance(result, SelectionScene):
            return result
        snapshot = result.get("snapshot", result)
        return SelectionScene(snapshot, result.get("texture_paths", []), alpha_cache=self._selection_alpha_cache)

    def _selection_coordinates(self, snapshot):
        if self.model is None:
            raise RuntimeError("No Live2D model is loaded")
        native = getattr(self.model, "_model", self.model)
        return ModelCoordinates(self.previewTransform(), native.GetMvp(), snapshot["canvas"])

    def windowPointToCanvas(self, x: float, y: float, snapshot=None):
        snapshot = snapshot if snapshot is not None else self.getSelectionScene().snapshot
        return self._selection_coordinates(snapshot).window_to_canvas((x, y))

    def canvasPointToWindow(self, x: float, y: float, snapshot=None):
        snapshot = snapshot if snapshot is not None else self.getSelectionScene().snapshot
        return self._selection_coordinates(snapshot).canvas_to_window((x, y))

    def pickDrawablesAt(self, x: float, y: float, selection_mode="replace") -> list[str]:
        if selection_mode not in ("replace", "toggle"):
            raise ValueError("Point selection operation must be replace or toggle")
        if self._selection_scene_provider is not None:
            scene = self.getSelectionScene()
            point = self._selection_coordinates(scene.snapshot).window_to_canvas((x, y))
            hits = scene.hit_point(point)
            self.drawablesPickedWithMode.emit(hits, selection_mode)
            self.drawablesPicked.emit(hits)
            return hits
        # Compatibility for callers without a Core snapshot: the SDK's real
        # drawable IDs, never HitPart's distinct Part IDs. No legacy normalized
        # FBO point signal is emitted alongside the new selection workflow.
        fbo_x, fbo_y = self.windowPointToModel(x, y)
        native = getattr(self.model, "_model", self.model)
        hits = [identifier for identifier in native.HitDrawable(fbo_x, fbo_y, False)
                if self._drawable_opacity_overrides.get(str(identifier), 1.0) > 0]
        self.drawablesPickedWithMode.emit(hits, selection_mode)
        self.drawablesPicked.emit(hits)
        if hits:
            self.drawableClicked.emit(str(hits[0]))
        return hits

    def pickDrawablesInRect(self, first, last, selection_mode="replace") -> list[str]:
        if selection_mode not in ("replace", "add"):
            raise ValueError("Rectangle selection operation must be replace or add")
        scene = self.getSelectionScene()
        coordinates = self._selection_coordinates(scene.snapshot)
        point = lambda p: (p.x(), p.y()) if hasattr(p, "x") else tuple(p)
        region = coordinates.window_rect(point(first), point(last))
        hits = scene.hit_region(region)
        self._last_selection_region = region
        self._selection_region_canvas = scene.snapshot["canvas"]
        self.regionPickedWithMode.emit(hits, region, selection_mode)
        self.regionPicked.emit(hits, region)
        return hits

    def setSelectionHighlights(self, selected, related=()):
        selected, related = set(selected), set(related) - set(selected)
        if (selected, related) != (self._highlight_selected, self._highlight_related):
            self._highlight_selected, self._highlight_related = selected, related
            self._highlight_edges.clear()
            self.update()

    def _selection_highlight_lines(self):
        if not self.model or not self._selection_scene_provider:
            return [], []
        if not self._highlight_selected and not self._highlight_related:
            return [], []
        scene = self.getSelectionScene()
        coordinates = self._selection_coordinates(scene.snapshot)
        selected, related = [], []
        for drawable in scene.drawables:
            identifier = drawable.get("id")
            if identifier not in self._highlight_selected and identifier not in self._highlight_related:
                continue
            vertices, indices = drawable.get("vertices", []), drawable.get("indices", [])
            key = identifier, tuple(indices)
            if key not in self._highlight_edges:
                counts = {}
                for offset in range(0, len(indices) - 2, 3):
                    triangle = indices[offset:offset + 3]
                    if any(index < 0 or index >= len(vertices) for index in triangle):
                        continue
                    for a, b in zip(triangle, triangle[1:] + triangle[:1]):
                        edge = tuple(sorted((a, b)))
                        counts[edge] = counts.get(edge, 0) + 1
                self._highlight_edges[key] = [edge for edge, count in counts.items() if count == 1]
            output = selected if identifier in self._highlight_selected else related
            for a, b in self._highlight_edges[key]:
                if max(a, b) < len(vertices):
                    p, q = coordinates.canvas_to_window(vertices[a]), coordinates.canvas_to_window(vertices[b])
                    if all(math.isfinite(v) for v in (*p, *q)):
                        output.append(QLineF(QPointF(*p), QPointF(*q)))
        return selected, related

    def _draw_selection_highlights(self):
        try:
            selected, related = self._selection_highlight_lines()
        except (ValueError, RuntimeError, np.linalg.LinAlgError):
            return  # model load / close may temporarily lack a valid MVP
        if not selected and not related:
            return
        # QOpenGLWindow is a paint device: paint directly on the native surface
        # after composition, so the overlay stays above WindowContainer on Windows.
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            if related:
                painter.setPen(QPen(QColor(255, 125, 54), 2, Qt.PenStyle.DashLine))
                painter.drawLines(related)
            if selected:
                painter.setPen(QPen(QColor(255, 218, 70), 2))
                painter.drawLines(selected)
        finally:
            painter.end()

    def draw_overlay(self, width, height, dpr):
        self._draw_selection_highlights()
        if self._selection_drag is not None and self._effective_selection_mode() == "rectangle":
            first, last = self._selection_drag
            points = [(first.x(), first.y()), (last.x(), first.y()),
                      (last.x(), last.y()), (first.x(), last.y())]
        elif self._last_selection_region and self._selection_region_canvas:
            coordinates = self._selection_coordinates({"canvas": self._selection_region_canvas})
            points = [coordinates.canvas_to_window(p) for p in self._last_selection_region["polygon"]]
        else:
            return
        # Paint an outline on the final native framebuffer; a QWidget overlay
        # would be behind createWindowContainer on Windows. Preserve GL state.
        scissor_enabled = GL.glIsEnabled(GL.GL_SCISSOR_TEST)
        scissor_box = GL.glGetIntegerv(GL.GL_SCISSOR_BOX)
        clear_color = GL.glGetFloatv(GL.GL_COLOR_CLEAR_VALUE)
        GL.glEnable(GL.GL_SCISSOR_TEST)
        GL.glClearColor(.15, .67, 1., 1.)
        try:
            for a, b in zip(points, points[1:] + points[:1]):
                steps = max(1, math.ceil(max(abs(b[0] - a[0]), abs(b[1] - a[1])) * dpr / 4))
                for index in range(steps + 1):
                    x = int(round((a[0] + (b[0] - a[0]) * index / steps) * dpr))
                    y = height - int(round((a[1] + (b[1] - a[1]) * index / steps) * dpr))
                    GL.glScissor(max(0, x - 1), max(0, y - 1), 3, 3)
                    GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        finally:
            GL.glScissor(*scissor_box)
            GL.glClearColor(*clear_color)
            if not scissor_enabled:
                GL.glDisable(GL.GL_SCISSOR_TEST)
    def setPartOpacityOverrides(self, values: dict[str, float], defaults: dict[str, float] | None = None):
        if defaults is not None:
            self._part_opacity_defaults = dict(defaults)
        for key in values:
            self._part_opacity_defaults.setdefault(str(key), 1.0)
        effective = dict(self._part_opacity_defaults)
        effective.update(values)
        overrides = {str(key): max(0.0, min(1.0, float(value))) for key, value in effective.items()}
        self._part_opacity_overrides = overrides
        self.update()

    def supportsDrawableOpacityOverrides(self) -> bool:
        native = getattr(self.model, "_model", self.model)
        return bool(native is not None and callable(getattr(native, "SetDrawableOpacityOverrides", None)))

    def drawableOpacitySupportReason(self) -> str:
        return preview_native_status().get("reason", "")

    def setDrawableOpacityOverrides(self, values: dict[str, float]):
        from app.core.live2d_drawables import opacity_multiplier
        overrides = {str(key): opacity_multiplier(value) for key, value in values.items()}
        native = getattr(self.model, "_model", self.model)
        setter = getattr(native, "SetDrawableOpacityOverrides", None)
        if not callable(setter):
            if overrides:
                raise RuntimeError(self.drawableOpacitySupportReason() or "Drawable-opacity preview is unavailable.")
            self._drawable_opacity_overrides = {}
            return
        if any(identifier not in self._drawable_indices for identifier in overrides):
            raise ValueError("Unknown preview ArtMesh.")
        setter({self._drawable_indices[key]: value for key, value in overrides.items()})
        self._drawable_opacity_overrides = overrides
        self._selection_alpha_cache.clear()
        self.update()

    def isMotionFrozen(self) -> bool:
        return self._motion_frozen

    def setMotionLoop(self, enabled: bool):
        self._motion_loop_enabled = bool(enabled)

    def setMotionTime(self, motion: Dict[str, Any] | None, seconds: float) -> dict[str, float]:
        """Apply motion Parameter curves for deterministic frozen-pose scrubbing."""
        if self.model is None or not motion:
            return {}
        values = evaluate_motion_parameters(str(motion.get("file") or ""), seconds)
        setter = getattr(self.model, "SetParameterValue", None)
        if not callable(setter):
            return {}
        for parameter_id, value in values.items():
            try:
                setter(parameter_id, float(value))
            except Exception:
                continue
        if self._motion_frozen:
            duration = max(0.0, float(motion.get("duration") or 0))
            self._motion_scrub = {
                "group": str(motion.get("group", "")), "index": int(motion.get("index", 0)),
                "time": min(max(0.0, float(seconds)), duration) if duration else max(0.0, float(seconds)),
                "duration": duration, "playing": False, "scrubbed": True,
            }
        self.update()
        return values

    def _clear_motion_clock(self):
        self._motion_request_serial += 1
        self._motion_playback = self._motion_scrub = None

    def _reset_update_timestamp(self):
        # live2d-py's wrapper computes the SDK delta from _lastFrame.  Frozen or
        # hidden time must not become a 100 ms jump on the next native Update.
        if self.model is not None and hasattr(self.model, "_lastFrame"):
            self.model._lastFrame = time.time()

    def _update_model_motion(self):
        previous = getattr(self.model, "_lastFrame", None)
        self._motion_started_during_update = False
        self._updating_motion = True
        try:
            self.model.Update()
        finally:
            self._updating_motion = False
        current = getattr(self.model, "_lastFrame", None)
        if (self._motion_playback and self._motion_playback["playing"]
                and not self._motion_started_during_update
                and previous is not None and current is not None):
            # This is the exact capped delta used by the installed LAppModel,
            # not the UI timer or time spent waiting while the page is hidden.
            delta = max(0.0, min(float(current) - float(previous), 0.1))
            self._motion_playback["time"] += delta

    def getMotionPlaybackState(self) -> dict | None:
        state = self._motion_scrub if self._motion_frozen and self._motion_scrub else self._motion_playback
        if state is None:
            return None
        result = dict(state)
        duration = float(result["duration"])
        if duration > 0:
            result["time"] = (float(result["time"]) % duration if result.get("native_loop")
                              else min(float(result["time"]), duration))
        result["frozen"] = self._motion_frozen
        return result

    def _start_native_motion(self, group: str, index: int):
        self._motion_request_serial += 1
        serial = self._motion_request_serial
        owner = weakref.ref(self)
        motion = self.findMotion(group, index) or {}
        duration = max(0.0, float(motion.get("duration") or 0))
        native_loop = False
        try:
            data = json.loads(Path(str(motion.get("file") or "")).read_text(encoding="utf-8-sig"))
            native_loop = bool((data.get("Meta") or {}).get("Loop"))
        except (OSError, ValueError):
            pass

        def started(actual_group, actual_index):
            canvas = owner()
            if canvas is None or canvas._motion_request_serial != serial:
                return
            canvas._motion_playback = {
                "group": str(actual_group), "index": int(actual_index), "duration": duration,
                "time": 0.0, "playing": True, "native_loop": native_loop, "scrubbed": False,
            }
            canvas._motion_started_during_update = canvas._updating_motion

        def finished(actual_group, actual_index):
            canvas = owner()
            if canvas is None or canvas._motion_request_serial != serial or canvas._motion_playback is None:
                return
            if (canvas._motion_playback["group"], canvas._motion_playback["index"]) == (str(actual_group), int(actual_index)):
                canvas._motion_playback.update(playing=False, time=duration)

        self.model.StartMotion(group, index, 3, onStartMotionHandler=started, onFinishMotionHandler=finished)
        self._last_played_motion = (str(group), int(index))
        self._motion_scrub = None

    def _restart_loop_motion_if_finished(self):
        if not self._motion_loop_enabled or self._last_played_motion is None or self.model is None:
            return
        is_finished = getattr(self.model, "IsMotionFinished", None)
        if not callable(is_finished):
            return
        try:
            if not bool(is_finished()):
                return
            group, index = self._last_played_motion
            self._start_native_motion(group, index)
        except Exception:
            pass

    def _apply_advanced_params(self):
        if self.model is None or not self._advanced_params:
            return
        set_val = getattr(self.model, "SetParameterValue", None)
        if not callable(set_val):
            # If API not available, do nothing to honor request of using SetParameterValue
            return
        for pid, v in self._advanced_params.items():
            try:
                set_val(pid, float(v))
            except Exception:
                continue

    # --- Motion discovery and playback helpers ---
    @staticmethod
    def _load_motions_from_model_json(model_json_path: str) -> List[Dict[str, Any]]:
        """Parse the Live2D model json (model*.json) to discover motion groups and files.
        Returns a list of items: { 'group': str, 'index': int, 'file': str, 'display': str }
        """
        return load_live2d_motions(model_json_path)

    def listMotions(self) -> List[Dict[str, Any]]:
        """Return cached motion list. Safe to call before GL init (may be empty)."""
        return list(self._motions or [])

    def resetMotionState(self) -> None:
        """Best-effort reset before playing another motion.

        Some Live2D motions leave parameters, expressions, or poses in a modified
        state. Resetting here makes debug playback predictable without reloading
        the whole model.
        """
        if self.model is None:
            return
        self._clear_motion_clock()
        for name in (
            "StopAllMotions",
            "ResetExpressions",
            "ResetExpression",
            "ResetParameters",
            "ResetPose",
        ):
            fn = getattr(self.model, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
        try:
            self.model.Update()
        except Exception:
            pass

    def playMotion(self, group: str, index: int, reset_state: bool = True) -> bool:
        """Try to play a motion by group/index using best-effort API calls.
        Returns True if a call was attempted successfully.
        """
        if self.model is None:
            return False
        if self._motion_frozen:
            return False
        if reset_state:
            self.resetMotionState()
        try:
            self._reset_update_timestamp()
            self._start_native_motion(group, index)
            return True
        except Exception:
            pass
        return False

    def findMotion(self, group: str, index: int) -> Dict[str, Any] | None:
        for motion in self.listMotions():
            try:
                if str(motion.get("group", "")) == str(group) and int(motion.get("index", -1)) == int(index):
                    return motion
            except Exception:
                continue
        return None

    def playMotionItem(self, motion: Dict[str, Any] | None) -> Dict[str, Any] | None:
        if not motion:
            return None
        ok = self.playMotion(str(motion.get("group", "")), int(motion.get("index", 0)))
        return motion if ok else None

    def pickInteractionMotion(
        self,
        x_ratio: float = 0.5,
        y_ratio: float = 0.5,
        region_words: tuple[str, ...] | None = None,
        hit_area_name: str | None = None,
    ) -> Dict[str, Any] | None:
        """Choose a likely interaction motion from click position.

        y_ratio is top=0, bottom=1. Model-specific hit testing is not exposed by
        live2d-py here, so this uses group/file naming conventions first.
        """
        motions = self.listMotions()
        if not motions:
            return None
        y_ratio = max(0.0, min(1.0, float(y_ratio)))
        x_ratio = max(0.0, min(1.0, float(x_ratio)))
        if not region_words:
            if y_ratio < 0.36:
                region_words = ("head", "face", "hair", "eye")
            elif y_ratio < 0.76:
                region_words = ("body", "chest", "breast", "arm", "hand")
            else:
                region_words = ("leg", "foot", "skirt")
        if x_ratio < 0.33:
            side_words = ("left",)
        elif x_ratio > 0.67:
            side_words = ("right",)
        else:
            side_words = ()
        area_words = tuple(str(word).lower() for word in (region_words or ()) if word)
        if hit_area_name:
            area_words += tuple(part for part in str(hit_area_name).lower().replace("_", " ").split() if part)
        preferred_words = area_words + tuple(side_words) + ("tap", "touch", "hit", "click")

        def score(motion: Dict[str, Any]) -> int:
            text = " ".join(
                str(motion.get(key, "")).lower()
                for key in ("group", "display", "rel", "file", "sound_rel")
            )
            value = 0
            for word in preferred_words:
                if word and word in text:
                    value += 10 if word in area_words else 3
            if "tap" in text and "touch" in text:
                value += 2
            if "idle" in text:
                value -= 5
            if "start" in text:
                value -= 2
            return value

        selected = max(motions, key=score)
        if score(selected) <= 0:
            tap_words = ("tap", "touch", "hit", "click")
            for motion in motions:
                text = " ".join(str(motion.get(key, "")).lower() for key in ("group", "display", "rel", "file"))
                if any(word in text for word in tap_words):
                    return motion
            return motions[0]
        return selected

    def playInteractiveMotion(
        self,
        x_ratio: float = 0.5,
        y_ratio: float = 0.5,
        region_words: tuple[str, ...] | None = None,
        hit_area_name: str | None = None,
    ) -> Dict[str, Any] | None:
        return self.playMotionItem(
            self.pickInteractionMotion(x_ratio, y_ratio, region_words, hit_area_name)
        )

    def playDefaultTapMotion(self) -> bool:
        """Play a likely tap/touch motion when the user clicks the model."""
        return self.playInteractiveMotion(0.5, 0.5) is not None
