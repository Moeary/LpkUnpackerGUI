from __future__ import annotations

import hashlib
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from app.core.live2d_editor_session import Live2DEditorSession
from app.gui.editor_dialogs import ThemedEditorDialog
from app.gui.live2d_skin_controls import SkinNameDialog, skin_text
from tests import test_live2d_editor_page as fixtures
from tests.test_live2d_editor_session import make_model


class Live2DSkinWorkspaceTests(unittest.TestCase):
    # Reuse the isolated source/settings fixture, without inheriting unrelated
    # tests or creating a second QApplication in the full test process.
    setUpClass = classmethod(fixtures.Live2DEditorPageTests.setUpClass.__func__)
    setUp = fixtures.Live2DEditorPageTests.setUp
    _cleanup_page = fixtures.Live2DEditorPageTests._cleanup_page
    open = fixtures.Live2DEditorPageTests.open
    _wait_psd_worker = fixtures.Live2DEditorPageTests._wait_psd_worker

    def _show(self):
        self.page.resize(1040, 760)
        self.page.show()
        self.page.activateWindow()
        self.app.processEvents()

    def _click_tab(self, tabs, index):
        item = tabs.pivot.widget(tabs._keys[index])
        tabs.tab_scroll.ensureWidgetVisible(item, 0, 0)
        self.app.processEvents()
        QTest.mouseClick(item, Qt.LeftButton)
        self.app.processEvents()
        self.assertEqual(tabs.currentIndex(), index)

    def _paint_texture(self, name="pink", color=(240, 20, 90, 255)):
        path = self.root / f"{name}.png"
        Image.new("RGBA", (32, 32), color).save(path)
        return path

    def _source_hashes(self):
        return {str(path.relative_to(self.model.parent)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.model.parent.rglob("*") if path.is_file()}

    def _export_psd(self, mode="mesh"):
        psd = self.page.psd_panel
        psd.workflow_tabs.setCurrentIndex(0)
        psd.mode_combo.setCurrentIndex(psd._combo_index_by_data(psd.mode_combo, mode))
        QTest.mouseClick(psd.reconstruct_button, Qt.LeftButton)
        self._wait_psd_worker()
        return psd.current_project.data["pose_schemes"][-1]

    def test_psd_repack_creates_skin_and_full_export_keeps_current_motion(self):
        from psd_tools import PSDImage
        from psd_tools.api.layers import PixelLayer
        from psd_tools.constants import Compression, Tag

        before = self._source_hashes()
        self.open()
        self._show()
        session = self.page.session
        session.create_motion("CurrentAction", 2)
        session.set_keyframes("CurrentAction", "ParamAngleY", [{"time": 0, "value": -10}, {"time": 2, "value": 10}])
        self.page._populate_motions("CurrentAction")
        self._click_tab(self.page.tabs, 3)  # PSD needs no second model picker.
        psd = self.page.psd_panel
        self.assertIsNotNone(psd.current_project)
        self.assertTrue(psd.current_project.project_dir.is_relative_to(session.root / "psd"))
        scheme = self._export_psd()
        self.assertEqual(scheme["skin_source"]["id"], "original")
        psd_path = psd.current_project.project_dir / scheme["psd"]
        metadata_path = psd.current_project.project_dir / scheme["metadata"]
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        target = metadata["layers"][0]
        document = PSDImage.open(psd_path)
        original = next(layer for layer in document if layer.layer_id == target["layer_id"])
        image = original.topil().convert("RGBA")
        position = min(((x, y) for y in range(image.height) for x in range(image.width)
                        if image.getpixel((x, y))[3] > 240),
                       key=lambda p: (p[0] - image.width / 2) ** 2 + (p[1] - image.height / 2) ** 2)
        for y in range(position[1] - 1, position[1] + 2):
            for x in range(position[0] - 1, position[0] + 2):
                image.putpixel((x, y), (0, 255, 220, 255))
        replacement = PixelLayer.frompil(image, document, name=original.name,
                                        top=original.top, left=original.left, compression=Compression.RAW)
        replacement.tagged_blocks.set_data(Tag.LAYER_ID, target["layer_id"])
        document.remove(original)
        document.save(psd_path)
        baseline = session.texture_paths[0].read_bytes()
        psd.workflow_tabs.setCurrentIndex(1)
        psd.metadata_edit.setText(str(metadata_path))
        psd._compact_scrolls[1].ensureWidgetVisible(psd.single_repack_button)
        self.app.processEvents()
        QTest.mouseClick(psd.single_repack_button, Qt.LeftButton)
        self._wait_psd_worker()
        self.assertTrue(psd.current_project.data["repack_history"], psd.log_text.toPlainText())
        self.assertNotEqual(session.texture_paths[0].read_bytes(), baseline, self.page.status_label.text())
        new_skin = next(item for item in session.list_skins() if item["id"] == session.active_skin_id)
        self.assertEqual(new_skin["source_kind"], "psd")
        self.assertEqual(new_skin["source"]["metadata"]["export_skin"]["id"], "original")
        changed = session.texture_paths[0].read_bytes()
        self.page.undo()
        self.assertEqual(session.active_skin_id, "original")
        self.assertEqual(session.texture_paths[0].read_bytes(), baseline)
        self.assertNotIn(new_skin["id"], [item["id"] for item in session.list_skins()])
        self.page.redo()
        self.assertEqual(session.active_skin_id, new_skin["id"])
        self.assertEqual(session.texture_paths[0].read_bytes(), changed)
        # Ordinary export does not create a ViewerEX subproject.
        self._click_tab(self.page.tabs, 4)
        self.assertIsNone(session.mod_project)
        self.assertFalse(self.page.mod_panel.isVisible())
        self.assertFalse(self.page._in_viewer_workspace())
        with patch("app.gui.Live2DEditorPage.QFileDialog.getExistingDirectory", return_value=str(self.root / "exports")):
            QTest.mouseClick(self.page.export_model_button, Qt.LeftButton)
        exports = list((self.root / "exports").glob("*/model.json"))
        self.assertEqual(len(exports), 1, self.page.status_label.text())
        output = exports[0]
        content = json.loads(output.read_text(encoding="utf-8"))
        self.assertIn("CurrentAction", content["FileReferences"]["Motions"])
        self.assertEqual((output.parent / content["FileReferences"]["Textures"][0]).read_bytes(), changed)
        self.assertFalse((output.parent / "lpk_live2d_editor.json").exists())
        self.assertTrue(session.dirty)
        saved = self.page.save_copy(str(self.root / "project"))
        self.assertIsNotNone(saved, self.page.status_label.text())
        reopened = Live2DEditorSession(saved["model_path"])
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.active_skin_id, new_skin["id"])
        self.assertEqual(reopened.texture_paths[0].read_bytes(), changed)
        self.assertEqual(len(reopened.list_skins()), 2)
        self.assertIn("CurrentAction[0]", reopened.project.motions)
        self.assertTrue(reopened.psd_project.data["repack_history"])
        self.assertFalse(reopened.dirty)
        self.assertEqual(before, self._source_hashes())

    def test_dropped_foreign_model_requires_explicit_mapping_and_does_not_switch_project(self):
        from app.gui.Live2DModPage import TextureMappingDialog
        self.open()
        self._show()
        self._click_tab(self.page.tabs, 2)
        session = self.page.session
        source = make_model(self.root / "foreign")
        source.parent.joinpath("model.moc3").write_bytes(b"another UV model")
        self._paint_texture().replace(source.parent / "texture.png")
        foreign_before = {path.name: path.read_bytes() for path in source.parent.iterdir()}
        self.assertFalse(session.inspect_skin_source(source)["automatic_mapping"])
        seen = []

        def map_explicitly(dialog):
            self.assertIsInstance(dialog, ThemedEditorDialog)
            self.assertIs(dialog.parentWidget(), self.page.window())
            combo = dialog.table.cellWidget(0, 2)
            self.assertEqual(str(combo.currentData()), "-1")
            combo.setCurrentIndex(combo.findData("0"))
            seen.append(dialog.mappings())
            return QDialog.Accepted

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(source))])
        surface = self.page.texture_scroll.viewport()
        enter = QDragEnterEvent(surface.rect().center(), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        drop = QDropEvent(QPointF(surface.rect().center()), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        with patch.object(self.page, "_skin_name_dialog", return_value="Mapped foreign skin"), \
             patch.object(TextureMappingDialog, "exec", map_explicitly):
            QApplication.sendEvent(surface, enter)
            QApplication.sendEvent(surface, drop)
        self.assertTrue(drop.isAccepted())
        self.assertEqual(len(seen), 1)
        self.assertIs(self.page.session, session)
        self.assertEqual(session.source_path, self.model.resolve())
        self.assertEqual(Image.open(session.texture_paths[0]).getpixel((0, 0)), (240, 20, 90, 255))
        self.assertEqual(len(session.list_skins()), 2)
        self.page.undo()
        self.assertEqual(len(session.list_skins()), 1)
        self.assertFalse(session.dirty)
        self.assertEqual(foreign_before, {path.name: path.read_bytes() for path in source.parent.iterdir()})

    def test_switch_keeps_external_working_edit_and_skin_catalog_undo(self):
        self.open()
        self._show()
        self._click_tab(self.page.tabs, 2)
        self.assertFalse(self.page.session.dirty)
        self.assertFalse(self.page.skin_controls.rename_button.isEnabled())
        self.assertFalse(self.page.skin_controls.delete_button.isEnabled())
        path = self._paint_texture()
        imported = self.page.import_skin(None, "Pink", mapping={0: path}, source_kind="textures")
        self.assertIsNotNone(imported)
        edit = self._paint_texture("external", (50, 230, 20, 255)).read_bytes()
        self.page.session.texture_paths[0].write_bytes(edit)
        self.page._pending_textures.add(0)
        self.page._process_texture_changes()
        self.assertTrue(self.page.session.skin_modified)
        self.assertEqual(self.page.skin_controls.state_label.text(), skin_text("editor.skin.modified", name="Pink"))
        self.page.skin_controls.combo.setCurrentIndex(self.page.skin_controls.combo.findData("original"))
        records = self.page.session.list_skins()
        working = next(item for item in records if item["source_kind"] == "working")
        self.assertEqual(self.page.session.skin_texture_paths(working["id"])[0].read_bytes(), edit)
        self.assertEqual(self.page.session.active_skin_id, "original")
        self.page.undo()
        self.assertEqual(self.page.session.active_skin_id, imported["id"])
        self.assertEqual(self.page.session.texture_paths[0].read_bytes(), edit)
        self.assertTrue(self.page.session.skin_modified)
        self.assertNotIn(working["id"], [item["id"] for item in self.page.session.list_skins()])
        self.page.redo()
        self.assertEqual(self.page.session.active_skin_id, "original")
        self.assertIn(working["id"], [item["id"] for item in self.page.session.list_skins()])

    def test_ctrl_z_y_keep_valid_motion_parameter_time_mesh_filter_and_scroll(self):
        inventory = self.model.parent / "live2d_parameter_inventory.json"
        data = json.loads(inventory.read_text(encoding="utf-8"))
        data["parameters"].extend({"id": f"Param{i:02}", "name": f"Parameter {i}", "min": -30, "max": 30, "default": 0}
                                  for i in range(45))
        inventory.write_text(json.dumps(data), encoding="utf-8")
        Image.new("RGBA", (32, 32), (60, 90, 120, 255)).save(self.model.parent / "second.png")
        document = json.loads(self.model.read_text(encoding="utf-8"))
        document["FileReferences"]["Textures"].append("second.png")
        self.model.write_text(json.dumps(document), encoding="utf-8")
        sidecar = self.model.parent / "drawables.json"
        meshes = json.loads(sidecar.read_text(encoding="utf-8"))
        meshes["drawables"] = [dict(meshes["drawables"][0], id=f"ArtMeshFace{i:02}", texture_index=i % 2) for i in range(70)]
        sidecar.write_text(json.dumps(meshes), encoding="utf-8")
        self.open()
        self._show()
        session = self.page.session
        session.create_motion("Stable", 3)
        session.set_keyframes("Stable", "Param32", [{"time": 0, "value": -5}, {"time": 3, "value": 5}])
        self.page._populate_motions("Stable")
        self.page.parameter_search.setText("Param")
        row = next(i for i in range(self.page.parameter_table.rowCount()) if self.page.parameter_table.item(i, 0).data(Qt.UserRole) == "Param32")
        self.page.parameter_table.setCurrentCell(row, 0)
        self._click_tab(self.page.tabs, 1)
        inspector = self.page.artmesh_inspector
        inspector.select_entry("ArtMeshFace43")
        inspector.texture_combo.setCurrentIndex(inspector.texture_combo.findData(1))
        self.page.texture_combo.setCurrentIndex(1)
        self.page.timeline.set_time(1.4)
        self.page.part_opacity.setValue(.2)
        self.app.processEvents()
        bars = [self.page.parameter_table.verticalScrollBar(), inspector.entry_list.verticalScrollBar()]
        for bar in bars:
            self.assertGreater(bar.maximum(), 0)
            bar.setValue(min(18, bar.maximum()))
        scroll_values = [bar.value() for bar in bars]
        self.page.timeline.canvas.setFocus()
        QTest.keyClick(self.page.timeline.canvas, Qt.Key_Z, Qt.ControlModifier)
        self.app.processEvents()
        self.assertEqual(session.part_overrides, {})
        QTest.keyClick(self.page.timeline.canvas, Qt.Key_Y, Qt.ControlModifier)
        self.app.processEvents()
        self.assertAlmostEqual(session.part_overrides["PartFace"], .2)
        self.assertEqual(self.page._current_motion(), "Stable")
        self.assertEqual(self.page._selected_parameter, "Param32")
        self.assertAlmostEqual(self.page.timeline.current_time, 1.4)
        self.assertEqual(self.page.parameter_search.text(), "Param")
        self.assertEqual(inspector.current_entry().drawable_id, "ArtMeshFace43")
        self.assertEqual(inspector.texture_combo.currentData(), 1)
        self.assertEqual(self.page.texture_combo.currentIndex(), 1)
        self.assertEqual([bar.value() for bar in bars], scroll_values)

    def test_undo_created_motion_falls_back_and_hidden_mesh_reloads_current_skin(self):
        self.open()
        self._show()
        session = self.page.session
        session.create_motion("Stable", 2)
        session.create_motion("Transient", 3)
        self.page._populate_motions("Transient")
        self.page.timeline.set_time(2.7)
        self.page.undo()
        self.assertNotIn("Transient", session.project.motions)
        self.assertEqual(self.page._current_motion(), "Stable")
        self.assertEqual(self.page.timeline.current_time, 2)
        self.assertEqual(self.page._selected_parameter, "ParamAngleY")
        self._click_tab(self.page.tabs, 1)
        self.page.artmesh_inspector.select_entry("ArtMeshFace")
        self._click_tab(self.page.tabs, 2)
        record = self.page.import_skin(None, "Pink", mapping={0: self._paint_texture()}, source_kind="textures")
        self.assertIsNone(self.page._inspector_session)
        self._click_tab(self.page.tabs, 1)
        inspector = self.page.artmesh_inspector
        self.assertIs(self.page._inspector_session, session)
        self.assertEqual(inspector.current_entry().drawable_id, "ArtMeshFace")
        self.assertEqual(Image.open(inspector.texture_paths[0]).getpixel((0, 0)), (240, 20, 90, 255))
        local = session.local_artmesh_image(inspector.current_entry().raw)
        self.assertIn((240, 20, 90, 255), set(local.get_flattened_data()))
        self.assertEqual(session.active_skin_id, record["id"])

    def test_old_psd_version_provenance_does_not_follow_latest_export(self):
        from app.core.psd_project import create_repack_dir, record_repack
        self.open()
        self._show()
        self.assertTrue(self.page.open_psd_workspace())
        psd = self.page.psd_panel
        skin_a = self.page.capture_skin("A")
        scheme_a = self._export_psd()
        version_id, directory = create_repack_dir(psd.current_project, scheme_a["id"])
        output = directory / "changed.png"
        Image.new("RGBA", (32, 32), (220, 50, 10, 255)).save(output)
        project = record_repack(psd.current_project, version_id, psd.current_project.project_dir / scheme_a["psd"],
                                None, directory, [output], scheme_id=scheme_a["id"], texture_outputs={0: output})
        psd.set_current_project(project)
        self.assertTrue(self.page.replace_texture(0, str(self._paint_texture())))
        skin_b = self.page.capture_skin("B")
        self._export_psd()
        self.assertEqual(psd.pending_skin_context["id"], skin_b["id"])
        record = self.page.save_psd_version_as_skin("repack:" + version_id, "Old A version")
        self.assertIsNotNone(record, self.page.status_label.text())
        self.assertEqual(record["source"]["metadata"]["export_skin"]["id"], skin_a["id"])
        unknown_id, directory = create_repack_dir(psd.current_project)
        output = directory / "external.png"
        Image.new("RGBA", (32, 32), (60, 140, 20, 255)).save(output)
        psd.set_current_project(record_repack(psd.current_project, unknown_id, self.root / "old-external.psd",
                                             None, directory, [output], texture_outputs={0: output}))
        record = self.page.save_psd_version_as_skin("repack:" + unknown_id, "Unknown historical source")
        self.assertIsNone(record["source"]["metadata"]["export_skin"])

    def test_five_main_tabs_and_viewer_advanced_are_mouse_reachable_at_1040(self):
        self.open()
        self._show()
        for index in range(5):
            self._click_tab(self.page.tabs, index)
            self.assertTrue(self.page.tabs.widget(index).isVisible())
        self.assertIsNone(self.page.session.mod_project)
        self._click_tab(self.page.export_tabs, 1)
        self.assertTrue(self.page.mod_panel.isVisible())
        self.assertTrue(self.page._in_viewer_workspace())
        self.assertIsNotNone(self.page.session.mod_project)
        for index in range(self.page.mod_panel.workflow_tabs.count()):
            self._click_tab(self.page.mod_panel.workflow_tabs, index)
        self._click_tab(self.page.export_tabs, 0)
        self.assertFalse(self.page._in_viewer_workspace())
        self.assertFalse(self.page.mod_panel.isVisible())
        self._click_tab(self.page.tabs, 2)
        for width in (1320, 1040):
            self.page.resize(width, 760)
            self.app.processEvents()
            image = self.page.texture_image
            self.assertLessEqual(image.pixmap().width(), image.contentsRect().width())
            self.assertLessEqual(image.pixmap().height(), image.contentsRect().height())
            self.assertLess(image.geometry().bottom(), self.page.replace_texture_button.geometry().top())
        for widget in (self.page.skin_controls.import_model_button, self.page.skin_controls.import_textures_button,
                       self.page.skin_controls.capture_button, self.page.skin_controls.combo):
            self.assertTrue(widget.isVisible())
            self.assertGreater(widget.width(), 20)
            self.assertLessEqual(widget.mapTo(self.page.texture_tab, widget.rect().topRight()).x(), self.page.texture_tab.width())
        self.assertEqual((self.page.width(), self.page.height()), (1040, 760))
        self.assertFalse(self.page.hasHeightForWidth())

    def test_skin_name_errors_remain_editable_and_cancel_does_not_change_catalog(self):
        self.open()
        self._show()
        self._click_tab(self.page.tabs, 2)
        dialog = SkinNameDialog("Skin", self.page, {"Original", "Pink"})
        self.addCleanup(dialog.deleteLater)
        dialog.show()
        self.app.processEvents()
        QTest.mouseClick(dialog.yesButton, Qt.LeftButton)
        self.assertTrue(dialog.isVisible())
        self.assertTrue(dialog.error_label.isVisible())
        dialog.name_edit.setText("Pink")
        QTest.keyClick(dialog.name_edit, Qt.Key_Return)
        self.assertTrue(dialog.isVisible())
        dialog.name_edit.setText("Blue")
        QTest.mouseClick(dialog.cancelButton, Qt.LeftButton)
        self.assertFalse(dialog.isVisible())
        self.assertFalse(self.page.session.dirty)
        self.assertEqual(len(self.page.session.list_skins()), 1)
        self.assertIsNone(QApplication.activeModalWidget())


if __name__ == "__main__":
    unittest.main()
