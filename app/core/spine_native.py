"""ctypes adapter for the versioned official spine-cpp bridge.

The bridge is intentionally a small POD/C ABI.  It keeps all Spine C++
objects on the native side and exposes animation state plus render batches to
the Qt canvas.  No web runtime, HTTP server, or browser is involved here.
"""

from __future__ import annotations

import ctypes
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from app.paths import PROJECT_ROOT


class SpineNativeError(RuntimeError):
    """Native Spine runtime could not load or render an asset."""


class _Vertex(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_float), ("y", ctypes.c_float),
        ("u", ctypes.c_float), ("v", ctypes.c_float),
        ("r", ctypes.c_float), ("g", ctypes.c_float),
        ("b", ctypes.c_float), ("a", ctypes.c_float),
    ]


class _Batch(ctypes.Structure):
    _fields_ = [
        ("vertex_offset", ctypes.c_uint32), ("vertex_count", ctypes.c_uint32),
        ("index_offset", ctypes.c_uint32), ("index_count", ctypes.c_uint32),
        ("page", ctypes.c_int32), ("blend", ctypes.c_int32),
    ]


class _PageInfo(ctypes.Structure):
    _fields_ = [
        ("path", ctypes.c_char_p), ("width", ctypes.c_int32),
        ("height", ctypes.c_int32), ("pma", ctypes.c_int32),
    ]


@dataclass(frozen=True)
class SpineNativePage:
    path: Path
    width: int
    height: int
    pma: bool = False


@dataclass(frozen=True)
class SpineNativeBatch:
    vertices: tuple[_Vertex, ...]
    indices: tuple[int, ...]
    page: int
    blend: int


def _as_path(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve()


def find_native_library(runtime_root: str | os.PathLike[str] | None = None,
                        family: str | None = None) -> Path | None:
    """Find a packaged bridge without guessing a different runtime family.

    A sibling ``spine_native.json`` is provenance, not decoration: when a
    family is requested, a library whose manifest names another family is
    rejected.  An explicitly selected runtime root never silently falls back
    to an unrelated global bridge.
    """

    root = _as_path(runtime_root) if runtime_root else None
    candidates: list[Path] = []
    explicit = root is not None
    if root:
        if family:
            candidates.extend((root / "native" / family / "spine_bridge.dll",
                               root / "native" / family / "spine_bridge.so",
                               root / family / "spine_bridge.dll",
                               root / family / "spine_bridge.so"))
        candidates.extend((root / "native" / "spine_bridge.dll",
                           root / "native" / "spine_bridge.so",
                           root / "spine_bridge.dll",
                           root / "spine_bridge.so"))
    package_roots: list[Path] = []
    if not explicit:
        # Installed builds place downloadable bridges beside the executable
        # (``tools/spine_native``); source checkouts keep the same package
        # under ``runtime/tools``.  Keep both roots in the search list so a
        # frozen app does not accidentally load a development DLL.
        project_tools_root = PROJECT_ROOT / "tools"
        tools_root = PROJECT_ROOT / "runtime" / "tools"
        # ``spine_native`` is the installer-owned package root; the older
        # ``spine/native`` layout remains supported for development builds.
        package_roots.extend((project_tools_root / "spine_native",
                              tools_root / "spine_native",
                              tools_root / "spine" / "native"))
        for package_root in package_roots:
            if family:
                candidates.extend((package_root / family / "spine_bridge.dll",
                                   package_root / family / "spine_bridge.so"))
            try:
                candidates.extend(package_root.rglob("spine_bridge.dll"))
                candidates.extend(package_root.rglob("spine_bridge.so"))
            except OSError:
                pass
    elif family:
        # A selected runtime directory may be the common ``spine`` tools root
        # while the installer stores bridges in its sibling ``spine_native``.
        tools_root = root.parent if root.name.lower() in {"spine", "spine_native"} else None
        if tools_root:
            package_root = tools_root / "spine_native"
            try:
                candidates.extend(package_root.rglob("spine_bridge.dll"))
                candidates.extend(package_root.rglob("spine_bridge.so"))
            except OSError:
                pass

    def matches(candidate: Path) -> bool:
        if not family:
            return True
        manifest = candidate.with_name("spine_native.json")
        try:
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Family-specific directories are safe only when the package
            # layout itself carries the requested family; generic locations
            # require provenance to prevent a 3.8/4.0 mix-up.
            return candidate.parent.name == family
        return str(metadata.get("runtimeFamily") or metadata.get("family") or "") == family

    for candidate in candidates:
        if candidate.is_file() and matches(candidate):
            return candidate
    return None


def _configure(function: Any, restype: Any, *argtypes: Any) -> None:
    function.restype = restype
    function.argtypes = list(argtypes)


class SpineNativeModel:
    """One loaded skeleton and its native animation state."""

    def __init__(self, library: str | os.PathLike[str], skeleton_path: str | os.PathLike[str],
                 atlas_path: str | os.PathLike[str], skeleton_format: str | None = None):
        self.library_path = _as_path(library)
        self.skeleton_path = _as_path(skeleton_path)
        self.atlas_path = _as_path(atlas_path)
        if not self.library_path.is_file():
            raise SpineNativeError(f"Native Spine bridge not found: {self.library_path}")
        if not self.skeleton_path.is_file():
            raise SpineNativeError(f"Spine skeleton not found: {self.skeleton_path}")
        if not self.atlas_path.is_file():
            raise SpineNativeError(f"Spine atlas not found: {self.atlas_path}")
        loader = ctypes.WinDLL if os.name == "nt" else ctypes.CDLL
        try:
            self._lib = loader(str(self.library_path))
        except OSError as exc:
            raise SpineNativeError(f"Could not load Spine native bridge: {exc}") from exc
        self._bind()
        self._handle = self._create()
        if not self._handle:
            raise SpineNativeError("Native Spine bridge returned an invalid handle.")
        self._closed = False
        try:
            skeleton_data = self.skeleton_path.read_bytes()
            atlas_data = self.atlas_path.read_bytes()
            fmt = (skeleton_format or ("json" if self.skeleton_path.suffix.lower() == ".json" else "binary")).lower()
            format_id = 0 if fmt in {"json", "text"} else 1
            self._load(skeleton_data, format_id, atlas_data, str(self.atlas_path.parent))
            self.pages = self._read_pages()
            self.animations = self._read_animations()
            self.skins = self._read_skins()
            self.selected_animation = self._choose_animation()
            self.selected_skin = self.skins[0] if self.skins else ""
            self.loop = True
            self.paused = False
            self._capacity = (0, 0, 0)
        except Exception:
            self.close()
            raise

    def _bind(self) -> None:
        p = ctypes.c_void_p
        cstr = ctypes.c_char_p
        _configure(self._lib.spine_native_create, p)
        _configure(self._lib.spine_native_destroy, None, p)
        _configure(self._lib.spine_native_load_memory, ctypes.c_int, p, p, ctypes.c_int32,
                   ctypes.c_int32, p, ctypes.c_int32, cstr)
        _configure(self._lib.spine_native_last_error, cstr, p)
        _configure(self._lib.spine_native_skeleton_version, cstr, p)
        _configure(self._lib.spine_native_animation_count, ctypes.c_int32, p)
        _configure(self._lib.spine_native_animation_name, cstr, p, ctypes.c_int32)
        _configure(self._lib.spine_native_animation_duration, ctypes.c_float, p, ctypes.c_int32)
        _configure(self._lib.spine_native_skin_count, ctypes.c_int32, p)
        _configure(self._lib.spine_native_skin_name, cstr, p, ctypes.c_int32)
        _configure(self._lib.spine_native_page_count, ctypes.c_int32, p)
        _configure(self._lib.spine_native_page_info, _PageInfo, p, ctypes.c_int32)
        for name, restype, args in (
            ("spine_native_set_skin", ctypes.c_int, (p, cstr)),
            ("spine_native_set_animation", ctypes.c_int, (p, cstr, ctypes.c_int)),
            ("spine_native_set_loop", ctypes.c_int, (p, ctypes.c_int)),
            ("spine_native_set_paused", ctypes.c_int, (p, ctypes.c_int)),
            ("spine_native_set_time", ctypes.c_int, (p, ctypes.c_float)),
            ("spine_native_reset_pose", ctypes.c_int, (p,)),
            ("spine_native_time", ctypes.c_float, (p,)),
            ("spine_native_duration", ctypes.c_float, (p,)),
            ("spine_native_update", None, (p, ctypes.c_float)),
            ("spine_native_required_sizes", ctypes.c_int, (p, ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_int32))),
            ("spine_native_render", ctypes.c_int, (p, ctypes.POINTER(_Vertex), ctypes.c_int32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int32, ctypes.POINTER(_Batch), ctypes.c_int32)),
            ("spine_native_bounds", ctypes.c_int, (p, ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float))),
        ):
            _configure(getattr(self._lib, name), restype, *args)
        self._create = self._lib.spine_native_create
        self._destroy = self._lib.spine_native_destroy
        self._native_load = self._lib.spine_native_load_memory

    def _load(self, skeleton_data: bytes, format_id: int, atlas_data: bytes, atlas_dir: str) -> None:
        skeleton_buffer = ctypes.create_string_buffer(skeleton_data)
        atlas_buffer = ctypes.create_string_buffer(atlas_data)
        ok = self._native_load(self._handle, ctypes.cast(skeleton_buffer, ctypes.c_void_p), len(skeleton_data), format_id,
                        ctypes.cast(atlas_buffer, ctypes.c_void_p), len(atlas_data), atlas_dir.encode("utf-8"))
        if not ok:
            message = self._lib.spine_native_last_error(self._handle)
            text = message.decode("utf-8", "replace") if message else "Spine native load failed."
            raise SpineNativeError(text)

    @staticmethod
    def _decode(value: bytes | None) -> str:
        return value.decode("utf-8", "replace") if value else ""

    def _read_pages(self) -> tuple[SpineNativePage, ...]:
        pma_flags = self._read_atlas_pma_flags()
        pages = []
        for index in range(int(self._lib.spine_native_page_count(self._handle))):
            info = self._lib.spine_native_page_info(self._handle, index)
            raw = self._decode(info.path)
            path = Path(raw)
            if not path.is_absolute():
                path = self.atlas_path.parent / path
            pages.append(SpineNativePage(path.resolve(), int(info.width), int(info.height), bool(info.pma) or index in pma_flags))
        return tuple(pages)

    def _read_atlas_pma_flags(self) -> set[int]:
        """Read the page-level pma flag for 3.8 bridges without AtlasPage.pma."""
        flags: set[int] = set()
        try:
            page_index = -1
            for raw_line in self.atlas_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw_line.strip().lower()
                if not line:
                    continue
                if line.startswith("pma:"):
                    if line.split(":", 1)[1].strip() in {"true", "1", "yes"} and page_index >= 0:
                        flags.add(page_index)
                elif ":" not in line and page_index < int(self._lib.spine_native_page_count(self._handle)) - 1:
                    # A page name is followed by a size line; count only page
                    # headers, which are the non-indented lines before size.
                    if page_index < 0 or line.endswith((".png", ".jpg", ".jpeg", ".webp")):
                        page_index += 1
        except (OSError, UnicodeError):
            return set()
        return flags

    def _read_animations(self) -> tuple[tuple[str, float], ...]:
        return tuple((self._decode(self._lib.spine_native_animation_name(self._handle, i)),
                      float(self._lib.spine_native_animation_duration(self._handle, i)))
                     for i in range(int(self._lib.spine_native_animation_count(self._handle))) )

    def _read_skins(self) -> tuple[str, ...]:
        return tuple(self._decode(self._lib.spine_native_skin_name(self._handle, i))
                     for i in range(int(self._lib.spine_native_skin_count(self._handle))))

    def _choose_animation(self) -> str:
        lowered = {name.lower(): name for name, _ in self.animations}
        return lowered.get("normal") or lowered.get("idle") or next(
            (name for name, duration in self.animations if duration > 0.01),
            self.animations[0][0] if self.animations else "",
        )

    @property
    def version(self) -> str:
        return self._decode(self._lib.spine_native_skeleton_version(self._handle))

    @property
    def duration(self) -> float:
        return float(self._lib.spine_native_duration(self._handle))

    @property
    def time(self) -> float:
        return float(self._lib.spine_native_time(self._handle))

    def set_skin(self, name: str) -> bool:
        if not name:
            return False
        ok = bool(self._lib.spine_native_set_skin(self._handle, name.encode("utf-8")))
        if ok:
            self.selected_skin = name
        return ok

    def set_animation(self, name: str, loop: bool | None = None) -> bool:
        if not name:
            return False
        if loop is not None:
            self.loop = bool(loop)
        ok = bool(self._lib.spine_native_set_animation(self._handle, name.encode("utf-8"), int(self.loop)))
        if ok:
            self.selected_animation = name
        return ok

    def set_loop(self, value: bool) -> bool:
        self.loop = bool(value)
        return bool(self._lib.spine_native_set_loop(self._handle, int(self.loop)))

    def set_paused(self, value: bool) -> bool:
        self.paused = bool(value)
        return bool(self._lib.spine_native_set_paused(self._handle, int(self.paused)))

    def set_time(self, value: float) -> bool:
        return bool(self._lib.spine_native_set_time(self._handle, float(value)))

    def reset_pose(self) -> bool:
        ok = bool(self._lib.spine_native_reset_pose(self._handle))
        if ok:
            self.selected_animation = self._choose_animation()
        return ok

    def update(self, delta: float) -> None:
        self._lib.spine_native_update(self._handle, max(0.0, float(delta)))

    def render(self) -> tuple[tuple[_Vertex, ...], tuple[int, ...], tuple[_Batch, ...]]:
        v, i, b = ctypes.c_int32(), ctypes.c_int32(), ctypes.c_int32()
        if not self._lib.spine_native_required_sizes(self._handle, ctypes.byref(v), ctypes.byref(i), ctypes.byref(b)):
            return (), (), ()
        capacity = (max(1, v.value), max(1, i.value), max(1, b.value))
        vertex_array = (_Vertex * capacity[0])()
        index_array = (ctypes.c_uint32 * capacity[1])()
        batch_array = (_Batch * capacity[2])()
        count = self._lib.spine_native_render(self._handle, vertex_array, capacity[0], index_array, capacity[1], batch_array, capacity[2])
        if count < 0:
            raise SpineNativeError("Native Spine render buffer changed during capture.")
        return tuple(vertex_array[:v.value]), tuple(int(x) for x in index_array[:i.value]), tuple(batch_array[:count])

    def bounds(self) -> tuple[float, float, float, float]:
        values = [ctypes.c_float() for _ in range(4)]
        if not self._lib.spine_native_bounds(self._handle, *(ctypes.byref(item) for item in values)):
            return (0.0, 0.0, 1.0, 1.0)
        return tuple(float(item.value) for item in values)  # type: ignore[return-value]

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        if getattr(self, "_handle", None):
            self._destroy(self._handle)
            self._handle = None

    def __enter__(self) -> "SpineNativeModel":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def __del__(self):  # pragma: no cover - interpreter shutdown order varies
        try:
            self.close()
        except Exception:
            pass


__all__ = ["SpineNativeError", "SpineNativeModel", "SpineNativePage", "find_native_library"]
