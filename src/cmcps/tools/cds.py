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
            "Search CDS (cds.cern.ch) records via the Invenio search API. "
            "Optionally filter by experiment, collection doc_type, and a "
            "YYYY-MM-DD date range. Sort: mostrecent, bestmatch, mostcited. "
            "The CDS endpoint does not expose a total hit count."
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
            "Fetch the full metadata of one cds.cern.ch record by numeric recid "
            "('12345' or 'cds:12345'), including detailed authors and files."
        ),
    )
    async def cds_get_record(recid: str) -> ToolEnvelope:
        """CDS record detail."""
        return await guarded(auth, lambda: cds.get_record(http, settings, recid=recid))

    @server.tool(
        name="cds_get_files",
        description=(
            "List the files attached to one cds.cern.ch record by numeric recid "
            "('12345' or 'cds:12345'). Returns download links only; file content "
            "is never fetched."
        ),
    )
    async def cds_get_files(recid: str) -> ToolEnvelope:
        """CDS record file links."""
        return await guarded(auth, lambda: cds.get_files(http, settings, recid=recid))

    @server.tool(
        name="cds_download_file",
        description=(
            "Download a cds.cern.ch attachment (a download_url from cds_get_files) "
            "into local CMcpS download storage. Returns the local path and size; "
            "file content is not inlined. CMS Internal Note files served by legacy "
            "iCMS (CMSFence) are detected and reported instead of returning the "
            "login placeholder."
        ),
    )
    async def cds_download_file(download_url: str) -> ToolEnvelope:
        """Bounded local download of a CDS attachment."""
        return await guarded(
            auth,
            lambda: cds.download_file(http, settings, download_url=download_url),
        )
