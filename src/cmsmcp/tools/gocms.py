"""MCP tool registration for the goCMS shortlink adapter."""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmsmcp.adapters import gocms
from cmsmcp.auth.service import AuthService
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.results import ToolEnvelope
from cmsmcp.net.client import SafeHttpClient
from cmsmcp.tools.common import guarded


def register_gocms_tools(
    server: MCPServer,
    _settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only goCMS shortlink tools."""

    @server.tool(
        name="gocms_list_links",
        description=(
            "Search/list CMS go-links (gocms.cern.ch shortlinks): short name, "
            "target URL, description, tags, created_by — the directory to find "
            "a CMS page/URL when you only know its name. NOT a full-text "
            "documentation search (cmssearch_search). Optional text search and "
            "tag filter; paginated. Anonymous public API."
        ),
    )
    async def gocms_list_links(
        search: str | None = None,
        tags: list[str] | None = None,
        page: Annotated[int, Field(ge=1)] = 1,
        per_page: Annotated[int, Field(ge=1, le=100)] = 20,
        sort_order: Literal["asc", "desc"] = "desc",
    ) -> ToolEnvelope:
        """Go-link search/list."""
        return await guarded(
            auth,
            lambda: gocms.list_links(
                http,
                search=search,
                tags=tags,
                page=page,
                per_page=per_page,
                sort_order=sort_order,
            ),
        )

    @server.tool(
        name="gocms_get_link",
        description=("One go-link by numeric id; follow gocms_list_links with an id it returns."),
    )
    async def gocms_get_link(link_id: Annotated[int, Field(ge=1)]) -> ToolEnvelope:
        """Go-link detail."""
        return await guarded(auth, lambda: gocms.get_link(http, link_id=link_id))

    @server.tool(
        name="gocms_list_tags",
        description=(
            "All tags in the CMS go-link service; use them with gocms_list_links' tags filter."
        ),
    )
    async def gocms_list_tags() -> ToolEnvelope:
        """Go-link tags."""
        return await guarded(auth, lambda: gocms.list_tags(http))
