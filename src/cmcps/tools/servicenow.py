"""MCP tool registration for the CERN Service-Now (service portal) adapter."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import servicenow
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded


def register_servicenow_tools(
    server: MCPServer,
    _settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only CERN Service-Now tools."""

    @server.tool(
        name="servicenow_search_service_portal",
        description=(
            "Full-text search across the CERN Service Portal "
            "(cern.service-now.com): knowledge-base articles, service-catalog "
            "items, service elements, outages, and other portal content — "
            "'how do I register a device', 'desk phone provisioning' "
            "questions. Relevance-ranked, no pagination (backend limitation). "
            "Use sources=['kb'] to restrict to the Knowledge Base, "
            "sources=['actions','items'] for the service catalogue. "
            "For internal CMS resources (DocDB, GLANCE, CERN IT notes) use "
            "docdb_*/glance_*/cmssearch_* instead. Requires CERN SSO."
        ),
    )
    async def servicenow_search_service_portal(
        query: str,
        sources: list[str] | None = None,
        limit: Annotated[int, Field(ge=1, le=30)] = 10,
    ) -> ToolEnvelope:
        """Service-Portal multi-source search."""
        return await guarded(
            auth,
            lambda: servicenow.search_portal(http, query=query, sources=sources, limit=limit),
        )

    @server.tool(
        name="servicenow_search_kb",
        description=(
            "Search the CERN IT Knowledge Base (Knowledge Management articles "
            "KB00xxxxx on cern.service-now.com): how-to guides like grid "
            "certificates, password reset, 2FA setup. Follow a result with "
            "servicenow_get_kb_article using the article number or sys_id. "
            "Relevance-ranked, no pagination (backend limitation). "
            "Requires CERN SSO."
        ),
    )
    async def servicenow_search_kb(
        query: str,
        limit: Annotated[int, Field(ge=1, le=30)] = 10,
    ) -> ToolEnvelope:
        """KB-only search."""
        return await guarded(
            auth,
            lambda: servicenow.search_portal(http, query=query, sources=["kb"], limit=limit),
        )

    @server.tool(
        name="servicenow_get_kb_article",
        description=(
            "Full record of one CERN KB article by number (KB0000705) or "
            "32-hex sys_id from servicenow_search_kb results: title, summary, "
            "category, body as plain text, dates, portal link. "
            "Requires CERN SSO."
        ),
    )
    async def servicenow_get_kb_article(reference: str) -> ToolEnvelope:
        """KB article detail."""
        return await guarded(auth, lambda: servicenow.get_kb_article(http, reference=reference))
