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
        # Atlas pieces as one layer: paint or erase the whole selection at
        # once.  Write-back still splits it per ArtMesh.
        self.single_layer = CheckBox(tr("psd.selection.single_layer", default="合并为单个图层"), self)
        self.single_layer.setChecked(False)
        self.hint = BodyLabel(self)
        self.hint.setWordWrap(True)
        self.viewLayout.addWidget(BodyLabel(tr("psd.selection.count", count=count), self))
        for control in (self.name_edit, self.mode_combo, self.packed, self.single_layer, self.hint):
            self.viewLayout.addWidget(control)
        self.mode_combo.currentIndexChanged.connect(self.update_mode)
        self.single_layer.toggled.connect(self.update_mode)
        self.name_edit.textChanged.connect(lambda text: self.yesButton.setEnabled(bool(text.strip())))
        self.bind_submit_edit(self.name_edit)
        self.update_mode()

    def update_mode(self, *_args):
        atlas = self.mode_combo.currentData() == "atlas-components"
        self.packed.setVisible(atlas)
        self.single_layer.setVisible(atlas)
        hint = tr("psd.selection.atlas_hint" if atlas else "psd.selection.hint")
        if atlas and self.single_layer.isChecked():
            hint += "\n" + tr("psd.selection.single_layer_hint", default=(
                "所有选中部件合成一个图层（原始布局下每张贴图一个），可整体绘制或擦除；"
                "回写时仍按各 ArtMesh 拆分，版本历史与多工程合成照常可用。"))
        self.hint.setText(hint)

    def options(self):
        atlas = self.mode_combo.currentData() == "atlas-components"
        return {"mode": self.mode_combo.currentData(),
                "atlas_layout": "packed" if self.packed.isChecked() else "original",
                "atlas_single_layer": atlas and self.single_layer.isChecked(),
                "output_name": self.name_edit.text().strip()}
