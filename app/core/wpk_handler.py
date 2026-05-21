import os
import zipfile
import shutil
import tempfile

class WPKHandler:
    """Handle WPK file extraction"""
    
    @staticmethod
    def is_wpk_file(file_path):
        """Check if file is a WPK file"""
        if not os.path.exists(file_path):
            return False
        
        # Check file extension
        if not file_path.lower().endswith('.wpk'):
            return False
        
        # Try to open as zip
        try:
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                files = zip_ref.namelist()
                # WPK files contain .lpk, .json, and .png files
                has_lpk = any(f.endswith('.lpk') for f in files)
                has_json = any(f.endswith('.json') for f in files)
                return has_lpk or has_json
        except:
            return False
    
    @staticmethod
    def extract_wpk(wpk_path, output_dir):
        """Extract WPK file and return paths to LPK and config files"""
        try:
            # Create temporary directory for extraction
            temp_dir = tempfile.mkdtemp(prefix='wpk_extract_')
            
            # Extract WPK file
            with zipfile.ZipFile(wpk_path, 'r') as zip_ref:
                zip_ref.extractall(temp_dir)
            
            # Find LPK and config.json files
            lpk_files = []
            config_files = []
            
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    full_path = os.path.join(root, file)
                    if file.endswith('.lpk'):
                        lpk_files.append(full_path)
                    elif file == 'config.json':
                        config_files.append(full_path)
            
            return temp_dir, lpk_files, config_files
        except Exception as e:
            print(f"Error extracting WPK: {e}")
            return None, [], []
    
    @staticmethod
    def cleanup_temp_dir(temp_dir):
        """Clean up temporary directory"""
        try:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
        except Exception as e:
            print(f"Error cleaning up temp directory: {e}")
