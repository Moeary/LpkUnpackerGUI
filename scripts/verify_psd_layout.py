"""Exercise the PSD workspace layout at desktop and high-DPI sizes.

The harness uses Qt's offscreen platform by default so it can run in CI.  It
captures collapsed and expanded log states for both application themes and
writes a small JSON report beside the screenshots.  A real OpenGL renderer is
intentionally not inferred here: this page only owns the PSD controls, while
the native preview acceptance harness is responsible for querying
``GL_RENDERER`` from an active context.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=1000)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--point-size", type=int, default=10)
    parser.add_argument("--dpr", type=str, default="1")
    parser.add_argument(
        "--platform",
        default=os.environ.get("QT_QPA_PLATFORM", "offscreen"),
        help="Qt platform plugin (offscreen for CI, windows for native screenshots)",
    )
    parser.add_argument("--language", default="zh_CN")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".codex-temp") / "psd-layout-acceptance",
    )
    return parser.parse_args()


def _settle_theme(app) -> None:
    """Let QFluent card palette animations finish before capturing evidence."""
    deadline = time.monotonic() + 0.25
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()


def main() -> int:
    args = _parse_args()
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    os.environ["QT_QPA_PLATFORM"] = args.platform
    os.environ.setdefault("QT_SCALE_FACTOR", str(args.dpr))

    from PySide6.QtWidgets import QApplication
    from qfluentwidgets import Theme

    from app.core.font_helper import apply_application_font
    from app.gui.PsdReconstructionPage import PsdReconstructionPage
    from app.gui.theme import apply_application_theme
    from app.i18n import get_i18n

    app = QApplication.instance() or QApplication([])
    get_i18n().set_language(args.language)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    page = PsdReconstructionPage()
    apply_application_font(app, "", args.point_size, root=page)
    page.resize(args.width, args.height)
    page.show()
    app.processEvents()

    report: dict[str, object] = {
        "requested_size": [args.width, args.height],
        "requested_dpr": str(args.dpr),
        "actual_dpr": app.primaryScreen().devicePixelRatio()
        if app.primaryScreen()
        else None,
        "language": get_i18n().language,
        "point_size": app.font().pointSize(),
        "themes": {},
    }
    for theme_name, theme in (("light", Theme.LIGHT), ("dark", Theme.DARK)):
        apply_application_theme(theme, page)
        _settle_theme(app)
        collapsed_path = args.output_dir / f"{theme_name}-collapsed.png"
        page.grab().save(str(collapsed_path))
        collapsed = {
            "log_expanded": page._log_expanded,
            "log_visible": page.log_text.isVisible(),
            "log_height": page.log_frame.height(),
            "left_width": page.left_scroll.viewport().width(),
            "left_panel_min_hint": page.left_panel.minimumSizeHint().width(),
            "screenshot": str(collapsed_path.resolve()),
        }
        page.append_log("layout acceptance error", expand=True)
        app.processEvents()
        expanded_path = args.output_dir / f"{theme_name}-expanded.png"
        page.grab().save(str(expanded_path))
        expanded = {
            "log_expanded": page._log_expanded,
            "log_visible": page.log_text.isVisible(),
            "log_height": page.log_frame.height(),
            "log_text_max_height": page.log_text.maximumHeight(),
            "screenshot": str(expanded_path.resolve()),
        }
        page.toggle_log_panel()
        app.processEvents()
        page.set_workflow("repack")
        app.processEvents()
        repack_path = args.output_dir / f"{theme_name}-repack.png"
        page.grab().save(str(repack_path))
        repack = {
            "left_width": page.left_scroll.viewport().width(),
            "left_panel_min_hint": page.left_panel.minimumSizeHint().width(),
            "screenshot": str(repack_path.resolve()),
        }
        page.set_workflow("export")
        report["themes"][theme_name] = {  # type: ignore[index]
            "collapsed": collapsed,
            "expanded": expanded,
            "repack": repack,
        }

    try:
        import OpenGL
        import OpenGL.acceleratesupport as accelerate_support

        report["opengl"] = {
            "version": str(OpenGL.__version__),
            "use_accelerate": bool(OpenGL.USE_ACCELERATE),
            "accelerate_available": bool(
                getattr(accelerate_support, "ACCELERATE_AVAILABLE", False)
            ),
            "renderer": None,
        }
    except Exception as exc:  # pragma: no cover - optional runtime diagnostic
        report["opengl"] = {"error": str(exc), "renderer": None}

    report_path = args.output_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    page.close()
    print(json.dumps({"ok": True, "report": str(report_path.resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
