import os
from PySide6.QtCore import QThread, Signal
from app.core.lpk_loader import LpkLoader
from app.core.wpk_handler import WPKHandler
from app.core.image_extractor import ImageExtractor

class ExtractorThread(QThread):
    """Thread for file extraction with proper signals"""
    
    # Signals
    progressUpdated = Signal(int)
    extractionFinished = Signal(str)
    extractionError = Signal(str)
    logMessage = Signal(str, str)  # (level, message)
    
    def __init__(self, files, config_files, output_dir, extract_images_only=False):
        super().__init__()
        self.files = files
        self.config_files = config_files
        self.output_dir = output_dir
        self.extract_images_only = extract_images_only
        self._is_running = True
        
        # Statistics
        self.total_extracted = 0
        self.total_skipped = 0
    
    def run(self):
        """Main extraction process"""
        try:
            self.analyze_files()
            
            total_files = len(self.files)
            for idx, file_path in enumerate(self.files):
                if not self._is_running:
                    break
                
                self.log_info(f"Processing file {idx + 1}/{total_files}: {os.path.basename(file_path)}")
                
                # Detect file type and process
                file_type = ImageExtractor.detect_file_type(file_path)
                self.log_info(f"Detected file type: {file_type}")
                
                self.process_file(file_path, file_type)
                
                # Update progress
                progress = int((idx + 1) / total_files * 100)
                self.progressUpdated.emit(progress)
            
            if self._is_running:
                self.log_statistics()
                self.extractionFinished.emit(self.output_dir)
            
        except Exception as e:
            self.handle_error("Extraction error", e)
            self.extractionError.emit(str(e))
    
    def process_file(self, file_path, file_type):
        """Process a single file based on its type"""
        if file_type == "WPK":
            self.process_wpk_file(file_path)
        elif file_type == "LPK":
            self.process_lpk_file(file_path)
        elif file_type == "UNITY":
            if self.extract_images_only:
                self.process_unity_file(file_path)
            else:
                self.log_warning("Unity files are only supported in 'Extract Images Only' mode")
        else:
            self.log_warning(f"Unsupported file type: {file_path}")
    
    def analyze_files(self):
        """Analyze and report file types"""
        self.log_info("=== Analyzing Files ===")
        
        type_counts = {"LPK": 0, "WPK": 0, "UNITY": 0, "UNKNOWN": 0}
        
        for file_path in self.files:
            file_type = ImageExtractor.detect_file_type(file_path)
            type_counts[file_type] = type_counts.get(file_type, 0) + 1
        
        self.log_info(f"Total files: {len(self.files)}")
        for ftype, count in type_counts.items():
            if count > 0:
                level = "WARNING" if ftype == "UNKNOWN" else "INFO"
                self.logMessage.emit(level, f"  - {ftype} files: {count}")
        
        mode = "Extract Textures Only" if self.extract_images_only else "Full Extraction"
        self.log_info(f"Extraction mode: {mode}")
        self.log_info("======================")
    
    def process_wpk_file(self, wpk_path):
        """Process WPK file"""
        self.log_info(f"Extracting WPK file: {os.path.basename(wpk_path)}")
        
        temp_dir, lpk_files, config_files = WPKHandler.extract_wpk(wpk_path, self.output_dir)
        
        if not lpk_files:
            self.log_warning("No LPK files found in WPK")
            return
        
        try:
            for lpk_file in lpk_files:
                config_file = self.find_config_for_lpk(lpk_file, config_files)
                self.process_lpk_file(lpk_file, config_file)
        finally:
            WPKHandler.cleanup_temp_dir(temp_dir)
    
    def process_lpk_file(self, lpk_path, config_path=None):
        """Process LPK file"""
        self.log_info(f"Processing LPK: {os.path.basename(lpk_path)}")
        
        if not config_path:
            config_path = self.find_config_for_lpk(lpk_path, self.config_files)
        
        if self.extract_images_only:
            self.extract_images(lpk_path, config_path)
        else:
            self.extract_full(lpk_path, config_path)
    
    def process_unity_file(self, unity_path):
        """Process Unity file"""
        self.log_info(f"Processing Unity file: {os.path.basename(unity_path)}")
        self.extract_images(unity_path, None)
    
    def extract_images(self, file_path, config_path):
        """Extract only images/textures"""
        try:
            extractor = ImageExtractor(file_path, config_path)
            image_output = os.path.join(self.output_dir, "images")
            
            extracted, skipped = extractor.extract_images(image_output)
            
            self.total_extracted += extracted
            self.total_skipped += skipped
            
            self.log_info(f"  Extracted: {extracted} textures")
            self.log_info(f"  Skipped: {skipped} duplicates")
            
        except Exception as e:
            self.handle_error("Error extracting images", e)
    
    def extract_full(self, lpk_path, config_path):
        """Extract full LPK contents"""
        try:
            loader = LpkLoader(lpk_path, config_path)
            loader.extract(self.output_dir)
            self.log_info(f"Extraction completed for: {os.path.basename(lpk_path)}")
        except Exception as e:
            self.handle_error("Error extracting LPK", e)
    
    def log_statistics(self):
        """Log final statistics"""
        if self.extract_images_only:
            self.log_info("=== Extraction Statistics ===")
            self.log_info(f"Total textures extracted: {self.total_extracted}")
            self.log_info(f"Total duplicates skipped: {self.total_skipped}")
            self.log_info("============================")
    
    def find_config_for_lpk(self, lpk_path, config_files=None):
        """Find config.json file for LPK"""
        lpk_dir = os.path.dirname(lpk_path)
        config_in_dir = os.path.join(lpk_dir, "config.json")
        
        if os.path.exists(config_in_dir):
            return config_in_dir
        
        if config_files:
            for config_file in config_files:
                if os.path.dirname(config_file) == lpk_dir:
                    return config_file
            if config_files:
                return config_files[0]
        
        return None
    
    def stop(self):
        """Stop the thread"""
        self._is_running = False
    
    # Helper methods for logging
    def log_info(self, message):
        self.logMessage.emit("INFO", message)
    
    def log_warning(self, message):
        self.logMessage.emit("WARNING", message)
    
    def log_error(self, message):
        self.logMessage.emit("ERROR", message)
    
    def handle_error(self, context, exception):
        """Handle and log errors"""
        import traceback
        error_msg = f"{context}: {str(exception)}"
        traceback_msg = traceback.format_exc()
        self.log_error(error_msg)
        self.log_error(traceback_msg)
