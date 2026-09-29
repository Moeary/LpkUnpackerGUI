"""Create self-contained Spine editor projects from converted skeleton data.

Spine JSON exports refer to images through ``skeleton.images`` and attachment
names.  A texture atlas is a runtime representation, so importing a JSON file
whose only images are atlas pages leaves the attachments unloaded in the
Spine editor.  This helper creates an editor-only package with one PNG per
atlas region, rewrites the image paths, and asks the installed official Spine
CLI to create the real ``.spine`` project.

The generated project is deliberately kept beside the converted runtime
assets under ``editor_project``.  The source JSON, atlas, and pages are never
modified.  The project package contains the rewritten import JSON, extracted
images, atlas metadata, the official CLI output, and a validation report.

Official references (checked 2026-09-29):

* https://en.esotericsoftware.com/spine-command-line-interface#Import
* https://us.esotericsoftware.com/spine-json-format#Attachments
* https://de.esotericsoftware.com/spine-atlas-format#Rendering
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.core.spine_atlas import AtlasRegion, SpineAtlasError, extract_spine_atlas, parse_atlas


OFFICIAL_CLI_IMPORT_URL = "https://en.esotericsoftware.com/spine-command-line-interface#Import"
OFFICIAL_JSON_FORMAT_URL = "https://us.esotericsoftware.com/spine-json-format#Attachments"
OFFICIAL_ATLAS_FORMAT_URL = "https://de.esotericsoftware.com/spine-atlas-format#Rendering"
EDITOR_PROJECT_DIRECTORY = "editor_project"
EDITOR_IMAGES_DIRECTORY = "images"
EDITOR_TARGET_VERSION = "3.8.75"


class SpineEditorError(RuntimeError):
    """Base class for editor project generation failures."""


class SpineEditorNotFoundError(SpineEditorError):
    """The requested official Spine editor CLI was not found."""


class SpineEditorInputError(SpineEditorError, ValueError):
    """The converted JSON/atlas set cannot be made editor-ready safely."""


class SpineEditorProcessError(SpineEditorError):
    """The official Spine CLI rejected import or project validation."""


@dataclass(frozen=True)
class SpineEditorProjectResult:
    """Artifacts produced by :func:`create_spine_editor_project`."""

    project_path: Path
    import_json_path: Path
    images_dir: Path
    report_path: Path
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ExtractedRegion:
    name: str
    region: AtlasRegion
    image_path: Path
    relative_image_path: str


def discover_spine_editor(
    explicit: str | os.PathLike[str] | None = None,
    *,
    target_version: str = EDITOR_TARGET_VERSION,
) -> Path | None:
    """Find an installed official Spine command-line executable.

    An explicit path is authoritative.  Automatic discovery only checks the
    executable names and common Windows installation locations; it does not
    scan the user's whole disk.  ``Spine.com`` is preferred because it waits
    for CLI completion and forwards output to the caller's console.
    """

    explicit_text = str(explicit or "").strip().strip('"')
    if explicit_text:
        candidate = Path(explicit_text).expanduser()
        if candidate.is_file() and candidate.suffix.casefold() in {".com", ".exe", ".bat", ".cmd"}:
            return candidate.resolve()
        # A configured path is authoritative.  Returning None makes the
        # failure actionable instead of silently choosing another editor.
        return None

    names = ("Spine.com", "Spine.exe")
    candidates: list[Path] = []
    for name in names:
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))

    version = str(target_version or EDITOR_TARGET_VERSION).strip()
    program_roots: list[Path] = []
    for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            program_roots.append(Path(raw))
    # The vendor has used both ``Spine 3.8.75`` and ``Spine pro 3.8.75``
    # folder names over time.  These are patterns, not machine-specific paths.
    folder_names = (
        f"Spine {version}",
        f"Spine pro {version}",
        f"Spine\\{version}",
        f"Spine\\Spine {version}",
        f"Spine\\Spine pro {version}",
    )
    for root in program_roots:
        for folder in folder_names:
            folder_path = root / folder
            for name in names:
                candidates.append(folder_path / name)
        for name in names:
            candidates.extend(
                (
                    root / "Spine" / name,
                    root / "Spine" / version / name,
                )
            )

    # A portable install is often kept beside the application.  Restrict this
    # to the current working directory and known immediate children.
    cwd = Path.cwd()
    for parent in (cwd, cwd / "tools", cwd / "runtime" / "tools"):
        for name in names:
            candidates.append(parent / name)
        for folder in folder_names[:2]:
            for name in names:
                candidates.append(parent / folder / name)

    seen: set[str] = set()
    for candidate in candidates:
        try:
            key = str(candidate.resolve()).casefold()
        except (OSError, RuntimeError):
            continue
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate.resolve()
    return None


def create_spine_editor_project(
    skeleton_path: str | os.PathLike[str],
    atlas_paths: Sequence[str | os.PathLike[str]],
    output_dir: str | os.PathLike[str],
    *,
    editor_path: str | os.PathLike[str] | None = None,
    target_version: str = EDITOR_TARGET_VERSION,
    project_name: str | None = None,
) -> SpineEditorProjectResult:
    """Create and validate a real Spine ``.spine`` project.

    ``skeleton_path`` must be the converted JSON.  Atlas pages are unpacked
    through :func:`app.core.spine_atlas.extract_spine_atlas`, so right-angle
    rotation, PMA, and trim offsets are resolved before import.  Mesh UVs are
    preserved: Spine JSON stores original-image ``regionUVs`` and the extracted
    images are original-size and unrotated, which is the inverse of the
    official runtime's atlas UV adjustment.

    The project is written to ``<output_dir>/editor_project``.  If that
    directory already exists, a numbered sibling is selected without
    overwriting an earlier export.  The official CLI is invoked twice: once
    to import the rewritten JSON and once with ``-i`` to reopen the resulting
    project and validate its Spine version and skeleton name.
    """

    target = str(target_version or "").strip()
    if target != EDITOR_TARGET_VERSION:
        raise SpineEditorInputError(
            f"Official editor project export currently supports Spine {EDITOR_TARGET_VERSION}; "
            f"target {target or '<empty>'!r} needs a matching installed editor."
        )
    source_json = Path(skeleton_path).expanduser().resolve()
    if source_json.suffix.casefold() != ".json" or not source_json.is_file():
        raise SpineEditorInputError(f"Editor project import requires a converted JSON skeleton: {source_json}")
    try:
        source_data = json.loads(source_json.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SpineEditorInputError(f"Unable to read converted skeleton JSON {source_json}: {exc}") from exc
    if not isinstance(source_data, dict) or not isinstance(source_data.get("skeleton"), dict):
        raise SpineEditorInputError(f"Converted skeleton JSON has no skeleton metadata: {source_json}")

    editor = discover_spine_editor(editor_path, target_version=target)
    if editor is None:
        requested = str(editor_path) if editor_path else "an installed Spine 3.8.75 Spine.com/Spine.exe"
        raise SpineEditorNotFoundError(
            f"Unable to find the official Spine editor CLI ({requested}). "
            "Install Spine 3.8.75 or configure tools.spine_editor_path to Spine.com."
        )

    destination_root = Path(output_dir).expanduser().resolve()
    if not destination_root.is_dir():
        raise SpineEditorInputError(f"Converted output directory does not exist: {destination_root}")
    try:
        source_json.relative_to(destination_root)
    except ValueError as exc:
        raise SpineEditorInputError(
            f"Converted skeleton must be inside its output directory: {source_json}"
        ) from exc
    atlas_files = [Path(value).expanduser().resolve() for value in atlas_paths]
    atlas_files = _unique_files(atlas_files)
    if not atlas_files:
        raise SpineEditorInputError(
            "Cannot build an editor project without an atlas. Convert with the referenced atlas and page files present."
        )
    for atlas_path in atlas_files:
        if not atlas_path.is_file():
            raise SpineEditorInputError(f"Converted atlas does not exist: {atlas_path}")

    package = _create_package_directory(destination_root)
    images_root = package / EDITOR_IMAGES_DIRECTORY
    images_root.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    extracted_by_name: dict[str, list[_ExtractedRegion]] = {}
    atlas_reports: list[dict[str, Any]] = []
    try:
        for atlas_index, atlas_path in enumerate(atlas_files):
            atlas = parse_atlas(atlas_path)
            atlas_dir = images_root / f"atlas_{atlas_index:02d}"
            extracted = extract_spine_atlas(atlas, atlas_dir)
            region_records: list[dict[str, Any]] = []
            for region in atlas.regions:
                image_path = extracted.region_paths.get(region.region_id)
                if image_path is None or not image_path.is_file():
                    raise SpineEditorInputError(
                        f"Atlas extraction did not produce an image for region {region.name!r} in {atlas_path}"
                    )
                relative = image_path.relative_to(images_root).as_posix()
                record = _ExtractedRegion(
                    name=region.name,
                    region=region,
                    image_path=image_path.resolve(),
                    relative_image_path=relative,
                )
                extracted_by_name.setdefault(region.name, []).append(record)
                region_records.append(
                    {
                        "name": region.name,
                        "region_id": region.region_id,
                        "image": str(image_path),
                        "relative_image": relative,
                        "page": region.page_index,
                        "rotation": region.rotation,
                        "packed_size": [region.width, region.height],
                        "original_size": list(region.original_size),
                        "offset": [region.offset_x, region.offset_top],
                        "pma": bool(atlas.pages[region.page_index].pma),
                    }
                )
            atlas_reports.append(
                {
                    "source": str(atlas_path),
                    "output": str(atlas_dir),
                    "regions": region_records,
                    "warnings": list(extracted.warnings),
                }
            )
            warnings.extend(str(item) for item in extracted.warnings)
    except (OSError, SpineAtlasError, ValueError) as exc:
        raise SpineEditorInputError(f"Unable to unpack atlas for editor import: {exc}") from exc

    rewritten = _rewrite_skeleton_for_images(source_data, extracted_by_name, warnings)
    stem = project_name or source_json.stem
    safe_stem = _safe_component(stem) or "skeleton"
    import_json = package / f"{safe_stem}.json"
    project_path = package / f"{safe_stem}.spine"
    report_path = package / "spine_editor_report.json"
    import_json.write_text(json.dumps(rewritten, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    skeleton_name = safe_stem
    import_command = [str(editor), "-i", str(import_json), "-o", str(project_path), "-r", skeleton_name]
    import_run = _run_editor_command(import_command, cwd=package)
    if import_run.returncode != 0:
        report = _editor_report(
            editor,
            target,
            import_command,
            import_run,
            project_path,
            import_json,
            images_root,
            atlas_reports,
            warnings,
        )
        _write_report(report_path, report)
        diagnostic = import_run.stderr.strip() or import_run.stdout.strip() or "no diagnostic output"
        raise SpineEditorProcessError(
            f"Spine editor import failed with exit code {import_run.returncode}: {diagnostic}. "
            f"Converted output remains available at {destination_root}."
        )
    if not project_path.is_file() or project_path.stat().st_size == 0:
        raise SpineEditorProcessError(
            f"Spine editor reported success but produced no project file: {project_path}"
        )

    reopen_command = [str(editor), "-i", str(project_path)]
    reopen_run = _run_editor_command(reopen_command, cwd=package)
    if reopen_run.returncode != 0:
        report = _editor_report(
            editor,
            target,
            import_command,
            import_run,
            project_path,
            import_json,
            images_root,
            atlas_reports,
            warnings,
            reopen_command=reopen_command,
            reopen_run=reopen_run,
        )
        _write_report(report_path, report)
        diagnostic = reopen_run.stderr.strip() or reopen_run.stdout.strip() or "no diagnostic output"
        raise SpineEditorProcessError(
            f"Spine editor could not reopen generated project {project_path}: {diagnostic}"
        )
    _validate_reopen_output(reopen_run.stdout, target, skeleton_name, project_path)
    report = _editor_report(
        editor,
        target,
        import_command,
        import_run,
        project_path,
        import_json,
        images_root,
        atlas_reports,
        warnings,
        reopen_command=reopen_command,
        reopen_run=reopen_run,
    )
    _write_report(report_path, report)
    return SpineEditorProjectResult(
        project_path=project_path,
        import_json_path=import_json,
        images_dir=images_root,
        report_path=report_path,
        warnings=tuple(warnings),
    )


def _rewrite_skeleton_for_images(
    source_data: Mapping[str, Any],
    extracted_by_name: Mapping[str, Sequence[_ExtractedRegion]],
    warnings: list[str],
) -> dict[str, Any]:
    """Rewrite image references while preserving skeleton data and animation."""

    data = json.loads(json.dumps(source_data, ensure_ascii=False))
    metadata = data.get("skeleton")
    if not isinstance(metadata, dict):
        raise SpineEditorInputError("Converted skeleton JSON has invalid skeleton metadata.")
    metadata["images"] = "./images/"
    unresolved: list[str] = []
    mapped = 0
    for skin in data.get("skins", []) or []:
        if not isinstance(skin, dict):
            continue
        attachments = skin.get("attachments")
        if not isinstance(attachments, dict):
            continue
        for slot_name, slot_attachments in attachments.items():
            if not isinstance(slot_attachments, dict):
                continue
            for attachment_name, attachment in slot_attachments.items():
                if not isinstance(attachment, dict):
                    continue
                attachment_type = str(attachment.get("type", "region")).casefold()
                if attachment_type not in {"region", "mesh", "linkedmesh"}:
                    continue
                raw_name = attachment.get("path") or attachment_name
                lookup_name = str(raw_name).replace("\\", "/").strip()
                candidates = list(extracted_by_name.get(lookup_name, ()))
                if not candidates:
                    unresolved.append(f"{slot_name}/{attachment_name} -> {lookup_name}")
                    continue
                selected = candidates[0]
                if len(candidates) > 1:
                    signatures = {
                        (
                            item.region.page_index,
                            item.region.width,
                            item.region.height,
                            item.region.rotation,
                            item.region.original_size,
                            item.region.offset_x,
                            item.region.offset_top,
                        )
                        for item in candidates
                    }
                    if len(signatures) > 1:
                        warnings.append(
                            f"Atlas name {lookup_name!r} occurs in multiple pages; the first atlas occurrence "
                            "was selected, matching Spine's atlas search order."
                        )
                    else:
                        warnings.append(f"Atlas name {lookup_name!r} occurs more than once; one shared image was selected.")
                # Spine image paths are extensionless; the editor resolves
                # ``path`` against skeleton.images and appends the image
                # extension while scanning the directory.
                attachment["path"] = _attachment_image_path(selected.relative_image_path)
                if "uvs" in attachment:
                    _validate_mesh_uvs(attachment, f"{slot_name}/{attachment_name}")
                mapped += 1
    if unresolved:
        sample = "; ".join(unresolved[:8])
        more = "" if len(unresolved) <= 8 else f"; and {len(unresolved) - 8} more"
        raise SpineEditorInputError(
            "Atlas does not contain images for editor attachments: " + sample + more
        )
    if mapped == 0:
        warnings.append("Converted skeleton has no visual image attachments; editor project contains bones and animations only.")
    return data


def _validate_mesh_uvs(attachment: Mapping[str, Any], label: str) -> None:
    values = attachment.get("uvs")
    if not isinstance(values, list) or len(values) % 2:
        raise SpineEditorInputError(f"Mesh attachment {label} has malformed UV data.")
    for index in range(0, len(values), 2):
        try:
            u = float(values[index])
            v = float(values[index + 1])
        except (TypeError, ValueError) as exc:
            raise SpineEditorInputError(f"Mesh attachment {label} has non-numeric UV data.") from exc
        if not math.isfinite(u) or not math.isfinite(v):
            raise SpineEditorInputError(f"Mesh attachment {label} has non-finite UV data.")
    # Spine JSON stores ``regionUVs``: normalized coordinates in the original
    # image.  The official 3.8 runtime applies atlas trim/rotation in
    # MeshAttachment.updateUVs.  We extract the original-size, unrotated image,
    # so preserving these values is the required inverse of that atlas step.


def _attachment_image_path(relative_image_path: str) -> str:
    """Return a forward-slash, extensionless path for a Spine attachment."""

    path = str(relative_image_path).replace("\\", "/")
    if path.casefold().endswith(".png"):
        path = path[:-4]
    return path


def _run_editor_command(command: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise SpineEditorProcessError(
            f"Spine editor CLI timed out after 120 seconds while running {command[0]!r}."
        ) from exc
    except OSError as exc:
        raise SpineEditorProcessError(f"Unable to launch Spine editor CLI {command[0]!r}: {exc}") from exc


def _validate_reopen_output(stdout: str, target: str, skeleton_name: str, project_path: Path) -> None:
    text = str(stdout or "")
    if f"Spine version: {target}" not in text:
        raise SpineEditorProcessError(
            f"Spine editor reopened {project_path} but reported no matching Spine version {target}."
        )
    if f"Skeleton: {skeleton_name}" not in text:
        raise SpineEditorProcessError(
            f"Spine editor reopened {project_path} but reported no skeleton named {skeleton_name!r}."
        )


def _editor_report(
    editor: Path,
    target: str,
    import_command: Sequence[str],
    import_run: subprocess.CompletedProcess[str],
    project_path: Path,
    import_json: Path,
    images_dir: Path,
    atlas_reports: Sequence[Mapping[str, Any]],
    warnings: Sequence[str],
    *,
    reopen_command: Sequence[str] | None = None,
    reopen_run: subprocess.CompletedProcess[str] | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "editor": str(editor),
        "editor_type": "official Spine CLI",
        "target_version": target,
        "project_path": str(project_path),
        "import_json": str(import_json),
        "images_dir": str(images_dir),
        "import_command": list(import_command),
        "import_returncode": int(import_run.returncode),
        "import_stdout": import_run.stdout,
        "import_stderr": import_run.stderr,
        "reopen_command": list(reopen_command or []),
        "reopen_returncode": int(reopen_run.returncode) if reopen_run is not None else None,
        "reopen_stdout": reopen_run.stdout if reopen_run is not None else "",
        "reopen_stderr": reopen_run.stderr if reopen_run is not None else "",
        "atlas_sources": list(atlas_reports),
        "warnings": list(warnings),
        "official_references": {
            "cli_import": OFFICIAL_CLI_IMPORT_URL,
            "json_format": OFFICIAL_JSON_FORMAT_URL,
            "atlas_format": OFFICIAL_ATLAS_FORMAT_URL,
        },
    }
    return report


def _create_package_directory(root: Path) -> Path:
    base = root / EDITOR_PROJECT_DIRECTORY
    candidate = base
    index = 2
    while candidate.exists():
        candidate = root / f"{EDITOR_PROJECT_DIRECTORY}_{index}"
        index += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def _unique_files(values: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        resolved = value.resolve()
        key = str(resolved).casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(resolved)
    return result


def _safe_component(value: str) -> str:
    text = "".join(char if char.isalnum() or char in " ._()-" else "_" for char in str(value or ""))
    return text.strip(" .")[:120]


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


__all__ = [
    "EDITOR_IMAGES_DIRECTORY",
    "EDITOR_PROJECT_DIRECTORY",
    "EDITOR_TARGET_VERSION",
    "SpineEditorError",
    "SpineEditorInputError",
    "SpineEditorNotFoundError",
    "SpineEditorProcessError",
    "SpineEditorProjectResult",
    "create_spine_editor_project",
    "discover_spine_editor",
]
