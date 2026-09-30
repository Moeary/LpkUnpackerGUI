"""Preview a MOD skin using the same main model and texture mappings as export."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import tempfile

from app.core.live2d_editor_session import Live2DEditorSession
from app.core.live2dviewer_mod_project import Live2DViewerModProject, Live2DViewerModProjectError


def _project_asset(project: Live2DViewerModProject, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (project.project_dir / path).resolve()


@contextmanager
def mapped_mod_skin_preview(project: Live2DViewerModProject, model_id: str):
    """Yield a temporary, complete package; consumers must copy it before return.

    Every exported skin uses the main MOC and motions, including skins imported
    from a different complete model. Its texture mappings supply the variation.
    Neither the project files nor either imported source is changed here.
    """
    if not project.models:
        raise Live2DViewerModProjectError("The project has no main model.")
    main = project.models[0]
    selected = next((item for item in project.models if str(item.get("id")) == model_id), None)
    if selected is None:
        raise Live2DViewerModProjectError("The selected MOD model no longer exists.")
    source = _project_asset(project, str(main.get("model_json") or ""))
    session = Live2DEditorSession(source)
    try:
        if selected is not main:
            applied = 0
            targets = set()
            for mapping in selected.get("texture_mappings") or []:
                try:
                    target = int(mapping.get("target_index"))
                except (AttributeError, TypeError, ValueError):
                    continue
                image = _project_asset(project, str(mapping.get("source_texture") or mapping.get("source") or ""))
                if not image.is_file() or not 0 <= target < len(session.texture_paths):
                    continue
                if target in targets:
                    raise Live2DViewerModProjectError("A main texture is mapped more than once.")
                session.replace_texture(target, image)
                targets.add(target)
                applied += 1
            if not applied:
                raise Live2DViewerModProjectError("No valid texture mapping is configured for this skin.")
        with tempfile.TemporaryDirectory(prefix="lpk-mod-skin-preview-") as directory:
            result = session.save_copy(Path(directory) / "model")
            yield Path(result["model_path"])
    finally:
        session.close()
