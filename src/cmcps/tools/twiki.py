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
            "Full-text search of CERN TWiki webs, by default both CMS "
            "(private collaboration web) and CMSPublic in one request — the "
            "legacy CMS internal wiki for documentation pages not yet "
            "migrated elsewhere. NOT for community Q&A (discourse_search), "
            "CMS batch docs (cmssearch_search), or MkDocs sites "
            "(mkdocs_search). search_type is keyword (default), literal, or "
            "regex. Requires CERN SSO beyond anonymous view pages."
        ),
    )
    async def twiki_search(
        query: str,
        search_type: Annotated[
            Literal["keyword", "literal", "regex"],
            Field(
                description=(
                    "keyword (default) requires all terms in any order; literal "
                    "matches the string exactly; regex treats query as a TWiki "
                    "regular expression"
                )
            ),
        ] = "keyword",
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        web: Annotated[
            str,
            Field(
                description=(
                    "TWiki web(s) to search (case-sensitive). Defaults to "
                    "'CMS,CMSPublic' — private CMS web plus public web, SSO "
                    "required for the private web. Pass a single web (e.g. "
                    "'CMSPublic' for public docs only) or a comma-separated "
                    "list, e.g. 'CMS,Main'."
                )
            ),
        ] = "CMS,CMSPublic",
    ) -> ToolEnvelope:
        """Twiki web search."""
        return await guarded(
            auth,
            lambda: twiki.search(http, web=web, query=query, search_type=search_type, limit=limit),
        )

    @server.tool(
        name="twiki_get_page",
        description=(
            "Fetch one TWiki page by web/topic naming (e.g. web='CMSPublic', "
            "topic='PhysicsResultsHIG'); follow twiki_search or "
            "twiki_list_topics. raw=true (default) returns the wiki source "
            "text with parsed metadata (author, revision, date, attachments) "
            "and requires SSO; raw=false returns the anonymous rendered page "
            "as plain text."
        ),
    )
    async def twiki_get_page(
        web: Annotated[
            str,
            Field(
                description=(
                    "TWiki web (case-sensitive) — take it from the twiki_search hit's "
                    "web field (CMS = private, CMSPublic = public), e.g. CMSPublic, Main"
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
            "(anonymous for public webs) — browse a web before searching with "
            "twiki_search."
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
