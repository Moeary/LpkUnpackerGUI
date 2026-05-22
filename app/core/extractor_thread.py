from PySide6.QtCore import QThread, Signal

from app.core.extract import ExtractMode, run_extraction_batch


class ExtractorThread(QThread):
    """Qt wrapper around the core extraction batch pipeline."""

    progressUpdated = Signal(int)
    extractionFinished = Signal(object)
    extractionError = Signal(str)
    logMessage = Signal(str, str)

    def __init__(self, files, config_files, output_dir, extract_images_only=False):
        super().__init__()
        self.files = files
        self.config_files = config_files
        self.output_dir = output_dir
        self.extract_images_only = extract_images_only
        self._is_running = True

    def run(self):
        try:
            mode = ExtractMode.TEXTURES if self.extract_images_only else ExtractMode.FULL
            result = run_extraction_batch(
                self.files,
                self.output_dir,
                mode,
                config_files=self.config_files,
                progress=self._on_progress,
                log=self.logMessage.emit,
                should_continue=lambda: self._is_running,
                texture_output_subdir=True,
            )
            if self._is_running:
                self.extractionFinished.emit(result)
        except Exception as exc:
            self.extractionError.emit(str(exc))

    def _on_progress(self, done: int, total: int):
        self.progressUpdated.emit(int(done / max(1, total) * 100))

    def stop(self):
        self._is_running = False
