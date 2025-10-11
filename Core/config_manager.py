import os
import json

class ConfigManager:
    """Manage application configuration"""
    
    def __init__(self):
        self.config_dir = os.path.join(os.path.expanduser("~"), ".lpkunpacker")
        self.config_file = os.path.join(self.config_dir, "config.json")
        self.config = self.load_config()
    
    def load_config(self):
        """Load configuration from file"""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Error loading config: {e}")
                return self.get_default_config()
        else:
            return self.get_default_config()
    
    def get_default_config(self):
        """Get default configuration"""
        return {
            "language": "en-US",
            "last_output_dir": "",
            "extract_images_only": False
        }
    
    def save_config(self):
        """Save configuration to file"""
        try:
            os.makedirs(self.config_dir, exist_ok=True)
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            print(f"Error saving config: {e}")
    
    def get_language(self):
        """Get current language setting"""
        return self.config.get("language", "en-US")
    
    def set_language(self, language):
        """Set language"""
        self.config["language"] = language
        self.save_config()
    
    def get_last_output_dir(self):
        """Get last output directory"""
        return self.config.get("last_output_dir", "")
    
    def set_last_output_dir(self, path):
        """Set last output directory"""
        self.config["last_output_dir"] = path
        self.save_config()
    
    def get_extract_images_only(self):
        """Get extract images only setting"""
        return self.config.get("extract_images_only", False)
    
    def set_extract_images_only(self, value):
        """Set extract images only"""
        self.config["extract_images_only"] = value
        self.save_config()
