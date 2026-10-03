"""Select one validated preview wrapper before Cubism Framework initialization."""
from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys

from app.paths import PROJECT_ROOT

_DLL_DIRECTORY_HANDLES = []
_STATUS = {"supported": False, "reason": "Preview runtime has not been selected.", "path": None}


def runtime_candidates():
    selected = os.environ.get("LPK_LIVE2D_NATIVE_DIR")
    if selected:
        return [Path(selected).expanduser().resolve()]
    return [PROJECT_ROOT / "tools/live2d_native/opacity-v1",
            PROJECT_ROOT / "runtime/tools/live2d_native/opacity-v1"]


def validate_preview_runtime(root: Path) -> dict:
    root = root.resolve()
    metadata = json.loads((root / "live2d_native.json").read_text(encoding="utf-8"))
    if (metadata.get("format") != "LpkUnpacker.Live2DPreviewRuntime" or metadata.get("version") != 1
            or metadata.get("drawable_opacity_api_version") != 1 or metadata.get("upstream_version") != "0.7.0"
            or metadata.get("sdk_version") != "5-r.4.1" or metadata.get("python_abi") != "cp310-abi3"
            or metadata.get("architecture") != "win_amd64"):
        raise RuntimeError("The preview runtime version/API/ABI does not match this editor.")
    if sys.platform != "win32" or struct.calcsize("P") != 8 or sys.version_info < (3, 10):
        raise RuntimeError("The preview runtime requires 64-bit Windows CPython 3.10 or newer.")
    files = metadata.get("files", {})
    if not {"_v3cpp.pyd", "LpkPreviewCubismCore.dll"}.issubset(files) or not any(
            relative.startswith("FrameworkShaders/") for relative in files):
        raise RuntimeError("The preview runtime manifest is incomplete.")
    for relative, digest in files.items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise RuntimeError(f"Missing or escaping preview runtime file: {relative}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"Preview runtime SHA256 mismatch: {relative}")
    return metadata


def preview_native_status() -> dict:
    return dict(_STATUS)


def load_preview_runtime():
    """Import either the controlled overlay or the installed wrapper, once.

    Never switch an initialized wrapper or create two Cubism Frameworks in one
    GL context. Fallback supports ordinary preview but explicitly lacks the
    drawable-opacity capability; callers must disable that feature honestly.
    """
    name = "live2d.v3._v3cpp"
    existing = sys.modules.get(name)
    if existing is not None:
        supported = (getattr(existing, "DRAWABLE_OPACITY_API_VERSION", None) == 1
                     and getattr(existing, "UPSTREAM_VERSION", None) == "0.7.0")
        _STATUS.update(supported=supported, reason="" if supported else "The installed wrapper was already imported.",
                       path=getattr(existing, "__file__", None))
        return importlib.import_module("live2d.v3")
    selected_root = None
    error = "The drawable-opacity preview runtime is not built."
    if os.environ.get("LPK_DISABLE_DRAWABLE_OPACITY") == "1":
        error = "Drawable-opacity preview is explicitly disabled."
    else:
        for root in runtime_candidates():
            if not (root / "live2d_native.json").is_file():
                continue
            try:
                validate_preview_runtime(root)
                if importlib.metadata.version("live2d-py") != "0.7.0":
                    raise RuntimeError("The controlled overlay requires live2d-py 0.7.0's Python wrapper.")
                handle = os.add_dll_directory(str(root))
                try:
                    spec = importlib.util.spec_from_file_location(name, root / "_v3cpp.pyd")
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    if (getattr(module, "DRAWABLE_OPACITY_API_VERSION", None) != 1
                            or getattr(module, "UPSTREAM_VERSION", None) != "0.7.0"
                            or not hasattr(module.Model, "SetDrawableOpacityOverrides")
                            or not hasattr(module.Model, "GetIntrinsicDrawableOpacity")):
                        raise RuntimeError("The loaded preview extension does not provide the expected API.")
                except Exception:
                    handle.close()
                    raise
                _DLL_DIRECTORY_HANDLES.append(handle)
                sys.modules[name] = module
                selected_root = root
                break
            except Exception as exc:
                error = str(exc)
    runtime = importlib.import_module("live2d.v3")
    if selected_root is not None:
        # Use the matching SDK shaders. The installed module's init_internal
        # comes from the selected extension; no second wrapper is initialized.
        runtime.init = lambda: runtime.init_internal(str(selected_root))
        _STATUS.update(supported=True, reason="", path=str(selected_root))
    else:
        _STATUS.update(supported=False, reason=error, path=getattr(runtime, "__file__", None))
    return runtime
