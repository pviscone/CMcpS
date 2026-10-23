"""MCP tool registration for the CERN TWiki adapter."""

from __future__ import annotations

from typing import Annotated

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
        name="twiki_get_page",
        description=(
            "Fetch one TWiki page by web/topic naming (e.g. web='CMSPublic', "
            "topic='PhysicsResultsHIG'); webs are hierarchical, so subwebs "
            "are 'Web/Subweb' (e.g. CMS/Internal). Locate pages with "
            "cmssearch_search (indices 'cms-twiki' and 'cms-public-twiki') "
            "or browse with twiki_list_topics. Pages are fetched with the "
            "low-noise print cover (cover=print) and returned as plain text; "
            "raw=true (default) additionally returns the footer metadata "
            "(revision, author, date, parent topic, attachments). CERN TWiki "
            "no longer serves the wiki source (?raw=on returns the rendered "
            "page), so raw=true does NOT return raw markup. Works "
            "anonymously for every web."
        ),
    )
    async def twiki_get_page(
        web: Annotated[
            str,
            Field(
                description=(
                    "TWiki web (case-sensitive), with subwebs joined by '/' "
                    "(e.g. CMSPublic, Main, CMS/Internal); derived from the "
                    "cmssearch hit URL /twiki/bin/view/<Web>/<Topic>"
                )
            ),
        ],
        topic: Annotated[
            str,
            Field(description="TWiki topic name (CamelCase page title), e.g. PhysicsResultsHIG"),
        ],
        raw: bool = True,
    ) -> ToolEnvelope:
        """Twiki page (raw source or rendered text)."""
        return await guarded(auth, lambda: twiki.get_page(http, web=web, topic=topic, raw=raw))

    @server.tool(
        name="twiki_list_topics",
        description=(
            "List topics of one public TWiki web via its WebTopicList page "
            "(anonymous for public webs) — browse a web before fetching pages "
            "with twiki_get_page; for search use cmssearch_search."
        ),
    )
    async def twiki_list_topics(
        web: Annotated[
            str,
            Field(description="TWiki web name (case-sensitive), e.g. CMSPublic, Main"),
        ],
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
    ) -> ToolEnvelope:
        """Twiki web topic list."""
        return await guarded(auth, lambda: twiki.list_topics(http, web=web, limit=limit))

    @server.tool(
        name="twiki_download_attachment",
        description=(
            "Download one TWiki attachment (/twiki/pub/... URL) into local "
            "CMcpS storage; take URLs from the attachments reported by "
            "twiki_get_page."
        ),
    )
    async def twiki_download_attachment(attachment_url: str) -> ToolEnvelope:
        """Bounded attachment download."""
        return await guarded(
            auth, lambda: twiki.download_attachment(http, attachment_url=attachment_url)
        )
