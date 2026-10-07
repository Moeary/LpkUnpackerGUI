import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import unittest

from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication
from app.core.settings_manager import SettingsManager
from app.core.selection_history import SelectionHistory
from app.gui.editor_preferences import ShortcutSettingsCard
from app.gui.editor_shortcuts import SPECS, bindings


class EditorPreferencesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_engine_defaults_conflicts_persistence_disable_and_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SettingsManager(Path(directory) / "settings.json")
            settings.set("remember_paths", False)
            for kind in SPECS:
                card = ShortcutSettingsCard(kind, settings)
                try:
                    self.assertTrue(card.validate())
                    card.edits["save"].setKeySequence(QKeySequence("Ctrl+Z"))
                    self.assertFalse(card.validate())
                    self.assertFalse(card.apply.isEnabled())
                    card.edits["save"].setKeySequence(QKeySequence("Ctrl+Alt+F5"))
                    card.edits["play"].clear()
                    card.save()
                    reloaded = SettingsManager(settings.settings_file)
                    self.assertEqual(bindings(reloaded, kind)["save"], "Ctrl+Alt+F5")
                    self.assertEqual(bindings(reloaded, kind)["play"], "")
                    card.defaults()
                    card.save()
                    self.assertEqual(bindings(SettingsManager(settings.settings_file), kind)["save"], "Ctrl+S")
                finally:
                    card.close()
                    card.deleteLater()

    def test_selection_branching_noop_and_limit(self):
        history = SelectionHistory(limit=2)
        a, b, c = (("a",), "a"), (("a", "b"), "b"), (("c",), "c")
        history.record(a)
        history.record(a)
        history.record(b)
        history.record(c)
        self.assertEqual(history.undo(), b)
        self.assertEqual(history.undo(), a)
        self.assertIsNone(history.undo())
        self.assertEqual(history.redo(), b)
        history.record(c)
        self.assertIsNone(history.redo())
        history.reset()
        self.assertIsNone(history.undo())
