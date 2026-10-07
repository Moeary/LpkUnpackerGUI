import sys
from pathlib import Path

# Nuitka never sets ``sys.frozen``; compiled modules get ``__compiled__``.
_COMPILED = globals().get("__compiled__")


def is_packaged() -> bool:
    return _COMPILED is not None or bool(getattr(sys, "frozen", False))


# Read-only files shipped inside the build.  In a Nuitka onefile build this is
# the temporary extraction directory, which is deleted when the app exits.
BUNDLE_ROOT = Path(__file__).resolve().parent.parent


def project_root() -> Path:
    """Directory that holds the visible EXE (or the source checkout).

    Writable state (``runtime/``) and user-placed tools live here so they
    survive restarts.  For standalone builds it equals ``BUNDLE_ROOT``.
    """
    containing_dir = getattr(_COMPILED, "containing_dir", None)
    if containing_dir:
        return Path(containing_dir).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return BUNDLE_ROOT


def app_executable() -> Path:
    """Command that relaunches the app from outside this process tree.

    In onefile builds ``sys.executable`` is the extracted inner binary, which
    disappears on exit, so persistent configs must point at the outer EXE.
    """
    original = getattr(_COMPILED, "original_argv0", None)
    if original:
        return Path(original).resolve()
    return Path(sys.executable)


PROJECT_ROOT = project_root()
ASSETS_DIR = BUNDLE_ROOT / "assets"
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
    "psd_projects": "psd_projects",
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
