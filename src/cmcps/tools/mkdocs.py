"""MCP tool registration for the MkDocs adapter."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import mkdocs
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded

_BASE_DOC = (
    " base_url is the docs site root (e.g. "
    "https://cms-l1t-correlator.docs.cern.ch); SSO-protected sites work "
    "through the shared cookie session after cern_auth_sign_in."
)


def register_mkdocs_tools(
    server: MCPServer,
    _settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only MkDocs tools."""

    @server.tool(
        name="mkdocs_search",
        description=(
            "Full-text search of a MkDocs site over its search index "
            "(title hits weigh 10x). Client-side scoring; snippets included." + _BASE_DOC
        ),
    )
    async def mkdocs_search(
        base_url: str,
        query: str,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> ToolEnvelope:
        """MkDocs site search."""
        return await guarded(
            auth,
            lambda: mkdocs.search(http, base_url=base_url, query=query, limit=limit),
        )

    @server.tool(
        name="mkdocs_list_pages",
        description=(
            "List all pages of a MkDocs site (locations + titles) from its "
            "search index, with optional substring path filter." + _BASE_DOC
        ),
    )
    async def mkdocs_list_pages(
        base_url: str,
        path_filter: str | None = None,
    ) -> ToolEnvelope:
        """MkDocs page listing."""
        return await guarded(
            auth,
            lambda: mkdocs.list_pages(http, base_url=base_url, path_filter=path_filter),
        )

    @server.tool(
        name="mkdocs_get_page",
        description=(
            "Fetch one MkDocs page as plain text (HTML stripped; code and "
            "tables preserved as text). page is the 'location' value from "
            "mkdocs_search/mkdocs_list_pages (empty string = home page)." + _BASE_DOC
        ),
    )
    async def mkdocs_get_page(base_url: str, page: str = "") -> ToolEnvelope:
        """MkDocs page fetch."""
        return await guarded(auth, lambda: mkdocs.get_page(http, base_url=base_url, page=page))

    @server.tool(
        name="mkdocs_download_file",
        description=(
            "Bounded download of one static asset hosted on the docs site "
            "(plot, PDF, ROOT file) into local CMcpS storage."
        ),
    )
    async def mkdocs_download_file(file_url: str) -> ToolEnvelope:
        """MkDocs asset download."""
        return await guarded(auth, lambda: mkdocs.download_file(http, file_url=file_url))
