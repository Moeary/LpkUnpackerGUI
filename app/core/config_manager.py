from app.core.settings_manager import SettingsManager
from app.paths import RUNTIME_OUTPUT_DIR


class ConfigManager:
    """Compatibility wrapper for the old extractor configuration API."""

    def __init__(self):
        self.settings = SettingsManager()

    def get_language(self):
        return self.settings.get("language", "en_US")

    def set_language(self, language):
        self.settings.set("language", language)

    def get_last_output_dir(self):
        last_output = self.settings.get("last_output_path", "")
        default_output = str(RUNTIME_OUTPUT_DIR.resolve())
        if not last_output:
            return default_output
        normalized_last = str(last_output).replace("\\", "/").rstrip("/")
        if normalized_last.endswith("/runtime/output/live2d"):
            return default_output
        return str(last_output)

    def set_last_output_dir(self, path):
        self.settings.set("last_output_path", path)

    def get_extract_images_only(self):
        return self.settings.get("extraction_settings.extract_images_only", False)

    def set_extract_images_only(self, value):
        self.settings.set("extraction_settings.extract_images_only", bool(value))
