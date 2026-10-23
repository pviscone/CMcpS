"""Contract tests for the local PDF figure tools (no network)."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from cmcps.adapters import pdftools
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, PolicyViolationError, RequestTooLargeError

PAGE_W, PAGE_H = 400.0, 600.0
FIG_RECT = (100.0, 200.0, 300.0, 400.0)
CAPTION_Y = 415.0


@pytest.fixture
def figure_pdf(tmp_path: Path) -> Path:
    """One page with body text, a vector rectangle, and a caption under it."""
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_text((50, 50), "Section text above the figure.")
    page.draw_rect(pymupdf.Rect(*FIG_RECT), color=(1, 0, 0), width=2)
    page.insert_text((FIG_RECT[0], FIG_RECT[1] + 20), "axis label")
    page.insert_text((50, CAPTION_Y), "Figure 1.1: caption below the frame.")
    path = tmp_path / "figure.pdf"
    doc.save(path)
    doc.close()
    return path


async def test_probe_page_reports_geometry(figure_pdf: Path) -> None:
    probe = await pdftools.probe_page(str(figure_pdf), 1)
    assert probe["page_count"] == 1
    assert probe["page_size"] == [PAGE_W, PAGE_H]
    texts = [block["text"] for block in probe["text_blocks"]]
    assert any("caption" in text for text in texts)
    assert any("axis label" in text for text in texts)
    # The vector frame extends below the last text inside the figure; its union
    # must reach the frame bottom, further down than the text union.
    drawings_union = probe["clip_hints"]["drawings_union"]
    assert drawings_union == pytest.approx(list(FIG_RECT), abs=1.5)
    assert probe["images"] == []
    assert probe["drawing_count"] >= 1


async def test_crop_figure_preserves_vectors(figure_pdf: Path, settings: Settings) -> None:
    result = await pdftools.crop_figure(
        settings, str(figure_pdf), 1, (90.0, 190.0, 320.0, FIG_RECT[3] + 2)
    )
    out_path = Path(result["local_path"])
    assert out_path.is_file()
    assert out_path.parent == (settings.download_dir).resolve()
    cropped = pymupdf.open(out_path)
    try:
        page = cropped[0]
        assert page.rect.width == pytest.approx(230.0, abs=0.01)
        assert page.rect.height == pytest.approx(212.0, abs=0.01)
        assert page.get_drawings(), "vector frame lost during crop"
        assert "axis label" in page.get_text()
        assert "caption" not in page.get_text()
        assert "Section text" not in page.get_text()
    finally:
        cropped.close()


async def test_crop_figure_clamps_clip_to_page(figure_pdf: Path, settings: Settings) -> None:
    result = await pdftools.crop_figure(settings, str(figure_pdf), 1, (100.0, 200.0, 9999.0, 400.0))
    assert result["clip"] == [100.0, 200.0, PAGE_W, 400.0]


async def test_crop_figure_rejects_non_intersecting_bbox(
    figure_pdf: Path, settings: Settings
) -> None:
    with pytest.raises(InvalidInputError):
        await pdftools.crop_figure(settings, str(figure_pdf), 1, (1000.0, 1000.0, 1200.0, 1400.0))


async def test_crop_figure_rejects_inverted_bbox(figure_pdf: Path, settings: Settings) -> None:
    with pytest.raises(InvalidInputError):
        await pdftools.crop_figure(settings, str(figure_pdf), 1, (300.0, 400.0, 100.0, 200.0))


async def test_output_name_cannot_escape_download_dir(figure_pdf: Path, settings: Settings) -> None:
    with pytest.raises(PolicyViolationError):
        await pdftools.crop_figure(
            settings, str(figure_pdf), 1, FIG_RECT, output_name="../escaped.pdf"
        )


async def test_crop_figure_accepts_output_name(figure_pdf: Path, settings: Settings) -> None:
    result = await pdftools.crop_figure(
        settings, str(figure_pdf), 1, FIG_RECT, output_name="figure_1_1.pdf"
    )
    assert Path(result["local_path"]).name == "figure_1_1.pdf"


async def test_render_page_writes_png(figure_pdf: Path, settings: Settings) -> None:
    result = await pdftools.render_page(settings, str(figure_pdf), 1, bbox=FIG_RECT, dpi=110)
    out_path = Path(result["local_path"])
    assert out_path.suffix == ".png"
    assert out_path.parent == (settings.download_dir).resolve()
    assert result["width_px"] == pytest.approx((FIG_RECT[2] - FIG_RECT[0]) * 110 / 72, abs=2)
    assert out_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


async def test_render_page_enforces_pixel_budget(figure_pdf: Path, settings: Settings) -> None:
    settings.max_download_bytes = 4000
    with pytest.raises(RequestTooLargeError):
        await pdftools.render_page(settings, str(figure_pdf), 1, dpi=600)


async def test_probe_rejects_out_of_range_page(figure_pdf: Path) -> None:
    with pytest.raises(InvalidInputError):
        await pdftools.probe_page(str(figure_pdf), 2)


async def test_probe_rejects_non_pdf(tmp_path: Path) -> None:
    not_pdf = tmp_path / "not-a-pdf.txt"
    not_pdf.write_text("plain text")
    with pytest.raises(InvalidInputError):
        await pdftools.probe_page(str(not_pdf), 1)


async def test_probe_rejects_missing_file() -> None:
    with pytest.raises(InvalidInputError):
        await pdftools.probe_page("/nonexistent/missing.pdf", 1)
