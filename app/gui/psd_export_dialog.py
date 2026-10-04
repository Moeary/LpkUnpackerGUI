"""A single choice of name, coordinate space and layout for selected parts."""
from qfluentwidgets import BodyLabel, CheckBox, ComboBox, LineEdit

from app.gui.editor_dialogs import ThemedEditorDialog
from app.i18n import tr


class SelectionPsdDialog(ThemedEditorDialog):
    def __init__(self, count, parent=None, *, mode="mesh"):
        super().__init__(parent)
        self.setWindowTitle(tr("psd.selection.export"))
        self.name_edit = LineEdit(self)
        self.name_edit.setText(tr("psd.selection.default_name"))
        self.mode_combo = ComboBox(self)
        self.mode_combo.addItem(tr("psd.mode.mesh_pose"), userData="mesh")
        self.mode_combo.addItem(tr("psd.mode.editable_atlas"), userData="atlas-components")
        self.mode_combo.setCurrentIndex(1 if mode == "atlas-components" else 0)
        self.packed = CheckBox(tr("psd.selection.pack_atlas"), self)
        self.packed.setChecked(True)
        self.hint = BodyLabel(self)
        self.hint.setWordWrap(True)
        self.viewLayout.addWidget(BodyLabel(tr("psd.selection.count", count=count), self))
        for control in (self.name_edit, self.mode_combo, self.packed, self.hint):
            self.viewLayout.addWidget(control)
        self.mode_combo.currentIndexChanged.connect(self.update_mode)
        self.name_edit.textChanged.connect(lambda text: self.yesButton.setEnabled(bool(text.strip())))
        self.bind_submit_edit(self.name_edit)
        self.update_mode()

    def update_mode(self):
        atlas = self.mode_combo.currentData() == "atlas-components"
        self.packed.setVisible(atlas)
        self.hint.setText(tr("psd.selection.atlas_hint" if atlas else "psd.selection.hint"))

    def options(self):
        return {"mode": self.mode_combo.currentData(),
                "atlas_layout": "packed" if self.packed.isChecked() else "original",
                "output_name": self.name_edit.text().strip()}
