import ctypes
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


ProgressCallback = Callable[[int, str], None]
DRAWABLES_FORMAT = "LpkUnpacker.Live2DDrawables"
DRAWABLES_VERSION = 1

MOC_VERSION_NAMES = {
    0: "unknown",
    1: "3.0",
    2: "3.3",
    3: "4.0",
    4: "4.2",
    5: "5.0",
    6: "5.3",
}

BLEND_MODE_NAMES = {
    0: "normal",
    1: "add-compatible",
    2: "multiply-compatible",
    3: "add",
    4: "add-glow",
    5: "darken",
    6: "multiply",
    7: "color-burn",
    8: "linear-burn",
    9: "lighten",
    10: "screen",
    11: "color-dodge",
    12: "overlay",
    13: "soft-light",
    14: "hard-light",
    15: "linear-light",
    16: "hue",
    17: "color",
}


class CubismCoreError(RuntimeError):
    pass


class CsmVector2(ctypes.Structure):
    _fields_ = [("X", ctypes.c_float), ("Y", ctypes.c_float)]


class CsmVector4(ctypes.Structure):
    _fields_ = [
        ("X", ctypes.c_float),
        ("Y", ctypes.c_float),
        ("Z", ctypes.c_float),
        ("W", ctypes.c_float),
    ]


class _AlignedBuffer:
    def __init__(self, size: int, alignment: int, data: bytes | None = None):
        self.size = size
        self.alignment = alignment
        self._raw = ctypes.create_string_buffer(size + alignment)
        base = ctypes.addressof(self._raw)
        offset = (alignment - (base % alignment)) % alignment
        self.address = base + offset
        self.pointer = ctypes.c_void_p(self.address)
        if data is not None:
            if len(data) > size:
                raise ValueError("data is larger than aligned buffer")
            ctypes.memmove(self.pointer, data, len(data))


@dataclass
class Live2DModelSource:
    root_dir: Path
    model_json: Path
    moc3: Path
    textures: list[Path]


@dataclass
class CubismCoreModel:
    core: "CubismCore"
    moc_path: Path
    moc_version: int
    moc_buffer: _AlignedBuffer
    model_buffer: _AlignedBuffer
    model_pointer: ctypes.c_void_p

    def drawable_snapshot(self) -> dict[str, Any]:
        dll = self.core.dll
        model = self.model_pointer

        dll.csmUpdateModel(model)
        drawable_count = dll.csmGetDrawableCount(model)
        if drawable_count < 0:
            raise CubismCoreError("Cubism Core returned an invalid drawable count.")

        offscreen_count = self.core.get_offscreen_count(model)
        canvas = self._read_canvas()
        parts = self._read_parts()

        ids = dll.csmGetDrawableIds(model)
        constant_flags = dll.csmGetDrawableConstantFlags(model)
        dynamic_flags = dll.csmGetDrawableDynamicFlags(model)
        blend_modes = self.core.get_optional_int_array("csmGetDrawableBlendModes", model)
        texture_indices = dll.csmGetDrawableTextureIndices(model)
        draw_orders = dll.csmGetDrawableDrawOrders(model)
        render_orders = self.core.get_render_orders(model)
        opacities = dll.csmGetDrawableOpacities(model)
        mask_counts = dll.csmGetDrawableMaskCounts(model)
        masks = dll.csmGetDrawableMasks(model)
        vertex_counts = dll.csmGetDrawableVertexCounts(model)
        vertex_positions = dll.csmGetDrawableVertexPositions(model)
        vertex_uvs = dll.csmGetDrawableVertexUvs(model)
        index_counts = dll.csmGetDrawableIndexCounts(model)
        indices = dll.csmGetDrawableIndices(model)
        multiply_colors = self.core.get_optional_vector4_array(
            "csmGetDrawableMultiplyColors", model
        )
        screen_colors = self.core.get_optional_vector4_array("csmGetDrawableScreenColors", model)
        parent_part_indices = self.core.get_optional_int_array(
            "csmGetDrawableParentPartIndices", model
        )

        drawables: list[dict[str, Any]] = []
        for index in range(drawable_count):
            vertex_count = int(vertex_counts[index])
            index_count = int(index_counts[index])
            raw_vertices = [
                [float(vertex_positions[index][item].X), float(vertex_positions[index][item].Y)]
                for item in range(vertex_count)
            ]
            pixel_vertices = [self._model_to_pixel(point, canvas) for point in raw_vertices]
            uv_values = [
                [float(vertex_uvs[index][item].X), float(vertex_uvs[index][item].Y)]
                for item in range(vertex_count)
            ]
            index_values = [int(indices[index][item]) for item in range(index_count)]
            mask_count = max(0, int(mask_counts[index]))
            mask_values = [int(masks[index][item]) for item in range(mask_count)]
            constant_flag = int(constant_flags[index])
            dynamic_flag = int(dynamic_flags[index])
            blend_mode_value = int(blend_modes[index]) if blend_modes else self._blend_from_flags(
                constant_flag
            )
            drawable_id = ids[index].decode("utf-8", errors="replace")

            drawables.append(
                {
                    "id": drawable_id,
                    "texture_index": int(texture_indices[index]),
                    "vertices": pixel_vertices,
                    "core_vertices": raw_vertices,
                    "uvs": uv_values,
                    "indices": index_values,
                    "draw_order": int(draw_orders[index]),
                    "render_order": int(render_orders[index]) if render_orders else int(draw_orders[index]),
                    "opacity": float(opacities[index]),
                    "masks": mask_values,
                    "constant_flags": constant_flag,
                    "dynamic_flags": dynamic_flag,
                    "blend_mode": BLEND_MODE_NAMES.get(blend_mode_value, str(blend_mode_value)),
                    "blend_mode_value": blend_mode_value,
                    "inverted_mask": bool(constant_flag & 8),
                    "double_sided": bool(constant_flag & 4),
                    "multiply_color": self._color_at(multiply_colors, index),
                    "screen_color": self._color_at(screen_colors, index),
                    "parent_part_index": int(parent_part_indices[index])
                    if parent_part_indices
                    else -1,
                    "parent_part_id": self._part_id_at(
                        parts,
                        int(parent_part_indices[index]) if parent_part_indices else -1,
                    ),
                }
            )

        return {
            "format": DRAWABLES_FORMAT,
            "version": DRAWABLES_VERSION,
            "coordinate_space": "canvas-pixels-y-down",
            "core": {
                "dll": str(self.core.dll_path),
                "version": self.core.version_string,
                "version_raw": self.core.version,
                "latest_moc_version": MOC_VERSION_NAMES.get(
                    self.core.latest_moc_version, str(self.core.latest_moc_version)
                ),
                "moc_version": MOC_VERSION_NAMES.get(self.moc_version, str(self.moc_version)),
                "moc_version_raw": self.moc_version,
            },
            "canvas": canvas,
            "parts": parts,
            "offscreen_count": offscreen_count,
            "drawables": drawables,
        }

    def _read_parts(self) -> list[dict[str, Any]]:
        dll = self.core.dll
        model = self.model_pointer
        count = self.core.get_part_count(model)
        if count <= 0:
            return []

        ids = self.core.get_part_ids(model)
        opacities = self.core.get_part_opacities(model)
        parents = self.core.get_part_parent_indices(model)
        offscreens = self.core.get_part_offscreen_indices(model)
        parts = []
        for index in range(count):
            part_id = ids[index].decode("utf-8", errors="replace") if ids else f"Part{index}"
            parent_index = int(parents[index]) if parents else -1
            parts.append(
                {
                    "index": index,
                    "id": part_id,
                    "opacity": float(opacities[index]) if opacities else 1.0,
                    "parent_part_index": parent_index,
                    "offscreen_index": int(offscreens[index]) if offscreens else -1,
                }
            )
        for part in parts:
            part["parent_part_id"] = self._part_id_at(parts, int(part["parent_part_index"]))
        return parts

    def _read_canvas(self) -> dict[str, float]:
        size = CsmVector2()
        origin = CsmVector2()
        pixels_per_unit = ctypes.c_float()
        self.core.dll.csmReadCanvasInfo(
            self.model_pointer,
            ctypes.byref(size),
            ctypes.byref(origin),
            ctypes.byref(pixels_per_unit),
        )
        return {
            "width": float(size.X),
            "height": float(size.Y),
            "origin_x": float(origin.X),
            "origin_y": float(origin.Y),
            "pixels_per_unit": float(pixels_per_unit.value),
            "offset_x": 0.0,
            "offset_y": 0.0,
        }

    @staticmethod
    def _model_to_pixel(point: list[float], canvas: dict[str, float]) -> list[float]:
        return [
            float(canvas["origin_x"] + point[0] * canvas["pixels_per_unit"]),
            float(canvas["origin_y"] - point[1] * canvas["pixels_per_unit"]),
        ]

    @staticmethod
    def _blend_from_flags(flags: int) -> int:
        if flags & 1:
            return 1
        if flags & 2:
            return 2
        return 0

    @staticmethod
    def _color_at(array, index: int) -> Optional[list[float]]:
        if not array:
            return None
        color = array[index]
        return [float(color.X), float(color.Y), float(color.Z), float(color.W)]

    @staticmethod
    def _part_id_at(parts: list[dict[str, Any]], index: int) -> Optional[str]:
        if index < 0 or index >= len(parts):
            return None
        return str(parts[index]["id"])


class CubismCore:
    def __init__(self, dll_path: str | Path | None = None):
        self.dll_path = resolve_cubism_core_dll(dll_path)
        if not self.dll_path:
            raise CubismCoreError(
                "Live2DCubismCore.dll was not found. Set LPK_CUBISM_CORE_DLL or place it in app/tools/CubismCore/."
            )
        self.dll = self._load_dll(self.dll_path)
        self._bind_api()
        self.version = int(self.dll.csmGetVersion())
        self.version_string = _format_core_version(self.version)
        self.latest_moc_version = int(self.dll.csmGetLatestMocVersion())

    def load_moc(self, moc_path: str | Path) -> CubismCoreModel:
        path = Path(moc_path).resolve()
        if not path.is_file():
            raise CubismCoreError(f"MOC3 file does not exist: {path}")

        data = path.read_bytes()
        if not data:
            raise CubismCoreError(f"MOC3 file is empty: {path}")

        moc_buffer = _AlignedBuffer(len(data), 64, data)
        moc_version = int(self.dll.csmGetMocVersion(moc_buffer.pointer, len(data)))
        if moc_version == 0:
            raise CubismCoreError(f"Unsupported or unknown MOC3 version: {path}")

        if not self.dll.csmHasMocConsistency(moc_buffer.pointer, len(data)):
            raise CubismCoreError(f"MOC3 consistency check failed: {path}")

        moc_pointer = self.dll.csmReviveMocInPlace(moc_buffer.pointer, len(data))
        if not moc_pointer:
            raise CubismCoreError(f"Failed to revive MOC3 with Cubism Core: {path}")

        model_size = int(self.dll.csmGetSizeofModel(moc_pointer))
        if model_size <= 0:
            raise CubismCoreError(f"Cubism Core returned invalid model size for: {path}")

        model_buffer = _AlignedBuffer(model_size, 16)
        model_pointer = self.dll.csmInitializeModelInPlace(
            moc_pointer, model_buffer.pointer, model_size
        )
        if not model_pointer:
            raise CubismCoreError(f"Failed to initialize Cubism model for: {path}")

        self._reset_parameters_to_default(model_pointer)
        self.dll.csmUpdateModel(model_pointer)

        return CubismCoreModel(
            core=self,
            moc_path=path,
            moc_version=moc_version,
            moc_buffer=moc_buffer,
            model_buffer=model_buffer,
            model_pointer=ctypes.c_void_p(model_pointer),
        )

    def get_render_orders(self, model) -> Optional[Any]:
        if hasattr(self.dll, "csmGetRenderOrders"):
            return self.dll.csmGetRenderOrders(model)
        if hasattr(self.dll, "csmGetDrawableRenderOrders"):
            return self.dll.csmGetDrawableRenderOrders(model)
        return None

    def get_offscreen_count(self, model) -> int:
        if not hasattr(self.dll, "csmGetOffscreenCount"):
            return 0
        return max(0, int(self.dll.csmGetOffscreenCount(model)))

    def get_part_count(self, model) -> int:
        if not hasattr(self.dll, "csmGetPartCount"):
            return 0
        return max(0, int(self.dll.csmGetPartCount(model)))

    def get_part_ids(self, model) -> Optional[Any]:
        if not hasattr(self.dll, "csmGetPartIds"):
            return None
        return self.dll.csmGetPartIds(model)

    def get_part_opacities(self, model) -> Optional[Any]:
        if not hasattr(self.dll, "csmGetPartOpacities"):
            return None
        return self.dll.csmGetPartOpacities(model)

    def get_part_parent_indices(self, model) -> Optional[Any]:
        if not hasattr(self.dll, "csmGetPartParentPartIndices"):
            return None
        return self.dll.csmGetPartParentPartIndices(model)

    def get_part_offscreen_indices(self, model) -> Optional[Any]:
        if not hasattr(self.dll, "csmGetPartOffscreenIndices"):
            return None
        return self.dll.csmGetPartOffscreenIndices(model)

    def get_optional_int_array(self, name: str, model) -> Optional[Any]:
        if not hasattr(self.dll, name):
            return None
        return getattr(self.dll, name)(model)

    def get_optional_vector4_array(self, name: str, model) -> Optional[Any]:
        if not hasattr(self.dll, name):
            return None
        return getattr(self.dll, name)(model)

    @staticmethod
    def _load_dll(path: Path):
        if os.name == "nt":
            return ctypes.WinDLL(str(path))
        return ctypes.CDLL(str(path))

    def _bind_api(self) -> None:
        c_void_p = ctypes.c_void_p
        c_uint = ctypes.c_uint
        c_int = ctypes.c_int
        c_float = ctypes.c_float
        c_ushort = ctypes.c_ushort
        c_ubyte = ctypes.c_ubyte

        self.dll.csmGetVersion.argtypes = []
        self.dll.csmGetVersion.restype = c_uint
        self.dll.csmGetLatestMocVersion.argtypes = []
        self.dll.csmGetLatestMocVersion.restype = c_uint
        self.dll.csmGetMocVersion.argtypes = [c_void_p, c_uint]
        self.dll.csmGetMocVersion.restype = c_uint
        self.dll.csmHasMocConsistency.argtypes = [c_void_p, c_uint]
        self.dll.csmHasMocConsistency.restype = c_int
        self.dll.csmReviveMocInPlace.argtypes = [c_void_p, c_uint]
        self.dll.csmReviveMocInPlace.restype = c_void_p
        self.dll.csmGetSizeofModel.argtypes = [c_void_p]
        self.dll.csmGetSizeofModel.restype = c_uint
        self.dll.csmInitializeModelInPlace.argtypes = [c_void_p, c_void_p, c_uint]
        self.dll.csmInitializeModelInPlace.restype = c_void_p
        self.dll.csmUpdateModel.argtypes = [c_void_p]
        self.dll.csmUpdateModel.restype = None
        self.dll.csmReadCanvasInfo.argtypes = [
            c_void_p,
            ctypes.POINTER(CsmVector2),
            ctypes.POINTER(CsmVector2),
            ctypes.POINTER(c_float),
        ]
        self.dll.csmReadCanvasInfo.restype = None

        self.dll.csmGetParameterCount.argtypes = [c_void_p]
        self.dll.csmGetParameterCount.restype = c_int
        self.dll.csmGetParameterDefaultValues.argtypes = [c_void_p]
        self.dll.csmGetParameterDefaultValues.restype = ctypes.POINTER(c_float)
        self.dll.csmGetParameterValues.argtypes = [c_void_p]
        self.dll.csmGetParameterValues.restype = ctypes.POINTER(c_float)

        self._bind_optional("csmGetPartCount", [c_void_p], c_int)
        self._bind_optional("csmGetPartIds", [c_void_p], ctypes.POINTER(ctypes.c_char_p))
        self._bind_optional("csmGetPartOpacities", [c_void_p], ctypes.POINTER(c_float))
        self._bind_optional(
            "csmGetPartParentPartIndices",
            [c_void_p],
            ctypes.POINTER(c_int),
        )
        self._bind_optional("csmGetPartOffscreenIndices", [c_void_p], ctypes.POINTER(c_int))

        self.dll.csmGetDrawableCount.argtypes = [c_void_p]
        self.dll.csmGetDrawableCount.restype = c_int
        self.dll.csmGetDrawableIds.argtypes = [c_void_p]
        self.dll.csmGetDrawableIds.restype = ctypes.POINTER(ctypes.c_char_p)
        self.dll.csmGetDrawableConstantFlags.argtypes = [c_void_p]
        self.dll.csmGetDrawableConstantFlags.restype = ctypes.POINTER(c_ubyte)
        self.dll.csmGetDrawableDynamicFlags.argtypes = [c_void_p]
        self.dll.csmGetDrawableDynamicFlags.restype = ctypes.POINTER(c_ubyte)
        self._bind_optional("csmGetDrawableBlendModes", [c_void_p], ctypes.POINTER(c_int))
        self.dll.csmGetDrawableTextureIndices.argtypes = [c_void_p]
        self.dll.csmGetDrawableTextureIndices.restype = ctypes.POINTER(c_int)
        self.dll.csmGetDrawableDrawOrders.argtypes = [c_void_p]
        self.dll.csmGetDrawableDrawOrders.restype = ctypes.POINTER(c_int)
        self._bind_optional("csmGetRenderOrders", [c_void_p], ctypes.POINTER(c_int))
        self._bind_optional("csmGetDrawableRenderOrders", [c_void_p], ctypes.POINTER(c_int))
        self.dll.csmGetDrawableOpacities.argtypes = [c_void_p]
        self.dll.csmGetDrawableOpacities.restype = ctypes.POINTER(c_float)
        self.dll.csmGetDrawableMaskCounts.argtypes = [c_void_p]
        self.dll.csmGetDrawableMaskCounts.restype = ctypes.POINTER(c_int)
        self.dll.csmGetDrawableMasks.argtypes = [c_void_p]
        self.dll.csmGetDrawableMasks.restype = ctypes.POINTER(ctypes.POINTER(c_int))
        self.dll.csmGetDrawableVertexCounts.argtypes = [c_void_p]
        self.dll.csmGetDrawableVertexCounts.restype = ctypes.POINTER(c_int)
        self.dll.csmGetDrawableVertexPositions.argtypes = [c_void_p]
        self.dll.csmGetDrawableVertexPositions.restype = ctypes.POINTER(
            ctypes.POINTER(CsmVector2)
        )
        self.dll.csmGetDrawableVertexUvs.argtypes = [c_void_p]
        self.dll.csmGetDrawableVertexUvs.restype = ctypes.POINTER(ctypes.POINTER(CsmVector2))
        self.dll.csmGetDrawableIndexCounts.argtypes = [c_void_p]
        self.dll.csmGetDrawableIndexCounts.restype = ctypes.POINTER(c_int)
        self.dll.csmGetDrawableIndices.argtypes = [c_void_p]
        self.dll.csmGetDrawableIndices.restype = ctypes.POINTER(ctypes.POINTER(c_ushort))
        self._bind_optional("csmGetDrawableMultiplyColors", [c_void_p], ctypes.POINTER(CsmVector4))
        self._bind_optional("csmGetDrawableScreenColors", [c_void_p], ctypes.POINTER(CsmVector4))
        self._bind_optional("csmGetDrawableParentPartIndices", [c_void_p], ctypes.POINTER(c_int))
        self._bind_optional("csmGetOffscreenCount", [c_void_p], c_int)

    def _bind_optional(self, name: str, argtypes: list[Any], restype: Any) -> None:
        if hasattr(self.dll, name):
            func = getattr(self.dll, name)
            func.argtypes = argtypes
            func.restype = restype

    def _reset_parameters_to_default(self, model_pointer) -> None:
        count = int(self.dll.csmGetParameterCount(model_pointer))
        if count <= 0:
            return
        defaults = self.dll.csmGetParameterDefaultValues(model_pointer)
        values = self.dll.csmGetParameterValues(model_pointer)
        if not defaults or not values:
            return
        for index in range(count):
            values[index] = defaults[index]


def export_drawables_sidecar(
    source: str | Path,
    output_dir: str | Path | None = None,
    dll_path: str | Path | None = None,
    progress: Optional[ProgressCallback] = None,
) -> Path:
    source_info = resolve_live2d_source(Path(source))
    output_path = Path(output_dir) if output_dir else source_info.root_dir
    output_path.mkdir(parents=True, exist_ok=True)

    _emit(progress, 10, "Loading Cubism Core")
    core = CubismCore(dll_path)
    _emit(progress, 20, f"Cubism Core {core.version_string}: {core.dll_path}")

    model = core.load_moc(source_info.moc3)
    snapshot = model.drawable_snapshot()
    snapshot["source_model"] = str(source_info.model_json)
    snapshot["moc"] = str(source_info.moc3)
    snapshot["textures"] = [
        {
            "index": index,
            "name": path.name,
            "relative_path": _relative_to(path, source_info.root_dir),
        }
        for index, path in enumerate(source_info.textures)
    ]

    sidecar_path = output_path / f"{_clean_model_name(source_info.model_json)}.drawables.json"
    with sidecar_path.open("w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
        f.write("\n")

    _emit(progress, 100, f"Drawable sidecar written: {sidecar_path}")
    return sidecar_path


def resolve_live2d_source(source: Path) -> Live2DModelSource:
    source = source.resolve()
    if not source.exists():
        raise CubismCoreError(f"Input path does not exist: {source}")

    if source.is_dir():
        model_json = _find_model_json(source)
    elif source.suffix.lower() == ".json":
        model_json = source
    elif source.suffix.lower() == ".moc3":
        model_json = _find_model_json_for_moc(source)
    else:
        raise CubismCoreError("Please select a model3.json file, .moc3 file, or folder.")

    data = _read_json(model_json)
    refs = data.get("FileReferences", {})
    moc_value = refs.get("Moc")
    if not isinstance(moc_value, str) or not moc_value:
        raise CubismCoreError(f"model3.json does not reference a Moc file: {model_json}")

    root_dir = model_json.parent
    moc3 = (root_dir / moc_value).resolve()
    if not moc3.is_file():
        raise CubismCoreError(f"MOC3 file referenced by model3.json was not found: {moc3}")

    textures = []
    for item in refs.get("Textures", []):
        if isinstance(item, str) and item:
            texture_path = (root_dir / item).resolve()
            if texture_path.is_file():
                textures.append(texture_path)

    return Live2DModelSource(root_dir=root_dir, model_json=model_json, moc3=moc3, textures=textures)


def resolve_cubism_core_dll(path: str | Path | None = None) -> Optional[Path]:
    explicit = _existing_file(path)
    if explicit:
        return explicit

    env_file = _existing_file(os.environ.get("LPK_CUBISM_CORE_DLL"))
    if env_file:
        return env_file

    env_dir = os.environ.get("LPK_CUBISM_CORE_DIR")
    if env_dir:
        env_candidate = _existing_file(Path(env_dir) / "Live2DCubismCore.dll")
        if env_candidate:
            return env_candidate

    root = Path(__file__).resolve().parents[2]
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "tools" / "CubismCore" / "Live2DCubismCore.dll")
    candidates.extend(
        [
            root / "app" / "tools" / "CubismCore" / "Live2DCubismCore.dll",
            root / "tools" / "CubismCore" / "Live2DCubismCore.dll",
        ]
    )

    for candidate in candidates:
        existing = _existing_file(candidate)
        if existing:
            return existing
    return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as exc:
        raise CubismCoreError(f"Failed to read JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CubismCoreError(f"JSON root must be an object: {path}")
    return data


def _find_model_json(folder: Path) -> Path:
    candidates = []
    for path in folder.rglob("*.json"):
        try:
            data = _read_json(path)
        except CubismCoreError:
            continue
        refs = data.get("FileReferences")
        if isinstance(refs, dict) and refs.get("Moc") and refs.get("Textures"):
            score = 0 if path.name.endswith(".model3.json") else 1
            candidates.append((score, len(path.parts), path))
    if not candidates:
        raise CubismCoreError("No Live2D model3.json file was found in the folder.")
    return sorted(candidates)[0][2]


def _find_model_json_for_moc(moc3: Path) -> Path:
    moc_name = moc3.name.lower()
    candidates = []
    for path in moc3.parent.rglob("*.json"):
        try:
            data = _read_json(path)
        except CubismCoreError:
            continue
        moc_value = data.get("FileReferences", {}).get("Moc")
        if isinstance(moc_value, str) and Path(moc_value).name.lower() == moc_name:
            score = 0 if path.name.endswith(".model3.json") else 1
            candidates.append((score, len(path.parts), path))
    if not candidates:
        raise CubismCoreError("A .moc3 file needs a sibling model3.json that references it.")
    return sorted(candidates)[0][2]


def _existing_file(path: str | Path | None) -> Optional[Path]:
    if not path:
        return None
    candidate = Path(path).expanduser().resolve()
    return candidate if candidate.is_file() else None


def _relative_to(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _clean_model_name(model_json: Path) -> str:
    name = model_json.name
    if name.endswith(".model3.json"):
        return name[: -len(".model3.json")]
    return model_json.stem


def _format_core_version(version: int) -> str:
    major = (version >> 24) & 0xFF
    minor = (version >> 16) & 0xFF
    patch = version & 0xFFFF
    return f"{major}.{minor}.{patch}"


def _emit(progress: Optional[ProgressCallback], value: int, message: str) -> None:
    if progress:
        progress(value, message)
