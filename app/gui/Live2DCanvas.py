import math
import numpy as np
from typing import Optional, List, Dict, Any

from PySide6.QtOpenGL import QOpenGLWindow
from PySide6.QtCore import Qt
import OpenGL.GL as GL
from abc import abstractmethod

import live2d.v3 as live2d
from live2d.utils.canvas import Canvas

from app.core.model.motions import load_live2d_motions

live2d.init()

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

    vao = GL.glGenVertexArrays(1)
    vbo = GL.glGenBuffers(1)
    uvbo = GL.glGenBuffers(1)

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
    old_fbo = GL.glGetIntegerv(GL.GL_FRAMEBUFFER_BINDING)
    fbo = GL.glGenFramebuffers(1)

    GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)

    texture = GL.glGenTextures(1)
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

    def __init__(self):
        super().__init__()
        self.__canvas_opacity = 1.0
        self.__rotation_angle = 0.0
        self.__model_scale = 1.0
        self.__model_offset = (0.0, 0.0)
        # Background control
        self.__use_background = False
        self.__bg_color = (0.0, 0.0, 0.0, 0.0)
        # High-DPI handling
        self._dpr = 1.0
        self._canvas_framebuffer = None
        self._canvas_texture = None
        self._fbo_width = 0
        self._fbo_height = 0

    def __create_program(self):
        vertex_shader = """#version 330 core
        layout(location = 0) in vec2 a_position;
        layout(location = 1) in vec2 a_texCoord;

        out vec2 v_texCoord;
        uniform float rotation_angle;
        uniform float model_scale;
        uniform vec2 model_offset;

        void main() {
            gl_Position = vec4(a_position, 0.0, 1.0);

            float angle = radians(rotation_angle);
            mat2 rotationMatrix = mat2(cos(angle), -sin(angle),
                                       sin(angle), cos(angle));

            vec2 centeredTexCoord = a_texCoord - vec2(0.5, 0.5);
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

    def __create_canvas_framebuffer(self):
        # Determine device-pixel size for FBO
        self._dpr = float(self.devicePixelRatioF()) if hasattr(self, 'devicePixelRatioF') else float(self.devicePixelRatio())
        fb_w = max(1, int(math.ceil(self.width() * self._dpr)))
        fb_h = max(1, int(math.ceil(self.height() * self._dpr)))
        # Recreate only if size changed
        if self._canvas_framebuffer and (fb_w == self._fbo_width and fb_h == self._fbo_height):
            return
        # Delete old
        self.__delete_canvas_framebuffer()
        # Create new
        self._canvas_framebuffer, self._canvas_texture = create_canvas_framebuffer(fb_w, fb_h)
        self._fbo_width, self._fbo_height = fb_w, fb_h

    def __draw_on_canvas(self):
        # Draw model into offscreen FBO at device-pixel resolution
        old_fbo = GL.glGetIntegerv(GL.GL_FRAMEBUFFER_BINDING)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self._canvas_framebuffer)
        # Ensure viewport matches FBO size
        GL.glViewport(0, 0, int(self._fbo_width), int(self._fbo_height))
        # Keep FBO transparent so compositing in second pass works
        GL.glClearColor(0.0, 0.0, 0.0, 0.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        self.on_draw()
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, old_fbo)

    def initializeGL(self):
        self.__create_program()
        self.__create_vao()
        self.__create_canvas_framebuffer()
        self.on_init()
        # Ensure model has correct initial size in pixels
        self.on_resize(self._fbo_width, self._fbo_height)

    def resizeGL(self, w, h):
        # Recreate FBO when widget size or DPR changes
        old_dpr = self._dpr
        self._dpr = float(self.devicePixelRatioF()) if hasattr(self, 'devicePixelRatioF') else float(self.devicePixelRatio())
        if (int(math.ceil(w * self._dpr)) != self._fbo_width) or (int(math.ceil(h * self._dpr)) != self._fbo_height) or (self._dpr != old_dpr):
            self.__create_canvas_framebuffer()
        # Notify subclass with pixel sizes
        self.on_resize(self._fbo_width, self._fbo_height)

    def paintGL(self):
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
        GL.glProgramUniform4f(self._program, self._bg_color_loc, *self.__bg_color)
        GL.glProgramUniform1i(self._program, self._use_bg_loc, 1 if self.__use_background else 0)

        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self._canvas_texture)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 6)

        GL.glBindVertexArray(0)

    def setCanvasOpacity(self, value):
        self.__canvas_opacity = value
        self.update()

    def setRotationAngle(self, angle):
        self.__rotation_angle = float(angle)
        self.update()

    def setModelTransform(self, scale: float, offset_x: float, offset_y: float):
        """Apply responsive model scale and offsets at the final composition pass."""
        self.__model_scale = max(0.25, min(3.0, float(scale)))
        self.__model_offset = (
            max(-1.0, min(1.0, float(offset_x))),
            max(-1.0, min(1.0, -float(offset_y))),
        )
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
            self.__bg_color = (0.98, 0.985, 0.992, 1.0)
            self.update()
            return
        if qcolor is not None:
            # QColor -> normalized RGBA
            r, g, b, a = qcolor.redF(), qcolor.greenF(), qcolor.blueF(), 1.0
            self.__bg_color = (r, g, b, a)
        else:
            self.__bg_color = (0.0, 0.0, 0.0, 1.0)
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
    def __init__(self, model_path=None, embedded: bool = False):
        super().__init__()
        self.model_path = model_path
        self._embedded = bool(embedded)
        self.model: Optional[live2d.LAppModel] = None
        # tool for controlling model opacity
        self.canvas: Optional[Canvas] = None
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
        self._render_timer_id = None
        self._gl_initialized = False
        # Cached motions metadata
        self._motions: List[Dict[str, Any]] = []

    def on_init(self):
        live2d.glInit()
        # must be created after opengl context is configured
        self.canvas = Canvas()
        self._gl_initialized = True
        if self.model_path:
            self._load_model_with_current_context(self.model_path)
        self._render_timer_id = self.startTimer(int(1000 / 60))

    def _load_model_with_current_context(self, model_path: str):
        model = live2d.LAppModel()
        model.LoadModelJson(model_path)
        if self._fbo_width > 0 and self._fbo_height > 0:
            model.Resize(self._fbo_width, self._fbo_height)
        self.model = model
        self.model_path = model_path
        try:
            model.SetAutoBlinkEnable(self._auto_blink_enabled)
            model.SetAutoBreathEnable(self._auto_breath_enabled)
        except Exception:
            pass
        self._motion_frozen = False
        self._last_played_motion = None
        self._advanced_params = {}
        try:
            self._motions = self._load_motions_from_model_json(model_path)
        except Exception:
            self._motions = []

    def loadModel(self, model_path: str):
        """Load a model into the already-created OpenGL widget."""
        if not model_path:
            raise ValueError("A Live2D model path is required.")
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
        finally:
            if context_current:
                self.doneCurrent()
        self.model_path = None
        self._motions = []
        self._last_played_motion = None
        self._motion_frozen = False
        self._advanced_params = {}
        self.update()

    def timerEvent(self, a0):
        self.update()

    def on_draw(self):
        live2d.clearBuffer()
        if self.model is None:
            return
        if not self._motion_frozen:
            self.model.Update()
            self._restart_loop_motion_if_finished()
        # Apply advanced parameter overrides each frame if enabled
        if self._advanced_enabled:
            try:
                self._apply_advanced_params()
            except Exception:
                pass
        self.model.Draw()

    def on_resize(self, width: int, height: int):
        if self.model is not None:
            self.model.Resize(width, height)

    # --- Mouse tracking and follow implementation ---
    def setMouseTracking(self, enable: bool) -> None:  # type: ignore[override]
        # QWindow receives pointer move events directly; keep this flag for
        # Live2D's optional gaze-follow behaviour.
        self._mouse_follow_enabled = bool(enable)

    def mouseMoveEvent(self, event):
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
            try:
                self.playDefaultTapMotion()
            except Exception:
                pass
        return super().mousePressEvent(event)

    def leaveEvent(self, event):
        # Reset follow when cursor leaves
        if self._mouse_follow_enabled and self.model is not None:
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
        self._motion_frozen = bool(frozen)
        self.update()

    def isMotionFrozen(self) -> bool:
        return self._motion_frozen

    def setMotionLoop(self, enabled: bool):
        self._motion_loop_enabled = bool(enabled)

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
            self.model.StartMotion(group, index, 3)
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
            self.model.StartMotion(group, index, 3)
            self._last_played_motion = (str(group), int(index))
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
