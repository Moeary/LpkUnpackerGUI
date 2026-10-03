from PySide6.QtCore import QThread, Signal

from app.core.extract import ExtractMode, run_extraction_batch
from app.core.settings_manager import SettingsManager
from app.core.spine_converter import SpineConversionOptions


class ExtractorThread(QThread):
    """Qt wrapper around the core extraction batch pipeline."""

    progressUpdated = Signal(int)
    extractionFinished = Signal(object)
    extractionError = Signal(str)
    logMessage = Signal(str, str)

    def __init__(self, files, config_files, output_dir, extract_images_only=False, spine_conversion=None):
        super().__init__()
        self.files = files
        self.config_files = config_files
        self.output_dir = output_dir
        self.extract_images_only = extract_images_only
        self.spine_conversion = spine_conversion
        self._is_running = True

    def run(self):
        try:
            mode = ExtractMode.TEXTURES if self.extract_images_only else ExtractMode.FULL
            spine_conversion = self.spine_conversion
            if spine_conversion is None:
                spine_conversion = _spine_conversion_snapshot()
            if self.extract_images_only:
                # Texture-only extraction never invokes model conversion, even
                # if the setting was enabled after the thread was created.
                spine_conversion = SpineConversionOptions()
            result = run_extraction_batch(
                self.files,
                self.output_dir,
                mode,
                config_files=self.config_files,
                progress=self._on_progress,
                log=self.logMessage.emit,
                should_continue=lambda: self._is_running,
                texture_output_subdir=True,
                spine_conversion=spine_conversion,
            )
            if self._is_running:
                self.extractionFinished.emit(result)
        except Exception as exc:
            self.extractionError.emit(str(exc))

    def _on_progress(self, done: int, total: int):
        self.progressUpdated.emit(int(done / max(1, total) * 100))

    def stop(self):
        self._is_running = False


def _spine_conversion_snapshot() -> SpineConversionOptions:
    """Read conversion settings once for an extraction worker."""

    settings = SettingsManager()
    values = settings.get_spine_conversion_options()
    return SpineConversionOptions(**values)
