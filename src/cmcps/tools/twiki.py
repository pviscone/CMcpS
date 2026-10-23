"""MCP tool registration for the CERN TWiki adapter."""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import twiki
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded


def register_twiki_tools(
    server: MCPServer,
    _settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only twiki.cern.ch tools."""

    @server.tool(
        name="twiki_search",
        description=(
            "Full-text search of one CERN TWiki web (e.g. CMSPublic, Main, "
            "Sandbox). search_type is keyword (default), literal, or regex. "
            "Requires CERN SSO beyond anonymous view pages."
        ),
    )
    async def twiki_search(
        web: str,
        query: str,
        search_type: Literal["keyword", "literal", "regex"] = "keyword",
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> ToolEnvelope:
        """Twiki web search."""
        return await guarded(
            auth,
            lambda: twiki.search(http, web=web, query=query, search_type=search_type, limit=limit),
        )

    @server.tool(
        name="twiki_get_page",
        description=(
            "Fetch one TWiki page by web/topic. raw=true (default) returns "
            "the wiki source text with parsed metadata (author, revision, "
            "date, attachments) and requires SSO; raw=false returns the "
            "anonymous rendered page as plain text."
        ),
    )
    async def twiki_get_page(
        web: str,
        topic: str,
        raw: bool = True,
    ) -> ToolEnvelope:
        """Twiki page (raw source or rendered text)."""
        return await guarded(auth, lambda: twiki.get_page(http, web=web, topic=topic, raw=raw))

    @server.tool(
        name="twiki_list_topics",
        description=(
            "List topics of one public TWiki web via its WebTopicList page "
            "(anonymous for public webs)."
        ),
    )
    async def twiki_list_topics(
        web: str,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
    ) -> ToolEnvelope:
        """Twiki web topic list."""
        return await guarded(auth, lambda: twiki.list_topics(http, web=web, limit=limit))

    @server.tool(
        name="twiki_download_attachment",
        description=(
            "Download one TWiki attachment (/twiki/pub/... URL) into local "
            "CMcpS storage; take URLs from twiki_get_page attachments."
        ),
    )
    async def twiki_download_attachment(attachment_url: str) -> ToolEnvelope:
        """Bounded attachment download."""
        return await guarded(
            auth, lambda: twiki.download_attachment(http, attachment_url=attachment_url)
        )
