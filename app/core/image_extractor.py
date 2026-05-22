import os
import json
import hashlib
import tempfile
import shutil
from typing import Dict, Tuple, List
from app.core.assetstudio_cli import AssetStudioCLI, AssetStudioCLIError
from app.core.lpk_loader import LpkLoader

class ImageExtractor:
    """Extract only images/textures from LPK, WPK, and Unity files"""
    
    def __init__(self, file_path, config_path=None):
        self.file_path = file_path
        self.config_path = config_path
        self.file_type = self.detect_file_type(file_path)
        
        # For LPK files
        if self.file_type == "LPK":
            try:
                self.loader = LpkLoader(file_path, config_path)
            except Exception as e:
                print(f"Warning: Failed to initialize LPK loader: {e}")
                self.loader = None
        
        # MD5 registry for deduplication
        # Format: {base_name: {md5: output_filename}}
        self.md5_registry: Dict[str, Dict[str, str]] = {}
        
        # File counter for each base name
        # Format: {base_name: file_index}
        self.file_counter: Dict[str, int] = {}
    
    @staticmethod
    def detect_file_type(file_path):
        """Detect file type based on extension and content"""
        if not os.path.exists(file_path):
            return "UNKNOWN"
        
        # Check extension
        ext = os.path.splitext(file_path)[1].lower()
        
        if ext == '.lpk':
            return "LPK"
        elif ext == '.wpk':
            return "WPK"
        elif ext in ['', '.assets', '.sharedassets', '.bundle', '.unity3d', '.resource', '.resS']:
            return "UNITY"
        else:
            return "UNKNOWN"
    
    def extract_images(self, output_dir):
        """Extract images based on file type"""
        os.makedirs(output_dir, exist_ok=True)
        
        if self.file_type == "LPK":
            return self.extract_from_lpk(output_dir)
        elif self.file_type == "WPK":
            return self.extract_from_wpk(output_dir)
        elif self.file_type == "UNITY":
            return self.extract_from_unity(output_dir)
        else:
            raise ValueError(f"Unsupported file type: {self.file_type}")
    
    def extract_from_lpk(self, output_dir):
        """Extract textures from LPK file - first unpack to temp, then extract textures"""
        if not self.loader:
            print("  Error: LPK loader not initialized")
            return 0, 0
        
        # Get title from config if available
        title = self.get_title_from_config()
        
        # Sanitize title to remove control characters
        title = self.sanitize_path(title)
        
        # Create output directory with title
        image_dir = os.path.join(output_dir, title)
        os.makedirs(image_dir, exist_ok=True)
        
        extracted_count = 0
        skipped_count = 0
        
        # Create temporary directory for unpacking LPK
        temp_dir = tempfile.mkdtemp(prefix='lpk_extract_')
        
        try:
            print(f"  Unpacking LPK to temporary directory...")
            
            # First, extract the LPK file to temp directory
            # Patch the check_decrypt to skip manual input
            original_check = self.loader.check_decrypt
            
            def patched_check_decrypt(filename):
                """Patched version that skips manual input"""
                import logging
                logger = logging.getLogger("lpkLoder")
                logger.info("try to decrypt entry model.json")
                
                try:
                    self.loader.decrypt_file(filename).decode(encoding="utf8")
                    return True
                except UnicodeDecodeError:
                    logger.info("trying to auto fix fileId")
                    success = False
                    possible_fileId = []
                    
                    if hasattr(self.loader, 'config') and 'lpkFile' in self.loader.config:
                        possible_fileId.append(self.loader.config["lpkFile"].strip('.lpk'))
                    
                    for fileid in possible_fileId:
                        self.loader.config["fileId"] = fileid
                        try:
                            self.loader.decrypt_file(filename).decode(encoding="utf8")
                            success = True
                            break
                        except UnicodeDecodeError:
                            continue
                    
                    if not success:
                        # Skip manual input - just log and return False
                        logger.warning("Auto-fix fileId failed. Skipping this file.")
                        return False
                    
                    return True
            
            # Temporarily replace check_decrypt
            self.loader.check_decrypt = patched_check_decrypt
            
            try:
                self.loader.extract(temp_dir)
            except SystemExit:
                # Catch exit(0) calls from lpk_loader
                print("  Warning: Extraction encountered errors, continuing with available files...")
            except Exception as e:
                print(f"  Warning: Extraction error: {e}, continuing with available files...")
            finally:
                # Restore original method
                self.loader.check_decrypt = original_check
            
            print(f"  LPK unpacked, scanning for image files...")
            
            # Now scan the extracted files for images
            for root, dirs, files in os.walk(temp_dir):
                for filename in files:
                    if self.is_image_file(filename):
                        file_path = os.path.join(root, filename)
                        
                        try:
                            # Read the image file
                            with open(file_path, 'rb') as f:
                                data = f.read()
                            
                            # Calculate MD5
                            data_md5 = hashlib.md5(data).hexdigest()
                            
                            # Get base name and sanitize
                            base_name = os.path.splitext(filename)[0]
                            base_name = self.sanitize_filename(base_name)
                            
                            # Check for duplicates
                            if base_name not in self.md5_registry:
                                self.md5_registry[base_name] = {}
                                self.file_counter[base_name] = 0
                            
                            if data_md5 in self.md5_registry[base_name]:
                                print(f"  Skipped duplicate: {filename} (MD5: {data_md5[:8]}...)")
                                skipped_count += 1
                                continue
                            
                            # Generate output filename
                            ext = os.path.splitext(filename)[1]
                            file_index = self.file_counter[base_name]
                            self.file_counter[base_name] += 1
                            
                            output_name = f"{base_name}_{file_index}{ext}"
                            output_path = os.path.join(image_dir, output_name)
                            
                            # Save
                            with open(output_path, 'wb') as f:
                                f.write(data)
                            
                            # Register MD5
                            self.md5_registry[base_name][data_md5] = output_name
                            
                            print(f"  ✓ Extracted: {output_name}")
                            extracted_count += 1
                            
                        except Exception as e:
                            print(f"  Error extracting {filename}: {e}")
            
            print(f"  Running AssetStudio CLI for Unity textures...")
            unity_extracted, unity_skipped = self.extract_unity_textures_from_path(
                temp_dir,
                image_dir,
                title,
            )
            extracted_count += unity_extracted
            skipped_count += unity_skipped
            
        finally:
            # Clean up temporary directory
            if temp_dir and os.path.exists(temp_dir):
                print(f"  Cleaning up temporary directory...")
                try:
                    shutil.rmtree(temp_dir)
                except Exception as e:
                    print(f"  Warning: Failed to clean up temp directory: {e}")
        
        return extracted_count, skipped_count
    
    def extract_unity_textures_from_path(self, input_path, output_dir, base_name=None):
        """Extract textures from Unity assets through AssetStudio CLI."""
        try:
            result = AssetStudioCLI().export_textures(input_path, output_dir)
            for path in result.exported_files:
                print(f"  ✓ AssetStudio exported: {path.name}")
            return result.exported_count, 0
        except AssetStudioCLIError as e:
            print(f"  Warning: AssetStudio CLI skipped {base_name or input_path}: {e}")
            return 0, 0
    
    def extract_from_wpk(self, output_dir):
        """Extract textures from WPK file"""
        from app.core.wpk_handler import WPKHandler
        import shutil
        
        temp_dir = None
        
        try:
            print(f"  Extracting WPK to temporary directory...")
            temp_dir, lpk_files, config_files = WPKHandler.extract_wpk(
                self.file_path, temp_dir
            )
            
            total_extracted = 0
            total_skipped = 0
            
            # Process each LPK file
            for lpk_file in lpk_files:
                print(f"  Processing LPK from WPK: {os.path.basename(lpk_file)}")
                
                # Find config
                config_file = None
                lpk_dir = os.path.dirname(lpk_file)
                for cfg in config_files:
                    if os.path.dirname(cfg) == lpk_dir:
                        config_file = cfg
                        break
                
                # Extract from LPK
                extractor = ImageExtractor(lpk_file, config_file)
                extracted, skipped = extractor.extract_from_lpk(output_dir)
                total_extracted += extracted
                total_skipped += skipped
            
            return total_extracted, total_skipped
            
        finally:
            # Clean up
            if temp_dir and os.path.exists(temp_dir):
                print(f"  Cleaning up WPK temporary directory...")
                shutil.rmtree(temp_dir)
    
    def extract_from_unity(self, output_dir):
        """Extract Texture2D/Sprite assets from Unity files via AssetStudio CLI."""
        # Get base name from file
        base_name = os.path.basename(self.file_path)
        
        # Create output directory
        image_dir = os.path.join(output_dir, base_name)
        os.makedirs(image_dir, exist_ok=True)
        
        # Extract textures
        return self.extract_unity_textures_from_path(self.file_path, image_dir, base_name)
    
    def get_title_from_config(self):
        """Get title from config file or use default"""
        title = "images"
        if self.config_path and os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    title = config.get("title", "images")
                    # Sanitize title
                    title = self.sanitize_path(title)
            except:
                pass
        
        # If no title, use LPK filename
        if title == "images":
            title = os.path.splitext(os.path.basename(self.file_path))[0]
            title = self.sanitize_path(title)
        
        return title
    
    @staticmethod
    def sanitize_path(path):
        """Sanitize path to remove control characters and invalid characters"""
        import re
        # Remove control characters including \r, \n, \t
        path = ''.join(c for c in path if ord(c) >= 32 or c == ' ')
        # Remove invalid Windows path characters
        path = re.sub(r'[<>:"|?*]', '', path)
        # Remove leading/trailing whitespace
        path = path.strip()
        # Replace multiple spaces with single space
        path = re.sub(r'\s+', ' ', path)
        return path if path else "images"
    
    @staticmethod
    def sanitize_filename(filename):
        """Sanitize filename to remove invalid characters"""
        import re
        # Remove control characters
        filename = ''.join(c for c in filename if ord(c) >= 32 or c == ' ')
        # Remove invalid filename characters
        filename = re.sub(r'[<>:"/\\|?*]', '', filename)
        # Remove leading/trailing whitespace and dots
        filename = filename.strip('. ')
        return filename if filename else "file"
    
    @staticmethod
    def is_image_file(filename):
        """Check if file is an image"""
        image_extensions = ['.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp', '.tga']
        ext = os.path.splitext(filename.lower())[1]
        return ext in image_extensions
    
    @staticmethod
    def has_extension(filename):
        """Check if filename has extension"""
        return '.' in filename and not filename.startswith('.')
    
    @staticmethod
    def calculate_image_md5(image):
        """Calculate MD5 of PIL Image"""
        import io
        buf = io.BytesIO()
        image.save(buf, format='PNG')
        buf.seek(0)
        return hashlib.md5(buf.read()).hexdigest()
