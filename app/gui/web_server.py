import os
import sys
import socket
import threading
import json
import base64
import binascii
from pathlib import Path

from app.paths import ASSETS_DIR, RUNTIME_TEMP_DIR
from app.core.spine_preview import SpinePoseLayer, export_spine_pose_psd
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
import uuid
from typing import Dict, Set


def _resolve_assets_dir() -> Path:
    """Resolve the assets directory containing 'live2d' and 'vendor'.

    Works in both dev and packaged (sys.frozen) environments.
    """
    if getattr(sys, 'frozen', False):
        base = Path(sys.executable).parent
        candidates = [
            base / "assets",
            ASSETS_DIR,
            Path(os.getcwd()) / "assets",
        ]
    else:
        candidates = [
            ASSETS_DIR,
            Path(os.getcwd()) / "assets",
        ]

    for p in candidates:
        if p.exists():
            return p
    return ASSETS_DIR


assets_dir = _resolve_assets_dir()

app = FastAPI(title="LpkUnpacker Web Proxy")

# 添加CORS中间件支持跨域请求
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源（本地开发）
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files at /static so relative paths in web.html resolve to /static/vendor/...
app.mount("/static", StaticFiles(directory=str(assets_dir), html=True), name="static")


@app.get("/")
def root():
    # Redirect to the Live2D web page with embedded controls
    return RedirectResponse(url="/static/live2d/web.html")


@app.get("/favicon.ico")
async def favicon():
    """返回404避免favicon请求错误日志"""
    from fastapi.responses import Response
    return Response(status_code=404)


def _find_free_port(host: str = "127.0.0.1") -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((host, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_server(host: str = "127.0.0.1", port: int = 0) -> int:
    """Start uvicorn server as a daemon thread and return the actual port.

    Uses a minimal logging configuration to avoid dynamic formatter imports
    (e.g., 'uvicorn.logging.DefaultFormatter') that can break in packaged builds.
    """
    try:
        import uvicorn
    except ImportError:
        raise RuntimeError("uvicorn is required to start the web proxy. Please install 'uvicorn'.")

    actual_port = port or _find_free_port(host)

    config = uvicorn.Config(
        app,
        host=host,
        port=actual_port,
        log_level="info",
    )
    server = uvicorn.Server(config)

    # 启动服务器线程
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    
    # 等待一小段时间确保服务器完全启动
    import time
    time.sleep(0.8)
    
    return actual_port


# ---------------- Dynamic model directory mounting ----------------

_mounted_models: Dict[str, Path] = {}
_spine_pose_exports: Dict[str, Path] = {}

def mount_model_dir(dir_path: str) -> str:
    """Mount a local directory under a unique URL prefix and return the base path.

    Example return: "/model/abcde" so a file "model.json" becomes "/model/abcde/model.json".

    This allows loading a selected Live2D folder via HTTP, so the web UI can
    fetch JSON and related textures relative to the same base path.
    """
    p = Path(dir_path)
    if not p.exists() or not p.is_dir():
        raise ValueError(f"Model directory does not exist: {dir_path}")

    # Reuse existing mount if already mounted
    for mount_id, mounted_path in _mounted_models.items():
        if mounted_path.resolve() == p.resolve():
            return f"/model/{mount_id}"

    # Create a new unique mount id
    mount_id = uuid.uuid4().hex[:8]
    base_path = f"/model/{mount_id}"
    app.mount(base_path, StaticFiles(directory=str(p), html=False), name=f"model_{mount_id}")
    _mounted_models[mount_id] = p
    return base_path


# ---------------- Preview message bus (WebSocket + HTTP broadcast) ----------------

_preview_clients: Set[WebSocket] = set()


@app.websocket("/ws/preview")
async def ws_preview(ws: WebSocket):
    await ws.accept()
    _preview_clients.add(ws)
    try:
        while True:
            # We don't expect messages from clients; just keep the connection alive
            await ws.receive_text()
    except WebSocketDisconnect:
        try:
            _preview_clients.remove(ws)
        except KeyError:
            pass


async def _broadcast_to_clients(message: dict):
    # Send to a snapshot of current clients to avoid set mutation issues
    dead_clients = []
    for ws in list(_preview_clients):
        try:
            await ws.send_text(json.dumps(message))
        except Exception:
            dead_clients.append(ws)
    # Cleanup dead clients
    for ws in dead_clients:
        try:
            _preview_clients.remove(ws)
        except KeyError:
            pass


@app.post("/api/preview/broadcast")
async def http_broadcast(request: Request):
    """Accept JSON and broadcast to all connected preview clients."""
    try:
        payload = await request.json()
    except Exception:
        payload = {"type": "error", "message": "Invalid JSON"}
    await _broadcast_to_clients(payload)
    return {"ok": True, "clients": len(_preview_clients)}


@app.post("/api/spine/pose-export")
async def spine_pose_export(request: Request):
    """Convert browser-rasterized Spine attachment layers into a PSD.

    The web page sends transparent PNG data URLs for the visible slots at the
    current animation time.  The server writes those disposable images under
    ``runtime/temp`` and delegates the actual PSD construction to the core
    exporter.  Source models and user runtimes are never used as output roots.
    """
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc
    raw_layers = payload.get("layers") if isinstance(payload, dict) else None
    if not isinstance(raw_layers, list) or not raw_layers:
        raise HTTPException(status_code=400, detail="layers must be a non-empty list")
    if len(raw_layers) > 64:
        raise HTTPException(status_code=413, detail="at most 64 visible attachment layers may be exported")
    unsupported = [str(value) for value in (payload.get("unsupported") or []) if str(value)]
    if unsupported:
        raise HTTPException(
            status_code=422,
            detail="Pose export blocked by unsupported Spine features: " + ", ".join(unsupported),
        )

    export_id = uuid.uuid4().hex
    output_root = (Path(RUNTIME_TEMP_DIR) / "spine_pose_exports" / export_id).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    layers: list[SpinePoseLayer] = []
    total_bytes = 0
    try:
        for index, item in enumerate(raw_layers):
            if not isinstance(item, dict) or not item.get("data"):
                continue
            data_url = str(item.get("data"))
            encoded = data_url.split(",", 1)[1] if "," in data_url else data_url
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise HTTPException(status_code=400, detail=f"invalid layer {index} PNG data") from exc
            total_bytes += len(raw)
            if total_bytes > 100 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="pose layer payload is too large")
            image_path = output_root / f"layer_{index:03d}.png"
            image_path.write_bytes(raw)
            raw_opacity = item.get("opacity", 1.0)
            opacity = float(1.0 if raw_opacity is None else raw_opacity)
            layers.append(
                SpinePoseLayer(
                    name=str(item.get("name") or f"attachment_{index:03d}"),
                    image=image_path,
                    left=int(item.get("left", 0) or 0),
                    top=int(item.get("top", 0) or 0),
                    opacity=opacity,
                    visible=bool(item.get("visible", True)),
                    attachment=str(item.get("attachment") or ""),
                )
            )
        if not layers:
            raise HTTPException(status_code=400, detail="No visible PNG layers were supplied")
        width = int(payload.get("width", 0) or 0)
        height = int(payload.get("height", 0) or 0)
        report = await run_in_threadpool(
            export_spine_pose_psd,
            layers,
            output_root / "spine_pose.psd",
            canvas_size=(width, height) if width > 0 and height > 0 else None,
            unsupported=unsupported,
        )
        _spine_pose_exports[export_id] = report.output_path
        return {
            "ok": True,
            "id": export_id,
            "download": f"/api/spine/pose-export/{export_id}",
            "layers": list(report.layer_names),
            "unsupported": list(report.unsupported),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/spine/pose-export/{export_id}")
def download_spine_pose_export(export_id: str):
    path = _spine_pose_exports.get(str(export_id))
    if not path or not path.is_file():
        raise HTTPException(status_code=404, detail="pose export no longer exists")
    return FileResponse(path, media_type="image/vnd.adobe.photoshop", filename="spine_pose.psd")
