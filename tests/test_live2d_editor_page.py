from __future__ import annotations

import os
import hashlib
import json
import tempfile
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from tests.test_live2d_editor_session import make_model
from tests.test_live2d_mod_page_smoke import _TestSettings
from app.gui.Live2DEditorPage import Live2DEditorPage


class _Canvas(QObject):
    modelLoaded = Signal()
    drawableClicked = Signal(str)
    modelPointClicked = Signal(float, float)


class _EditorTestSettings(_TestSettings):
    def get_output_root(self):
        return str(self.root / "output")

    def get_texture_viewer_mode(self):
        return "builtin"


class _Preview(QWidget):
    def __init__(self, model_path=None, parent=None, embedded=False):
        super().__init__(parent)
        self.live2d_canvas = _Canvas(self)
        self.live2d_container = QWidget(self)
        self.model_path = model_path
        self.values = {}
        self.reloads = 0

    def load_model(self, path):
        self.model_path = path
        self.reloads += 1
        self.live2d_canvas.modelLoaded.emit()

    def set_editor_mode(self, _enabled):
        pass

    def apply_settings(self, settings):
        self.values.update(settings.get("advanced_params", {}))

    def set_rendering_active(self, active):
        self.active = active

    def set_part_opacity_overrides(self, values, defaults=None):
        self.parts = dict(defaults or {}, **values)

    def set_motion_frozen(self, _frozen):
        pass


class Live2DEditorPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="live2d-editor-ui-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.model = make_model(self.root / "source")
        settings = _EditorTestSettings(self.root)
        self.settings_patch = patch("app.gui.Live2DModPage.SettingsManager", return_value=settings)
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.psd_settings_patch = patch("app.gui.PsdReconstructionPage.SettingsManager", return_value=settings)
        self.psd_settings_patch.start()
        self.addCleanup(self.psd_settings_patch.stop)
        self.page = Live2DEditorPage()
        self.addCleanup(self._cleanup_page)

    def _cleanup_page(self):
        self.page.shutdown()
        self.page.close()
        self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def open(self):
        with patch("app.gui.Live2DPreviewWindow.Live2DPreviewWindow", _Preview):
            self.assertTrue(self.page.open_source(str(self.model)), self.page.status_label.text())

    def test_empty_editor_is_lazy_and_compact_mod_is_preserved(self):
        self.assertIsNone(self.page.preview)
        self.assertIsNone(self.page.session)
        self.assertFalse(self.page.save_button.isEnabled())
        self.assertTrue(self.page.mod_panel._compact)
        self.assertTrue(hasattr(self.page.mod_panel, "export_button"))

    def test_viewport_resize_does_not_propagate_hidden_tabs_preferred_height(self):
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.addWidget(self.page)
        try:
            host.resize(1320, 900)
            host.show()
            self.app.processEvents()
            host.resize(1040, 760)
            self.app.processEvents()
            self.assertEqual((host.width(), host.height()), (1040, 760))
            self.assertFalse(host.hasHeightForWidth())
            self.assertFalse(self.page.hasHeightForWidth())
            self.assertEqual(self.page.layout().totalHeightForWidth(991), -1)
            self.assertLessEqual(self.page.minimumSizeHint().height(), 700)
        finally:
            self.page.setParent(None)
            host.close()
            host.deleteLater()

    def test_parameter_pose_key_and_seek_dirty_semantics(self):
        self.open()
        session = self.page.session
        session.create_motion("Move", 2)
        self.page._populate_motions("Move")
        self.page._parameter_spins["ParamAngleY"].setValue(-20)
        self.assertEqual(self.page.preview.values["ParamAngleY"], -20)
        self.page._key_current_pose()
        result = self.page.save_copy(str(self.root / "copy"))
        self.assertIsNotNone(result)
        self.assertFalse(session.dirty)
        self.page.timeline._seek(.5)
        self.assertEqual(self.page.preview.values["ParamAngleY"], -20)
        self.assertFalse(session.dirty)
        self.assertFalse(self.page.open_source(str(self.root / "missing.json")))
        self.assertIs(self.page.session, session)
        self.assertTrue(self.page.open_source(result["model_path"]))
        self.assertEqual(self.page._current_motion(), "Move[0]")

    def test_watcher_external_save_reload_and_history(self):
        from PIL import Image
        self.open()
        self.page._pending_textures.add(0)
        Image.new("RGBA", (32, 32), (220, 20, 60, 255)).save(self.page.session.texture_paths[0])
        self.page._process_texture_changes()
        self.assertTrue(self.page.session.dirty)
        self.assertGreater(self.page.preview.reloads, 0)
        self.page.undo()
        self.assertFalse(self.page.session.dirty)
        self.page.redo()
        self.assertTrue(self.page.session.dirty)
        self.page.artmesh_inspector.select_entry("ArtMeshFace")
        self.page.part_opacity.setValue(0)
        self.assertEqual(self.page.preview.parts["PartFace"], 0)
        self.page.undo()
        self.assertEqual(self.page.preview.parts["PartFace"], .7)

    def test_frozen_native_update_applies_parameters_and_restores_part_defaults(self):
        from app.gui.Live2DCanvas import Live2DCanvas
        calls = []
        model = SimpleNamespace(
            _model=SimpleNamespace(Update=lambda delta: calls.append(("update", delta))),
            SetPartOpacity=lambda index, value: calls.append(("part", index, value)),
            Draw=lambda: calls.append(("draw",)),
        )
        canvas = SimpleNamespace(model=model, _part_indices={"PartFace": 0},
                                 _part_opacity_defaults={}, _part_opacity_overrides={},
                                 _motion_frozen=True, _advanced_enabled=True,
                                 _apply_advanced_params=lambda: calls.append(("parameters",)),
                                 update=lambda: None)
        with patch("app.gui.Live2DCanvas.live2d.clearBuffer"):
            Live2DCanvas.setPartOpacityOverrides(canvas, {"PartFace": 0}, {"PartFace": .7})
            Live2DCanvas.on_draw(canvas)
            self.assertEqual(calls, [("parameters",), ("part", 0, 0), ("update", 0), ("draw",)])
            calls.clear()
            Live2DCanvas.setPartOpacityOverrides(canvas, {})
            Live2DCanvas.on_draw(canvas)
            self.assertIn(("part", 0, .7), calls)

    def test_native_point_selection_uses_actual_artmesh_geometry(self):
        self.open()
        self.page.preview.live2d_canvas.modelPointClicked.emit(.5, .5)
        self.assertEqual(self.page.artmesh_inspector.current_entry().drawable_id, "ArtMeshFace")

    def _wait_psd_worker(self):
        limit = time.monotonic() + 15
        while self.page.psd_panel.is_busy() and time.monotonic() < limit:
            self.app.processEvents()
            time.sleep(.01)
        self.app.processEvents()
        self.assertFalse(self.page.psd_panel.is_busy(), self.page.psd_panel.log_text.toPlainText())
        self.assertEqual(self.page.psd_panel.progress_bar.value(), 100, self.page.psd_panel.log_text.toPlainText())

    def test_complete_psd_workflows_fit_single_column_and_keep_original_page(self):
        self.open()
        self.assertTrue(self.page.open_psd_workspace())
        self.page.resize(1040, 760)
        self.page.show()
        self.app.processEvents()
        psd = self.page.psd_panel
        self.assertTrue(psd._compact)
        self.assertEqual(psd.workflow_tabs.count(), 4)
        self.assertFalse(psd.content_splitter.isVisible())
        self.assertFalse(psd.preview_dialog.isVisible())
        for index, card in enumerate((psd.export_card, psd.repack_card, psd.preview_control_frame, psd.project_frame)):
            psd.workflow_tabs.setCurrentIndex(index)
            self.app.processEvents()
            self.assertTrue(card.isVisible(), str(index))
            self.assertEqual(psd._compact_scrolls[index].horizontalScrollBar().maximum(), 0)
            self.assertLessEqual(card.width(), psd._compact_scrolls[index].viewport().width())
        self.page.tabs.setCurrentWidget(self.page.mod_tab)
        self.app.processEvents()
        self.assertTrue(self.page.mod_panel.isVisible())
        self.assertEqual((self.page.width(), self.page.height()), (1040, 760))
        self.assertFalse(self.page.hasHeightForWidth())

    def test_psd_three_exports_use_current_snapshot_preserve_prior_baseline(self):
        from PIL import Image
        self.open()
        self.page.session.create_motion("NewAction", 2)
        self.assertTrue(self.page.open_psd_workspace())
        psd = self.page.psd_panel
        schemes = []
        old_bytes = {}
        for mode in ("mesh", "atlas-components", "atlas-artmesh"):
            psd.mode_combo.setCurrentIndex(psd._combo_index_by_data(psd.mode_combo, mode))
            psd.start_reconstruction()
            self._wait_psd_worker()
            scheme = psd.current_project.data["pose_schemes"][-1]
            schemes.append(scheme)
            self.assertEqual(scheme["mode"], mode)
            exported = psd.current_project.project_dir / scheme["psd"]
            self.assertTrue(exported.is_file())
            snapshot = psd.current_project.project_dir / scheme["editor_snapshot"]
            self.assertIn("NewAction", json.loads(snapshot.read_text(encoding="utf-8"))["FileReferences"]["Motions"])
            old_bytes[exported] = exported.read_bytes()
            if mode == "mesh":
                replacement = self.root / "new-texture.png"
                Image.new("RGBA", (32, 32), (220, 20, 70, 255)).save(replacement)
                self.assertTrue(self.page.replace_texture(0, str(replacement)))
        self.assertEqual(len({scheme["id"] for scheme in schemes}), 3)
        self.assertEqual(Image.open(psd.current_project.project_dir / schemes[0]["source_textures"][0]).getpixel((0, 0)), (30, 60, 90, 255))
        self.assertEqual(Image.open(psd.current_project.project_dir / schemes[-1]["source_textures"][0]).getpixel((0, 0)), (220, 20, 70, 255))
        for path, data in old_bytes.items():
            self.assertEqual(path.read_bytes(), data)

    def test_psd_apply_preview_return_and_project_save_reopen(self):
        from PIL import Image
        from app.core.psd_project import create_repack_dir, record_repack
        from app.core.live2d_editor_session import Live2DEditorSession
        self.open()
        source_hash = hashlib.sha256(self.model.parent.joinpath("texture.png").read_bytes()).hexdigest()
        session = self.page.session
        session.create_motion("NewAction", 2)
        self.page._populate_motions("NewAction")
        self.page._parameter_spins["ParamAngleY"].setValue(-15)
        self.assertTrue(self.page.open_psd_workspace())
        psd = self.page.psd_panel
        version_id, directory = create_repack_dir(psd.current_project)
        texture = directory / "changed.png"
        Image.new("RGBA", (32, 32), (210, 35, 110, 255)).save(texture)
        project = record_repack(psd.current_project, version_id, psd.current_project.base_model_json,
                                None, directory, [texture], texture_outputs={0: texture})
        psd.set_current_project(project)
        self.page.tabs.setCurrentWidget(self.page.mod_tab)
        result = self.page.save_copy(str(self.root / "before"))
        self.assertIsNotNone(result)
        self.assertFalse(session.dirty)
        model = self.page._psd_version_workspace("repack:" + version_id)
        self.page._open_psd_preview(str(model), str(project.project_file))
        self.assertIs(self.page.session, session)
        self.assertIsNotNone(self.page._preview_session)
        self.assertFalse(session.dirty)
        psd.preview_source_combo.setCurrentIndex(psd._combo_index_by_data(psd.preview_source_combo, "repack:" + version_id))
        self.assertFalse(session.dirty)
        self.assertTrue(self.page.return_to_current_model())
        self.assertEqual(self.page.preview.model_path, str(session.model_path))
        self.assertEqual(self.page.preview.values["ParamAngleY"], -15)
        self.assertFalse(session.dirty)
        original = session.texture_paths[0].read_bytes()
        self.assertTrue(self.page.apply_psd_version("repack:" + version_id), self.page.status_label.text())
        self.assertEqual(Image.open(session.texture_paths[0]).getpixel((0, 0)), (210, 35, 110, 255))
        self.assertIn("NewAction", session.project.motions)
        self.page.undo()
        self.assertEqual(session.texture_paths[0].read_bytes(), original)
        self.page.redo()
        result = self.page.save_copy(str(self.root / "saved"))
        self.assertIsNotNone(result, self.page.status_label.text())
        self.assertEqual(Path(result["model_path"]).name, "model.json")
        reopened = Live2DEditorSession(result["model_path"])
        self.addCleanup(reopened.close)
        self.assertTrue(reopened.psd_project.project_file.is_file())
        self.assertTrue(reopened.mod_project.project_file.is_file())
        self.assertEqual(reopened.psd_project.data["repack_history"][0]["id"], version_id)
        self.assertIn("NewAction[0]", reopened.project.motions)
        self.assertEqual(Image.open(reopened.texture_paths[0]).getpixel((0, 0)), (210, 35, 110, 255))
        self.assertEqual(hashlib.sha256(self.model.parent.joinpath("texture.png").read_bytes()).hexdigest(), source_hash)

    def test_child_dirty_busy_and_failed_open_preserve_current_project(self):
        self.open()
        self.assertTrue(self.page.open_psd_workspace())
        session = self.page.session
        self.page.save_copy(str(self.root / "clean"))
        self.page.psd_panel._project_dirty = True
        with patch("app.gui.Live2DEditorPage.QMessageBox.warning", return_value=0x00400000):  # Cancel
            self.assertFalse(self.page.confirm_discard_or_save())
        self.page.psd_panel._project_dirty = False
        self.page.psd_panel._is_busy = True
        self.assertFalse(self.page.confirm_discard_or_save())
        self.assertIsNone(self.page.save_copy(str(self.root / "busy")))
        self.page.psd_panel._is_busy = False
        replacement = make_model(self.root / "other")
        original_binding = self.page._bind_subprojects
        def fail_candidate_binding():
            if self.page.session is not session:
                raise RuntimeError("broken child project")
            return original_binding()
        with patch.object(self.page, "_bind_subprojects", side_effect=fail_candidate_binding):
            self.assertFalse(self.page.open_source(str(replacement)))
        self.assertIs(self.page.session, session)
        self.assertEqual(self.page.preview.model_path, str(session.model_path))
        self.assertTrue(self.page.psd_panel.current_project.project_file.is_relative_to(session.root))
        self.assertFalse(session.dirty)
        self.assertEqual(self.page.last_open_error, "broken child project")

    def test_psd_picker_imports_recent_and_switches_owned_projects_without_writing_sources(self):
        from app.core.psd_project import create_project_from_source
        self.open()
        first = create_project_from_source(self.model, "PSD Alpha", output_root=self.root / "legacy")
        second = create_project_from_source(self.model, "PSD Beta", output_root=self.page.psd_panel.settings_manager.get_output_dir("psd_projects"))
        original_hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                           for root in (first.project_dir, second.project_dir, self.model.parent)
                           for path in root.rglob("*") if path.is_file()}
        psd = self.page.psd_panel
        psd.settings_manager.set("psd.project_files", [str(first.project_file)])
        self.assertTrue(self.page.open_psd_workspace())
        psd.workflow_tabs.setCurrentIndex(3)
        self.page.resize(1040, 760)
        self.page.show()
        self.app.processEvents()
        self.assertTrue(psd.project_combo.isVisible())
        def select(path):
            index = psd._combo_index_by_data(psd.project_combo, str(path))
            self.assertGreaterEqual(index, 0, psd.discover_project_files())
            psd.project_combo.setCurrentIndex(index)
            self.app.processEvents()
        select(first.project_file)
        owned_first = psd.current_project.project_file
        self.assertTrue(owned_first.is_relative_to(self.page.session.root / "psd"))
        self.assertNotEqual(owned_first, first.project_file)
        psd.mode_combo.setCurrentIndex(psd._combo_index_by_data(psd.mode_combo, "atlas-artmesh"))
        psd.mark_project_dirty()
        select(second.project_file)
        owned_second = psd.current_project.project_file
        self.assertNotEqual(owned_second, owned_first)
        select(owned_first)
        self.assertEqual(psd.current_project.project_file, owned_first)
        self.assertEqual(psd.mode_combo.currentData(), "atlas-artmesh")
        select(owned_second)
        self.assertEqual(psd.current_project.project_file, owned_second)
        self.assertEqual(psd._compact_scrolls[3].horizontalScrollBar().maximum(), 0)
        for path, digest in original_hashes.items():
            self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(), digest)
        self.assertEqual(psd.settings_manager.get("psd.project_files"), [str(first.project_file)])


if __name__ == "__main__":
    unittest.main()
