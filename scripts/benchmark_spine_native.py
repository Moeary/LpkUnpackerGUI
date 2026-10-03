"""Benchmark the native Spine CPU bridge and optional Qt/OpenGL renderer.

Examples (run from the repository root)::

    pixi run python scripts/benchmark_spine_native.py --source runtime/output/spine/贝尔法斯特.改 魔改/model0.json
    pixi run python scripts/benchmark_spine_native.py --source runtime/validation/native_converter_acceptance/skeleton_0_to_3.8.75_json/skeleton_0.json --mode all

The benchmark only reads the selected skeleton, atlas, bridge and textures. It
does not write settings or runtime files; use ``--report`` for a JSON copy.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.spine_native import SpineNativeModel, find_native_library  # noqa: E402
from app.core.spine_preview import load_spine_asset  # noqa: E402


def benchmark_core(source: Path, runtime: Path, frames: int) -> dict:
    asset = load_spine_asset(source)
    library = find_native_library(runtime, asset.family)
    if library is None:
        raise RuntimeError(f"No native Spine bridge for {asset.family} under {runtime}")
    model = SpineNativeModel(library, asset.skeleton_path, asset.atlas_paths[0], asset.skeleton_format)
    try:
        for _ in range(8):
            model.update(1.0 / 60.0)
            model.render_into()
        samples = []
        result = None
        for _ in range(max(1, frames)):
            model.update(1.0 / 60.0)
            started = time.perf_counter_ns()
            result = model.render_into()
            samples.append((time.perf_counter_ns() - started) / 1_000_000.0)
        return {
            "mode": "core",
            "source": str(source),
            "nativeVersion": model.version,
            "vertexCount": result[1] if result else 0,
            "batchCount": result[5] if result else 0,
            "frames": len(samples),
            "meanMs": statistics.mean(samples),
            "p95Ms": sorted(samples)[max(0, int(len(samples) * 0.95) - 1)],
            "maxMs": max(samples),
        }
    finally:
        model.close()


def benchmark_widget(source: Path, runtime: Path, frames: int) -> dict:
    from PySide6.QtCore import QCoreApplication, Qt

    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    from PySide6.QtWidgets import QApplication
    import OpenGL.GL as GL

    from app.gui.SpinePreviewWidget import SpinePreviewWidget

    asset = load_spine_asset(source)
    plan = SimpleNamespace(
        asset=asset,
        runtime=SimpleNamespace(root_dir=runtime, family=asset.family),
        mode="runtime",
        reason="benchmark",
    )
    app = QApplication.instance() or QApplication([])
    widget = SpinePreviewWidget()
    widget.resize(800, 600)
    widget.set_view_settings({"transparent_bg": False, "bg_color": "#ffffff"})
    samples: list[float] = []
    gpu = {"vendor": "", "renderer": "", "version": ""}
    original = widget._paint_native

    def measured(canvas):
        if not gpu["vendor"]:
            gpu["vendor"] = (GL.glGetString(GL.GL_VENDOR) or b"").decode("utf-8", "replace")
            gpu["renderer"] = (GL.glGetString(GL.GL_RENDERER) or b"").decode("utf-8", "replace")
            gpu["version"] = (GL.glGetString(GL.GL_VERSION) or b"").decode("utf-8", "replace")
        started = time.perf_counter_ns()
        original(canvas)
        samples.append((time.perf_counter_ns() - started) / 1_000_000.0)

    widget._paint_native = measured
    widget.show()
    widget.open_plan(plan)
    deadline = time.monotonic() + max(4.0, frames / 30.0)
    while time.monotonic() < deadline and len(samples) < frames + 10:
        app.processEvents()
        time.sleep(0.005)
    samples = samples[10:][-max(1, frames):]
    try:
        return {
            "mode": "widget",
            "source": str(source),
            "nativeVersion": widget.last_state.get("nativeVersion"),
            "vertexCount": widget.model.render_into()[1] if widget.model else 0,
            "timerIntervalMs": widget._status_timer.interval(),
            "gpu": gpu,
            "frames": len(samples),
            "meanMs": statistics.mean(samples) if samples else None,
            "p95Ms": sorted(samples)[max(0, int(len(samples) * 0.95) - 1)] if samples else None,
            "maxMs": max(samples) if samples else None,
        }
    finally:
        widget.shutdown()
        app.processEvents()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--runtime", type=Path, default=ROOT / "runtime" / "tools" / "spine_native")
    parser.add_argument("--mode", choices=("core", "widget", "all"), default="all")
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    runtime = args.runtime.resolve()
    results = []
    if args.mode in {"core", "all"}:
        results.append(benchmark_core(source, runtime, args.frames))
    if args.mode in {"widget", "all"}:
        results.append(benchmark_widget(source, runtime, args.frames))
    payload = {"results": results}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.report:
        args.report.resolve().write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
