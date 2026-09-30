"""Export real PSDs and record compositing, no-op and local edit evidence.

The optional GPU reference is an image supplied by a separate renderer.  This
script deliberately labels its provenance instead of treating any GL render
as a Cubism SDK framebuffer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.psd_reconstructor import (
    reconstruct_live2d_psd,
    repack_atlas_png_from_psd,
    resolve_live2d_source,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _checker(size: tuple[int, int]) -> Image.Image:
    width, height = size
    y, x = np.indices((height, width))
    shade = np.where(((x // 8 + y // 8) % 2) == 0, 230, 190).astype(np.uint8)
    return Image.fromarray(np.dstack((shade, shade, shade, np.full_like(shade, 255))), "RGBA")


def _montage(images: list[tuple[str, Image.Image]], path: Path) -> None:
    width = max(image.width for _, image in images)
    height = max(image.height for _, image in images)
    result = Image.new("RGBA", (width * len(images), height + 28), "white")
    draw = ImageDraw.Draw(result)
    for index, (label, image) in enumerate(images):
        panel = _checker((width, height))
        panel.alpha_composite(image, ((width - image.width) // 2, (height - image.height) // 2))
        result.alpha_composite(panel, (index * width, 28))
        draw.text((index * width + 6, 6), label, fill="black")
    result.save(path)


def _capture_sdk_reference(source, output: Path, sizes: dict[str, tuple[int, int]], parameters: dict) -> dict:
    """Capture the official Cubism renderer through the installed wrapper.

    No window is shown.  Motion, blinking, breath and physics are disabled so
    the reference uses the same frozen Core parameter values as the PSD.
    """
    import os
    from importlib.metadata import version
    os.environ.setdefault("QT_QPA_PLATFORM", "windows")
    from PySide6.QtGui import QGuiApplication, QOffscreenSurface, QOpenGLContext, QSurfaceFormat
    from OpenGL import GL
    import live2d.v3 as live2d

    staged = output / "native_source"
    staged.mkdir(exist_ok=True)
    shutil.copy2(source.moc3, staged / "model.moc3")
    texture_names = []
    for index, path in enumerate(source.textures):
        name = f"texture_{index:02d}.png"
        shutil.copy2(path, staged / name)
        texture_names.append(name)
    settings = {"Version": 3, "FileReferences": {"Moc": "model.moc3", "Textures": texture_names, "Motions": {}}, "Groups": []}
    model_json = staged / "sdk_reference.model3.json"
    # The installed CubismJson parser fails on this model's compact, single
    # line JSON.  The application also normalizes preview settings with indent.
    model_json.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    app = QGuiApplication.instance() or QGuiApplication([])
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.CompatibilityProfile)
    surface = QOffscreenSurface()
    surface.setFormat(fmt)
    surface.create()
    context = QOpenGLContext()
    context.setFormat(fmt)
    if not context.create() or not context.makeCurrent(surface):
        raise RuntimeError("Could not create hidden OpenGL reference context")
    renderer = GL.glGetString(GL.GL_RENDERER).decode()
    gl_version = GL.glGetString(GL.GL_VERSION).decode()
    live2d.init()
    live2d.glInit()
    model = live2d.LAppModel()
    model.LoadModelJson(model_json.as_posix())
    model.SetAutoBlinkEnable(False)
    model.SetAutoBreathEnable(False)
    model.ResetParameters()
    for name, value in parameters.items():
        model.SetParameterValue(name, float(value))
    model._model.Update(0.0)
    fbo = int(GL.glGenFramebuffers(1))
    texture = int(GL.glGenTextures(1))
    paths = {}
    for label, (width, height) in sizes.items():
        model.Resize(width, height)
        GL.glBindTexture(GL.GL_TEXTURE_2D, texture)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, width, height, 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, None)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_TEXTURE_2D, texture, 0)
        if GL.glCheckFramebufferStatus(GL.GL_FRAMEBUFFER) != GL.GL_FRAMEBUFFER_COMPLETE:
            raise RuntimeError("Incomplete native reference framebuffer")
        GL.glViewport(0, 0, width, height)
        live2d.clearBuffer()
        model.Draw()
        GL.glFinish()
        raw = GL.glReadPixels(0, 0, width, height, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE)
        rgba = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 4)[::-1].copy()
        alpha = rgba[:, :, 3:4].astype(np.float32) / 255
        rgba[:, :, :3] = np.divide(rgba[:, :, :3], alpha, out=np.zeros_like(rgba[:, :, :3], dtype=np.float32), where=alpha > 0).clip(0, 255).round().astype(np.uint8)
        path = output / f"native_sdk_{label}_full.png"
        image = Image.fromarray(rgba, "RGBA")
        image.save(path)
        thumb = image.copy()
        thumb.thumbnail((640, 960))
        thumb.save(output / f"native_sdk_{label}.png")
        paths[label] = str(path.resolve())
    model = None
    GL.glDeleteTextures([texture])
    GL.glDeleteFramebuffers(1, [fbo])
    live2d.glRelease()
    live2d.dispose()
    context.doneCurrent()
    return {"kind": "Cubism Native SDK framebuffer through live2d-py", "wrapper_version": version("live2d-py"), "renderer": renderer, "gl_version": gl_version, "parameters": parameters, "physics_enabled": False, "images": paths}


def _visible_rgba(image: Image.Image) -> np.ndarray:
    value = np.asarray(image.convert("RGBA"), dtype=np.float32).copy()
    value[:, :, :3] *= value[:, :, 3:4] / 255.0
    return value


def _one_pixel_probe(psd_path: Path, metadata_path: Path, texture: Path, output: Path, drawable_id: str) -> dict:
    from psd_tools import PSDImage
    from psd_tools.api.layers import PixelLayer
    from psd_tools.constants import Compression, Tag

    output.mkdir(parents=True, exist_ok=True)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    target = next(item for item in metadata["layers"] if item["drawable_id"] == drawable_id)
    psd = PSDImage.open(psd_path)
    pixel = next(layer for layer in psd if layer.layer_id == target["layer_id"])
    image = pixel.topil().convert("RGBA")
    rgba = np.asarray(image)
    points = np.argwhere(rgba[:, :, 3] > 240)
    if not len(points):
        raise RuntimeError(f"No opaque edit probe pixel in {drawable_id}")
    local_y, local_x = map(int, points[len(points) // 2])
    image.putpixel((local_x, local_y), (255, 0, 255, 255))
    index = list(psd).index(pixel)
    replacement = PixelLayer.frompil(
        image, psd, name=pixel.name, top=pixel.top, left=pixel.left, compression=Compression.RAW,
    )
    replacement.tagged_blocks.set_data(Tag.LAYER_ID, pixel.layer_id)
    replacement.blend_mode = pixel.blend_mode
    psd.remove(replacement)
    psd.remove(pixel)
    psd.insert(index, replacement)
    edited_path = output / "edited.psd"
    psd.save(edited_path)
    result = repack_atlas_png_from_psd(edited_path, output / "repacked", metadata_path=metadata_path)
    source = np.asarray(Image.open(texture).convert("RGBA"))
    packed = np.asarray(Image.open(result.output_paths[0]).convert("RGBA"))
    changed = np.any(source != packed, axis=2)
    affected = np.argwhere(changed)
    bbox = (
        [int(affected[:, 1].min()), int(affected[:, 0].min()), int(affected[:, 1].max()) + 1, int(affected[:, 0].max()) + 1]
        if len(affected) else None
    )
    if not len(affected) or len(affected) > 64 or (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) > 64:
        raise AssertionError(f"Nonlocal edit probe: {int(changed.sum())} pixels, bbox={bbox}")
    report = {"drawable": drawable_id, "local_pixel": [local_x, local_y], "changed_atlas_pixels": int(changed.sum()), "atlas_bbox": bbox}
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--previous-dir", type=Path)
    parser.add_argument("--gpu-reference", type=Path)
    parser.add_argument("--capture-sdk", action="store_true")
    parser.add_argument("--reuse-exports", action="store_true", help="Reuse already verified exports while adding reference evidence")
    parser.add_argument("--reference-kind", default="independent OpenGL triangles, not a Cubism SDK framebuffer")
    parser.add_argument("--inspect-drawable", default="ArtMesh0")
    parser.add_argument("--inspection-bbox", nargs=4, type=int)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    source = resolve_live2d_source(args.model)
    texture = source.textures[0]
    before_hash = _sha256(texture)
    report: dict = {"source_model": str(source.model_json), "source_texture": str(texture), "source_texture_sha256": before_hash, "exports": {}}
    from psd_tools import PSDImage

    if args.reuse_exports:
        report = json.loads((args.output_dir / "report.json").read_text(encoding="utf-8"))
    for label, maximum in (("original", 0), ("2048", 2048)):
        if args.reuse_exports:
            continue
        output = args.output_dir / label
        result = reconstruct_live2d_psd(args.model, output, resource_limits={"mesh_max_dimension": maximum})
        psd = PSDImage.open(result.psd_path)
        metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
        composite = psd.composite(force=True).convert("RGBA")
        composite.save(output / "actual_composite_full.png")
        thumb = composite.copy()
        thumb.thumbnail((640, 960))
        thumb.save(output / "actual_composite.png")
        names = [layer.name for layer in psd]
        expected = [layer["name"] for layer in metadata["layers"]]
        if names != expected or any(layer.is_group() for layer in psd):
            raise AssertionError("Saved PSD changed the flat ArtMesh stack")
        roundtrip = repack_atlas_png_from_psd(result.psd_path, output / "roundtrip")
        changed = sum(
            int(np.any(np.asarray(Image.open(src).convert("RGBA")) != np.asarray(Image.open(dst).convert("RGBA")), axis=2).sum())
            for src, dst in zip(source.textures, roundtrip.output_paths)
        )
        if changed:
            raise AssertionError(f"No-op repack changed {changed} atlas pixels")
        report["exports"][label] = {
            "psd": str(result.psd_path.resolve()), "canvas": list(psd.size), "layers": len(psd), "groups": 0,
            "render_order_preserved": True, "unchanged_roundtrip_pixels": changed,
            "blend_modes": {layer.name: layer.blend_mode.name for layer in psd if layer.blend_mode.name != "NORMAL"},
            "one_pixel_probe": _one_pixel_probe(result.psd_path, result.metadata_path, texture, output / "one_pixel_probe", args.inspect_drawable),
        }
        print(json.dumps({label: report["exports"][label]}, ensure_ascii=False), flush=True)

    mesh = json.loads((args.output_dir / "original" / f"{source.model_json.stem}.drawables.json").read_text(encoding="utf-8"))
    drawable = next(item for item in mesh["drawables"] if item["id"] == args.inspect_drawable)
    if args.capture_sdk:
        report["sdk_reference"] = _capture_sdk_reference(
            source, args.output_dir,
            {label: tuple(info["canvas"]) for label, info in report["exports"].items()},
            mesh.get("parameters", {}),
        )
        for label, info in report["exports"].items():
            native = Image.open(report["sdk_reference"]["images"][label]).convert("RGBA")
            current = Image.open(args.output_dir / label / "actual_composite_full.png").convert("RGBA")
            delta = np.abs(_visible_rgba(native) - _visible_rgba(current))
            info["sdk_visible_rgba_mae"] = float(delta.mean())
            if args.previous_dir:
                previous = Image.open(args.previous_dir / label / "actual_composite_full.png").convert("RGBA")
                info["previous_sdk_visible_rgba_mae"] = float(np.abs(_visible_rgba(native) - _visible_rgba(previous)).mean())
            if args.inspection_bbox:
                scale = native.height / report["exports"]["original"]["canvas"][1]
                x0, y0, x1, y1 = [int(round(value * scale)) for value in args.inspection_bbox]
                info["sdk_head_visible_rgba_mae"] = float(delta[y0:y1, x0:x1].mean())
                if args.previous_dir:
                    info["previous_sdk_head_visible_rgba_mae"] = float(np.abs(_visible_rgba(native.crop((x0,y0,x1,y1))) - _visible_rgba(previous.crop((x0,y0,x1,y1)))).mean())
    tex_image = Image.open(texture).convert("RGBA")
    uvs = np.asarray(drawable["uvs"], dtype=np.float64)
    uv_points = np.column_stack((uvs[:, 0] * tex_image.width, (1 - uvs[:, 1]) * tex_image.height))
    crop_bbox = [int(np.floor(uv_points[:, 0].min())), int(np.floor(uv_points[:, 1].min())), int(np.ceil(uv_points[:, 0].max())), int(np.ceil(uv_points[:, 1].max()))]
    crop = tex_image.crop(crop_bbox)
    crop_path = args.output_dir / f"{args.inspect_drawable}_source_rgba.png"
    crop.save(crop_path)
    crop_rgba = np.asarray(crop)
    report["source_crop"] = {
        "drawable": args.inspect_drawable, "bbox": crop_bbox, "image": str(crop_path.resolve()),
        "rgba_preserved": True, "transparent_pixels": int((crop_rgba[:, :, 3] == 0).sum()),
        "transparent_pixels_with_rgb_padding": int(((crop_rgba[:, :, 3] == 0) & np.any(crop_rgba[:, :, :3] > 0, axis=2)).sum()),
        "semi_transparent_pixels": int(((crop_rgba[:, :, 3] > 0) & (crop_rgba[:, :, 3] < 255)).sum()),
    }

    panels = [("SOURCE atlas RGBA (1:1)", crop)]
    scene_panels = []
    for label, folder in (("PREVIOUS PSD", args.previous_dir), ("NEW PSD", args.output_dir)):
        if folder is None:
            continue
        psd_file = next((folder / "original").glob("*.psd"))
        psd = PSDImage.open(psd_file)
        part = next(layer for layer in psd if layer.name == args.inspect_drawable)
        panels.append((label + " ArtMesh (1:1)", part.topil().convert("RGBA")))
        if args.inspection_bbox:
            scene_panels.append((label + " (1:1)", psd.composite(force=True).convert("RGBA").crop(args.inspection_bbox)))
    if args.gpu_reference:
        reference = Image.open(args.gpu_reference).convert("RGBA")
        report["gpu_reference"] = {"image": str(args.gpu_reference.resolve()), "kind": args.reference_kind}
        if args.inspection_bbox:
            scene_panels.append(("GPU triangles (1:1)", reference.crop(args.inspection_bbox)))
        gpu_part = args.gpu_reference.parent / f"{args.inspect_drawable}_gpu.png"
        if gpu_part.is_file():
            panels.append(("GPU ArtMesh (1:1)", Image.open(gpu_part).convert("RGBA")))
    if report.get("sdk_reference") and args.inspection_bbox:
        native = Image.open(report["sdk_reference"]["images"]["original"]).convert("RGBA")
        scene_panels.append(("CUBISM SDK (1:1)", native.crop(args.inspection_bbox)))
    _montage(panels, args.output_dir / "source_psd_gpu_hair_100percent.png")
    if scene_panels:
        _montage(scene_panels, args.output_dir / "previous_new_gpu_head_100percent.png")
    if _sha256(texture) != before_hash:
        raise AssertionError("Source texture changed during validation")
    report["source_texture_unchanged"] = True
    (args.output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
