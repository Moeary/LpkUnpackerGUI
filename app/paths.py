import sys
from pathlib import Path


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = project_root()
ASSETS_DIR = PROJECT_ROOT / "assets"
APP_ICON = ASSETS_DIR / "app" / "icon.ico"
RUNTIME_DIR = PROJECT_ROOT / "runtime"
RUNTIME_TEMP_DIR = RUNTIME_DIR / "temp"
RUNTIME_OUTPUT_DIR = RUNTIME_DIR / "output"
RUNTIME_LOG_DIR = RUNTIME_DIR / "log"
RUNTIME_SETTINGS_FILE = RUNTIME_DIR / "setting.json"

OUTPUT_DIR_NAMES = {
    "live2d": "live2d",
    "spine": "spine",
    "unity": "unity",
    "psd": "psd",
    "textures": "textures",
    "live2dviewer_mod": "live2dviewer_mod",
}


def ensure_runtime_dirs() -> None:
    RUNTIME_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_LOG_DIR.mkdir(parents=True, exist_ok=True)
    for name in OUTPUT_DIR_NAMES.values():
        (RUNTIME_OUTPUT_DIR / name).mkdir(parents=True, exist_ok=True)


def runtime_output_dir(output_type: str) -> Path:
    dirname = OUTPUT_DIR_NAMES.get(output_type, output_type)
    return RUNTIME_OUTPUT_DIR / dirname
