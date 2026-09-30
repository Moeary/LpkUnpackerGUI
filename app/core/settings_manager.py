import os
import json
import logging
import re
import threading
from pathlib import Path
from typing import Dict, Any, Optional

from app.paths import (
    OUTPUT_DIR_NAMES,
    PROJECT_ROOT,
    RUNTIME_DIR,
    RUNTIME_OUTPUT_DIR,
    RUNTIME_SETTINGS_FILE,
    RUNTIME_TEMP_DIR,
    ensure_runtime_dirs,
    runtime_output_dir,
)
from app.core.toolchain import detect_toolchain_paths

logger = logging.getLogger("SettingsManager")

DEFAULT_SPINE_TARGET_VERSION = "3.8.75"
DEFAULT_FONT_FAMILY = ""
DEFAULT_FONT_SIZE = 10
MIN_FONT_SIZE = 8
MAX_FONT_SIZE = 32
# Naming aliases for callers that describe the preference as application-wide.
DEFAULT_APPLICATION_FONT_FAMILY = DEFAULT_FONT_FAMILY
DEFAULT_APPLICATION_FONT_SIZE = DEFAULT_FONT_SIZE
_SPINE_TARGET_VERSION_RE = re.compile(r"^(?:3\.(?:5|6|7|8)|4\.(?:0|1|2))\.\d+$")
_SETTINGS_WRITE_LOCK = threading.RLock()

class SettingsManager:
    """Manages application settings and user preferences"""
    
    def __init__(self, settings_file: Optional[str] = None):
        ensure_runtime_dirs()
        if settings_file:
            self.settings_file = str(Path(settings_file).resolve())
        else:
            self.settings_file = str(RUNTIME_SETTINGS_FILE)
        settings_exists = os.path.exists(self.settings_file)
        self.settings = self.load_settings()
        runtime_changed = self._normalize_runtime_paths()
        if not settings_exists:
            self.save_settings()
        elif runtime_changed:
            self.save_settings()
    
    def get_default_settings(self) -> Dict[str, Any]:
        output_paths = {
            key: str(runtime_output_dir(key))
            for key in OUTPUT_DIR_NAMES
        }
        return {
            "runtime": {
                "dir": str(RUNTIME_DIR),
                "temp_dir": str(RUNTIME_TEMP_DIR),
                "output_root": str(RUNTIME_OUTPUT_DIR),
            },
            "output_paths": output_paths,
            "last_lpk_path": "",
            "last_config_path": "",
            "last_output_path": str(RUNTIME_OUTPUT_DIR),
            "steam_path": "",
            "auto_detect_steam": True,
            "remember_paths": True,
            "theme": "auto",
            "language": "en_US",
            # An empty family means automatic selection.  The GUI resolves it
            # to the first installed CJK-capable family and falls back to the
            # platform system font when none is available.
            "font": {
                "family": DEFAULT_FONT_FAMILY,
                "size": DEFAULT_FONT_SIZE,
            },
            "tools": {
                "archive_extractor_path": "",
                "assetstudio_cli_path": "",
                "cubism_core_dll_path": "",
                "photoshop_path": "",
                "image_viewer_path": "",
                "spine_converter_path": "",
            },
            "preview": {
                "image_limit": 48,
                "texture_viewer": "internal",
                "spine_runtime_dir": "",
                "ui_state": {
                    "opacity": 100,
                    "rotation": 0,
                    "scale": 100,
                    "offset_x": 0,
                    "offset_y": 0,
                    "transparent_bg": True,
                    "mouse_tracking": True,
                    "auto_blink": True,
                    "auto_breath": True,
                    "motion_loop": False,
                    "motion_auto_play": False,
                    "freeze_pose": False,
                    "left_sidebar_visible": True,
                    "right_sidebar_visible": True,
                    "selected_motion": "",
                },
            },
            "spine_conversion": {
                "enabled": False,
                "target_version": DEFAULT_SPINE_TARGET_VERSION,
                "output_format": "json",
                "create_project": True,
            },
            "psd": {
                "last_project_file": "",
                "project_files": [],
                "resource_limits": {
                    "max_cpu_threads": 2,
                    "max_memory_mb": 8192,
                    "max_texture_pixels": 64 * 1024 * 1024,
                    "max_canvas_pixels": 48 * 1024 * 1024,
                    "max_total_layer_pixels": 192 * 1024 * 1024,
                    "max_layer_count": 512,
                    "mesh_max_dimension": 2048,
                },
            },
            "live2dviewer_mod": {
                "last_project_file": "",
                "project_files": [],
            },
            "window_geometry": {
                "width": 1000,
                "height": 700,
                "x": 100,
                "y": 100
            },
            "extraction_settings": {
                "create_subfolders": True,
                "overwrite_existing": False,
                "log_level": "INFO",
                "extract_images_only": False,
            }
        }

    def load_settings(self) -> Dict[str, Any]:
        """Load settings from file"""
        default_settings = self.get_default_settings()
        
        if not os.path.exists(self.settings_file):
            legacy_settings = self._load_legacy_settings()
            if legacy_settings:
                settings = self._merge_defaults(legacy_settings, default_settings)
                self.settings = settings
                self.save_settings()
                return settings
            logger.info("Settings file not found, using defaults")
            return default_settings
        
        try:
            with open(self.settings_file, 'r', encoding='utf-8') as f:
                loaded_settings = json.load(f)
                return self._merge_defaults(loaded_settings, default_settings)
        except Exception as e:
            logger.error(f"Failed to load settings: {e}")
            return default_settings

    def reload_settings(self) -> Dict[str, Any]:
        """Refresh this manager's cache from disk without changing the file."""

        self.settings = self.load_settings()
        return self.settings
    
    def save_settings(self) -> bool:
        """Save current settings to file"""
        with _SETTINGS_WRITE_LOCK:
            try:
                os.makedirs(os.path.dirname(self.settings_file), exist_ok=True)
                with open(self.settings_file, 'w', encoding='utf-8') as f:
                    json.dump(self.settings, f, indent=2, ensure_ascii=False)
                logger.debug("Settings saved successfully")
                return True
            except Exception as e:
                logger.error(f"Failed to save settings: {e}")
                return False
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get a setting value"""
        keys = key.split('.')
        value = self.settings
        
        try:
            for k in keys:
                value = value[k]
            return value
        except (KeyError, TypeError):
            return default
    
    def set(self, key: str, value: Any) -> None:
        """Set one value while merging it into the newest on-disk settings.

        Individual pages own separate ``SettingsManager`` instances.  A page
        may therefore hold a stale snapshot while another page has just saved
        a different setting.  Reload the file under the write lock and apply
        only this key so unrelated values from the stale snapshot cannot
        overwrite newer settings.  The in-memory cache is still updated when
        ``remember_paths`` disables automatic persistence, matching the
        historical behavior of this method.
        """

        self._set_nested_value(self.settings, key, value)

        # Preserve the existing opt-out: callers may still update the local
        # cache without writing user settings when remember_paths is false.
        if not self.get("remember_paths", True):
            return

        with _SETTINGS_WRITE_LOCK:
            latest = self._load_latest_settings_for_update()
            if latest is None:
                # Do not replace a usable cache or overwrite a file that could
                # not be read safely.
                return

            latest_remember_paths = self._get_nested_value(
                latest, "remember_paths", True
            )
            # A different manager may have disabled persistence after this
            # manager loaded its cache.  Respect that newer on-disk choice for
            # ordinary keys.  Setting remember_paths=True remains an explicit
            # opt-in and is allowed to persist itself.
            if key != "remember_paths" and not latest_remember_paths:
                return

            self._set_nested_value(latest, key, value)
            self.settings = latest
            self.save_settings()

    @staticmethod
    def _get_nested_value(settings: Dict[str, Any], key: str, default: Any = None) -> Any:
        value: Any = settings
        try:
            for item in key.split("."):
                value = value[item]
            return value
        except (KeyError, TypeError):
            return default

    @staticmethod
    def _set_nested_value(settings: Dict[str, Any], key: str, value: Any) -> None:
        keys = key.split(".")
        target = settings
        for item in keys[:-1]:
            child = target.get(item)
            if not isinstance(child, dict):
                child = {}
                target[item] = child
            target = child
        target[keys[-1]] = value

    def _load_latest_settings_for_update(self) -> Optional[Dict[str, Any]]:
        """Read the current settings file for a single-key update.

        Missing files are initialized from defaults.  A malformed or
        unreadable existing file is left untouched so a stale page cache cannot
        destroy it while attempting an automatic save.
        """

        if not os.path.exists(self.settings_file):
            return self.get_default_settings()
        try:
            with open(self.settings_file, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if not isinstance(loaded, dict):
                raise ValueError("settings root must be a JSON object")
            return self._merge_defaults(loaded, self.get_default_settings())
        except Exception as exc:
            logger.error("Failed to reload settings before update: %s", exc)
            return None
    
    def update_last_paths(self, lpk_path: str = None, config_path: str = None, output_path: str = None):
        """Update last used paths"""
        if not self.get("remember_paths", True):
            return
            
        if lpk_path:
            self.set("last_lpk_path", lpk_path)
        if config_path:
            self.set("last_config_path", config_path)
        if output_path:
            self.set("last_output_path", output_path)

    def get_output_root(self) -> str:
        return str(Path(self.get("runtime.output_root", str(RUNTIME_OUTPUT_DIR))).resolve())

    def get_font_family(self) -> str:
        """Return the configured application font family.

        An empty string intentionally selects the automatic CJK-aware default
        in :mod:`app.core.font_helper`.
        """

        return str(self.get("font.family", DEFAULT_FONT_FAMILY) or "").strip()

    def set_font_family(self, family: Optional[str]) -> None:
        self.set("font.family", str(family or "").strip())

    @staticmethod
    def _normalize_font_size(value: Any) -> int:
        try:
            size = int(float(value))
        except (TypeError, ValueError, OverflowError):
            size = DEFAULT_FONT_SIZE
        return max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, size))

    def get_font_size(self) -> int:
        """Return the persisted application font size in points."""

        return self._normalize_font_size(self.get("font.size", DEFAULT_FONT_SIZE))

    def set_font_size(self, size: Any) -> None:
        self.set("font.size", self._normalize_font_size(size))

    def set_font_preferences(self, family: Optional[str], size: Any) -> None:
        """Persist family and size as one user-facing preference operation."""

        self.set_font_family(family)
        self.set_font_size(size)

    def reset_font_preferences(self) -> None:
        """Restore automatic CJK-aware family selection and the base size."""

        self.set_font_preferences(DEFAULT_FONT_FAMILY, DEFAULT_FONT_SIZE)

    # Explicit aliases make the setting API easy to discover for integrations
    # that use “application font” terminology.
    get_application_font_family = get_font_family
    set_application_font_family = set_font_family
    get_application_font_size = get_font_size
    set_application_font_size = set_font_size

    def set_output_root(self, path: str):
        output_root = str(Path(path).resolve())
        self.set("runtime.output_root", output_root)
        self.settings["output_paths"] = {
            key: str(Path(output_root) / dirname)
            for key, dirname in OUTPUT_DIR_NAMES.items()
        }
        self.settings["last_output_path"] = output_root
        self.save_settings()

    def get_temp_dir(self) -> str:
        temp_dir = self.get("runtime.temp_dir", str(RUNTIME_TEMP_DIR))
        path = Path(temp_dir).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def get_output_dir(self, output_type: str = "live2d") -> str:
        output_paths = self.get("output_paths", {})
        output_path = output_paths.get(output_type)
        if not output_path:
            output_path = str(Path(self.get_output_root()) / OUTPUT_DIR_NAMES.get(output_type, output_type))
        path = Path(output_path).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def set_output_dir(self, output_type: str, path: str):
        output_paths = self.settings.setdefault("output_paths", {})
        output_paths[output_type] = str(Path(path).resolve())
        if output_type == "live2d":
            self.settings["last_output_path"] = output_paths[output_type]
        self.save_settings()

    def get_archive_extractor_path(self) -> str:
        return str(self.get("tools.archive_extractor_path", "") or "").strip()

    def set_archive_extractor_path(self, path: str):
        self.set("tools.archive_extractor_path", str(path or "").strip())

    def get_assetstudio_cli_path(self) -> str:
        return str(self.get("tools.assetstudio_cli_path", "") or "").strip()

    def set_assetstudio_cli_path(self, path: str):
        self.set("tools.assetstudio_cli_path", str(path or "").strip())

    def get_cubism_core_dll_path(self) -> str:
        return str(self.get("tools.cubism_core_dll_path", "") or "").strip()

    def set_cubism_core_dll_path(self, path: str):
        self.set("tools.cubism_core_dll_path", str(path or "").strip())

    def get_photoshop_path(self) -> str:
        return str(self.get("tools.photoshop_path", "") or "").strip()

    def set_photoshop_path(self, path: str):
        self.set("tools.photoshop_path", str(path or "").strip())

    def get_photoshop_executable(self) -> str:
        configured = Path(self.get_photoshop_path()).expanduser()
        if configured.is_file():
            return str(configured.resolve())
        if configured.is_dir():
            candidate = configured / "Photoshop.exe"
            if candidate.is_file():
                return str(candidate.resolve())
        return ""

    def get_texture_viewer_mode(self) -> str:
        mode = str(self.get("preview.texture_viewer", "internal") or "internal")
        return mode if mode in {"internal", "system", "custom"} else "internal"

    def set_texture_viewer_mode(self, mode: str):
        normalized = str(mode or "internal")
        self.set(
            "preview.texture_viewer",
            normalized if normalized in {"internal", "system", "custom"} else "internal",
        )

    def get_image_viewer_path(self) -> str:
        return str(self.get("tools.image_viewer_path", "") or "").strip()

    def set_image_viewer_path(self, path: str):
        self.set("tools.image_viewer_path", str(path or "").strip())

    def get_image_viewer_executable(self) -> str:
        configured = Path(self.get_image_viewer_path()).expanduser()
        return str(configured.resolve()) if configured.is_file() else ""

    def get_spine_converter_path(self) -> str:
        """Return the configured Spine skeleton converter executable path."""

        return str(self.get("tools.spine_converter_path", "") or "").strip()

    def get_spine_editor_path(self) -> str:
        return str(self.get("tools.spine_editor_path", "") or "").strip()

    def set_spine_editor_path(self, path: str):
        self.set("tools.spine_editor_path", str(path or "").strip())

    def get_spine_create_project(self) -> bool:
        return bool(self.get("spine_conversion.create_project", True))

    def set_spine_create_project(self, enabled: bool):
        self.set("spine_conversion.create_project", bool(enabled))

    def set_spine_converter_path(self, path: str):
        """Store the optional Spine skeleton converter executable path."""

        self.set("tools.spine_converter_path", str(path or "").strip())

    def get_spine_native_runtime_path(self) -> str:
        """Return the common native Spine runtime root path.

        ``preview.spine_runtime_dir`` is the stable persisted key shared with
        the preview page.  The historical method name remains as a narrow
        compatibility alias while the value now represents native bridge
        families.
        """

        return self.get_spine_runtime_dir()

    def set_spine_native_runtime_path(self, path: str):
        """Store the common root containing native Spine runtime families."""

        self.set_spine_runtime_dir(path)

    # Compatibility aliases for integrations that used ``runtime_dir`` while
    # the native builder interface was being finalized.  The stored value is
    # deliberately kept under the preview key shared with PreviewPage.
    def get_spine_native_runtime_dir(self) -> str:
        return self.get_spine_native_runtime_path()

    def set_spine_native_runtime_dir(self, path: str):
        self.set_spine_native_runtime_path(path)

    def get_spine_runtime_dir(self) -> str:
        """Return the configured common native Spine runtime root."""

        return str(self.get("preview.spine_runtime_dir", "") or "").strip()

    def set_spine_runtime_dir(self, path: str):
        """Store the native runtime root used by the Spine preview backend."""

        self.set("preview.spine_runtime_dir", str(path or "").strip())

    def get_spine_auto_convert(self) -> bool:
        """Return whether formal Spine unpack/export may create a converted copy."""

        return self.get_spine_conversion_enabled()

    def set_spine_auto_convert(self, enabled: bool):
        """Store the opt-in formal Spine unpack/export conversion switch."""

        self.set_spine_conversion_enabled(enabled)

    def get_spine_conversion_enabled(self) -> bool:
        """Return whether automatic Spine conversion is enabled."""

        return bool(self.get("spine_conversion.enabled", False))

    def set_spine_conversion_enabled(self, enabled: bool):
        """Store the opt-in automatic Spine conversion switch."""

        self.set("spine_conversion.enabled", bool(enabled))

    def get_spine_target_version(self) -> str:
        """Return the complete Spine version used for automatic conversion."""

        return self.get_spine_conversion_target_version()

    def set_spine_target_version(self, version: str):
        """Store a complete Spine target version without rewriting its value."""

        self.set_spine_conversion_target_version(version)

    def get_spine_conversion_target_version(self) -> str:
        """Return the complete Spine version used by automatic conversion."""

        value = self.get("spine_conversion.target_version", None)
        if value is None:
            return DEFAULT_SPINE_TARGET_VERSION
        # Preserve an existing invalid value so the settings page can show it
        # and the converter can report the actual validation error.  Only an
        # absent key receives the default.
        return str(value).strip()

    @staticmethod
    def validate_spine_conversion_target_version(version: str) -> str:
        """Validate and normalize a complete supported Spine target version."""

        if not isinstance(version, str):
            raise ValueError(
                f"target_version must be a complete supported x.y.z string, got {version!r}"
            )
        normalized = version.strip()
        if not _SPINE_TARGET_VERSION_RE.fullmatch(normalized):
            raise ValueError(
                f"Unsupported target_version {version!r}; use a complete supported "
                "3.5.x/3.6.x/3.7.x/3.8.x/4.0.x/4.1.x/4.2.x version."
            )
        return normalized

    def set_spine_conversion_target_version(self, version: str):
        """Store the complete Spine target version without rewriting it."""

        normalized = self.validate_spine_conversion_target_version(version)
        self.set("spine_conversion.target_version", normalized)

    def get_spine_conversion_output_format(self) -> str:
        """Return the automatic conversion output format."""

        value = str(self.get("spine_conversion.output_format", "json") or "").strip().lower()
        return value if value in {"json", "skel"} else "json"

    def set_spine_conversion_output_format(self, output_format: str):
        """Store the automatic conversion output format."""

        value = str(output_format or "").strip().lower()
        self.set("spine_conversion.output_format", value if value in {"json", "skel"} else "json")

    def detect_toolchain_paths(self) -> Dict[str, str]:
        """Inspect optional tools without changing user settings."""

        configured = {
            "tools.archive_extractor_path": self.get_archive_extractor_path(),
            "tools.assetstudio_cli_path": self.get_assetstudio_cli_path(),
            "tools.cubism_core_dll_path": self.get_cubism_core_dll_path(),
            "tools.photoshop_path": self.get_photoshop_path(),
            "tools.spine_converter_path": self.get_spine_converter_path(),
            "preview.spine_runtime_dir": self.get_spine_runtime_dir(),
        }
        return detect_toolchain_paths(configured)

    def auto_detect_toolchain(
        self,
        *,
        overwrite_invalid: bool = False,
        save: bool = True,
    ) -> Dict[str, str]:
        """Fill only empty tool settings with discovered paths.

        Existing values are retained by default, including an invalid path a
        user may be repairing later.  ``overwrite_invalid`` is available for
        an explicit repair action, but the settings page does not use it for
        its normal one-click detection.  The write is batched so a detection
        pass creates at most one settings-file update.
        """

        detected = self.detect_toolchain_paths()
        changed: Dict[str, str] = {}
        for key, value in detected.items():
            current = str(self.get(key, "") or "").strip()
            if current and not overwrite_invalid:
                continue
            if current == value:
                continue
            keys = key.split(".")
            target = self.settings
            for item in keys[:-1]:
                target = target.setdefault(item, {})
            target[keys[-1]] = value
            changed[key] = value
        if changed and save:
            self.save_settings()
        return changed

    def reset_runtime_to_project(self) -> None:
        runtime = self.settings.setdefault("runtime", {})
        runtime["dir"] = str(RUNTIME_DIR)
        runtime["temp_dir"] = str(RUNTIME_TEMP_DIR)
        runtime["output_root"] = str(RUNTIME_OUTPUT_DIR)
        self.settings["output_paths"] = {
            key: str(runtime_output_dir(key))
            for key in OUTPUT_DIR_NAMES
        }
        self.settings["last_output_path"] = str(RUNTIME_OUTPUT_DIR)
        self.save_settings()

    def _normalize_runtime_paths(self) -> bool:
        changed = False
        runtime = self.settings.setdefault("runtime", {})

        if runtime.get("dir") != str(RUNTIME_DIR):
            runtime["dir"] = str(RUNTIME_DIR)
            changed = True
        if runtime.get("temp_dir") != str(RUNTIME_TEMP_DIR):
            runtime["temp_dir"] = str(RUNTIME_TEMP_DIR)
            changed = True

        output_root = str(runtime.get("output_root") or RUNTIME_OUTPUT_DIR)
        if self._is_stale_managed_runtime_path(output_root, ("runtime", "output")):
            output_root = str(RUNTIME_OUTPUT_DIR)
            runtime["output_root"] = output_root
            changed = True
        elif runtime.get("output_root") != output_root:
            runtime["output_root"] = output_root
            changed = True

        output_paths = self.settings.setdefault("output_paths", {})
        for key, dirname in OUTPUT_DIR_NAMES.items():
            current = str(output_paths.get(key) or "")
            expected = str(Path(output_root) / dirname)
            if not current or self._is_stale_managed_runtime_path(
                current,
                ("runtime", "output", dirname),
            ):
                output_paths[key] = expected
                changed = True

        if str(self.settings.get("last_output_path") or "") and self._is_stale_managed_runtime_path(
            str(self.settings.get("last_output_path")),
            ("runtime", "output"),
        ):
            self.settings["last_output_path"] = output_root
            changed = True

        return changed

    @staticmethod
    def _is_stale_managed_runtime_path(path: str, suffix: tuple[str, ...]) -> bool:
        if not path:
            return True
        try:
            resolved = Path(path).resolve()
        except Exception:
            return True

        current = {
            ("runtime", "output"): RUNTIME_OUTPUT_DIR.resolve(),
            ("runtime", "temp"): RUNTIME_TEMP_DIR.resolve(),
        }.get(suffix)
        if len(suffix) == 3 and suffix[:2] == ("runtime", "output"):
            current = (RUNTIME_OUTPUT_DIR / suffix[2]).resolve()
        if current and resolved == current:
            return False

        parts = tuple(part.lower() for part in resolved.parts[-len(suffix):])
        return parts == tuple(part.lower() for part in suffix)
    
    def update_window_geometry(self, width: int, height: int, x: int, y: int):
        """Update window geometry"""
        self.set("window_geometry.width", width)
        self.set("window_geometry.height", height)
        self.set("window_geometry.x", x)
        self.set("window_geometry.y", y)
    
    def get_recent_files(self, max_count: int = 10) -> list:
        """Get list of recently used files"""
        recent = self.get("recent_files", [])
        return recent[:max_count]
    
    def add_recent_file(self, file_path: str, file_type: str = "lpk"):
        """Add a file to recent files list"""
        recent = self.get("recent_files", [])
        
        # Remove if already exists
        recent = [item for item in recent if item.get("path") != file_path]
        
        # Add to beginning
        recent.insert(0, {
            "path": file_path,
            "type": file_type,
            "timestamp": self._get_timestamp()
        })
        
        # Limit to 10 items
        recent = recent[:10]
        
        self.set("recent_files", recent)
    
    def _get_timestamp(self) -> str:
        """Get current timestamp as string"""
        import datetime
        return datetime.datetime.now().isoformat()
    
    def reset_to_defaults(self):
        """Reset all settings to defaults"""
        if os.path.exists(self.settings_file):
            os.remove(self.settings_file)
        self.settings = self.get_default_settings()
        self.save_settings()
        logger.info("Settings reset to defaults")

    def _load_legacy_settings(self) -> Optional[Dict[str, Any]]:
        legacy_file = PROJECT_ROOT / "settings.json"
        if Path(self.settings_file) == RUNTIME_SETTINGS_FILE and legacy_file.exists():
            try:
                with open(legacy_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as exc:
                logger.warning("Failed to load legacy settings.json: %s", exc)
        return None

    def _merge_defaults(self, loaded: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(defaults)
        for key, value in loaded.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = self._merge_defaults(value, merged[key])
            else:
                merged[key] = value
        return merged
