import os
import json
import logging
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

logger = logging.getLogger("SettingsManager")

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
            "tools": {
                "archive_extractor_path": "",
                "assetstudio_cli_path": "",
                "cubism_core_dll_path": "",
            },
            "preview": {
                "image_limit": 48,
            },
            "psd": {
                "last_project_file": "",
                "project_files": [],
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
    
    def save_settings(self) -> bool:
        """Save current settings to file"""
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
        """Set a setting value"""
        keys = key.split('.')
        setting = self.settings
        
        # Navigate to the parent of the target key
        for k in keys[:-1]:
            if k not in setting:
                setting[k] = {}
            setting = setting[k]
        
        # Set the value
        setting[keys[-1]] = value
        
        # Auto-save if remember_paths is enabled
        if self.get("remember_paths", True):
            self.save_settings()
    
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
