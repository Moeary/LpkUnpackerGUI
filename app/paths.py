import sys
from pathlib import Path


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = project_root()
ASSETS_DIR = PROJECT_ROOT / "assets"
APP_ICON = ASSETS_DIR / "app" / "icon.ico"
