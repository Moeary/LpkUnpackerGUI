"""Safe Spine texture-atlas extraction and PNG write-back helpers.

This module deliberately handles the texture-atlas interchange format only.  It
does not load a Spine skeleton or implement a Spine runtime.  A parsed atlas
can be exported as editable region PNG files (and, when ``psd-tools`` is
installed, as a PSD) together with JSON metadata.  The metadata is sufficient
to write edited regions back into copies of the original atlas page images;
the atlas text and skeleton files are never repacked or modified.

The parser follows the two atlas spellings documented by Esoteric Software:
the older ``xy``/``size``/``orig``/``offset`` form and the newer
``bounds``/``offsets`` form.  Official references checked for this
implementation:

* https://esotericsoftware.com/spine-atlas-format
* https://esotericsoftware.com/spine-texture-packer

Only right-angle region rotations are accepted because those are lossless
pixel operations.  Page paths and metadata paths are constrained to their
declared relative roots by default, which prevents an atlas from reading or
writing outside its source/output directory.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import warnings as pywarnings
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Mapping, Sequence

from PIL import Image


OFFICIAL_SPINE_ATLAS_FORMAT_URL = "https://esotericsoftware.com/spine-atlas-format"
OFFICIAL_SPINE_TEXTURE_PACKER_URL = "https://esotericsoftware.com/spine-texture-packer"
SPINE_ATLAS_METADATA_FORMAT = "LpkUnpacker.SpineAtlas"
SPINE_ATLAS_METADATA_VERSION = 1
ProgressCallback = Callable[[int, str], None]


class SpineAtlasError(RuntimeError):
    """Base exception for atlas parsing, extraction, and write-back errors."""


class SpineAtlasParseError(SpineAtlasError, ValueError):
    """Raised when an atlas cannot be interpreted safely."""


class SpineAtlasPathError(SpineAtlasError, ValueError):
    """Raised when a page or metadata path escapes its permitted root."""


class SpineAtlasBoundsError(SpineAtlasError, ValueError):
    """Raised when a region or edit does not fit its declared container."""


class SpineAtlasDependencyError(SpineAtlasError):
    """Raised only when optional PSD support was requested without psd-tools."""


@dataclass
class AtlasPage:
    """One page image declaration from a ``.atlas`` file."""

    name: str
    index: int
    size: tuple[int, int] | None = None
    format: str = "RGBA8888"
    filter_min: str | None = None
    filter_mag: str | None = None
    repeat: str = "none"
    pma: bool = False
    scale: float | None = None
    properties: dict[str, str] = field(default_factory=dict)

    @property
    def width(self) -> int | None:
        return self.size[0] if self.size else None

    @property
    def height(self) -> int | None:
        return self.size[1] if self.size else None


@dataclass
class AtlasRegion:
    """A region rectangle and its whitespace-stripping information."""

    name: str
    page_index: int
    x: int
    y: int
    width: int
    height: int
    rotation: int = 0
    orig_width: int | None = None
    orig_height: int | None = None
    offset_x: int = 0
    offset_bottom: int = 0
    index: int = -1
    region_id: str = ""
    properties: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.orig_width is None:
            # Atlas ``size``/``bounds`` is expressed in the region's logical
            # orientation.  For a 90-degree region the stored page rectangle
            # is swapped, but ``orig`` still follows the logical width/height
            # (as the official runtimes do before swapping packed dimensions).
            self.orig_width = self.width
        if self.orig_height is None:
            self.orig_height = self.height

    @property
    def packed_size(self) -> tuple[int, int]:
        return self.physical_size

    @property
    def packed_width(self) -> int:
        return self.physical_size[0]

    @property
    def packed_height(self) -> int:
        return self.physical_size[1]

    @property
    def original_width(self) -> int:
        return int(self.orig_width or 0)

    @property
    def original_height(self) -> int:
        return int(self.orig_height or 0)

    @property
    def offset_y(self) -> int:
        """Runtime-compatible alias for Spine's bottom offset field."""

        return self.offset_bottom

    @property
    def degrees(self) -> int:
        return self.rotation

    @property
    def rotate(self) -> bool:
        return self.rotation != 0

    @property
    def logical_size(self) -> tuple[int, int]:
        """The logical (unrotated) ``size``/``bounds`` tuple from the atlas."""

        return self.width, self.height

    @property
    def physical_size(self) -> tuple[int, int]:
        """The rectangle width/height occupied by the region on the page."""

        if self.rotation in (90, 270):
            return self.height, self.width
        return self.width, self.height

    @property
    def original_size(self) -> tuple[int, int]:
        return int(self.orig_width or 0), int(self.orig_height or 0)

    @property
    def offset_top(self) -> int:
        """Whitespace removed from the original image's top edge.

        Spine stores the bottom offset in atlas coordinates.  Images exported
        by Pillow use a top-left origin, so the top offset is derived from the
        original and packed dimensions.
        """

        return int(self.orig_height or 0) - self.offset_bottom - self._unrotated_height

    @property
    def offset_right(self) -> int:
        return int(self.orig_width or 0) - self.offset_x - self.width

    @property
    def _unrotated_width(self) -> int:
        return self.width

    @property
    def _unrotated_height(self) -> int:
        return self.height


@dataclass
class SpineAtlas:
    """Parsed atlas data, including pages, regions, and non-fatal warnings."""

    pages: list[AtlasPage]
    regions: list[AtlasRegion]
    atlas_path: Path | None = None
    warnings: list[str] = field(default_factory=list)

    def region(self, region_id: str) -> AtlasRegion:
        for item in self.regions:
            if item.region_id == region_id:
                return item
        raise KeyError(region_id)

    def regions_for_page(self, page_index: int) -> list[AtlasRegion]:
        return [item for item in self.regions if item.page_index == page_index]

    @property
    def regions_by_id(self) -> dict[str, AtlasRegion]:
        return {item.region_id: item for item in self.regions}


@dataclass
class AtlasEditLayer:
    """An edit applied to an extracted region before it is written back.

    ``replace`` copies every RGBA pixel, including transparent pixels, and
    therefore supports erasing by simply supplying a transparent PNG.  The
    explicit ``erase`` mode clears pixels where the supplied image has alpha;
    ``overlay`` alpha-composites the supplied image.  ``left`` and ``top`` are
    coordinates inside the region's original (untrimmed) image.
    """

    region_id: str
    image: Image.Image | str | Path
    mode: str = "replace"
    left: int = 0
    top: int = 0
    opacity: float = 1.0


SpineAtlasEditLayer = AtlasEditLayer


@dataclass
class SpineAtlasExportResult:
    atlas: SpineAtlas
    output_dir: Path
    metadata_path: Path
    region_paths: dict[str, Path]
    psd_path: Path | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def metadata(self) -> Path:
        """Compatibility alias for callers that use ``metadata``."""

        return self.metadata_path


@dataclass
class SpineAtlasWritebackResult:
    output_dir: Path
    page_paths: dict[int, Path]
    changed_regions: list[str]
    warnings: list[str] = field(default_factory=list)

    @property
    def output_paths(self) -> list[Path]:
        return list(self.page_paths.values())


SpineAtlasExtractionResult = SpineAtlasExportResult
SpineAtlasRepackResult = SpineAtlasWritebackResult


def parse_atlas(source: str | Path, *, atlas_path: str | Path | None = None) -> SpineAtlas:
    """Parse atlas text or a filesystem atlas path.

    A string containing a newline is treated as atlas text.  A string without
    a newline is treated as a path if it exists; otherwise it is parsed as
    text, which keeps this helper convenient for tests and callers holding
    in-memory atlas data.
    """

    path: Path | None = None
    if isinstance(source, Path):
        path = source.resolve()
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise SpineAtlasParseError(f"Unable to read atlas file {path}: {exc}") from exc
    else:
        candidate = Path(source)
        if "\n" not in source and "\r" not in source and candidate.is_file():
            path = candidate.resolve()
            try:
                text = path.read_text(encoding="utf-8-sig")
            except OSError as exc:
                raise SpineAtlasParseError(f"Unable to read atlas file {path}: {exc}") from exc
        else:
            text = source
    if atlas_path is not None:
        path = Path(atlas_path).resolve()
    return parse_atlas_text(text, atlas_path=path)


def load_atlas(source: str | Path, **kwargs: Any) -> SpineAtlas:
    """Alias for :func:`parse_atlas`."""

    return parse_atlas(source, **kwargs)


parse_spine_atlas = parse_atlas


def parse_atlas_text(text: str, *, atlas_path: str | Path | None = None) -> SpineAtlas:
    """Parse old and new Spine atlas spellings from a text string."""

    if not isinstance(text, str):
        raise SpineAtlasParseError("Atlas text must be a string.")
    source_path = Path(atlas_path).resolve() if atlas_path is not None else None
    # Keep blank separators because they delimit pages in the documented
    # format.  A trailing blank line is harmless.
    blocks: list[list[str]] = []
    block: list[str] = []
    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.rstrip("\r\n")
        if not line.strip():
            if block:
                blocks.append(block)
                block = []
            continue
        block.append(line)
    if block:
        blocks.append(block)
    if not blocks:
        raise SpineAtlasParseError("Atlas text is empty.")

    pages: list[AtlasPage] = []
    regions: list[AtlasRegion] = []
    parse_warnings: list[str] = []
    for block_index, lines in enumerate(blocks):
        page, block_regions, block_warnings = _parse_atlas_block(lines, block_index)
        pages.append(page)
        regions.extend(block_regions)
        parse_warnings.extend(block_warnings)

    _assign_region_ids(regions)
    atlas = SpineAtlas(pages=pages, regions=regions, atlas_path=source_path, warnings=parse_warnings)
    return atlas


def extract_spine_atlas(
    atlas_source: str | Path | SpineAtlas,
    output_dir: str | Path,
    *,
    write_psd: bool = False,
    metadata_path: str | Path | None = None,
    include_page_copies: bool = False,
    allow_external_paths: bool = False,
    progress: ProgressCallback | None = None,
) -> SpineAtlasExportResult:
    """Extract every atlas region as an untrimmed RGBA PNG and metadata.

    ``atlas_source`` may be a parsed :class:`SpineAtlas` or an atlas file
    path.  Extraction needs a filesystem atlas path to resolve page images;
    in-memory atlas text can therefore be parsed first and assigned
    ``atlas.atlas_path`` by the caller.
    """

    atlas = _coerce_atlas(atlas_source)
    _emit_progress(progress, 0, "Atlas parsed")
    if atlas.atlas_path is None:
        raise SpineAtlasPathError("Atlas extraction needs an atlas file path to resolve page images.")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    regions_dir = output / "regions"
    regions_dir.mkdir(parents=True, exist_ok=True)

    page_images: dict[int, Image.Image] = {}
    page_paths: dict[int, Path] = {}
    result_warnings = list(atlas.warnings)
    for page in atlas.pages:
        page_path = _resolve_relative_path(
            atlas.atlas_path.parent, page.name, allow_external=allow_external_paths, label="atlas page"
        )
        image = _load_page(page_path, page)
        _validate_page_regions(atlas, page, image)
        page_images[page.index] = image
        page_paths[page.index] = page_path
        _emit_progress(progress, min(20, int((page.index + 1) / max(1, len(atlas.pages)) * 20)), f"Loaded page {page.name}")

    region_paths: dict[str, Path] = {}
    metadata_regions: list[dict[str, Any]] = []
    for region in atlas.regions:
        page = atlas.pages[region.page_index]
        image = page_images[page.index]
        packed_width, packed_height = region.physical_size
        packed = image.crop((region.x, region.y, region.x + packed_width, region.y + packed_height))
        if page.pma:
            packed = _unpremultiply(packed)
        unrotated = _rotate_from_page(packed, region.rotation)
        expected_size = (region._unrotated_width, region._unrotated_height)
        if unrotated.size != expected_size:
            raise SpineAtlasBoundsError(
                f"Region {region.region_id or region.name!r} rotation {region.rotation} produced "
                f"{unrotated.size}, expected {expected_size}."
            )
        _validate_region_container(region)
        part = Image.new("RGBA", region.original_size, (0, 0, 0, 0))
        part.paste(unrotated, (region.offset_x, region.offset_top))
        target = regions_dir / f"{region.region_id}.png"
        part.save(target, format="PNG")
        region_paths[region.region_id] = target
        metadata_regions.append(
            _region_metadata(
                region,
                page,
                page_paths[page.index],
                target.relative_to(output).as_posix(),
                part,
                packed,
            )
        )
        _emit_progress(
            progress,
            20 + int((len(metadata_regions) / max(1, len(atlas.regions))) * 55),
            f"Extracted {region.region_id}",
        )

    metadata = _build_metadata(atlas, page_paths, metadata_regions, output)
    metadata_file = Path(metadata_path).resolve() if metadata_path else output / "spine_atlas.json"
    _ensure_output_path(metadata_file, output, allow_external=allow_external_paths, label="metadata")
    _write_json(metadata_file, metadata)
    _emit_progress(progress, 80, "Metadata written")

    psd_path: Path | None = None
    if write_psd:
        psd_path = output / "spine_atlas.psd"
        try:
            psd_baseline = _write_psd(psd_path, atlas, region_paths, metadata_regions, progress=progress)
            # A PSD decoder can normalize hidden RGB in transparent pixels and
            # quantize semi-transparent channels.  Record the decoder's own
            # baseline so an untouched PSD is never mistaken for an edit on a
            # later write-back.  The raw atlas page remains the source of truth
            # for pixels that are not actually changed in the PSD.
            metadata["psd_baseline"] = psd_baseline
            _write_json(metadata_file, metadata)
        except SpineAtlasDependencyError:
            raise
        except Exception as exc:
            raise SpineAtlasError(f"Failed to write Spine atlas PSD: {exc}") from exc

    if include_page_copies:
        pages_dir = output / "pages"
        for page in atlas.pages:
            destination = _safe_join(pages_dir, page.name, label="page copy")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(page_paths[page.index], destination)

    return SpineAtlasExportResult(
        atlas=atlas,
        output_dir=output,
        metadata_path=metadata_file,
        region_paths=region_paths,
        psd_path=psd_path,
        warnings=result_warnings,
    )


def extract_atlas(*args: Any, **kwargs: Any) -> SpineAtlasExportResult:
    """Short alias for :func:`extract_spine_atlas`."""

    return extract_spine_atlas(*args, **kwargs)


unpack_atlas = extract_spine_atlas
export_spine_atlas = extract_spine_atlas
extract_atlas_regions = extract_spine_atlas
export_spine_atlas_parts = extract_spine_atlas


def writeback_spine_atlas(
    metadata_or_atlas: str | Path | Mapping[str, Any] | SpineAtlas,
    edited_layers: Mapping[str, Any]
    | Sequence[AtlasEditLayer]
    | str
    | Path
    | None = None,
    output_dir: str | Path | None = None,
    *,
    atlas_path: str | Path | None = None,
    preserve_unmodified: bool = True,
    allow_external_paths: bool = False,
    progress: ProgressCallback | None = None,
) -> SpineAtlasWritebackResult:
    """Write edited regions into copies of atlas page images.

    ``edited_layers`` can be a region-id mapping, a sequence of
    :class:`AtlasEditLayer`, a directory containing ``regions/<id>.png`` (or
    ``<id>.png``), or a PSD produced by :func:`extract_spine_atlas`.  A plain
    image value in a mapping means a full-region ``replace`` edit.
    """

    if output_dir is None:
        raise SpineAtlasPathError("writeback_spine_atlas requires an output_dir; source pages are never overwritten implicitly.")
    metadata, metadata_file = _coerce_metadata(metadata_or_atlas)
    _emit_progress(progress, 0, "Metadata loaded")
    base_dir = metadata_file.parent if metadata_file else None
    source_atlas = _resolve_atlas_from_metadata(metadata, base_dir, atlas_path)
    atlas = parse_atlas(source_atlas)
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    _validate_metadata_matches_atlas(metadata, atlas)

    page_images: dict[int, Image.Image] = {}
    page_paths: dict[int, Path] = {}
    for page in atlas.pages:
        source_page = _resolve_relative_path(
            source_atlas.parent, page.name, allow_external=allow_external_paths, label="atlas page"
        )
        page_paths[page.index] = source_page
        page_images[page.index] = _load_page(source_page, page)
        _validate_page_regions(atlas, page, page_images[page.index])

    edits = _normalise_edits(edited_layers, metadata, base_dir, atlas)
    unknown_edit_ids = sorted(set(edits) - {region.region_id for region in atlas.regions})
    if unknown_edit_ids:
        raise SpineAtlasParseError(
            f"Edited layers refer to unknown region IDs: {', '.join(unknown_edit_ids)}"
        )
    changed_regions: list[str] = []
    result_warnings = list(atlas.warnings)
    for region in atlas.regions:
        region_edits = edits.get(region.region_id, [])
        if not region_edits:
            continue
        page = atlas.pages[region.page_index]
        source = page_images[page.index]
        packed_width, packed_height = region.physical_size
        packed_original = source.crop((region.x, region.y, region.x + packed_width, region.y + packed_height))
        packed_source = _unpremultiply(packed_original) if page.pma else packed_original.copy()
        baseline = Image.new("RGBA", region.original_size, (0, 0, 0, 0))
        unrotated = _rotate_from_page(packed_source, region.rotation)
        baseline.paste(unrotated, (region.offset_x, region.offset_top))
        edited = baseline.copy()
        for edit in region_edits:
            _apply_edit(edited, edit, region)
        if edited.tobytes() == baseline.tobytes():
            continue
        _validate_region_container(region)
        crop = edited.crop(
            (
                region.offset_x,
                region.offset_top,
                region.offset_x + region._unrotated_width,
                region.offset_top + region._unrotated_height,
            )
        )
        packed_straight = _rotate_to_page(crop, region.rotation)
        if page.pma:
            packed = _premultiply(packed_straight)
        else:
            packed = packed_straight
        # Preserve bytes for packed pixels that the user did not change.  It
        # matters for PMA pages, where integer unpremultiply/premultiply is
        # not mathematically reversible for every possible channel value, and
        # for transparent pixels that may intentionally carry hidden RGB.
        packed_reference = packed_source
        raw_pixels = list(packed_original.getdata())
        new_pixels = list(packed_straight.getdata())
        reference_pixels = list(packed_reference.getdata())
        packed_pixels = list(packed.getdata())
        for pixel_index, (new_pixel, reference_pixel) in enumerate(zip(new_pixels, reference_pixels)):
            if new_pixel == reference_pixel:
                packed_pixels[pixel_index] = raw_pixels[pixel_index]
        packed.putdata(packed_pixels)
        if packed.size != region.physical_size:
            raise SpineAtlasBoundsError(
                f"Edited region {region.region_id!r} produced packed size {packed.size}; "
                f"atlas container is {region.physical_size}."
            )
        source.paste(packed, (region.x, region.y))
        changed_regions.append(region.region_id)
        _emit_progress(
            progress,
            10 + int((len(changed_regions) / max(1, len(edits))) * 80),
            f"Packed {region.region_id}",
        )

    page_outputs: dict[int, Path] = {}
    for page in atlas.pages:
        destination = _safe_join(output, page.name, label="output page")
        if destination.resolve() == page_paths[page.index].resolve():
            raise SpineAtlasPathError(
                f"Output page would overwrite the source atlas page {page_paths[page.index]}; choose a separate output_dir."
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        if preserve_unmodified and page.index not in {atlas.region(r).page_index for r in changed_regions}:
            shutil.copy2(page_paths[page.index], destination)
        else:
            _save_page(destination, page_images[page.index])
        page_outputs[page.index] = destination
        _emit_progress(progress, 90 + int((page.index + 1) / max(1, len(atlas.pages)) * 10), f"Wrote page {page.name}")

    return SpineAtlasWritebackResult(
        output_dir=output,
        page_paths=page_outputs,
        changed_regions=changed_regions,
        warnings=result_warnings,
    )


def repack_atlas(*args: Any, **kwargs: Any) -> SpineAtlasWritebackResult:
    """Compatibility alias for :func:`writeback_spine_atlas`."""

    return writeback_spine_atlas(*args, **kwargs)


def repack_spine_atlas(*args: Any, **kwargs: Any) -> SpineAtlasWritebackResult:
    """Compatibility alias for :func:`writeback_spine_atlas`."""

    return writeback_spine_atlas(*args, **kwargs)


writeback_atlas = writeback_spine_atlas
reassemble_atlas = writeback_spine_atlas
writeback_atlas_pages = writeback_spine_atlas


class SpineAtlasPage:
    """Small orchestration facade suitable for a GUI page or CLI caller."""

    def parse(self, atlas_source: str | Path) -> SpineAtlas:
        return parse_atlas(atlas_source)

    def extract(self, atlas_source: str | Path | SpineAtlas, output_dir: str | Path, **kwargs: Any) -> SpineAtlasExportResult:
        return extract_spine_atlas(atlas_source, output_dir, **kwargs)

    def writeback(self, metadata: str | Path | Mapping[str, Any], edits: Any, output_dir: str | Path, **kwargs: Any) -> SpineAtlasWritebackResult:
        return writeback_spine_atlas(metadata, edits, output_dir, **kwargs)


class SpineAtlasExtractor(SpineAtlasPage):
    """Backward-compatible class name for code that expects an extractor."""


class SpineAtlasRepacker(SpineAtlasPage):
    """Backward-compatible class name for code that expects a repacker."""


def _parse_atlas_block(lines: list[str], page_index: int) -> tuple[AtlasPage, list[AtlasRegion], list[str]]:
    if not lines or not lines[0].strip():
        raise SpineAtlasParseError(f"Atlas page block {page_index} is empty.")
    page_name = lines[0].strip()
    page_values: dict[str, str] = {}
    cursor = 1
    while cursor < len(lines) and _is_property_line(lines[cursor]):
        key, value = _split_property(lines[cursor])
        page_values[key] = value
        cursor += 1
    page = AtlasPage(
        name=page_name,
        index=page_index,
        size=_parse_size(page_values.get("size"), f"page {page_name} size") if page_values.get("size") else None,
        format=page_values.get("format", "RGBA8888"),
        filter_min=_parse_filter(page_values.get("filter"))[0] if page_values.get("filter") else None,
        filter_mag=_parse_filter(page_values.get("filter"))[1] if page_values.get("filter") else None,
        repeat=page_values.get("repeat", "none"),
        pma=_parse_bool(page_values.get("pma", "false"), f"page {page_name} pma"),
        scale=_parse_float(page_values.get("scale"), f"page {page_name} scale") if page_values.get("scale") else None,
        properties=page_values,
    )
    regions: list[AtlasRegion] = []
    parse_warnings: list[str] = []
    while cursor < len(lines):
        if _is_property_line(lines[cursor]):
            raise SpineAtlasParseError(
                f"Unexpected page property {lines[cursor].strip()!r} after region data in page {page_name!r}."
            )
        name = lines[cursor].strip()
        cursor += 1
        values: dict[str, str] = {}
        while cursor < len(lines) and _is_property_line(lines[cursor]):
            key, value = _split_property(lines[cursor])
            values[key] = value
            cursor += 1
        region = _parse_region(name, page_index, values)
        regions.append(region)
        if any(key in values for key in ("polygon", "vertices", "triangles")) and not any(
            key in values for key in ("polygon_context", "uvs", "weights")
        ):
            parse_warnings.append(
                f"Region {name!r} has polygon-related data without polygon context; falling back to rectangular bounds."
            )
    return page, regions, parse_warnings


def _parse_region(name: str, page_index: int, values: dict[str, str]) -> AtlasRegion:
    if "bounds" in values:
        x, y, width, height = _parse_ints(values["bounds"], 4, f"region {name} bounds")
    else:
        if "xy" not in values or "size" not in values:
            raise SpineAtlasParseError(
                f"Region {name!r} must provide bounds or both xy and size properties."
            )
        x, y = _parse_ints(values["xy"], 2, f"region {name} xy")
        width, height = _parse_ints(values["size"], 2, f"region {name} size")
    rotation = _parse_rotation(values.get("rotate", "0"), f"region {name} rotate")
    if "offsets" in values:
        offset_x, offset_bottom, orig_width, orig_height = _parse_ints(
            values["offsets"], 4, f"region {name} offsets"
        )
    else:
        orig_width, orig_height = (None, None)
        if "orig" in values:
            orig_width, orig_height = _parse_ints(values["orig"], 2, f"region {name} orig")
        offset_x, offset_bottom = (0, 0)
        if "offset" in values:
            offset_x, offset_bottom = _parse_ints(values["offset"], 2, f"region {name} offset")
    if width <= 0 or height <= 0:
        raise SpineAtlasParseError(f"Region {name!r} has invalid packed size {width}x{height}.")
    if x < 0 or y < 0:
        raise SpineAtlasParseError(f"Region {name!r} has a negative atlas coordinate ({x}, {y}).")
    if offset_x < 0 or offset_bottom < 0:
        raise SpineAtlasParseError(f"Region {name!r} has negative trim offsets.")
    if orig_width is not None and orig_width <= 0 or orig_height is not None and orig_height <= 0:
        raise SpineAtlasParseError(f"Region {name!r} has invalid original size.")
    return AtlasRegion(
        name=name,
        page_index=page_index,
        x=x,
        y=y,
        width=width,
        height=height,
        rotation=rotation,
        orig_width=orig_width,
        orig_height=orig_height,
        offset_x=offset_x,
        offset_bottom=offset_bottom,
        index=_parse_int(values.get("index", "-1"), f"region {name} index"),
        properties=values,
    )


def _assign_region_ids(regions: list[AtlasRegion]) -> None:
    counts: dict[tuple[int, str, int], int] = {}
    used: set[str] = set()
    for region in regions:
        key = (region.page_index, region.name, region.index)
        counts[key] = counts.get(key, 0) + 1
        base = f"p{region.page_index:02d}_{_safe_component(region.name)}_i{region.index}"
        occurrence = counts[key]
        candidate = base if occurrence == 1 else f"{base}_r{occurrence}"
        if candidate in used:
            digest = hashlib.sha1(region.name.encode("utf-8")).hexdigest()[:8]
            candidate = f"{candidate}_h{digest}"
        used.add(candidate)
        region.region_id = candidate


def _region_metadata(
    region: AtlasRegion,
    page: AtlasPage,
    page_path: Path,
    relative_output: str,
    part: Image.Image,
    packed: Image.Image,
) -> dict[str, Any]:
    return {
        "region_id": region.region_id,
        "name": region.name,
        "index": region.index,
        "page_index": region.page_index,
        "page_name": page.name,
        "page_relative_path": page.name.replace("\\", "/"),
        "x": region.x,
        "y": region.y,
        "packed_width": region.physical_size[0],
        "packed_height": region.physical_size[1],
        "logical_width": region.width,
        "logical_height": region.height,
        "rotate": region.rotation,
        "orig_width": int(region.orig_width or 0),
        "orig_height": int(region.orig_height or 0),
        "offset_x": region.offset_x,
        "offset_bottom": region.offset_bottom,
        "offset_top": region.offset_top,
        "offset_right": region.offset_right,
        "pma": page.pma,
        "part_path": relative_output.replace("\\", "/"),
        "part_sha256": _image_sha256(part),
        "packed_sha256": _image_sha256(packed),
        "properties": dict(region.properties),
    }


def _build_metadata(
    atlas: SpineAtlas,
    page_paths: Mapping[int, Path],
    regions: list[dict[str, Any]],
    output: Path,
) -> dict[str, Any]:
    _assign_psd_layer_ids(regions)
    source_path = atlas.atlas_path.resolve() if atlas.atlas_path else None
    pages = []
    for page in atlas.pages:
        source = page_paths[page.index]
        pages.append(
            {
                "index": page.index,
                "name": page.name,
                "relative_path": page.name.replace("\\", "/"),
                "source_path": str(source),
                "width": page.width,
                "height": page.height,
                "format": page.format,
                "filter_min": page.filter_min,
                "filter_mag": page.filter_mag,
                "repeat": page.repeat,
                "pma": page.pma,
                "scale": page.scale,
            }
        )
    return {
        "format": SPINE_ATLAS_METADATA_FORMAT,
        "version": SPINE_ATLAS_METADATA_VERSION,
        "mode": "spine-atlas-regions",
        "atlas_path": str(source_path) if source_path else "",
        "atlas_name": source_path.name if source_path else "",
        "output_directory": str(output),
        "parts_directory": "regions",
        "pages": pages,
        "regions": regions,
        "warnings": list(atlas.warnings),
        "official_sources": [OFFICIAL_SPINE_ATLAS_FORMAT_URL, OFFICIAL_SPINE_TEXTURE_PACKER_URL],
    }


def _assign_psd_layer_ids(regions: Sequence[dict[str, Any]]) -> None:
    """Assign deterministic PSD ``lyid`` values used for rename-safe binding."""

    used: set[int] = set()
    for item in regions:
        region_id = str(item.get("region_id", ""))
        digest = int(hashlib.sha1(region_id.encode("utf-8")).hexdigest()[:8], 16) & 0x7FFFFFFF
        layer_id = digest or 1
        while layer_id in used:
            layer_id = (layer_id + 1) & 0x7FFFFFFF
            if layer_id == 0:
                layer_id = 1
        used.add(layer_id)
        item["psd_layer_id"] = layer_id


def _coerce_atlas(source: str | Path | SpineAtlas) -> SpineAtlas:
    if isinstance(source, SpineAtlas):
        return source
    return parse_atlas(source)


def _coerce_metadata(
    source: str | Path | Mapping[str, Any] | SpineAtlas,
) -> tuple[dict[str, Any], Path | None]:
    if isinstance(source, SpineAtlas):
        if source.atlas_path is None:
            raise SpineAtlasPathError("A SpineAtlas without atlas_path cannot be written back.")
        # Build the small metadata shape required by the write-back path.
        pages = []
        for page in source.pages:
            pages.append({"index": page.index, "name": page.name, "relative_path": page.name, "pma": page.pma})
        regions = []
        for region in source.regions:
            regions.append(
                {
                    "region_id": region.region_id,
                    "name": region.name,
                    "index": region.index,
                    "page_index": region.page_index,
                    "page_name": source.pages[region.page_index].name,
                    "page_relative_path": source.pages[region.page_index].name,
                    "x": region.x,
                    "y": region.y,
                    "packed_width": region.physical_size[0],
                    "packed_height": region.physical_size[1],
                    "logical_width": region.width,
                    "logical_height": region.height,
                    "rotate": region.rotation,
                    "orig_width": region.orig_width,
                    "orig_height": region.orig_height,
                    "offset_x": region.offset_x,
                    "offset_bottom": region.offset_bottom,
                    "pma": source.pages[region.page_index].pma,
                }
            )
        return {"format": SPINE_ATLAS_METADATA_FORMAT, "version": 1, "atlas_path": str(source.atlas_path), "pages": pages, "regions": regions}, None
    if isinstance(source, Mapping):
        return dict(source), None
    path = Path(source).resolve()
    if path.suffix.lower() == ".atlas":
        atlas = parse_atlas(path)
        return _coerce_metadata(atlas)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise SpineAtlasParseError(f"Unable to read Spine atlas metadata {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SpineAtlasParseError(f"Spine atlas metadata root must be an object: {path}")
    return data, path


def _resolve_atlas_from_metadata(
    metadata: Mapping[str, Any], base_dir: Path | None, explicit: str | Path | None
) -> Path:
    if explicit is not None:
        path = Path(explicit)
        if not path.is_absolute() and base_dir:
            path = base_dir / path
        return path.resolve()
    raw = metadata.get("atlas_path") or metadata.get("source_atlas")
    if not raw:
        raise SpineAtlasPathError("Spine atlas metadata does not contain atlas_path; pass atlas_path explicitly.")
    path = Path(str(raw))
    if not path.is_absolute() and base_dir:
        path = base_dir / path
    path = path.resolve()
    if not path.is_file():
        raise SpineAtlasPathError(f"Spine atlas file does not exist: {path}")
    return path


def _validate_metadata_matches_atlas(metadata: Mapping[str, Any], atlas: SpineAtlas) -> None:
    if metadata.get("format") not in (None, SPINE_ATLAS_METADATA_FORMAT):
        raise SpineAtlasParseError(f"Unsupported Spine atlas metadata format: {metadata.get('format')!r}")
    entries = metadata.get("regions")
    if entries is not None and not isinstance(entries, list):
        raise SpineAtlasParseError("Spine atlas metadata regions must be a list.")
    actual_ids = {region.region_id for region in atlas.regions}
    if isinstance(entries, list):
        metadata_ids = {str(item.get("region_id")) for item in entries if isinstance(item, Mapping)}
        missing = actual_ids - metadata_ids
        if missing:
            raise SpineAtlasParseError(
                f"Spine atlas metadata is missing region IDs: {', '.join(sorted(missing))}"
            )


def _normalise_edits(
    source: Mapping[str, Any] | Sequence[AtlasEditLayer] | str | Path | None,
    metadata: Mapping[str, Any],
    metadata_dir: Path | None,
    atlas: SpineAtlas,
) -> dict[str, list[AtlasEditLayer]]:
    result: dict[str, list[AtlasEditLayer]] = {}
    if source is None:
        return result
    if isinstance(source, (str, Path)):
        path = Path(source).resolve()
        if path.is_dir():
            parts_dir = _safe_join(
                path,
                str(metadata.get("parts_directory") or "regions"),
                label="parts directory",
            )
            for region in atlas.regions:
                candidates = [parts_dir / f"{region.region_id}.png", path / f"{region.region_id}.png"]
                for candidate in candidates:
                    if candidate.is_file():
                        result.setdefault(region.region_id, []).append(AtlasEditLayer(region.region_id, candidate))
                        break
            for region in atlas.regions:
                for mode, suffix in (("overlay", "__overlay"), ("erase", "__erase")):
                    candidate = parts_dir / f"{region.region_id}{suffix}.png"
                    if candidate.is_file():
                        result.setdefault(region.region_id, []).append(AtlasEditLayer(region.region_id, candidate, mode=mode))
            return result
        if path.suffix.lower() == ".psd":
            return _read_psd_edits(path, atlas, metadata, metadata_dir)
        raise SpineAtlasPathError(f"Edited layer source is neither a directory nor a PSD: {path}")
    if isinstance(source, Mapping):
        for key, value in source.items():
            region_id = str(key)
            if isinstance(value, AtlasEditLayer):
                result.setdefault(region_id, []).append(value)
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, Image.Image)):
                for item in value:
                    if not isinstance(item, AtlasEditLayer):
                        raise SpineAtlasParseError(f"Edit list for {region_id!r} contains a non-AtlasEditLayer item.")
                    result.setdefault(region_id, []).append(item)
            else:
                result.setdefault(region_id, []).append(AtlasEditLayer(region_id, value))
        return result
    for item in source:
        if not isinstance(item, AtlasEditLayer):
            raise SpineAtlasParseError("edited_layers sequences must contain AtlasEditLayer values.")
        result.setdefault(item.region_id, []).append(item)
    known_ids = {region.region_id for region in atlas.regions}
    unknown = sorted(set(result) - known_ids)
    if unknown:
        raise SpineAtlasParseError(f"Edited layers refer to unknown region IDs: {', '.join(unknown)}")
    return result


def _read_psd_edits(
    path: Path,
    atlas: SpineAtlas,
    metadata: Mapping[str, Any] | None = None,
    metadata_dir: Path | None = None,
) -> dict[str, list[AtlasEditLayer]]:
    try:
        from psd_tools import PSDImage
    except Exception as exc:
        raise SpineAtlasDependencyError("PSD write-back requires psd-tools. Install project dependencies first.") from exc
    psd = PSDImage.open(str(path))
    by_id = {region.region_id: region for region in atlas.regions}
    result: dict[str, list[AtlasEditLayer]] = {}
    raw_baseline = (metadata or {}).get("psd_baseline")
    baseline: dict[str, str] = {}
    baseline_images: dict[str, Image.Image] = {}
    strict_baseline = raw_baseline is not None
    if not strict_baseline:
        pywarnings.warn(
            "PSD metadata has no psd_baseline; using legacy whole-layer comparison. "
            "Re-export the PSD to enable pixel-accurate edit detection.",
            RuntimeWarning,
            stacklevel=2,
        )
    else:
        if not isinstance(raw_baseline, Mapping):
            raise SpineAtlasParseError("PSD metadata psd_baseline must be an object.")
        known_region_ids = {region.region_id for region in atlas.regions}
        missing_entries = sorted(known_region_ids - {str(key) for key in raw_baseline})
        if missing_entries:
            raise SpineAtlasParseError(
                "PSD metadata baseline is missing region IDs: "
                + ", ".join(missing_entries)
            )
        if metadata_dir is None:
            raise SpineAtlasPathError(
                "PSD metadata declares psd_baseline paths but has no metadata directory. "
                "Pass the metadata JSON path so baseline files can be validated."
            )
        for key, value in raw_baseline.items():
            region_id = str(key)
            if not isinstance(value, Mapping):
                raise SpineAtlasParseError(
                    f"PSD baseline entry {region_id!r} must contain sha256, size, and path."
                )
            expected_hash = str(value.get("sha256") or "")
            baseline_path = value.get("path")
            if not expected_hash or not baseline_path:
                raise SpineAtlasParseError(
                    f"PSD baseline entry {region_id!r} is missing sha256 or path."
                )
            # _safe_join intentionally raises on absolute/traversal paths;
            # silently falling back would defeat the baseline guarantee.
            baseline_file = _safe_join(metadata_dir, str(baseline_path), label="PSD baseline")
            if not baseline_file.is_file():
                raise SpineAtlasPathError(
                    f"PSD baseline image for {region_id!r} does not exist: {baseline_file}"
                )
            image = _open_rgba(baseline_file)
            raw_size = value.get("size")
            if (
                not isinstance(raw_size, Sequence)
                or isinstance(raw_size, (str, bytes))
                or len(raw_size) != 2
            ):
                raise SpineAtlasParseError(
                    f"PSD baseline entry {region_id!r} has invalid size metadata."
                )
            try:
                expected_size = (int(raw_size[0]), int(raw_size[1]))
            except (TypeError, ValueError) as exc:
                raise SpineAtlasParseError(
                    f"PSD baseline entry {region_id!r} has invalid size metadata."
                ) from exc
            if image.size != expected_size:
                raise SpineAtlasBoundsError(
                    f"PSD baseline image for {region_id!r} has size {image.size}, "
                    f"metadata declares {expected_size}."
                )
            actual_hash = _image_sha256(image)
            if actual_hash != expected_hash:
                raise SpineAtlasParseError(
                    f"PSD baseline image for {region_id!r} failed SHA256 validation."
                )
            baseline[region_id] = expected_hash
            baseline_images[region_id] = image

    layer_ids: dict[int, str] = {}
    metadata_regions = (metadata or {}).get("regions", [])
    if isinstance(metadata_regions, Sequence) and not isinstance(metadata_regions, (str, bytes)):
        for item in metadata_regions:
            if not isinstance(item, Mapping):
                continue
            region_id = str(item.get("region_id", ""))
            raw_layer_id = item.get("psd_layer_id")
            try:
                layer_id = int(raw_layer_id)
            except (TypeError, ValueError):
                continue
            if region_id in by_id:
                layer_ids[layer_id] = region_id

    # Stable IDs are the primary binding.  A unique original region name and
    # a display label containing the ID are accepted as conveniences for PSDs
    # whose author renamed a layer or group; duplicate names remain unbound so
    # two same-name regions can never be edited accidentally.
    aliases: dict[str, set[str]] = {}
    for region in atlas.regions:
        aliases.setdefault(region.name, set()).add(region.region_id)
    metadata_regions = (metadata or {}).get("regions", [])
    if isinstance(metadata_regions, Sequence) and not isinstance(metadata_regions, (str, bytes)):
        for item in metadata_regions:
            if not isinstance(item, Mapping):
                continue
            region_id = str(item.get("region_id", ""))
            display_name = str(item.get("name", ""))
            if region_id in by_id and display_name:
                aliases.setdefault(display_name, set()).add(region_id)

    def resolve_name(value: str) -> str | None:
        candidate = value.strip()
        if candidate in by_id:
            return candidate
        matching = aliases.get(candidate, set())
        if len(matching) == 1:
            return next(iter(matching))
        return None

    def resolve_binding(node: Any) -> tuple[str | None, str]:
        value = str(getattr(node, "name", ""))
        for suffix, mode in (("__overlay", "overlay"), ("__erase", "erase")):
            if value.endswith(suffix):
                return resolve_name(value[: -len(suffix)].rstrip(" _-")), mode
        try:
            layer_id = int(getattr(node, "layer_id", -1))
        except (TypeError, ValueError):
            layer_id = -1
        if layer_id in layer_ids:
            return layer_ids[layer_id], "replace"
        return resolve_name(value), "replace"

    def visible(node: Any) -> bool:
        current = node
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if hasattr(current, "visible") and not bool(current.visible):
                return False
            current = getattr(current, "parent", None)
        return True

    def add_node(node: Any, name: str, mode: str = "replace") -> None:
        if not visible(node):
            return
        region = by_id[name]
        image = _decode_psd_node(node)
        if image is None:
            return
        image = image.convert("RGBA")
        raw_left = int(getattr(node, "left", 0) or 0)
        raw_top = int(getattr(node, "top", 0) or 0)
        # PSD exports place a region layer at its atlas-page origin, while
        # hand-authored edit layers are often positioned directly in the
        # region canvas.  Normalize both conventions before bounds checks.
        left = raw_left - region.x if raw_left >= region.x else raw_left
        top = raw_top - region.y if raw_top >= region.y else raw_top
        _validate_psd_node_bounds(image, left, top, region, name)
        # PixelLayer.topil() reads the stored layer channels directly and
        # preserves hidden RGB.  Groups still use their composited image, but
        # the export-time decoder hash below makes an untouched group compare
        # equal to its own PSD baseline despite renderer normalization.
        expected_hash = baseline.get(name) if mode == "replace" else None
        if expected_hash is not None and _image_sha256(image) == expected_hash:
            return
        baseline_image = baseline_images.get(name) if mode == "replace" else None
        if baseline_image is not None and baseline_image.size == image.size:
            # A group composite may normalize thousands of untouched pixels
            # relative to the raw atlas.  Compare against the saved PSD
            # decoder baseline and return only changed scanline runs, so
            # write-back can preserve the original RGBA bytes everywhere else.
            patches = _psd_pixel_patches(name, image, baseline_image)
            if not patches:
                return
            result.setdefault(name, []).extend(patches)
            return
        local_left = 0
        local_top = 0
        # psd-tools returns a layer-sized image for most pixel layers, but a
        # group composite may be larger (or a caller may provide a full-PSD
        # composite).  Normalize both forms to the region's original canvas so
        # semi-transparent group overlays are decoded as one stable edit.
        if image.size != region.original_size:
            if mode in {"overlay", "erase"} and image.width <= region.orig_width and image.height <= region.orig_height:
                # A sibling overlay layer exported in page coordinates uses
                # the region's page origin.  Keep its local offset so partial
                # erase/overlay edits remain partial on write-back.
                local_left = left
                local_top = top
                if local_left >= 0 and local_top >= 0 and local_left + image.width <= region.orig_width and local_top + image.height <= region.orig_height:
                    result.setdefault(name, []).append(
                        AtlasEditLayer(name, image, mode=mode, left=local_left, top=local_top)
                    )
                    return
            if image.width >= region.orig_width and image.height >= region.orig_height:
                crop_left = max(0, -left)
                crop_top = max(0, -top)
                if crop_left + region.orig_width <= image.width and crop_top + region.orig_height <= image.height:
                    image = image.crop((crop_left, crop_top, crop_left + region.orig_width, crop_top + region.orig_height))
        if image.size != region.original_size:
            raise SpineAtlasBoundsError(
                f"PSD layer {name!r} composites to {image.size}, expected region canvas {region.original_size}."
            )
        result.setdefault(name, []).append(AtlasEditLayer(name, image, mode=mode))

    layers = list(psd.descendants())
    # A region group is the single binding unit.  Its composite includes base,
    # masks, visibility, and any newly added child overlay/erase layers.  Skip
    # descendants of a bound group so the same pixels are never applied twice.
    bound_groups: dict[int, str] = {}
    for layer in layers:
        if not callable(getattr(layer, "descendants", None)):
            continue
        name, mode = resolve_binding(layer)
        if name is not None and mode == "replace":
            bound_groups[id(layer)] = name

    def inside_bound_group(layer: Any) -> bool:
        current = getattr(layer, "parent", None)
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            current_id = id(current)
            if current_id in bound_groups:
                return True
            seen.add(current_id)
            current = getattr(current, "parent", None)
        return False

    for layer in layers:
        if inside_bound_group(layer):
            continue
        name, mode = resolve_binding(layer)
        if name is not None:
            add_node(layer, name, mode)
    return result


def _validate_psd_node_bounds(
    image: Image.Image,
    left: int,
    top: int,
    region: AtlasRegion,
    region_id: str,
) -> None:
    """Reject visible PSD pixels outside one region's original container."""

    alpha_bbox = image.getchannel("A").getbbox()
    if alpha_bbox is None:
        return
    x0, y0, x1, y1 = alpha_bbox
    width, height = region.original_size
    if left + x0 < 0 or top + y0 < 0 or left + x1 > width or top + y1 > height:
        raise SpineAtlasBoundsError(
            f"PSD layer {region_id!r} paints outside region container {region.original_size} "
            f"at ({left}, {top}) with image {image.size}; re-pack the layer inside the region."
        )


def _psd_pixel_patches(
    region_id: str,
    image: Image.Image,
    baseline: Image.Image,
) -> list[AtlasEditLayer]:
    """Return partial internal patches for pixels changed since PSD export."""

    current = image.convert("RGBA")
    original = baseline.convert("RGBA")
    if current.size != original.size:
        return []
    current_bytes = current.tobytes()
    original_bytes = original.tobytes()
    width, height = current.size
    patches: list[AtlasEditLayer] = []
    row_stride = width * 4
    for top in range(height):
        row_start = top * row_stride
        x = 0
        while x < width:
            pixel_start = row_start + x * 4
            if current_bytes[pixel_start : pixel_start + 4] == original_bytes[pixel_start : pixel_start + 4]:
                x += 1
                continue
            run_start = x
            x += 1
            while x < width:
                pixel_start = row_start + x * 4
                if current_bytes[pixel_start : pixel_start + 4] == original_bytes[pixel_start : pixel_start + 4]:
                    break
                x += 1
            patches.append(
                AtlasEditLayer(
                    region_id,
                    current.crop((run_start, top, x, top + 1)),
                    mode="patch",
                    left=run_start,
                    top=top,
                )
            )
    return patches


def _apply_edit(target: Image.Image, edit: AtlasEditLayer, region: AtlasRegion) -> None:
    mode = str(edit.mode or "replace").lower()
    if mode not in {"replace", "overlay", "erase", "patch"}:
        raise SpineAtlasParseError(f"Unsupported edit mode {edit.mode!r} for region {region.region_id!r}.")
    image = _open_rgba(edit.image)
    left, top = int(edit.left), int(edit.top)
    if left < 0 or top < 0 or left + image.width > target.width or top + image.height > target.height:
        raise SpineAtlasBoundsError(
            f"Edit layer for region {region.region_id!r} at ({left}, {top}) with size {image.size} "
            f"exceeds its original container {target.size}."
        )
    if mode == "patch":
        target.paste(image, (left, top))
        return
    if mode == "replace":
        if left != 0 or top != 0 or image.size != target.size:
            raise SpineAtlasBoundsError(
                f"Replace layer for region {region.region_id!r} must cover exactly {target.size}; "
                f"got {image.size} at ({left}, {top}). Use overlay or erase for a partial edit."
            )
        target.paste(image, (0, 0))
        return
    if mode == "overlay":
        if edit.opacity != 1.0:
            image = _scale_alpha(image, edit.opacity)
        target.alpha_composite(image, dest=(left, top))
        return
    # Erase uses the supplied alpha as a mask.  A fully opaque mask clears the
    # destination; partially transparent masks linearly reduce alpha and RGB.
    mask = image.getchannel("A")
    if edit.opacity != 1.0:
        mask = _scale_alpha(Image.merge("RGBA", (mask, mask, mask, mask)), edit.opacity).getchannel("A")
    pixels = list(target.getdata())
    masks = list(mask.getdata())
    width = target.width
    for yy in range(image.height):
        for xx in range(image.width):
            amount = masks[yy * image.width + xx]
            if not amount:
                continue
            pos = (top + yy) * width + left + xx
            if amount >= 255:
                pixels[pos] = (0, 0, 0, 0)
                continue
            old = pixels[pos]
            keep = 255 - amount
            pixels[pos] = tuple((channel * keep + 127) // 255 for channel in old)
    target.putdata(pixels)


def _load_page(path: Path, page: AtlasPage) -> Image.Image:
    if not path.is_file():
        raise SpineAtlasPathError(f"Atlas page image does not exist: {path}")
    try:
        with Image.open(path) as source:
            image = source.convert("RGBA")
    except Exception as exc:
        raise SpineAtlasError(f"Unable to load atlas page {path}: {exc}") from exc
    if page.size and page.size != image.size:
        raise SpineAtlasBoundsError(
            f"Atlas page {page.name!r} declares {page.size} but image is {image.size}."
        )
    if page.size is None:
        page.size = image.size
    return image


def _validate_page_regions(atlas: SpineAtlas, page: AtlasPage, image: Image.Image) -> None:
    for region in atlas.regions_for_page(page.index):
        packed_width, packed_height = region.physical_size
        if region.x + packed_width > image.width or region.y + packed_height > image.height:
            raise SpineAtlasBoundsError(
                f"Region {region.region_id or region.name!r} rectangle "
                f"({region.x}, {region.y}, {packed_width}, {packed_height}) exceeds page "
                f"{page.name!r} size {image.size}."
            )
        _validate_region_container(region)


def _validate_region_container(region: AtlasRegion) -> None:
    original = region.original_size
    if original[0] <= 0 or original[1] <= 0:
        raise SpineAtlasBoundsError(f"Region {region.region_id or region.name!r} has invalid original size {original}.")
    if region.offset_x + region._unrotated_width > original[0] or region.offset_top < 0 or region.offset_top + region._unrotated_height > original[1]:
        raise SpineAtlasBoundsError(
            f"Region {region.region_id or region.name!r} packed content ({region._unrotated_width}x{region._unrotated_height}) "
            f"with offsets ({region.offset_x}, bottom={region.offset_bottom}) does not fit original container {original}."
        )


def _rotate_from_page(image: Image.Image, rotation: int) -> Image.Image:
    if rotation == 0:
        return image
    return image.rotate(-rotation, resample=Image.Resampling.NEAREST, expand=True)


def _rotate_to_page(image: Image.Image, rotation: int) -> Image.Image:
    if rotation == 0:
        return image
    return image.rotate(rotation, resample=Image.Resampling.NEAREST, expand=True)


def _open_rgba(value: Image.Image | str | Path) -> Image.Image:
    if isinstance(value, Image.Image):
        return value.convert("RGBA").copy()
    path = Path(value)
    if not path.is_file():
        raise SpineAtlasPathError(f"Edit image does not exist: {path}")
    try:
        with Image.open(path) as image:
            return image.convert("RGBA")
    except Exception as exc:
        raise SpineAtlasError(f"Unable to load edit image {path}: {exc}") from exc


def _scale_alpha(image: Image.Image, opacity: float) -> Image.Image:
    try:
        value = float(opacity)
    except (TypeError, ValueError) as exc:
        raise SpineAtlasParseError(f"Invalid edit opacity: {opacity!r}") from exc
    if value < 0 or value > 1:
        raise SpineAtlasParseError(f"Edit opacity must be between 0 and 1, got {opacity!r}.")
    if value == 1:
        return image
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A").point(lambda item: max(0, min(255, int(round(item * value)))))
    rgba.putalpha(alpha)
    return rgba


def _unpremultiply(image: Image.Image) -> Image.Image:
    pixels = list(image.convert("RGBA").getdata())
    result = []
    for red, green, blue, alpha in pixels:
        if alpha == 0:
            result.append((0, 0, 0, 0))
            continue
        result.append(
            (
                min(255, (red * 255 + alpha // 2) // alpha),
                min(255, (green * 255 + alpha // 2) // alpha),
                min(255, (blue * 255 + alpha // 2) // alpha),
                alpha,
            )
        )
    output = Image.new("RGBA", image.size)
    output.putdata(result)
    return output


def _premultiply(image: Image.Image) -> Image.Image:
    pixels = list(image.convert("RGBA").getdata())
    result = []
    for red, green, blue, alpha in pixels:
        result.append(
            (
                (red * alpha + 127) // 255,
                (green * alpha + 127) // 255,
                (blue * alpha + 127) // 255,
                alpha,
            )
        )
    output = Image.new("RGBA", image.size)
    output.putdata(result)
    return output


def _resolve_relative_path(root: Path, raw: str, *, allow_external: bool, label: str) -> Path:
    value = str(raw).replace("\\", "/")
    if not value:
        raise SpineAtlasPathError(f"{label} path is empty.")
    if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute() or Path(value).drive:
        if not allow_external:
            raise SpineAtlasPathError(f"{label} path must be relative: {raw!r}")
        return Path(raw).resolve()
    parts = PurePosixPath(value).parts
    if ".." in parts and not allow_external:
        raise SpineAtlasPathError(f"{label} path contains traversal: {raw!r}")
    candidate = (root / Path(*parts)).resolve()
    if not allow_external and not _is_relative_to(candidate, root.resolve()):
        raise SpineAtlasPathError(f"{label} path escapes its root: {raw!r}")
    return candidate


def _safe_join(root: Path, raw: str, *, label: str) -> Path:
    return _resolve_relative_path(root, raw, allow_external=False, label=label)


def _ensure_output_path(path: Path, root: Path, *, allow_external: bool, label: str) -> None:
    if allow_external:
        return
    if not _is_relative_to(path.resolve(), root.resolve()):
        raise SpineAtlasPathError(f"{label} path escapes output directory: {path}")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_component(value: str) -> str:
    text = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value)).strip("._")
    return text or "region"


def _is_property_line(line: str) -> bool:
    return ":" in line


def _split_property(line: str) -> tuple[str, str]:
    if ":" not in line:
        raise SpineAtlasParseError(f"Invalid atlas property line: {line!r}")
    key, value = line.split(":", 1)
    key = key.strip().lower()
    if not key:
        raise SpineAtlasParseError(f"Invalid atlas property line: {line!r}")
    return key, value.strip()


def _parse_ints(value: str, count: int, label: str) -> tuple[int, ...]:
    parts = [part.strip() for part in re.split(r"[,\s]+", value) if part.strip()]
    if len(parts) != count:
        raise SpineAtlasParseError(f"{label} expects {count} integers, got {value!r}.")
    try:
        return tuple(int(part) for part in parts)
    except ValueError as exc:
        raise SpineAtlasParseError(f"{label} contains a non-integer value: {value!r}.") from exc


def _parse_size(value: str, label: str) -> tuple[int, int]:
    width, height = _parse_ints(value, 2, label)
    if width <= 0 or height <= 0:
        raise SpineAtlasParseError(f"{label} must be positive, got {width}x{height}.")
    return width, height


def _parse_int(value: str, label: str) -> int:
    try:
        return int(str(value).strip())
    except ValueError as exc:
        raise SpineAtlasParseError(f"{label} must be an integer, got {value!r}.") from exc


def _parse_float(value: str, label: str) -> float:
    try:
        return float(str(value).strip())
    except ValueError as exc:
        raise SpineAtlasParseError(f"{label} must be a number, got {value!r}.") from exc


def _parse_bool(value: str, label: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes", "1"}:
        return True
    if normalized in {"false", "no", "0"}:
        return False
    raise SpineAtlasParseError(f"{label} must be true or false, got {value!r}.")


def _parse_rotation(value: str, label: str) -> int:
    normalized = str(value).strip().lower()
    if normalized in {"false", "none", "no"}:
        return 0
    if normalized in {"true", "yes"}:
        return 90
    try:
        degrees = float(normalized)
    except ValueError as exc:
        raise SpineAtlasParseError(f"{label} must be false, true, or degrees, got {value!r}.") from exc
    if degrees % 90 != 0:
        raise SpineAtlasParseError(f"{label}={value!r} is not a right-angle rotation; lossless extraction needs a multiple of 90.")
    return int(degrees) % 360


def _parse_filter(value: str) -> tuple[str, str]:
    parts = [item.strip() for item in value.split(",")]
    if len(parts) == 1:
        return parts[0], parts[0]
    return parts[0], parts[1]


def _image_sha256(image: Image.Image) -> str:
    return hashlib.sha256(image.convert("RGBA").tobytes()).hexdigest()


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise SpineAtlasPathError(f"Unable to write metadata {path}: {exc}") from exc


def _save_page(path: Path, image: Image.Image) -> None:
    try:
        image.save(path, format="PNG")
    except OSError as exc:
        raise SpineAtlasPathError(f"Unable to write atlas page {path}: {exc}") from exc


def _write_psd(
    path: Path,
    atlas: SpineAtlas,
    region_paths: Mapping[str, Path],
    metadata_regions: Sequence[Mapping[str, Any]],
    progress: ProgressCallback | None = None,
) -> dict[str, dict[str, Any]]:
    try:
        from psd_tools import PSDImage
        from psd_tools.api.layers import Group, PixelLayer
        from psd_tools.constants import Compression, Tag
    except Exception as exc:
        raise SpineAtlasDependencyError(
            "PSD export requires optional psd-tools. PNG and metadata export do not require it."
        ) from exc
    canvas_width = max((page.width or 0) for page in atlas.pages)
    canvas_height = max((page.height or 0) for page in atlas.pages)
    total = max(1, len(metadata_regions))
    for index, item in enumerate(metadata_regions, start=1):
        canvas_width = max(canvas_width, int(item["x"]) + int(item["orig_width"]))
        canvas_height = max(canvas_height, int(item["y"]) + int(item["orig_height"]))
    if canvas_width <= 0 or canvas_height <= 0:
        raise SpineAtlasBoundsError("Cannot create a PSD with an empty canvas.")
    # ``PSDImage.save`` normally composites the whole layer tree whenever a
    # layer was added.  That is prohibitively expensive for a 4096x4096 atlas
    # with hundreds of regions: psd-tools materializes a full-canvas array for
    # every layer while building its preview.  Keep the layer records, but
    # install a bounded preview ourselves and mark the tree clean before save.
    # The preview is still useful in Photoshop, while layer decoding and PSD
    # write-back remain unchanged.
    psd = PSDImage.new("RGBA", (canvas_width, canvas_height), compression=Compression.RLE)
    for index, item in enumerate(metadata_regions, start=1):
        region_id = str(item["region_id"])
        image = _open_rgba(region_paths[region_id])
        # Keep one stable, machine-readable group per region.  Artists may
        # add ``<region_id>__overlay`` or ``<region_id>__erase`` layers inside
        # that group; read-back composites the group once, so an edit is never
        # applied twice through both the group and its child layers.
        group = Group.new(psd, name=region_id, open_folder=True)
        group.tagged_blocks.set_data(Tag.LAYER_ID, int(item["psd_layer_id"]))
        PixelLayer.frompil(
            image,
            group,
            name="base",
            top=int(item["y"]),
            left=int(item["x"]),
            # RLE keeps the channel buffers bounded while psd-tools builds a
            # many-region PSD; transparent/trimmed areas compress especially
            # well and the PSD remains lossless.
            compression=Compression.RLE,
        )
        _emit_progress(progress, 80 + int(index / total * 18), f"Prepared PSD layer {index}/{total}")
    preview = _psd_preview(atlas, canvas_width, canvas_height, region_paths, metadata_regions)
    # ImageData.set_data is the same channel encoding used by psd-tools' own
    # save path.  It uses the RLE compressor configured above and avoids the
    # repeated full-canvas compositing pass in PSDImage.save.
    psd._record.image_data.set_data(  # type: ignore[attr-defined]
        [channel.tobytes() for channel in preview.split()], psd._record.header  # type: ignore[attr-defined]
    )
    psd._updated = False  # type: ignore[attr-defined]
    path.parent.mkdir(parents=True, exist_ok=True)
    psd.save(str(path))
    baseline = _read_psd_baseline(path, metadata_regions)
    _emit_progress(progress, 99, "PSD written")
    return baseline


def _decode_psd_node(node: Any) -> Image.Image | None:
    """Decode one PSD node while preserving raw PixelLayer channel values."""

    has_mask = getattr(node, "has_mask", None)
    if callable(has_mask) and has_mask():
        composite = getattr(node, "composite", None)
        if callable(composite):
            return composite(force=True)
    topil = getattr(node, "topil", None)
    if callable(topil):
        # PixelLayer.topil() returns the layer-sized image directly.  Calling
        # composite() here would replace transparent RGB with the compositor
        # backdrop and round semi-transparent channels.
        image = topil()
        if image is not None:
            return image
    composite = getattr(node, "composite", None)
    if callable(composite):
        return composite(force=True)
    return None


def _read_psd_baseline(
    path: Path,
    metadata_regions: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Record hashes of the decoder output from the PSD just written.

    The baseline intentionally comes from reopening the saved file.  This
    catches channel compression/decoding behavior of the installed psd-tools
    version instead of assuming a PIL source image will decode identically.
    """

    try:
        from psd_tools import PSDImage
    except Exception as exc:
        raise SpineAtlasDependencyError(
            "PSD baseline decoding requires optional psd-tools."
        ) from exc
    expected = {str(item["region_id"]): item for item in metadata_regions}
    baseline_dir = path.parent / "psd_baseline"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    psd = PSDImage.open(str(path))
    result: dict[str, dict[str, Any]] = {}
    for node in psd.descendants():
        name = str(getattr(node, "name", ""))
        item = expected.get(name)
        if item is None:
            continue
        image = _decode_psd_node(node)
        if image is None:
            continue
        image = image.convert("RGBA")
        baseline_file = baseline_dir / f"{_safe_component(name)}.png"
        image.save(baseline_file, format="PNG")
        result[name] = {
            "sha256": _image_sha256(image),
            "size": [image.width, image.height],
            "path": baseline_file.relative_to(path.parent).as_posix(),
        }
    return result


def _psd_preview(
    atlas: SpineAtlas,
    canvas_width: int,
    canvas_height: int,
    region_paths: Mapping[str, Path],
    metadata_regions: Sequence[Mapping[str, Any]],
) -> Image.Image:
    """Build a PSD preview without psd-tools' full layer-tree compositor.

    A single-page atlas can use its source page directly, which both preserves
    the original preview and avoids touching hundreds of layer images.  For
    multiple pages the PSD layer layout overlays regions at their page
    coordinates, so compose those small region images into one bounded PIL
    canvas instead.  The resulting image is only used as the PSD composite
    preview; individual layers remain the editable source of truth.
    """

    if len(atlas.pages) == 1 and atlas.atlas_path is not None:
        page = atlas.pages[0]
        try:
            page_path = _resolve_relative_path(
                atlas.atlas_path.parent,
                page.name,
                allow_external=False,
                label="atlas page",
            )
            source = _load_page(page_path, page)
            if source.width <= canvas_width and source.height <= canvas_height:
                if source.size == (canvas_width, canvas_height):
                    return source
                preview = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
                preview.alpha_composite(source, dest=(0, 0))
                return preview
        except SpineAtlasError:
            # Extraction has already validated this page.  Keep PSD export
            # useful for parsed/in-memory callers by falling back to the
            # region preview if a source path is unavailable at this point.
            pass

    preview = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
    for item in metadata_regions:
        image = _open_rgba(region_paths[str(item["region_id"])])
        left = int(item["x"])
        top = int(item["y"])
        if left < 0 or top < 0 or left + image.width > canvas_width or top + image.height > canvas_height:
            continue
        preview.alpha_composite(image, dest=(left, top))
    return preview


def _emit_progress(progress: ProgressCallback | None, value: int, message: str) -> None:
    if progress is None:
        return
    try:
        progress(max(0, min(100, int(value))), str(message))
    except Exception:
        # Progress UI must never make a successful extraction fail.
        return


__all__ = [
    "AtlasEditLayer",
    "AtlasPage",
    "AtlasRegion",
    "OFFICIAL_SPINE_ATLAS_FORMAT_URL",
    "OFFICIAL_SPINE_TEXTURE_PACKER_URL",
    "ProgressCallback",
    "SPINE_ATLAS_METADATA_FORMAT",
    "SPINE_ATLAS_METADATA_VERSION",
    "SpineAtlas",
    "SpineAtlasDependencyError",
    "SpineAtlasError",
    "SpineAtlasExportResult",
    "SpineAtlasExtractionResult",
    "SpineAtlasPage",
    "SpineAtlasParseError",
    "SpineAtlasPathError",
    "SpineAtlasRepackResult",
    "SpineAtlasRepacker",
    "SpineAtlasBoundsError",
    "SpineAtlasEditLayer",
    "SpineAtlasWritebackResult",
    "extract_atlas",
    "extract_atlas_regions",
    "export_spine_atlas",
    "export_spine_atlas_parts",
    "extract_spine_atlas",
    "load_atlas",
    "parse_atlas",
    "parse_atlas_text",
    "parse_spine_atlas",
    "repack_atlas",
    "repack_spine_atlas",
    "reassemble_atlas",
    "unpack_atlas",
    "writeback_atlas",
    "writeback_atlas_pages",
    "writeback_spine_atlas",
]
