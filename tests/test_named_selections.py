from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from threading import Event
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
from PIL import Image
from PySide6.QtWidgets import QApplication

from app.core.animation_editing import AnimationEditingError
from app.core.live2d_editor_session import Live2DEditorSession
from app.core.psd_selection import selection_uv_constraints
from app.gui.ArtMeshInspector import ArtMeshInspector
from app.gui.artmesh_selection_tools import ArtMeshSelectionTools
from app.gui.artmesh_selection_tools import SelectionNameDialog
from tests.test_live2d_editor_session import make_model


class NamedSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.model = make_model(self.root / "source")
        self.session = Live2DEditorSession(self.model)
        self.addCleanup(self.session.close)

    def test_save_rename_delete_undo_and_portable_reopen(self):
        source = {p.name: p.read_bytes() for p in self.model.parent.iterdir() if p.is_file()}
        self.session.save_named_selection("眼睛", ["ArtMeshFace", "ArtMeshFace"])
        self.assertTrue(self.session.dirty)
        sets = self.session.named_selections()
        sets["眼睛"].clear()
        self.assertEqual(self.session.named_selections(), {"眼睛": ["ArtMeshFace"]})
        self.session.rename_named_selection("眼睛", "面部")
        self.session.delete_named_selection("面部")
        self.session.undo()
        self.session.undo()
        self.assertEqual(self.session.named_selections(), {"眼睛": ["ArtMeshFace"]})
        self.session.undo()
        self.assertFalse(self.session.dirty)
        self.session.redo()
        saved = self.session.save_copy(self.root / "saved")
        self.assertFalse(self.session.dirty)
        (self.root / "saved").rename(self.root / "moved")
        restored = Live2DEditorSession(self.root / "moved" / "model.json")
        self.addCleanup(restored.close)
        self.assertEqual(restored.named_selections(), {"眼睛": ["ArtMeshFace"]})
        self.assertFalse(restored.dirty)
        self.assertEqual(source, {p.name: p.read_bytes() for p in self.model.parent.iterdir() if p.is_file()})
        self.session.save_copy(self.root / "ordinary", include_editor_metadata=False, include_projects=False, include_skins=False)
        self.assertFalse((self.root / "ordinary" / "lpk_live2d_editor.json").exists())

    def test_validation_and_missing_ids_are_non_destructive(self):
        self.session.save_named_selection("Face", ["ArtMeshFace"])
        count = len(self.session._undo)
        self.session.save_named_selection("Face", ["ArtMeshFace"], replace=True)
        self.assertEqual(len(self.session._undo), count)
        for name, ids in (("Face", ["ArtMeshFace"]), ("face", ["ArtMeshFace"]),
                          (" ", ["ArtMeshFace"]), ("x" * 81, ["ArtMeshFace"]),
                          ("other", []), ("other", ["missing"])):
            with self.assertRaises(AnimationEditingError):
                self.session.save_named_selection(name, ids)
        self.assertEqual(len(self.session._undo), count)
        self.session.save_copy(self.root / "saved")
        manifest = self.root / "saved" / "lpk_live2d_editor.json"
        state = json.loads(manifest.read_text(encoding="utf-8"))
        state["named_selections"]["Face"].append("MissingID")
        manifest.write_text(json.dumps(state), encoding="utf-8")
        restored = Live2DEditorSession(manifest.parent / "model.json")
        self.addCleanup(restored.close)
        self.assertEqual(restored.named_selections()["Face"], ["ArtMeshFace", "MissingID"])


def impact_snapshot():
    base = {"id": "selected", "texture_index": 0,
            "uvs": [[.1, .9], [.8, .9], [.1, .2]], "indices": [0, 1, 2],
            "vertices": [[10, 10], [80, 10], [10, 80]]}
    shared = dict(copy.deepcopy(base), id="hidden_shared", visible=False, opacity=0)
    other_page = dict(copy.deepcopy(base), id="other_page", texture_index=1)
    # Overlapping bounding boxes, but no shared triangle pixels.
    disjoint = dict(copy.deepcopy(base), id="disjoint", uvs=[[.8, .2], [.8, .6], [.4, .2]])
    return {"canvas": {"width": 100, "height": 100}, "drawables": [base, shared, other_page, disjoint]}


class SharedUvTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.texture = self.root / "atlas.png"
        Image.new("RGBA", (100, 100)).save(self.texture)
        self.inspector = ArtMeshInspector()
        self.tools = ArtMeshSelectionTools(self.inspector)
        self.addCleanup(self.cleanup)
        self.inspector.load_snapshot(impact_snapshot(), [self.texture, self.texture])
        self.session = unittest.mock.Mock()
        self.session.named_selections.return_value = {"脸": ["selected"]}
        self.tools.refresh(self.session, True, context=self.session)

    def cleanup(self):
        self.tools.shutdown()
        deadline = time.monotonic() + 3
        while self.tools._job is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.tools.deleteLater()
        self.inspector.deleteLater()
        self.app.processEvents()

    def wait_result(self):
        deadline = time.monotonic() + 4
        while self.tools._state == "busy" and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.assertNotEqual(self.tools._state, "busy")

    def test_actual_pixels_hidden_mesh_and_atlas_isolation_match_psd_policy(self):
        _protected, shared = selection_uv_constraints(cv2, impact_snapshot()["drawables"], ["selected"], [(100, 100)] * 2)
        self.assertEqual([r["unselected_id"] for r in shared], ["hidden_shared"])
        self.assertGreater(shared[0]["pixels"], 0)
        with self.assertRaises(InterruptedError):
            selection_uv_constraints(cv2, impact_snapshot()["drawables"], ["selected"], [(100, 100)] * 2,
                                     cancelled=lambda: True)

    def test_background_highlight_preserves_selection_and_explicit_include(self):
        self.inspector.select_entries(["selected"])
        self.tools.shared_check.setChecked(True)
        self.wait_result()
        self.assertEqual(self.tools._related, ["hidden_shared"])
        self.assertEqual(self.inspector.selected_drawable_ids(), ["selected"])
        self.assertEqual(self.inspector.atlas_canvases[0].shared_indices, {1})
        self.assertEqual(self.inspector.atlas_canvases[1].shared_indices, set())
        self.assertFalse(self.inspector.entry_list.item(1).icon().isNull())
        self.tools.include_related()
        self.wait_result()
        self.assertEqual(set(self.inspector.selected_drawable_ids()), {"selected", "hidden_shared"})
        self.assertEqual(self.tools._related, [])
        self.session.save_named_selection.assert_not_called()

    def test_same_named_selection_can_be_recalled_without_dirtying_and_readonly_is_guarded(self):
        self.tools.combo.setCurrentIndex(1)
        self.tools.combo.activated.emit(1)
        self.assertEqual(self.inspector.selected_drawable_ids(), ["selected"])
        self.inspector.select_entries(["other_page"])
        self.tools.combo.activated.emit(1)
        self.assertEqual(self.inspector.selected_drawable_ids(), ["selected"])
        self.session.save_named_selection.assert_not_called()
        self.tools.refresh(self.session, False, context=object())
        self.assertFalse(self.tools.save_button.isEnabled())
        self.assertFalse(self.tools.combo.isEnabled())

    def test_pose_refresh_reuses_analysis_and_switch_or_disable_discards_old_result(self):
        self.inspector.select_entries(["selected"])
        self.tools.shared_check.setChecked(True)
        self.wait_result()
        key = self.tools._key
        changed_pose = impact_snapshot()
        changed_pose["drawables"][0]["vertices"][0][0] = 14
        self.inspector.load_snapshot(changed_pose, [self.texture, self.texture])
        self.assertEqual(self.tools._key, key)
        self.assertEqual(self.tools._state, "result")
        self.assertFalse(self.inspector.entry_list.item(1).icon().isNull())
        self.tools.shared_check.setChecked(False)
        self.tools._completed(key, [{"unselected_id": "hidden_shared"}], "")
        self.assertEqual(self.tools._related, [])
        self.assertEqual(self.inspector.atlas_canvases[0].shared_indices, set())
        self.assertTrue(self.inspector.entry_list.item(1).icon().isNull())

    def test_slow_previous_request_cannot_overwrite_latest_selection(self):
        entered, release = Event(), Event()
        original = selection_uv_constraints
        def delayed(*args, **kwargs):
            if not entered.is_set():
                entered.set()
                release.wait(2)
            return original(*args, **kwargs)
        with patch("app.gui.artmesh_selection_tools.selection_uv_constraints", side_effect=delayed):
            self.inspector.select_entries(["selected"])
            self.tools.shared_check.setChecked(True)
            self.tools.timer.stop()
            self.tools._analyze()
            self.assertTrue(entered.wait(1))
            self.inspector.select_entries(["other_page"])
            self.tools.timer.stop()
            self.tools._analyze()
            release.set()
            self.wait_result()
        self.assertEqual(self.inspector.selected_drawable_ids(), ["other_page"])
        self.assertEqual(self.tools._related, [])
        self.assertEqual(self.tools._state, "none")

    def test_missing_texture_reports_unknown_instead_of_no_shared_pixels(self):
        self.inspector.load_snapshot(impact_snapshot(), [self.root / "missing.png", self.texture])
        self.inspector.select_entries(["selected"])
        self.tools.shared_check.setChecked(True)
        self.wait_result()
        self.assertEqual(self.tools._state, "failed")
        self.assertEqual(self.tools._related, [])
        self.assertIn("Missing atlas", self.tools.summary.toolTip())

    def test_names_dialog_rejects_duplicate_and_empty_then_accepts_unicode(self):
        dialog = SelectionNameDialog("Save", ["Face"], self.tools)
        self.addCleanup(dialog.deleteLater)
        for name in (" ", "face"):
            dialog.name_edit.setText(name)
            dialog.accept()
            self.assertEqual(dialog.result(), 0)
            self.assertFalse(dialog.error.isHidden())
        dialog.name_edit.setText("袖口")
        dialog.accept()
        self.assertEqual(dialog.result(), 1)

    def test_compact_controls_do_not_force_width_in_any_supported_language(self):
        from app.i18n import get_i18n
        from app.gui.theme import apply_application_theme
        from qfluentwidgets import Theme
        i18n = get_i18n()
        previous = i18n.language
        self.addCleanup(lambda: i18n.set_language(previous))
        for language in ("zh_CN", "en_US", "ja_JP"):
            i18n.set_language(language)
            for theme in (Theme.LIGHT, Theme.DARK):
                apply_application_theme(theme, self.tools)
                self.tools.resize(410, self.tools.sizeHint().height())
                self.tools.show()
                for _ in range(5):
                    self.app.processEvents()
                self.assertEqual(self.tools.width(), 410, (language, theme))
                self.assertGreater(self.tools.combo.width(), 40)
                self.assertGreater(self.tools.summary.width(), 30)
                self.assertTrue(self.tools.rect().contains(self.tools.more_button.geometry()))
                self.assertTrue(self.tools.rect().contains(self.tools.include_button.geometry()),
                                (language, self.tools.rect(), self.tools.include_button.geometry()))
        self.tools.hide()


if __name__ == "__main__":
    unittest.main()
