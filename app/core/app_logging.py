from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

from app.paths import RUNTIME_LOG_DIR, ensure_runtime_dirs


class TeeStream:
    def __init__(self, stream, log_file):
        self.stream = stream
        self.log_file = log_file

    def write(self, text):
        if not text:
            return
        try:
            self.stream.write(text)
        except Exception:
            pass
        try:
            self.log_file.write(text)
            self.log_file.flush()
        except Exception:
            pass

    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            pass
        try:
            self.log_file.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return self.stream.isatty()
        except Exception:
            return False


def setup_runtime_logging() -> Path:
    ensure_runtime_dirs()
    log_path = RUNTIME_LOG_DIR / f"{datetime.now():%Y%m%d_%H%M%S}.log"
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.__stdout__),
        ],
        force=True,
    )

    sys.stdout = TeeStream(sys.__stdout__, log_file)
    sys.stderr = TeeStream(sys.__stderr__, log_file)

    def excepthook(exc_type, exc, tb):
        logging.getLogger(__name__).exception(
            "Uncaught exception",
            exc_info=(exc_type, exc, tb),
        )
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = excepthook
    logging.getLogger(__name__).info("Runtime log initialized: %s", log_path)
    return log_path
