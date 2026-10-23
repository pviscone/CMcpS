"""MCP tool registration for the CMS public results adapter."""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import cmsresults
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded

_KIND_DOC = (
    "kind is publications (journal-accepted, default) or preliminary-results (PAS/conference)."
)


def register_cmsresults_tools(
    server: MCPServer,
    _settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only CMS public results tools."""

    @server.tool(
        name="cmsresults_list_groups",
        description=(
            "List CMS physics working groups (HIG, SMP, TOP, ...) with links "
            "to their publications and preliminary-results indexes — the entry "
            "point for CMS-APPROVED PUBLIC results (final plots and papers). "
            "NOT for internal analyses in progress (glance_*) or the published "
            "paper record itself (inspire_*)."
        ),
    )
    async def cmsresults_list_groups() -> ToolEnvelope:
        """Working-group listing."""
        return await guarded(auth, lambda: cmsresults.list_groups(http))

    @server.tool(
        name="cmsresults_list_papers",
        description=(
            "List CMS-approved public papers/PAS results of one working group "
            "with title, journal, DOI, and date; follow groups from "
            "cmsresults_list_groups. year and title_contains are client-side "
            "filters. NOT for arbitrary literature search (inspire_*). " + _KIND_DOC
        ),
    )
    async def cmsresults_list_papers(
        group: str,
        kind: Literal["publications", "preliminary-results"] = "publications",
        year: Annotated[int, Field(ge=1990, le=2100)] | None = None,
        title_contains: str | None = None,
    ) -> ToolEnvelope:
        """Per-group paper listing."""
        return await guarded(
            auth,
            lambda: cmsresults.list_papers(
                http, group=group, kind=kind, year=year, title_contains=title_contains
            ),
        )

    @server.tool(
        name="cmsresults_get_paper",
        description=(
            "Detail of one CMS public-results paper by id (e.g. HIG-11-024): "
            "title, abstract, journal/DOI, arXiv/CDS/INSPIRE links, and "
            "figure/table download URLs; follow cmsresults_list_papers. "
            "Searches publications then preliminary-results unless kind is given."
        ),
    )
    async def cmsresults_get_paper(
        paper_id: str,
        kind: Literal["publications", "preliminary-results"] | None = None,
    ) -> ToolEnvelope:
        """Paper detail page."""
        return await guarded(auth, lambda: cmsresults.get_paper(http, paper_id=paper_id, kind=kind))

    @server.tool(
        name="cmsresults_download_file",
        description=(
            "Download one figure/table/PDF hosted on cms-results.web.cern.ch "
            "into local CMcpS storage; pass a figures_and_tables URL returned "
            "by cmsresults_get_paper."
        ),
    )
    async def cmsresults_download_file(download_url: str) -> ToolEnvelope:
        """Bounded figure/table download."""
        return await guarded(
            auth, lambda: cmsresults.download_file(http, download_url=download_url)
        )
