"""Capture actual native Spine playback at fixed times for animation review."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--animation", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--times", default="0,0.5,1,1.5,2")
    parser.add_argument("--width", type=int, default=560)
    parser.add_argument("--height", type=int, default=700)
    args = parser.parse_args()
    from PySide6.QtCore import QCoreApplication, Qt
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    from PySide6.QtWidgets import QApplication
    from app.core.spine_preview import load_spine_asset, make_spine_preview_plan
    from app.gui.SpinePreviewWidget import SpinePreviewWidget
    from PIL import Image

    app = QApplication([])
    widget = SpinePreviewWidget()
    widget.resize(args.width, args.height)
    errors = []
    widget.statusChanged.connect(lambda kind, text: errors.append(text) if kind == "error" else None)
    widget.show()
    def pump(seconds=0.2):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.005)
    try:
        pump()
        plan = make_spine_preview_plan(load_spine_asset(args.source))
        widget.open_plan(plan)
        pump(0.5)
        if widget.model is None or errors:
            raise RuntimeError(str(errors or plan.reason))
        if not widget.model.set_animation(args.animation, False):
            raise ValueError("Animation not found: " + args.animation)
        widget.set_paused(True)
        widget.set_view_settings({"transparent_bg": False, "bg_color": "#dddde4"})
        args.output.mkdir(parents=True, exist_ok=True)
        frames = []
        times = list(map(float, args.times.split(",")))
        for i, seconds in enumerate(times):
            widget.set_time(seconds)
            pump(0.12)
            image = widget.canvas.grabFramebuffer()
            if image.isNull():
                raise RuntimeError("Empty native framebuffer")
            path = args.output / f"frame_{i:03d}_{seconds:.3f}.png"
            image.save(str(path))
            with Image.open(path) as frame:
                frames.append(frame.convert("RGB"))
        selected = sorted({round(i * (len(frames) - 1) / 4) for i in range(5)})
        sheet = Image.new("RGB", (frames[0].width * len(selected), frames[0].height), "white")
        for i, index in enumerate(selected):
            frame = frames[index]
            sheet.paste(frame, (i * frame.width, 0))
        sheet.save(args.output / "contact_sheet.png")
        if len(frames) > 1:
            durations = [max(20, round(1000 * (b - a))) for a, b in zip(times, times[1:])]
            durations.append(durations[-1])
            frames[0].save(args.output / "animation.gif", save_all=True, append_images=frames[1:], duration=durations, loop=0)
        report = {"source": str(args.source.resolve()), "animation": args.animation, "duration": widget.model.duration,
                  "times": times, "skeleton_version": widget.model.version,
                  "runtime_version": getattr(plan.runtime, "version", None), "errors": errors}
        report["native_library"] = str(widget.model.library_path)
        (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
    finally:
        widget.shutdown()
        widget.close()


if __name__ == "__main__":
    main()
