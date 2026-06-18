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
                }
            )

    return motions
