import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import unittest
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication
from app.gui.Live2DCanvas import ADPOpenGLCanvas
from app.gui.PreviewPage import Live2DSettingsPanel

class NativeViewportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def canvas_size(self, *, zoom=1, dpr=1, aa=False, aspect=1):
        host=SimpleNamespace(width=lambda:1920,height=lambda:1080,_dpr=dpr,
            _source_aspect_ratio=aspect,_antialias=aa,
            _ADPOpenGLCanvas__model_scale=zoom)
        return ADPOpenGLCanvas._desired_canvas_size(host)

    def test_hidpi_and_zoom_render_more_source_pixels(self):
        base=self.canvas_size()
        hidpi=self.canvas_size(dpr=2)
        zoom=self.canvas_size(zoom=2)
        self.assertEqual(hidpi,(base[0]*2,base[1]*2))
        self.assertGreater(zoom[0],base[0])
        self.assertGreater(self.canvas_size(aa=True)[0],base[0])

    def test_large_zoom_is_bounded_and_preserves_aspect(self):
        for aspect in (.1,1,10):
            width,height=self.canvas_size(dpr=3,zoom=4,aa=True,aspect=aspect)
            self.assertLessEqual(max(width,height),8192)
            self.assertLessEqual(width*height,32*1024*1024+8192)
            self.assertAlmostEqual(width/height,aspect,delta=.01)

    def test_fit_resets_view_without_disabling_antialias(self):
        panel=Live2DSettingsPanel()
        panel.scale_slider.setValue(25)
        panel.rotation_slider.setValue(90)
        panel.position_x_spinbox.setValue(30)
        panel.position_y_spinbox.setValue(-20)
        seen=[]
        panel.settingsChanged.connect(seen.append)
        panel.fit_model_to_view()
        self.assertEqual(len(seen),1)
        state=seen[0]
        self.assertEqual(state['model_scale'],1)
        self.assertEqual(state['model_rotation'],0)
        self.assertEqual(state['model_offset_x'],0)
        self.assertEqual(state['model_offset_y'],0)
        self.assertTrue(state['antialias'])
        panel.deleteLater()

if __name__=='__main__':unittest.main()
