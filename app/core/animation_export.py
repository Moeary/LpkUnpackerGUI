"""Encode rendered animation frames as GIF, WebP, a PNG sequence, WebM or MP4.

Rendering is the caller's job (the editors draw each frame off-screen); this
module only decides frame times and writes Pillow images, so it stays
independent of Qt and OpenGL and is easy to test.  GIF, WebP and PNG use
Pillow; WebM and MP4 pipe raw frames to an external ffmpeg.
"""

from __future__ import annotations

import functools
import math
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable

from PIL import Image

FORMATS = ("gif", "webp", "png", "webm", "mp4")
VIDEO_FORMATS = ("webm", "mp4")
SUFFIXES = {"gif": ".gif", "webp": ".webp", "png": "", "webm": ".webm", "mp4": ".mp4"}
# Preferred ffmpeg encoders per video format, best first:
# (name, options, keeps transparency).  Only VP8/VP9 in WebM carry alpha.
_VIDEO_ENCODERS = {
    "mp4": (
        ("libx264", ["-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"], False),
        ("h264_mf", ["-b:v", "12M", "-pix_fmt", "nv12"], False),
        ("libopenh264", ["-b:v", "12M", "-pix_fmt", "yuv420p"], False),
        ("mpeg4", ["-q:v", "2", "-pix_fmt", "yuv420p"], False),
    ),
    "webm": (
        ("libvpx-vp9", ["-crf", "28", "-b:v", "0", "-row-mt", "1", "-deadline", "good"], True),
        ("libvpx", ["-crf", "8", "-b:v", "8M", "-auto-alt-ref", "0"], True),
    ),
}
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


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


@functools.lru_cache(maxsize=8)
def _ffmpeg_encoders(ffmpeg: str) -> frozenset[str]:
    result = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=30, creationflags=_no_window())
    names = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] == "V":
            names.add(parts[1])
    return frozenset(names)


def video_encoder(ffmpeg: str | None, fmt: str):
    """(name, options, keeps_alpha) of the best encoder ffmpeg offers, else None."""
    if not ffmpeg or fmt not in _VIDEO_ENCODERS:
        return None
    try:
        available = _ffmpeg_encoders(str(ffmpeg))
    except (OSError, subprocess.SubprocessError):
        return None
    return next((encoder for encoder in _VIDEO_ENCODERS[fmt] if encoder[0] in available), None)


def _write_video(frames: list[Image.Image], target: Path, fmt: str, fps: float, *, transparent: bool,
                 background: tuple[int, int, int], ffmpeg: str | None, cancelled) -> Path:
    encoder = video_encoder(ffmpeg, fmt)
    if encoder is None:
        raise RuntimeError(f"ffmpeg with a {fmt.upper()} encoder is required")
    name, options, keeps_alpha = encoder
    alpha = transparent and keeps_alpha
    width, height = frames[0].size
    # 4:2:0 video needs even dimensions.
    even = (width + width % 2, height + height % 2)
    pixel_format = "rgba" if alpha else "rgb24"
    if alpha:
        options = options + ["-pix_fmt", "yuva420p"]
    elif fmt == "webm":
        options = options + ["-pix_fmt", "yuv420p"]
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", pixel_format, "-s", f"{even[0]}x{even[1]}", "-framerate", f"{fps:g}",
               "-i", "-", "-an", "-c:v", name, *options]
    if fmt == "mp4":
        command += ["-movflags", "+faststart"]
    command.append(str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors,
                                   creationflags=_no_window())
        try:
            for image in frames:
                if cancelled is not None and cancelled():
                    raise InterruptedError("Animation export cancelled")
                image = image.convert("RGBA")
                frame = image if alpha else _flatten(image, background)
                if frame.size != even:
                    padded = Image.new(frame.mode, even, (0, 0, 0, 0) if alpha else tuple(background))
                    padded.paste(frame, (0, 0))
                    frame = padded
                process.stdin.write(frame.tobytes())
            process.stdin.close()
            code = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            target.unlink(missing_ok=True)
            raise
        if code != 0:
            errors.seek(0)
            detail = errors.read().decode("utf-8", "replace").strip().splitlines()[-3:]
            target.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg ({name}) failed: " + " ".join(detail))
    return target


def write_animation(frames: Iterable[Image.Image], output: str | Path, fmt: str, fps: float, *,
                    transparent: bool = True, background: tuple[int, int, int] = (255, 255, 255),
                    quality: int = 90, progress=None, cancelled=None, ffmpeg: str | None = None) -> Path:
    """Write RGBA ``frames`` and return the file (or PNG folder) written.

    ``progress(index)`` is called after each frame is taken; ``cancelled()``
    returning true stops before anything is written.  WebM and MP4 need
    ``ffmpeg``; MP4 has no alpha, so it is always flattened on ``background``.
    """
    if fmt not in FORMATS:
        raise ValueError(f"Unknown animation format: {fmt}")
    target = output_path(output, fmt)
    duration = frame_duration_ms(fps)
    if fmt in VIDEO_FORMATS:
        frames = list(frames)
        if not frames:
            raise ValueError("No frames to export")
        return _write_video(frames, target, fmt, float(fps), transparent=transparent, background=background,
                            ffmpeg=ffmpeg, cancelled=cancelled)
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
    "FORMATS", "MAX_MEMORY_BYTES", "VIDEO_FORMATS", "estimated_memory", "frame_duration_ms", "frame_times",
    "output_path", "video_encoder", "write_animation",
]
