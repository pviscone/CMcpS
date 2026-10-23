"""MCP tool registration for the CDS adapter."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import cds
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded


def register_cds_tools(
    server: MCPServer,
    settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only cds.cern.ch tools."""

    @server.tool(
        name="cds_search",
        description=(
            "Search CERN Document Server records (cds.cern.ch) — CERN notes, "
            "theses, conference proceedings and archived documents — via the "
            "Invenio search API. NOT for published papers with citation data "
            "(inspire_search_papers), internal CMS analyses (glance_*), or "
            "wiki pages (twiki_search). Optionally filter by experiment, "
            "collection doc_type, and a YYYY-MM-DD date range. Sort: "
            "mostrecent, bestmatch, mostcited. The CDS endpoint does not "
            "expose a total hit count."
        ),
    )
    async def cds_search(
        *,
        query: str,
        experiment: str | None = None,
        doc_type: str | None = None,
        from_date: str | None = None,
        until_date: str | None = None,
        size: Annotated[int, Field(ge=1, le=100)] = 10,
        page: Annotated[int, Field(ge=1)] = 1,
        sort: str = "mostrecent",
    ) -> ToolEnvelope:
        """CDS record search as a normalized SearchResult."""
        return await guarded(
            auth,
            lambda: cds.search(
                http,
                settings,
                query=query,
                experiment=experiment,
                doc_type=doc_type,
                from_date=from_date,
                until_date=until_date,
                size=size,
                page=page,
                sort=sort,
            ),
        )

    @server.tool(
        name="cds_get_record",
        description=(
            "Full metadata of one CDS record, including detailed authors and "
            "files; follow cds_search with a recid it returns. recid is the "
            "numeric CDS record id ('12345' or 'cds:12345')."
        ),
    )
    async def cds_get_record(
        recid: Annotated[
            str,
            Field(
                description=(
                    "Numeric CDS record id, e.g. '12345' or 'cds:12345'; from cds_search results."
                )
            ),
        ],
    ) -> ToolEnvelope:
        """CDS record detail."""
        return await guarded(auth, lambda: cds.get_record(http, settings, recid=recid))

    @server.tool(
        name="cds_get_files",
        description=(
            "List the files attached to one CDS record; follow cds_search with "
            "a recid it returns ('12345' or 'cds:12345'). Returns download "
            "links only; file content is never fetched."
        ),
    )
    async def cds_get_files(
        recid: Annotated[
            str,
            Field(
                description=(
                    "Numeric CDS record id, e.g. '12345' or 'cds:12345'; from cds_search results."
                )
            ),
        ],
    ) -> ToolEnvelope:
        """CDS record file links."""
        return await guarded(auth, lambda: cds.get_files(http, settings, recid=recid))

    @server.tool(
        name="cds_download_file",
        description=(
            "Download one CDS attachment (a download_url from cds_get_files) "
            "into local CMcpS download storage. Returns the local path and "
            "size; file content is not inlined. CMS Internal Note files served "
            "by legacy iCMS (CMSFence) are detected and reported instead of "
            "returning the login placeholder."
        ),
    )
    async def cds_download_file(download_url: str) -> ToolEnvelope:
        """Bounded local download of a CDS attachment."""
        return await guarded(
            auth,
            lambda: cds.download_file(http, settings, download_url=download_url),
        )
