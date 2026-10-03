"""Real Qt/OpenGL acceptance: animation, HiDPI pixels, and window-state stability.

Run on the Windows desktop (not QT_QPA_PLATFORM=offscreen). User settings are
redirected to an isolated diagnostic directory. No browser or HTTP server is used.
"""
from __future__ import annotations
import argparse
import ctypes
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication, Qt
QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
from PySide6.QtWidgets import QApplication
QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

from app.core.spine_preview import load_spine_asset, read_spine_version


def pump(app, seconds=.1):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        app.processEvents()
        time.sleep(.005)


def wait(app, predicate, timeout=30):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        app.processEvents()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Timed out waiting for native preview')


def window_state(window):
    hwnd = int(window.winId())
    style = ctypes.windll.user32.GetWindowLongW(hwnd, -16) if os.name == 'nt' else 0
    return dict(hwnd=hwnd, maximized=window.isMaximized(), fullscreen=window.isFullScreen(),
                maximize_box=bool(style & 0x10000), resizable=bool(style & 0x40000),
                size=[window.width(), window.height()])


def pixels(canvas):
    image = canvas.grabFramebuffer()
    if image.isNull():
        raise AssertionError('Empty OpenGL framebuffer')
    raw = bytes(image.constBits())
    # More than a clear background must be present; use actual physical pixels.
    colors = set(raw[i:i+4] for i in range(0, len(raw), max(4, len(raw)//20000//4*4)))
    if len(colors) < 8:
        raise AssertionError(f'Blank/flat framebuffer ({len(colors)} sampled colors)')
    return image, dict(width=image.width(), height=image.height(), colors=len(colors),
                      sha256=hashlib.sha256(raw).hexdigest())


def source_original_spine_version(source):
    """Read the source skeleton version before the preview worker prepares its plan."""

    path = Path(source).resolve()
    try:
        asset = load_spine_asset(path)
        skeleton = getattr(asset, "skeleton_path", None)
        if skeleton:
            version = read_spine_version(skeleton)
            if version:
                return version
        return getattr(asset, "spine_version", None)
    except Exception:
        # Only a binary skeleton is safe to read directly after discovery
        # fails.  A model0.json may contain unrelated ``version`` metadata;
        # reading it directly would report that value as the Spine version.
        if path.is_file() and path.name.lower().endswith((".skel", ".skel.bytes")):
            return read_spine_version(path)
        # Archives and unresolved model configurations are reported as
        # unknown here; the prepared asset report remains authoritative.
        return None


def spine_plan_report(source, plan):
    """Return version/provenance fields needed by the acceptance report."""

    asset = getattr(plan, "asset", None)
    runtime = getattr(plan, "runtime", None)
    skeleton = getattr(asset, "skeleton_path", None)
    library = getattr(runtime, "library_path", None)
    root_dir = getattr(runtime, "root_dir", None)
    return {
        "source": {
            "path": str(Path(source).resolve()),
            "originalread_spine_version": source_original_spine_version(source),
        },
        "prepared": {
            "spine_version": getattr(asset, "spine_version", None),
            "skeleton_path": str(skeleton) if skeleton else None,
        },
        "plan": {
            "mode": getattr(plan, "mode", None),
            "reason": getattr(plan, "reason", ""),
            "warnings": list(getattr(plan, "warnings", ()) or ()),
        },
        "runtime": {
            "version": getattr(runtime, "version", None),
            "family": getattr(runtime, "family", None),
            "librarypath": str(library) if library else None,
            "root_dir": str(root_dir) if root_dir else None,
        },
    }


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--source', required=True)
    p.add_argument('--runtime', default='runtime/tools/spine_native')
    p.add_argument('--switch-source')
    p.add_argument('--live2d-source')
    p.add_argument('--expect-version', help='Assert every prepared plan.asset.spine_version equals this value')
    p.add_argument('--report', default='runtime/validation/spine_native_acceptance.json')
    p.add_argument('--screenshot', default='runtime/validation/spine_native_acceptance.png')
    p.add_argument('--width', type=int, default=1600)
    p.add_argument('--height', type=int, default=1000)
    p.add_argument('--timeout', type=float, default=45)
    args=p.parse_args()
    report={'ok':False, 'source':args.source, 'runtime':args.runtime,
            'expect_version':args.expect_version, 'spine_plans':{}}
    output=Path(args.report).resolve();output.parent.mkdir(parents=True,exist_ok=True)
    sandbox=Path(tempfile.mkdtemp(prefix='native-qa-',dir=output.parent))
    import app.core.settings_manager as sm
    original=sm.SettingsManager
    class DiagnosticSettings(original):
        def __init__(self, settings_file=None):
            super().__init__(settings_file or str(sandbox/'settings.json'))
    sm.SettingsManager=DiagnosticSettings
    settings=DiagnosticSettings()
    settings.set('preview.spine_runtime_dir',str(Path(args.runtime).resolve()))
    settings.set('runtime.temp_dir',str(sandbox/'temp'))
    settings.set_output_root(str(sandbox/'output'))
    settings.set('language','zh_CN')
    app=QApplication([])
    window=None
    try:
        from app.gui.MainWindow import MainWindow
        window=MainWindow();window.resize(args.width,args.height);window.show()
        window.switchTo(window.previewPage)
        pump(app,.3)
        window.showMaximized();pump(app,.3)
        baseline=window_state(window);report['before']=baseline
        assert baseline['maximized']
        available=window.screen().availableGeometry()
        report['available_geometry']=[available.width(),available.height()]
        assert window.height()<=available.height()+16,'Page minimum height exceeds monitor work area'
        assert window.width()<=available.width()+16,'Page minimum width exceeds monitor work area'
        page=window.previewPage
        page.show_error=lambda title,message: report.setdefault('errors',[]).append(str(message))
        for index,source in enumerate(filter(None,[args.source,args.switch_source])):
            label='first' if index == 0 else 'switch'
            page.start_spine_preview_import(source)
            widget=page.spine_preview
            wait(app,lambda: widget.last_state.get('runtimeLoaded') or bool(report.get('errors')),args.timeout)
            if report.get('errors'): raise AssertionError(report['errors'])
            pump(app,.25)
            state=widget.last_state
            assert state.get('skeletonLoaded'),state
            plan=getattr(page, '_active_spine_plan', None) or getattr(widget, '_plan', None)
            if plan is None:
                raise AssertionError('Spine preview did not expose its prepared plan')
            plan_report = spine_plan_report(source, plan)
            report['spine_plans'][label] = plan_report
            if args.expect_version is not None:
                assert plan.asset.spine_version == args.expect_version, (
                    f"Prepared Spine version {plan.asset.spine_version!r} "
                    f"does not match --expect-version {args.expect_version!r}"
                )
            canvas=widget.canvas
            first_image,first_pixels=pixels(canvas)
            entry={'state':state,'pixels':first_pixels,'window':window_state(window),
                   'device_pixel_ratio':canvas.devicePixelRatio(), 'samples':canvas.format().samples()}
            from OpenGL.GL import GL_RENDERER, GL_VENDOR, GL_VERSION, glGetString
            canvas.makeCurrent()
            try:
                entry['opengl'] = {
                    name: (glGetString(token) or b'').decode('utf-8', errors='replace')
                    for name, token in (
                        ('renderer', GL_RENDERER), ('vendor', GL_VENDOR), ('version', GL_VERSION)
                    )
                }
            finally:
                canvas.doneCurrent()
            entry['spine'] = plan_report
            report[label]=entry
            assert entry['window']['hwnd']==baseline['hwnd'],'Preview replaced the top-level HWND'
            assert entry['window']['maximized'],'Preview restored the maximized window'
            if os.name=='nt': assert entry['window']['maximize_box'] and entry['window']['resizable']
            controls=page.spine_controls
            choices=state.get('animationOptions',[])
            names=[x['value'] if isinstance(x,dict) else x for x in choices]
            preferred=next((n for n in names if n.lower() in ('normal','idle')),names[0] if names else '')
            if preferred:
                controls.animation_combo.setCurrentIndex(controls.animation_combo.findText(preferred))
                controls.loop_check.setChecked(True)
                controls.play_pause_btn.setChecked(False)
                pump(app,.3)
                t0=widget.last_state.get('time',0)
                screen_a=canvas.screen().grabWindow(int(canvas.winId())).toImage()
                screen_hash_a=hashlib.sha256(bytes(screen_a.constBits())).hexdigest()
                pump(app,.5)
                screen_b=canvas.screen().grabWindow(int(canvas.winId())).toImage()
                screen_hash_b=hashlib.sha256(bytes(screen_b.constBits())).hexdigest()
                assert screen_hash_a!=screen_hash_b,'On-screen animation pixels did not change'
                _,a=pixels(canvas)
                pump(app,.4);t1=widget.last_state.get('time',0);_,b=pixels(canvas)
                assert t0!=t1,'Animation clock did not advance'
                assert a['sha256']!=b['sha256'],'Animation pixels did not change'
                controls.play_pause_btn.click();pump(app,.3);paused=widget.last_state.get('time',0)
                assert widget.last_state.get('paused'), 'Native pause button did not pause'
                pump(app,.35);assert abs(widget.last_state.get('time',0)-paused)<.03
                duration=widget.last_state.get('timeMax',0)
                controls.time_slider.setValue(400);pump(app,.3)
                assert abs(widget.last_state.get('time',0)-duration*.4)<.08
                entry['animation']={'selected':preferred,'advanced':True,'pixels_changed':True,'pause_seek':True}
            for enabled in [False,True]:
                page.settings_panel.antialias_check.setChecked(enabled)
                page.settings_panel.scale_slider.setValue(200)
                pump(app,.25);_,frame=pixels(canvas)
                assert frame['width']>=round(canvas.width()*canvas.devicePixelRatio())-2
                entry['antialias_'+str(enabled)]=frame
            if entry['samples'] > 0:
                assert entry['antialias_False']['sha256'] != entry['antialias_True']['sha256'], 'MSAA toggle did not change edge samples'
            page.settings_panel.scale_slider.setValue(100)
            for _ in range(2):
                window.showNormal();pump(app,.15);assert not window.isMaximized()
                window.showMaximized();pump(app,.15);assert window.isMaximized()
            assert int(window.winId())==baseline['hwnd']
            window.showFullScreen();pump(app,.2);assert window.isFullScreen()
            window.showNormal();pump(app,.2);window.showMaximized();pump(app,.2)
            assert window.isMaximized() and int(window.winId())==baseline['hwnd']
            entry['maximize_cycles']=2
            entry['fullscreen_cycle']=True
            pump(app,.2)
            target=Path(args.screenshot).resolve()
            if index:target=target.with_stem(target.stem+'_switch')
            target.parent.mkdir(parents=True,exist_ok=True)
            window.screen().grabWindow(int(window.winId())).save(str(target))
            first_image.save(str(target.with_stem(target.stem+'_frame')))
        page.close_preview_window();pump(app,.2)
        window.showNormal();pump(app,.1);window.showMaximized();pump(app,.2)
        report['after_close']=window_state(window)
        assert report['after_close']['hwnd']==baseline['hwnd'] and window.isMaximized()
        if args.live2d_source:
            if Path(args.live2d_source).suffix.lower()=='.json':
                page.open_model_preview_source(args.live2d_source)
            else:
                page.on_file_dropped(args.live2d_source)
            wait(app,lambda:(page.live2d_preview is not None and page.current_model_path) or bool(report.get('errors')),args.timeout)
            if report.get('errors'): raise AssertionError(report['errors'])
            pump(app,1)
            live=page.live2d_preview.live2d_canvas
            _,report['live2d_pixels']=pixels(live)
            assert int(window.winId())==baseline['hwnd']
            window.showNormal();pump(app,.2);window.showMaximized();pump(app,.2)
            assert window.isMaximized()
            page.close_preview_window()
        report['ok']=True
    except Exception as exc:
        report['error']=repr(exc)
        import traceback
        traceback.print_exc()
    finally:
        if window:
            page=window.previewPage
            page.close_preview_window();page._destroy_embedded_spine();page._destroy_embedded_live2d()
            window.close();pump(app,.1)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'ok':report['ok'],'report':str(output),'error':report.get('error')},ensure_ascii=False))
    return 0 if report['ok'] else 1


if __name__=='__main__':
    raise SystemExit(main())
