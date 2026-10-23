"""MCP tool registration for the INSPIRE-HEP adapter."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import inspire
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded


def register_inspire_tools(
    server: MCPServer,
    settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only inspirehep.net tools."""

    @server.tool(
        name="inspire_search_papers",
        description=(
            "Search INSPIRE-HEP literature records with the native query syntax "
            "(e.g. 'a witten and t quark', 'date 2024->2025'). The right tool "
            "for published HEP papers, preprints and citation data; internal "
            "unpublished CMS analyses belong to glance_search_analyses, CERN "
            "notes/theses to cds_search, and CMS-approved public result pages "
            "to cmsresults_*. Sort is one of bestmatch, mostrecent, mostcited."
        ),
    )
    async def inspire_search_papers(
        query: str,
        sort: str = "bestmatch",
        size: Annotated[int, Field(ge=1, le=100)] = 10,
        page: Annotated[int, Field(ge=1)] = 1,
    ) -> ToolEnvelope:
        """INSPIRE literature search."""
        return await guarded(
            auth,
            lambda: inspire.search_papers(
                http, settings, query=query, sort=sort, size=size, page=page
            ),
        )

    @server.tool(
        name="inspire_get_paper",
        description=(
            "Full metadata of one published paper; follow inspire_search_papers "
            "with an identifier it returns. Provide exactly one identifier: "
            "inspire_id (record id), arxiv_id (e.g. 2401.12345), or doi. "
            "Priority: inspire_id > arxiv_id > doi."
        ),
    )
    async def inspire_get_paper(
        inspire_id: str | None = None,
        arxiv_id: str | None = None,
        doi: str | None = None,
    ) -> ToolEnvelope:
        """Paper detail by INSPIRE id, arXiv id, or DOI."""
        return await guarded(
            auth,
            lambda: inspire.get_paper(
                http, settings, inspire_id=inspire_id, arxiv_id=arxiv_id, doi=doi
            ),
        )

    @server.tool(
        name="inspire_get_author_papers",
        description=(
            "Papers of one published author — 'what has X published', 'papers "
            "by X': resolve by author_name (via the authors API, best match) or "
            "pass the author's INSPIRE BAI directly as author_id (e.g. J.Doe.1). "
            "Includes citation metrics computed from the returned page. NOT for "
            "CMS collaboration membership (glance_membership_search_members)."
        ),
    )
    async def inspire_get_author_papers(
        author_name: str | None = None,
        author_id: str | None = None,
        sort: str = "mostrecent",
        size: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> ToolEnvelope:
        """Author resolution plus their recent papers and metrics."""
        return await guarded(
            auth,
            lambda: inspire.get_author_papers(
                http,
                settings,
                author_name=author_name,
                author_id=author_id,
                sort=sort,
                size=size,
            ),
        )

    @server.tool(
        name="inspire_get_citations",
        description=(
            "Citation graph of one published record: papers citing it "
            "(direction=citing) or the references it cites (direction=cited_by), "
            "with a per-year timeline; follow inspire_search_papers with an "
            "inspire_id it returns."
        ),
    )
    async def inspire_get_citations(
        inspire_id: str,
        direction: str = "citing",
        size: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> ToolEnvelope:
        """Citation-direction search for one record."""
        return await guarded(
            auth,
            lambda: inspire.get_citations(
                http, settings, inspire_id=inspire_id, direction=direction, size=size
            ),
        )

    @server.tool(
        name="inspire_search_collaboration",
        description=(
            "Search the published papers of an experimental collaboration "
            "(ATLAS, CMS, LHCb, ALICE, Belle-II, ...) — 'all CMS publications "
            "in 2024'; optionally restricted to a year. Returns year "
            "distribution, total citations, and the top-cited papers of the "
            "returned page. NOT for CMS-approved public result pages or plots "
            "(cmsresults_*)."
        ),
    )
    async def inspire_search_collaboration(
        collaboration_name: str,
        sort: str = "mostrecent",
        size: Annotated[int, Field(ge=1, le=100)] = 20,
        year: Annotated[int, Field(ge=1800)] | None = None,
    ) -> ToolEnvelope:
        """Collaboration search with coarse statistics."""
        return await guarded(
            auth,
            lambda: inspire.search_collaboration(
                http,
                settings,
                collaboration_name=collaboration_name,
                sort=sort,
                size=size,
                year=year,
            ),
        )

    @server.tool(
        name="inspire_get_paper_figures",
        description=(
            "Figure links (caption, URL, description) of one published paper; "
            "follow inspire_search_papers. Provide exactly one identifier like "
            "inspire_get_paper. NOT for CMS public result-page plots "
            "(cmsresults_get_paper)."
        ),
    )
    async def inspire_get_paper_figures(
        inspire_id: str | None = None,
        arxiv_id: str | None = None,
        doi: str | None = None,
    ) -> ToolEnvelope:
        """Figure links for one paper."""
        return await guarded(
            auth,
            lambda: inspire.get_paper_figures(
                http, settings, inspire_id=inspire_id, arxiv_id=arxiv_id, doi=doi
            ),
        )

    @server.tool(
        name="inspire_get_references",
        description=(
            "Formatted reference list of one INSPIRE record; follow "
            "inspire_search_papers with an inspire_id it returns. format is "
            "bibtex, latex-us, latex-eu, or json (client-side shaping of "
            "reference metadata). Resolvable references are bulk-formatted via "
            "a single query (capped at 250; truncation reported)."
        ),
    )
    async def inspire_get_references(
        inspire_id: str,
        format: str = "bibtex",
        limit: Annotated[int, Field(ge=1)] | None = None,
    ) -> ToolEnvelope:
        """Bulk-formatted references of one record."""
        return await guarded(
            auth,
            lambda: inspire.get_references(
                http, settings, inspire_id=inspire_id, reference_format=format, limit=limit
            ),
        )

    @server.tool(
        name="inspire_get_bibtex",
        description=(
            "Single-entry BibTeX of one published paper; follow "
            "inspire_search_papers. Identifiers: inspire_id, arxiv_id, or doi "
            "(same priority as inspire_get_paper)."
        ),
    )
    async def inspire_get_bibtex(
        inspire_id: str | None = None,
        arxiv_id: str | None = None,
        doi: str | None = None,
    ) -> ToolEnvelope:
        """BibTeX passthrough for one paper."""
        return await guarded(
            auth,
            lambda: inspire.get_bibtex(
                http, settings, inspire_id=inspire_id, arxiv_id=arxiv_id, doi=doi
            ),
        )
