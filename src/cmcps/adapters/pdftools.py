"""Local PDF figure tooling: page probing, vector cropping, and PNG rendering.

Unlike every other adapter this module performs no network I/O: it processes
local PDF files with PyMuPDF. Inputs may be any readable local path (typically
a file produced by one of the ``*_download_*`` tools); outputs are constrained
to the CMcpS download directory, the same policy the HTTP downloads enforce.

The vector-crop recipe mirrors the one validated in the PhD thesis tooling
(notes/pdf_mining.md): normalize the crop box to the mediabox, clamp the clip
rectangle, replay the source page into a new same-size page via
``show_pdf_page`` so vector drawings and text survive un-rasterized, and save
with ``garbage=4, deflate=True``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pymupdf

from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, PolicyViolationError, RequestTooLargeError

_SOURCE = "local"
_BLOCK_TEXT_LIMIT = 300
_MAX_DPI = 600

BBox = tuple[float, float, float, float]


def _resolve_input(pdf_path: str) -> Path:
    """Resolve the input PDF path and prove it exists."""
    candidate = Path(pdf_path).expanduser()
    if not candidate.is_file():
        raise InvalidInputError(
            f"Not a readable file: {candidate.name}",
            instructions=(
                "Pass the path of a local PDF, for example one returned by a *_download_* tool."
            ),
        )
    return candidate


def _open_pdf(pdf_path: str) -> tuple[Path, pymupdf.Document]:
    """Open a local PDF, mapping failures to a safe error."""
    path = _resolve_input(pdf_path)
    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        raise InvalidInputError(
            f"The file is not a valid PDF: {path.name}",
            instructions="Pass a PDF file; other document formats are not supported.",
        ) from exc
    if not doc.is_pdf:
        doc.close()
        raise InvalidInputError(
            f"The file is not a PDF: {path.name}",
            instructions="Convert the document to PDF before processing it.",
        )
    if doc.is_encrypted:
        doc.close()
        raise InvalidInputError(
            f"The PDF is encrypted: {path.name}",
            instructions="Decrypt the PDF locally before processing it.",
        )
    return path, doc


def _get_page(doc: pymupdf.Document, page: int) -> pymupdf.Page:
    """Return a 1-based page after validation."""
    if page < 1 or page > doc.page_count:
        raise InvalidInputError(
            f"Page {page} is out of range; the document has {doc.page_count} page(s)",
            instructions="Pages are numbered from 1, like pdftotext.",
        )
    normalized = doc[page - 1]
    normalized.set_cropbox(normalized.mediabox)
    return normalized


def _clip(page: pymupdf.Page, bbox: BBox | None) -> pymupdf.Rect:
    """Validate a clip rectangle against the normalized page."""
    full = pymupdf.Rect(page.mediabox)
    if bbox is None:
        return full
    rect = pymupdf.Rect(*bbox)
    if rect.is_empty or rect.is_infinite:
        raise InvalidInputError(
            f"The bounding box {list(bbox)} is empty or inverted",
            instructions="Pass [x0, y0, x1, y1] in PDF points with x0 < x1 and y0 < y1.",
        )
    clipped = rect & full
    if clipped.is_empty:
        raise InvalidInputError(
            "The bounding box does not intersect the page",
            instructions=(
                f"Page size is [{full.x0:.0f}, {full.y0:.0f}, {full.x1:.0f}, {full.y1:.0f}] "
                "(x0, y0, x1, y1) in PDF points."
            ),
        )
    return clipped


def _output_path(settings: Settings, output_name: str | None, default: str) -> Path:
    """Constrain outputs to the CMcpS download directory."""
    settings.ensure_directories()
    name = output_name if output_name is not None else default
    candidate = Path(name)
    if candidate.is_absolute() or candidate.name != name:
        raise PolicyViolationError(
            "Output names must be plain file names without directories",
            instructions="Files are always written inside the CMcpS download directory.",
        )
    return (settings.download_dir / candidate.name).resolve()


def _check_size(settings: Settings, path: Path, description: str) -> int:
    """Enforce the bounded-output policy on a freshly written file."""
    size = path.stat().st_size
    if size > settings.max_download_bytes:
        path.unlink()
        raise RequestTooLargeError(
            f"The {description} exceeds the {settings.max_download_bytes}-byte limit",
            instructions="Lower the DPI or crop a smaller region.",
        )
    return size


def _union(rects: list[pymupdf.Rect]) -> list[float] | None:
    """Union of rectangles as a JSON-safe list, or None when empty."""
    if not rects:
        return None
    result = rects[0]
    for rect in rects[1:]:
        result |= rect
    return [round(result.x0, 2), round(result.y0, 2), round(result.x1, 2), round(result.y1, 2)]


async def probe_page(pdf_path: str, page: int) -> dict[str, Any]:
    """Return the geometry of one PDF page for deriving a crop box.

    Coordinates are PDF points in the same space used by ``crop_figure``:
    text blocks come from ``get_text('blocks')``, images from
    ``get_image_info``, vector drawings from ``get_drawings`` (dashed frames
    and plot axes extend below the last text block, so their union is key).
    """
    input_path, doc = _open_pdf(pdf_path)
    try:
        target = _get_page(doc, page)
        text_blocks = [
            {
                "bbox": [round(b[0], 2), round(b[1], 2), round(b[2], 2), round(b[3], 2)],
                "text": " ".join(b[4].split())[:_BLOCK_TEXT_LIMIT],
            }
            for b in target.get_text("blocks")
            if b[6] == 0
        ]
        images = [
            {
                "bbox": [round(v, 2) for v in info["bbox"]],
                "width_px": info["width"],
                "height_px": info["height"],
            }
            for info in target.get_image_info()
        ]
        drawings = [item["rect"] for item in target.get_drawings()]
        text_rects = [pymupdf.Rect(b["bbox"]) for b in text_blocks]
        image_rects = [pymupdf.Rect(i["bbox"]) for i in images]
        return {
            "source": _SOURCE,
            "pdf_path": str(input_path),
            "page": page,
            "page_count": doc.page_count,
            "page_size": [round(target.mediabox.width, 2), round(target.mediabox.height, 2)],
            "rotation": target.rotation,
            "text_blocks": text_blocks,
            "images": images,
            "drawing_count": len(drawings),
            "clip_hints": {
                "text_union": _union(text_rects),
                "images_union": _union(image_rects),
                "drawings_union": _union(drawings),
                "all_union": _union(text_rects + image_rects + drawings),
            },
        }
    finally:
        doc.close()


async def crop_figure(
    settings: Settings,
    pdf_path: str,
    page: int,
    bbox: BBox,
    output_name: str | None = None,
) -> dict[str, Any]:
    """Crop a page region into a new single-page PDF, preserving vectors.

    The source content stream is replayed into a new page sized to the clip,
    so drawings stay vector and text stays selectable; nothing is rasterized.
    """
    input_path, doc = _open_pdf(pdf_path)
    try:
        target = _get_page(doc, page)
        clip = _clip(target, bbox)
        default = (
            f"{input_path.stem}-p{page}-{clip.x0:.0f}-{clip.y0:.0f}-{clip.x1:.0f}-{clip.y1:.0f}.pdf"
        )
        destination = _output_path(settings, output_name, default)
        out = pymupdf.open()
        try:
            new_page = out.new_page(width=clip.width, height=clip.height)
            new_page.show_pdf_page(new_page.rect, doc, page - 1, clip=clip)
            out.save(destination, garbage=4, deflate=True)
        finally:
            out.close()
        size = _check_size(settings, destination, "cropped PDF")
        return {
            "source": _SOURCE,
            "pdf_path": str(input_path),
            "page": page,
            "clip": [round(clip.x0, 2), round(clip.y0, 2), round(clip.x1, 2), round(clip.y1, 2)],
            "local_path": str(destination),
            "size_bytes": size,
        }
    finally:
        doc.close()


async def render_page(
    settings: Settings,
    pdf_path: str,
    page: int,
    bbox: BBox | None = None,
    dpi: int = 150,
) -> dict[str, Any]:
    """Rasterize a page (or a clipped region) to PNG for visual verification."""
    if dpi < 1 or dpi > _MAX_DPI:
        raise InvalidInputError(
            f"DPI {dpi} is outside the supported range 1-{_MAX_DPI}",
            instructions="Render at ~110 DPI for a quick check, ~220 DPI for pale figures.",
        )
    input_path, doc = _open_pdf(pdf_path)
    try:
        target = _get_page(doc, page)
        clip = _clip(target, bbox)
        pixels = int(clip.width * dpi / 72) * int(clip.height * dpi / 72) * 4
        if pixels > settings.max_download_bytes:
            raise RequestTooLargeError(
                "The rendered bitmap exceeds the configured size limit",
                instructions="Lower the DPI or render a smaller region.",
            )
        pixmap = target.get_pixmap(dpi=dpi, clip=clip)
        default = f"{input_path.stem}-p{page}-{dpi}dpi.png"
        destination = _output_path(settings, None, default)
        pixmap.save(destination)
        size = _check_size(settings, destination, "rendered PNG")
        return {
            "source": _SOURCE,
            "pdf_path": str(input_path),
            "page": page,
            "clip": [round(clip.x0, 2), round(clip.y0, 2), round(clip.x1, 2), round(clip.y1, 2)],
            "dpi": dpi,
            "width_px": pixmap.width,
            "height_px": pixmap.height,
            "local_path": str(destination),
            "size_bytes": size,
        }
    finally:
        doc.close()
