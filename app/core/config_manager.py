from app.core.settings_manager import SettingsManager


class ConfigManager:
    """Compatibility wrapper for the old extractor configuration API."""

    def __init__(self):
        self.settings = SettingsManager()

    def get_language(self):
        return self.settings.get("language", "en_US")

    def set_language(self, language):
        self.settings.set("language", language)

    def get_last_output_dir(self):
        return self.settings.get_output_dir("live2d")

    def set_last_output_dir(self, path):
        self.settings.set_output_dir("live2d", path)

    def get_extract_images_only(self):
        return self.settings.get("extraction_settings.extract_images_only", False)

    def set_extract_images_only(self, value):
        self.settings.set("extraction_settings.extract_images_only", bool(value))
