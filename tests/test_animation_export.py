from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from app.core.animation_export import (
    estimated_memory, frame_duration_ms, frame_times, output_path, write_animation,
)


def _frames(count=4, size=(12, 8)):
    frames = []
    for index in range(count):
        image = Image.new("RGBA", size, (0, 0, 0, 0))
        image.paste((200, 40 * index, 10, 255), (index, 0, index + 4, 4))
        image.putpixel((size[0] - 1, size[1] - 1), (0, 0, 255, 60))  # a soft edge
        frames.append(image)
    return frames


class AnimationExportTests(unittest.TestCase):
    def test_frame_times_exclude_the_seam_and_follow_speed(self):
        self.assertEqual(frame_times(0, 1, 4), [0, 0.25, 0.5, 0.75])
        self.assertEqual(frame_times(1, 2, 4, speed=2), [1, 1.5])
        self.assertEqual(frame_times(0, 0, 30), [0])  # a still pose is one frame
        with self.assertRaises(ValueError):
            frame_times(0, 1, 0)
        self.assertEqual(frame_duration_ms(30), 33)
        self.assertEqual(estimated_memory(10, 100, 50), 200_000)

    def test_output_path_adds_the_format_suffix(self):
        self.assertEqual(output_path("a/run", "gif"), Path("a/run.gif"))
        self.assertEqual(output_path("a/run.GIF", "gif"), Path("a/run.GIF"))
        self.assertEqual(output_path("a/run.png", "png"), Path("a/run"))

    def test_gif_webp_and_png_sequence_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seen = []
            gif = write_animation(_frames(), root / "out", "gif", 10, transparent=True, progress=seen.append)
            self.assertEqual(seen, [0, 1, 2, 3])
            with Image.open(gif) as image:
                self.assertEqual((image.n_frames, image.size, image.info.get("duration")), (4, (12, 8), 100))
                self.assertEqual(image.info.get("transparency"), 255)
                self.assertEqual(image.convert("RGBA").getpixel((11, 0))[3], 0)  # clear stays clear
            opaque = write_animation(_frames(), root / "solid.gif", "gif", 10, transparent=False,
                                     background=(255, 255, 255))
            with Image.open(opaque) as image:
                self.assertEqual(image.convert("RGB").getpixel((11, 0)), (255, 255, 255))
            webp = write_animation(_frames(), root / "out", "webp", 10, transparent=True)
            with Image.open(webp) as image:
                self.assertEqual((image.n_frames, image.mode), (4, "RGBA"))
                self.assertEqual(image.getpixel((11, 0))[3], 0)
            folder = write_animation(_frames(), root / "seq", "png", 10, transparent=True)
            files = sorted(folder.glob("*.png"))
            self.assertEqual([path.name for path in files][:2], ["frame_0001.png", "frame_0002.png"])
            with Image.open(files[0]) as image:
                self.assertEqual(image.getpixel((11, 7)), (0, 0, 255, 60))  # soft alpha kept

    def test_video_needs_ffmpeg_and_encodes_webm_with_alpha_and_mp4(self):
        import shutil
        import subprocess
        from app.core.animation_export import video_encoder
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                write_animation(_frames(), Path(directory) / "x", "mp4", 10, ffmpeg=None)
        self.assertIsNone(video_encoder(None, "webm"))
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg or video_encoder(ffmpeg, "webm") is None or video_encoder(ffmpeg, "mp4") is None:
            self.skipTest("ffmpeg with WebM and MP4 encoders is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frames = _frames(size=(13, 9))  # odd sizes are padded for 4:2:0
            webm = write_animation(frames, root / "a", "webm", 10, transparent=True, ffmpeg=ffmpeg)
            mp4 = write_animation(frames, root / "a", "mp4", 10, transparent=True, ffmpeg=ffmpeg)
            self.assertEqual((webm.name, mp4.name), ("a.webm", "a.mp4"))
            png = root / "first.png"
            subprocess.run([ffmpeg, "-v", "error", "-y", "-c:v", video_encoder(ffmpeg, "webm")[0], "-i", str(webm),
                            "-frames:v", "1", str(png)], check=True)
            with Image.open(png) as image:
                self.assertEqual(image.size, (14, 10))
                self.assertEqual(image.convert("RGBA").getpixel((11, 0))[3], 0)  # alpha survives in WebM

    def test_cancel_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "out.webp"
            with self.assertRaises(InterruptedError):
                write_animation(_frames(), target, "webp", 10, cancelled=lambda: True)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
