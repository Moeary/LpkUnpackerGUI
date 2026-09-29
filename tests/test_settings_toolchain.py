import json
from pathlib import Path

from app.core import toolchain
from app.core.settings_manager import SettingsManager


def _make_tool_layout(root: Path) -> dict[str, Path]:
    assetstudio = (
        root
        / "runtime"
        / "tools"
        / "AssetStudioCLI"
        / "AssetStudioModCLI_net472_win32_64"
        / "AssetStudioModCLI.exe"
    )
    cubism = root / "app" / "tools" / "CubismCore" / "Live2DCubismCore.dll"
    archive = root / "runtime" / "tools" / "archive" / "bz.exe"
    spine_root = root / "runtime" / "tools" / "spine" / "3.8"
    for path in (assetstudio, cubism, archive):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"tool")
    spine_root.mkdir(parents=True, exist_ok=True)
    (spine_root / "spine_runtime.json").write_text(
        json.dumps(
            {
                "version": "3.8.99",
                "webgl": "spine-webgl.js",
                "core": "spine-core.js",
            }
        ),
        encoding="utf-8",
    )
    (spine_root / "spine-webgl.js").write_text("// webgl", encoding="utf-8")
    (spine_root / "spine-core.js").write_text("// core", encoding="utf-8")
    return {
        "assetstudio": assetstudio,
        "cubism": cubism,
        "archive": archive,
        "spine": spine_root,
    }


def test_toolchain_discovery_scans_runtime_tools(monkeypatch, tmp_path):
    paths = _make_tool_layout(tmp_path)
    monkeypatch.setattr(toolchain, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(toolchain.shutil, "which", lambda name: None)

    assert toolchain.find_assetstudio_cli() == paths["assetstudio"].resolve()
    assert toolchain.find_cubism_core() == paths["cubism"].resolve()
    assert toolchain.find_archive_extractor() == paths["archive"].resolve()
    assert toolchain.find_spine_runtime() == paths["spine"].resolve()


def test_auto_detect_preserves_existing_preferences(monkeypatch, tmp_path):
    paths = _make_tool_layout(tmp_path)
    monkeypatch.setattr(toolchain, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(toolchain.shutil, "which", lambda name: None)
    settings_path = tmp_path / "setting.json"
    settings_path.write_text(
        json.dumps(
            {
                "language": "zh_CN",
                "theme": "dark",
                "tools": {"photoshop_path": "C:/user/Photoshop.exe"},
            }
        ),
        encoding="utf-8",
    )

    manager = SettingsManager(settings_path)
    changed = manager.auto_detect_toolchain()
    saved = json.loads(settings_path.read_text(encoding="utf-8"))

    assert saved["language"] == "zh_CN"
    assert saved["theme"] == "dark"
    assert saved["tools"]["photoshop_path"] == "C:/user/Photoshop.exe"
    assert saved["tools"]["assetstudio_cli_path"] == str(paths["assetstudio"].resolve())
    assert saved["tools"]["cubism_core_dll_path"] == str(paths["cubism"].resolve())
    assert saved["tools"]["archive_extractor_path"] == str(paths["archive"].resolve())
    assert saved["preview"]["spine_runtime_dir"] == str(paths["spine"].resolve())
    assert "tools.photoshop_path" not in changed
