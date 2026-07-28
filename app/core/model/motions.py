import json
import os
from pathlib import Path
from typing import Any


def load_live2d_motions(model_json_path: str | Path | None) -> list[dict[str, Any]]:
    """Load unique playable motions from a Live2D model json.

    Some game model json files repeat the same motion file several times under
    one group. Keep the first playable entry so UI motion lists stay readable
    while preserving the original group/index used by Live2D StartMotion.
    """
    if not model_json_path:
        return []

    path = Path(model_json_path)
    if not path.is_file():
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return []

    base_dir = path.parent
    refs = (data or {}).get("FileReferences") or {}
    groups = refs.get("Motions") or {}
    motions: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for group, items in groups.items():
        if not isinstance(items, list):
            continue
        group_name = str(group)
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            rel = str(item.get("File") or "").strip()
            if not rel:
                continue
            rel_key = os.path.normpath(rel).replace("\\", "/").lower()
            key = (group_name.lower(), rel_key)
            if key in seen:
                continue
            seen.add(key)

            full_path = os.path.normpath(str(base_dir / rel))
            sound_rel = str(item.get("Sound") or "").strip()
            sound_path = os.path.normpath(str(base_dir / sound_rel)) if sound_rel else ""
            duration = _read_motion_duration(full_path)
            display = f"{group_name}[{index}] - {Path(rel).name}"
            motions.append(
                {
                    "group": group_name,
                    "index": int(index),
                    "file": full_path,
                    "rel": rel,
                    "sound": sound_path,
                    "sound_rel": sound_rel,
                    "display": display,
                    "duration": duration,
                }
            )

    return motions


def _read_motion_duration(path: str | Path) -> float:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return max(0.0, float((data.get("Meta") or {}).get("Duration") or 0.0))
    except Exception:
        return 0.0


def evaluate_motion_parameters(
    motion_path: str | Path,
    seconds: float,
) -> dict[str, float]:
    """Evaluate Cubism Parameter curves at a fixed motion time.

    This provides deterministic timeline scrubbing for frozen poses without
    waiting for real-time playback. Physics/expressions managed by the native
    runtime are intentionally left to the model; parameter curves are the
    editable pose component that is safe to expose to PSD export.
    """
    try:
        data = json.loads(Path(motion_path).read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    duration = max(0.0, float((data.get("Meta") or {}).get("Duration") or 0.0))
    time_value = min(max(0.0, float(seconds)), duration) if duration else max(0.0, float(seconds))
    values: dict[str, float] = {}
    for curve in data.get("Curves") or []:
        if not isinstance(curve, dict) or str(curve.get("Target") or "") != "Parameter":
            continue
        parameter_id = str(curve.get("Id") or "")
        segments = curve.get("Segments")
        if parameter_id and isinstance(segments, list):
            value = _evaluate_segments(segments, time_value)
            if value is not None:
                values[parameter_id] = value
    return values


def _evaluate_segments(segments: list[Any], time_value: float) -> float | None:
    if len(segments) < 2:
        return None
    try:
        last_time = float(segments[0])
        last_value = float(segments[1])
    except (TypeError, ValueError):
        return None
    cursor = 2
    while cursor < len(segments):
        try:
            segment_type = int(segments[cursor])
        except (TypeError, ValueError):
            return last_value
        cursor += 1
        try:
            if segment_type == 1:
                c1_time, c1_value, c2_time, c2_value, end_time, end_value = map(
                    float, segments[cursor : cursor + 6]
                )
                cursor += 6
                if time_value <= end_time:
                    return _bezier_value(
                        last_time, last_value, c1_time, c1_value,
                        c2_time, c2_value, end_time, end_value, time_value,
                    )
            else:
                end_time, end_value = map(float, segments[cursor : cursor + 2])
                cursor += 2
                if time_value <= end_time:
                    if segment_type == 2:  # stepped
                        return last_value
                    if segment_type == 3:  # inverse stepped
                        return end_value
                    ratio = 0.0 if end_time <= last_time else (time_value - last_time) / (end_time - last_time)
                    return last_value + (end_value - last_value) * min(1.0, max(0.0, ratio))
            last_time, last_value = end_time, end_value
        except (TypeError, ValueError):
            return last_value
    return last_value


def _bezier_value(
    x0: float, y0: float, x1: float, y1: float, x2: float, y2: float,
    x3: float, y3: float, target_x: float,
) -> float:
    low, high = 0.0, 1.0
    for _ in range(18):
        t = (low + high) / 2.0
        inv = 1.0 - t
        x = inv**3 * x0 + 3 * inv**2 * t * x1 + 3 * inv * t**2 * x2 + t**3 * x3
        if x < target_x:
            low = t
        else:
            high = t
    t = (low + high) / 2.0
    inv = 1.0 - t
    return inv**3 * y0 + 3 * inv**2 * t * y1 + 3 * inv * t**2 * y2 + t**3 * y3
