import os
import json
import shutil
from PySide6.QtWidgets import (QFrame, QVBoxLayout, QHBoxLayout, QLabel,
                            QApplication, QSizePolicy, QFileDialog, QPushButton,
                            QScrollArea, QWidget, QGridLayout, QTableWidget,
                            QTableWidgetItem, QHeaderView, QAbstractItemView)
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QFont, QDragEnterEvent, QDropEvent, QPixmap
from qfluentwidgets import (SubtitleLabel, PushButton, InfoBar, InfoBarPosition, 
                           LineEdit, FluentIcon, TextEdit, MessageBox)

class TexturePreviewButton(PushButton):
    """Button with texture preview capability"""
    
    def __init__(self, parent=None):
        # Call parent init with proper arguments
        super().__init__(parent=parent)
        self.setText("Preview")
        self.setIcon(FluentIcon.VIEW)
        self.texture_path = None
        self.preview_window = None
        self.clicked.connect(self.toggle_preview)
        self.setEnabled(False)
    
    def set_texture(self, path):
        """Set texture path"""
        self.texture_path = path
        self.setEnabled(path is not None and os.path.exists(path))
    
    def toggle_preview(self):
        """Toggle texture preview"""
        if self.preview_window and self.preview_window.isVisible():
            self.preview_window.close()
            self.preview_window = None
        else:
            self.show_preview()
    
    def show_preview(self):
        """Show texture preview"""
        if not self.texture_path or not os.path.exists(self.texture_path):
            return
        
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel
        
        self.preview_window = QDialog(self)
        self.preview_window.setWindowTitle(f"Preview - {os.path.basename(self.texture_path)}")
        layout = QVBoxLayout(self.preview_window)
        
        label = QLabel()
        pixmap = QPixmap(self.texture_path)
        # Scale to reasonable size if too large
        if pixmap.width() > 800 or pixmap.height() > 600:
            pixmap = pixmap.scaled(800, 600, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        label.setPixmap(pixmap)
        layout.addWidget(label)
        
        self.preview_window.resize(pixmap.width() + 40, pixmap.height() + 40)
        self.preview_window.show()

class Live2DModPage(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('live2dModPage')
        
        # Enable drag and drop
        self.setAcceptDrops(True)
        
        self.model_dir = None
        self.model_json_path = None
        self.model_data = None
        self.texture_table = None
        
        self.setupUI()
        
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    
    def setupUI(self):
        # Main layout
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(20, 20, 20, 20)
        self.main_layout.setSpacing(15)
        
        # Title
        self.title_label = SubtitleLabel("Live2D One-Click Mod Tool", self)
        self.main_layout.addWidget(self.title_label)
        
        # Model selection
        self.model_layout = QHBoxLayout()
        self.model_label = SubtitleLabel("Model File:", self)
        self.model_edit = LineEdit(self)
        self.model_edit.setPlaceholderText("Select model.json or model folder...")
        self.model_edit.setReadOnly(True)
        self.model_button = PushButton("Browse", self)
        self.model_button.setIcon(FluentIcon.FOLDER)
        self.model_button.clicked.connect(self.browse_model)
        self.model_layout.addWidget(self.model_label)
        self.model_layout.addWidget(self.model_edit, 1)
        self.model_layout.addWidget(self.model_button)
        self.main_layout.addLayout(self.model_layout)
        
        # HitArea input
        self.hitarea_layout = QHBoxLayout()
        self.hitarea_label = SubtitleLabel("HitArea ID:", self)
        self.hitarea_edit = LineEdit(self)
        self.hitarea_edit.setPlaceholderText("e.g., R_weiba_1, ArtMesh999, jizhongxian83...")
        self.hitarea_layout.addWidget(self.hitarea_label)
        self.hitarea_layout.addWidget(self.hitarea_edit, 1)
        self.main_layout.addLayout(self.hitarea_layout)
        
        # Skin configuration table (scrollable)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setMinimumHeight(300)
        
        self.scroll_widget = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_widget)
        self.scroll_area.setWidget(self.scroll_widget)
        
        self.main_layout.addWidget(self.scroll_area, 1)
        
        # Button row
        self.button_layout = QHBoxLayout()
        
        self.add_skin_btn = PushButton("Add Skin", self)
        self.add_skin_btn.setIcon(FluentIcon.ADD)
        self.add_skin_btn.clicked.connect(self.add_skin_row)
        self.add_skin_btn.setEnabled(False)
        
        self.remove_skin_btn = PushButton("Remove Last Skin", self)
        self.remove_skin_btn.setIcon(FluentIcon.REMOVE)
        self.remove_skin_btn.clicked.connect(self.remove_skin_row)
        self.remove_skin_btn.setEnabled(False)
        
        self.generate_btn = PushButton("Generate Mod", self)
        self.generate_btn.setIcon(FluentIcon.SAVE)
        self.generate_btn.clicked.connect(self.generate_mod)
        self.generate_btn.setEnabled(False)
        
        self.button_layout.addWidget(self.add_skin_btn)
        self.button_layout.addWidget(self.remove_skin_btn)
        self.button_layout.addStretch()
        self.button_layout.addWidget(self.generate_btn)
        
        self.main_layout.addLayout(self.button_layout)
    
    def browse_model(self):
        """Browse for model file or folder"""
        # Try file first
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Model JSON File", "", "JSON Files (*.json)"
        )
        
        if file_path:
            self.load_model(file_path)
        else:
            # Try folder
            folder_path = QFileDialog.getExistingDirectory(
                self, "Select Model Folder"
            )
            if folder_path:
                # Look for model*.json
                json_files = [f for f in os.listdir(folder_path) if f.startswith('model') and f.endswith('.json')]
                if json_files:
                    # Use first found
                    model_file = os.path.join(folder_path, json_files[0])
                    self.load_model(model_file)
                else:
                    InfoBar.error(
                        title="Error",
                        content="No model*.json file found in folder",
                        parent=self,
                        position=InfoBarPosition.TOP,
                        duration=3000
                    )
    
    def load_model(self, model_path):
        """Load model JSON file"""
        try:
            # Read JSON file directly without complex parsing
            with open(model_path, 'r', encoding='utf-8') as f:
                json_text = f.read()
                self.model_data = json.loads(json_text)
            
            self.model_json_path = model_path
            self.model_dir = os.path.dirname(model_path)
            self.model_edit.setText(model_path)
            
            # Get texture count
            file_refs = self.model_data.get('FileReferences')
            if not file_refs:
                raise ValueError("No FileReferences found in model file")
            
            textures = file_refs.get('Textures', [])
            texture_count = len(textures)
            
            if texture_count == 0:
                InfoBar.warning(
                    title="Warning",
                    content="No textures found in model file",
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=3000
                )
                return
            
            # Create texture table
            self.create_texture_table(texture_count)
            
            # Enable buttons
            self.add_skin_btn.setEnabled(True)
            self.remove_skin_btn.setEnabled(False)
            self.generate_btn.setEnabled(True)
            
            InfoBar.success(
                title="Success",
                content=f"Model loaded: {texture_count} texture(s) found",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )
            
        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            print(f"Error loading model: {error_details}")
            InfoBar.error(
                title="Error",
                content=f"Failed to load model: {str(e)}",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000
            )
    
    def create_texture_table(self, texture_count):
        """Create texture configuration table"""
        # Clear existing table
        if self.texture_table:
            self.scroll_layout.removeWidget(self.texture_table)
            self.texture_table.deleteLater()
        
        # Create new table
        self.texture_table = QTableWidget(1, texture_count * 2 + 1, self.scroll_widget)
        self.texture_table.setAcceptDrops(True)
        
        # Set headers: Texture 0, Preview 0, Texture 1, Preview 1, ..., Skin Name
        headers = []
        for i in range(texture_count):
            headers.append(f"Texture {i}")
            headers.append(f"Preview {i}")
        headers.append("Skin Name")
        
        self.texture_table.setHorizontalHeaderLabels(headers)
        
        # Set minimum column widths
        for i in range(texture_count * 2):
            if i % 2 == 0:  # Texture columns
                self.texture_table.setColumnWidth(i, 200)
            else:  # Preview columns
                self.texture_table.setColumnWidth(i, 80)
        self.texture_table.setColumnWidth(texture_count * 2, 150)  # Skin name column
        
        # Set row height
        self.texture_table.setRowHeight(0, 60)
        
        # Enable drag and drop
        self.texture_table.setDragDropMode(QAbstractItemView.DropOnly)
        
        # Add first row (original skin)
        self.add_table_row(0, "原皮")
        
        # Add to layout
        self.scroll_layout.addWidget(self.texture_table)
    
    def add_table_row(self, row_index, default_name=""):
        """Add a row to the texture table"""
        texture_count = (self.texture_table.columnCount() - 1) // 2
        
        # Add texture and preview columns
        for i in range(texture_count):
            # Texture path label
            texture_label = QLabel("(No texture)")
            texture_label.setAlignment(Qt.AlignCenter)
            texture_label.setStyleSheet("padding: 5px; border: 1px solid #ccc; border-radius: 3px;")
            texture_label.setProperty("texture_index", i)
            texture_label.setProperty("row_index", row_index)
            texture_label.setProperty("texture_path", None)
            texture_label.mousePressEvent = lambda event, row=row_index, tex=i: self.on_texture_label_click(event, row, tex)
            self.texture_table.setCellWidget(row_index, i * 2, texture_label)
            
            # Preview button - use parent properly
            preview_btn = TexturePreviewButton(self.texture_table)
            preview_btn.setProperty("texture_index", i)
            preview_btn.setProperty("row_index", row_index)
            self.texture_table.setCellWidget(row_index, i * 2 + 1, preview_btn)
        
        # Skin name input
        name_item = QTableWidgetItem(default_name)
        name_item.setTextAlignment(Qt.AlignCenter)
        self.texture_table.setItem(row_index, texture_count * 2, name_item)
    
    def on_texture_label_click(self, event, row, texture_index):
        """Handle texture label click to browse file"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Texture File", self.model_dir if self.model_dir else "", 
            "Image Files (*.png *.jpg *.jpeg *.bmp)"
        )
        if file_path:
            self.set_texture_for_cell(row, texture_index, file_path)
    
    def set_texture_for_cell(self, row, texture_index, file_path):
        """Set texture for a specific cell"""
        # Update texture label
        texture_label = self.texture_table.cellWidget(row, texture_index * 2)
        if texture_label:
            texture_label.setText(os.path.basename(file_path))
            texture_label.setProperty("texture_path", file_path)
            texture_label.setStyleSheet("padding: 5px; border: 1px solid #4CAF50; border-radius: 3px; background-color: #E8F5E9;")
        
        # Update preview button
        preview_btn = self.texture_table.cellWidget(row, texture_index * 2 + 1)
        if preview_btn:
            preview_btn.set_texture(file_path)
    
    def add_skin_row(self):
        """Add a new skin row to the table"""
        if not self.texture_table:
            return
        
        row_count = self.texture_table.rowCount()
        self.texture_table.insertRow(row_count)
        self.texture_table.setRowHeight(row_count, 60)
        
        self.add_table_row(row_count, f"Skin {row_count}")
        
        self.remove_skin_btn.setEnabled(row_count > 0)
    
    def remove_skin_row(self):
        """Remove the last skin row"""
        if not self.texture_table:
            return
        
        row_count = self.texture_table.rowCount()
        if row_count > 1:
            self.texture_table.removeRow(row_count - 1)
        
        self.remove_skin_btn.setEnabled(self.texture_table.rowCount() > 1)
    
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
    
    def dropEvent(self, event: QDropEvent):
        """Handle dropped files"""
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            
            # Check if it's a model file
            if path.lower().endswith('.json') and 'model' in os.path.basename(path).lower():
                self.load_model(path)
                break
        
        event.acceptProposedAction()
    
    def generate_mod(self):
        """Generate the modded Live2D files"""
        if not self.validate_inputs():
            return
        
        try:
            hitarea_id = self.hitarea_edit.text().strip()
            
            # Collect skin data from table
            skins_data = []
            texture_count = (self.texture_table.columnCount() - 1) // 2
            
            for row in range(self.texture_table.rowCount()):
                # Get skin name
                name_item = self.texture_table.item(row, texture_count * 2)
                skin_name = name_item.text().strip() if name_item else f"Skin {row}"
                
                # Get textures
                textures = []
                for i in range(texture_count):
                    texture_label = self.texture_table.cellWidget(row, i * 2)
                    texture_path = texture_label.property("texture_path") if texture_label else None
                    textures.append(texture_path)
                
                skins_data.append({
                    'name': skin_name,
                    'textures': textures
                })
            
            # Generate mod files
            self.create_mod_files(skins_data, hitarea_id)
            
            InfoBar.success(
                title="Success",
                content=f"Mod generated successfully with {len(skins_data)} skins!",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000
            )
            
        except Exception as e:
            import traceback
            InfoBar.error(
                title="Error",
                content=f"Failed to generate mod: {str(e)}",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000
            )
            print(traceback.format_exc())
    
    def validate_inputs(self):
        """Validate all inputs before generating"""
        if not self.model_json_path or not os.path.exists(self.model_json_path):
            InfoBar.error(
                title="Error",
                content="Please select a valid model file",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )
            return False
        
        hitarea_id = self.hitarea_edit.text().strip()
        if not hitarea_id:
            InfoBar.warning(
                title="Warning",
                content="Please enter a HitArea ID",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )
            return False
        
        if not self.texture_table or self.texture_table.rowCount() == 0:
            InfoBar.error(
                title="Error",
                content="No skins configured",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )
            return False
        
        return True
    
    def create_mod_files(self, skins_data, hitarea_id):
        """Create all modded files"""
        # Backup original model file
        backup_path = self.model_json_path + ".backup"
        if not os.path.exists(backup_path):
            shutil.copy2(self.model_json_path, backup_path)
            print(f"Backed up original model to: {backup_path}")
        
        # Get original textures list
        original_textures = self.model_data.get('FileReferences', {}).get('Textures', [])
        
        # Create modified model files for each skin
        for skin_index, skin_data in enumerate(skins_data):
            model_filename = f"model{skin_index}.json"
            model_path = os.path.join(self.model_dir, model_filename)
            
            # Read original file and parse again to avoid recursion
            with open(self.model_json_path, 'r', encoding='utf-8') as f:
                modified_model = json.loads(f.read())
            
            # Copy textures to model directory and update texture references
            new_texture_list = []
            for tex_index, texture_path in enumerate(skin_data['textures']):
                if texture_path and os.path.exists(texture_path):
                    # Generate new texture filename
                    ext = os.path.splitext(texture_path)[1]
                    new_texture_name = f"{skin_index}_{tex_index}{ext}"
                    new_texture_path = os.path.join(self.model_dir, new_texture_name)
                    
                    # Copy texture file
                    if not os.path.exists(new_texture_path) or os.path.abspath(texture_path) != os.path.abspath(new_texture_path):
                        shutil.copy2(texture_path, new_texture_path)
                        print(f"Copied texture: {new_texture_name}")
                    
                    new_texture_list.append(new_texture_name)
                else:
                    # Use original texture
                    if tex_index < len(original_textures):
                        new_texture_list.append(original_textures[tex_index])
                    else:
                        print(f"Warning: Missing texture {tex_index} for skin {skin_index}")
            
            # Update texture references in model
            if 'FileReferences' in modified_model:
                modified_model['FileReferences']['Textures'] = new_texture_list
            
            # For first model (model0.json), add motion configurations
            if skin_index == 0:
                self.add_motion_config(modified_model, skins_data, hitarea_id)
            
            # Save modified model
            with open(model_path, 'w', encoding='utf-8') as f:
                json.dump(modified_model, f, ensure_ascii=False, indent=2)
            
            print(f"Created: {model_filename}")
    
    def add_motion_config(self, model_data, skins_data, hitarea_id):
        """Add motion and hitarea configuration to model with correct format"""
        # Ensure FileReferences exists
        if 'FileReferences' not in model_data:
            model_data['FileReferences'] = {}
        
        # Get or create Motions section
        if 'Motions' not in model_data['FileReferences']:
            model_data['FileReferences']['Motions'] = {}
        
        motions = model_data['FileReferences']['Motions']
        
        # Add "Tap切换贴图" motion group with Choices menu format
        tap_motion_name = "Tap切换贴图"
        choices = []
        for skin_index, skin_data in enumerate(skins_data):
            choices.append({
                "Text": skin_data['name'],
                "NextMtn": f"切换贴图:{skin_index}"
            })
        
        motions[tap_motion_name] = [{
            "Name": "1",
            "Text": "切换贴图",
            "Choices": choices
        }]
        
        # Add "切换贴图" motion group with actual commands
        switch_motion_name = "切换贴图"
        switch_motions = []
        for skin_index, skin_data in enumerate(skins_data):
            switch_motions.append({
                "Name": str(skin_index),
                "Command": f"change_model model{skin_index}.json"
            })
        motions[switch_motion_name] = switch_motions
        
        # Add HitArea
        if 'HitAreas' not in model_data:
            model_data['HitAreas'] = []
        
        hit_areas = model_data['HitAreas']
        
        # Check if hitarea already exists
        existing_hitarea = None
        for ha in hit_areas:
            if ha.get('Name') == '切换贴图菜单':
                existing_hitarea = ha
                break
        
        if existing_hitarea:
            # Update existing
            existing_hitarea['Id'] = hitarea_id
            existing_hitarea['Motion'] = tap_motion_name
        else:
            # Add new hitarea
            hit_areas.append({
                "Name": "切换贴图菜单",
                "Id": hitarea_id,
                "Motion": tap_motion_name
            })
        
        print(f"Added motion configuration with {len(skins_data)} skins")
        print(f"Added HitArea with ID: {hitarea_id}")
    
    def updateUIScale(self, window_width, window_height):
        """根据窗口大小调整UI元素"""
        scale_factor = max(1.0, window_width / 1000.0)
        
        button_height = int(30 * scale_factor)
        for button in self.findChildren(PushButton):
            button.setMinimumHeight(button_height)
        
        for line_edit in self.findChildren(LineEdit):
            line_edit.setMinimumHeight(button_height)
        
        font = QApplication.instance().font()
        for label in self.findChildren(SubtitleLabel):
            label_font = label.font()
            label_font.setPointSize(font.pointSize() + 2)
            label.setFont(label_font)
