"""MCP tool registration for local PDF figure extraction."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import pdftools
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded

BBoxAnnotation = Annotated[
    tuple[float, float, float, float],
    Field(
        description=(
            "Clip rectangle [x0, y0, x1, y1] in PDF points (origin top-left, "
            "y grows downward), in the coordinate space returned by "
            "pdf_probe_page."
        )
    ),
]


def register_pdftools_tools(
    server: MCPServer,
    settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the local PDF figure-processing tools.

    Local processing only: no HTTP client is needed, but the signature keeps
    the shared registration shape.
    """
    del http

    @server.tool(
        name="pdf_probe_page",
        description=(
            "Geometry of one page of a local PDF: text blocks, embedded "
            "images, and union bounds of vector drawings, all in PDF points. "
            "Use it to derive the clip box for pdf_crop_figure: figure labels "
            "live between the surrounding text blocks, and dashed frames/plot "
            "axes can extend below the last text block, so always check the "
            "drawings_union clip hint before cropping. Pages are numbered "
            "from 1."
        ),
    )
    async def pdf_probe_page(
        *,
        pdf_path: Annotated[
            str,
            Field(description="Path of a local PDF, e.g. one returned by a *_download_* tool."),
        ],
        page: Annotated[int, Field(ge=1, description="1-based page number.")],
    ) -> ToolEnvelope:
        """Text/image/vector drawing geometry of one PDF page."""
        return await guarded(auth, lambda: pdftools.probe_page(pdf_path, page))

    @server.tool(
        name="pdf_crop_figure",
        description=(
            "Crop one region of a local PDF page into a new single-page PDF, "
            "preserving vector drawings and selectable text (no rasterization) "
            "— extract a figure out of a paper or note. The output is written "
            "inside CMcpS download storage. Derive the bbox with "
            "pdf_probe_page and verify the result with pdf_render_page (pale "
            "vector figures can look empty at low DPI; re-render higher "
            "before concluding they are broken)."
        ),
    )
    async def pdf_crop_figure(
        *,
        pdf_path: Annotated[
            str,
            Field(description="Path of a local PDF, e.g. one returned by a *_download_* tool."),
        ],
        page: Annotated[int, Field(ge=1, description="1-based page number.")],
        bbox: BBoxAnnotation,
        output_name: Annotated[
            str | None,
            Field(description="Optional plain output file name (no directories)."),
        ] = None,
    ) -> ToolEnvelope:
        """Vector-preserving crop of one PDF page region."""
        return await guarded(
            auth,
            lambda: pdftools.crop_figure(settings, pdf_path, page, bbox, output_name=output_name),
        )

    @server.tool(
        name="pdf_render_page",
        description=(
            "Rasterize one local PDF page (or a clipped region of it) to PNG "
            "inside CMcpS download storage, for visual verification of a page "
            "or of a crop. ~110 DPI is enough for a quick check; use ~220 DPI "
            "for light/pale vector figures."
        ),
    )
    async def pdf_render_page(
        *,
        pdf_path: Annotated[
            str,
            Field(description="Path of a local PDF, e.g. one returned by a *_download_* tool."),
        ],
        page: Annotated[int, Field(ge=1, description="1-based page number.")],
        bbox: BBoxAnnotation | None = None,
        dpi: Annotated[int, Field(ge=1, le=600, description="Render resolution, 1-600.")] = 150,
    ) -> ToolEnvelope:
        """PNG render of a PDF page or region for visual checks."""
        return await guarded(
            auth,
            lambda: pdftools.render_page(settings, pdf_path, page, bbox=bbox, dpi=dpi),
        )
