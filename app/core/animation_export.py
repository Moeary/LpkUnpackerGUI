"""Encode rendered animation frames as GIF, animated WebP or a PNG sequence.

Rendering is the caller's job (the Spine preview draws each frame off-screen);
this module only decides frame times and writes Pillow images, so it stays
independent of Qt and OpenGL and is easy to test.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Iterable

from PIL import Image

FORMATS = ("gif", "webp", "png")
SUFFIXES = {"gif": ".gif", "webp": ".webp", "png": ""}
# Frames are rendered first (on the GUI thread, which owns the GL context)
# and encoded afterwards, so all of them are held in memory at once.
MAX_MEMORY_BYTES = 2 * 1024 ** 3
_GIF_ALPHA_THRESHOLD = 128


def frame_times(start: float, end: float, fps: float, speed: float = 1.0) -> list[float]:
    """Animation times of the frames for ``start``..``end`` (end excluded,
    so a looping export does not show the seam frame twice).

    ``speed`` is the playback speed: at 2× each output frame advances twice
    as much animation time, so the file plays twice as fast.
    """
    fps, speed = float(fps), float(speed)
    if not (math.isfinite(fps) and fps > 0 and math.isfinite(speed) and speed > 0):
        raise ValueError("Frame rate and speed must be positive")
    step = speed / fps
    span = max(0.0, float(end) - float(start))
    count = max(1, int(math.floor(span / step + 1e-6)))
    return [float(start) + index * step for index in range(count)]


def frame_duration_ms(fps: float) -> int:
    return max(1, round(1000.0 / float(fps)))


def estimated_memory(frame_count: int, width: int, height: int) -> int:
    """Bytes of the rendered RGBA frames held until they are encoded."""
    return int(frame_count) * int(width) * int(height) * 4


def output_path(path: str | Path, fmt: str) -> Path:
    """``path`` with the format's suffix (a folder for a PNG sequence)."""
    path = Path(path)
    suffix = SUFFIXES[fmt]
    if fmt == "png":
        return path.with_suffix("") if path.suffix.lower() == ".png" else path
    return path if path.suffix.lower() == suffix else path.with_suffix(suffix)


def _flatten(image: Image.Image, background: tuple[int, int, int]) -> Image.Image:
    canvas = Image.new("RGB", image.size, tuple(background))
    canvas.paste(image, mask=image.getchannel("A"))
    return canvas


def _gif_frame(image: Image.Image, transparent: bool, background: tuple[int, int, int]) -> Image.Image:
    """A palette frame; with transparency, index 255 marks clear pixels.

    GIF transparency is on/off only, so soft edges are cut at half alpha.
    """
    if not transparent:
        return _flatten(image, background).quantize(colors=256, method=Image.Quantize.MEDIANCUT)
    clear = image.getchannel("A").point(lambda alpha: 255 if alpha < _GIF_ALPHA_THRESHOLD else 0)
    frame = image.convert("RGB").quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    frame.paste(255, mask=clear)
    frame.info["transparency"] = 255
    return frame


def write_animation(frames: Iterable[Image.Image], output: str | Path, fmt: str, fps: float, *,
                    transparent: bool = True, background: tuple[int, int, int] = (255, 255, 255),
                    quality: int = 90, progress=None, cancelled=None) -> Path:
    """Write RGBA ``frames`` and return the file (or PNG folder) written.

    ``progress(index)`` is called after each frame is taken; ``cancelled()``
    returning true stops before anything is written.
    """
    if fmt not in FORMATS:
        raise ValueError(f"Unknown animation format: {fmt}")
    target = output_path(output, fmt)
    duration = frame_duration_ms(fps)
    if fmt == "png":
        target.mkdir(parents=True, exist_ok=True)
        count = 0
        for index, image in enumerate(frames):
            if cancelled is not None and cancelled():
                raise InterruptedError("Animation export cancelled")
            frame = image if transparent else _flatten(image.convert("RGBA"), background)
            frame.save(target / f"frame_{index + 1:04d}.png", format="PNG")
            count += 1
            if progress is not None:
                progress(index)
        if not count:
            raise ValueError("No frames to export")
        return target
    encoded = []
    for index, image in enumerate(frames):
        if cancelled is not None and cancelled():
            raise InterruptedError("Animation export cancelled")
        image = image.convert("RGBA")
        if fmt == "gif":
            encoded.append(_gif_frame(image, transparent, background))
        else:
            encoded.append(image if transparent else _flatten(image, background))
        if progress is not None:
            progress(index)
    if not encoded:
        raise ValueError("No frames to export")
    if cancelled is not None and cancelled():
        raise InterruptedError("Animation export cancelled")
    target.parent.mkdir(parents=True, exist_ok=True)
    first, rest = encoded[0], encoded[1:]
    if fmt == "gif":
        options = {"disposal": 2, "optimize": False}
        if transparent:
            options["transparency"] = 255
        first.save(target, format="GIF", save_all=True, append_images=rest, duration=duration, loop=0, **options)
    else:
        first.save(target, format="WEBP", save_all=True, append_images=rest, duration=duration, loop=0,
                   quality=int(quality), method=4, background=(0, 0, 0, 0) if transparent else (*background, 255))
    return target


__all__ = [
    "FORMATS", "MAX_MEMORY_BYTES", "estimated_memory", "frame_duration_ms", "frame_times",
    "output_path", "write_animation",
]
