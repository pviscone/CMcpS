"""MCP tool registration for the CMS Search adapter."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import cmssearch
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded


def register_cmssearch_tools(
    server: MCPServer,
    _settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only CMS Search tools."""

    @server.tool(
        name="cmssearch_search",
        description=(
            "Unified full-text search over ~20 CMS/CERN indexed documentation "
            "sources (cms-search.web.cern.ch, SSO required) — batch "
            "documentation answers; returns title, URL, snippet, score, date, "
            "tags. For a single MkDocs site use mkdocs_search; for forums use "
            "discourse_search. TWiki content MUST be searched here via "
            "indices 'cms-twiki' (private web) and 'cms-public-twiki' (public "
            "web) — there is no separate twiki_search tool; fetch pages with "
            "twiki_get_page. Optional sort/rank passthrough, date range, and "
            "repeated indices filter. Offset pagination via offset+size."
        ),
    )
    async def cmssearch_search(  # noqa: PLR0917
        query: str,
        size: Annotated[int, Field(ge=1, le=50)] = 10,
        offset: Annotated[int, Field(ge=0)] = 0,
        sort: str | None = None,
        rank: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        indices: list[str] | None = None,
    ) -> ToolEnvelope:
        """CMS Search unified search."""
        return await guarded(
            auth,
            lambda: cmssearch.search(
                http,
                query=query,
                size=size,
                offset=offset,
                sort=sort,
                rank=rank,
                date_from=date_from,
                date_to=date_to,
                indices=indices,
            ),
        )

    @server.tool(
        name="cmssearch_get_document",
        description=(
            "Full content of one indexed CMS Search document by source_key "
            "and document_id (both from cmssearch_search results); follow "
            "cmssearch_search. Optional offset/limit page through long "
            "content. KNOWN LIMITATION: the backend answers 401 "
            "'Invalid internal service credential' for every source and "
            "session — fetch the hit's url with the matching source tool "
            "instead (twiki_get_page, mkdocs_get_page, discourse_get_topic, "
            "...)."
        ),
    )
    async def cmssearch_get_document(
        source_key: str,
        document_id: str,
        offset: Annotated[int, Field(ge=0)] | None = None,
        limit: Annotated[int, Field(ge=1)] | None = None,
    ) -> ToolEnvelope:
        """CMS Search document detail."""
        return await guarded(
            auth,
            lambda: cmssearch.get_document(
                http,
                source_key=source_key,
                document_id=document_id,
                offset=offset,
                limit=limit,
            ),
        )

    @server.tool(
        name="cmssearch_list_sources",
        description=(
            "List the documentation sources indexed by CMS Search: "
            "source_key, index_name, tags, URL — get the source_key/indices "
            "values accepted by cmssearch_search. SSO required."
        ),
    )
    async def cmssearch_list_sources() -> ToolEnvelope:
        """CMS Search source inventory."""
        return await guarded(auth, lambda: cmssearch.list_sources(http))

    @server.tool(
        name="cmssearch_index_stats",
        description=(
            "Index sizes, last-update timestamps, and latest document dates "
            "per CMS Search source (source_keys from cmssearch_list_sources); "
            "optional source_key filter. SSO required."
        ),
    )
    async def cmssearch_index_stats(source_key: str | None = None) -> ToolEnvelope:
        """CMS Search index stats."""
        return await guarded(auth, lambda: cmssearch.index_stats(http, source_key=source_key))
