from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app.gui.Live2DCanvas import ADPOpenGLCanvas


class Live2DCanvasLifecycleTests(unittest.TestCase):
    def test_model_ready_aspect_change_does_not_touch_another_current_context(self):
        fake = SimpleNamespace(_source_aspect_ratio=1.0, update=Mock(), context=Mock(), isValid=Mock())
        setattr(fake, "_ADPOpenGLCanvas__create_canvas_framebuffer", Mock())
        ADPOpenGLCanvas.setSourceAspectRatio(fake, 2.0)
        self.assertEqual(fake._source_aspect_ratio, 2.0)
        fake.update.assert_called_once()
        fake.context.assert_not_called()
        fake.isValid.assert_not_called()
        fake._ADPOpenGLCanvas__create_canvas_framebuffer.assert_not_called()

    def test_resize_schedules_owned_paint_instead_of_reallocating_on_surface_teardown(self):
        fake = SimpleNamespace(devicePixelRatioF=Mock(return_value=1.5), update=Mock(), on_resize=Mock())
        setattr(fake, "_ADPOpenGLCanvas__create_canvas_framebuffer", Mock())
        ADPOpenGLCanvas.resizeGL(fake, 640, 480)
        self.assertEqual(fake._dpr, 1.5)
        fake.update.assert_called_once()
        fake.on_resize.assert_not_called()
        fake._ADPOpenGLCanvas__create_canvas_framebuffer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
