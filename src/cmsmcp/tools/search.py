"""MCP registration for the unified ``cern_search`` tool."""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmsmcp.adapters.search import DEFAULT_SOURCES, SOURCES, unified_search
from cmsmcp.auth.service import AuthService
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.results import ToolEnvelope
from cmsmcp.net.client import SafeHttpClient
from cmsmcp.policy import Policy
from cmsmcp.tools.common import guarded

SourceName = Literal[
    "indico",
    "docs",
    "gitlab",
    "mattermost",
    "cms-talk",
    "jira",
    "glance",
    "inspire",
    "cds",
    "docdb",
    "servicenow",
    "gocms",
    "hypernews",
    "cms-pub-talk",
    "root-forum",
]

_GUIDE = "; ".join(f"{s.name}={s.guide}" for s in SOURCES)


def register_search_tools(
    server: MCPServer,
    settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
    policy: Policy,
) -> None:
    """Register ``cern_search``."""

    @server.tool(
        name="cern_search",
        description=(
            "START HERE for 'find anything about X': searches many CERN/CMS sources in "
            "parallel and returns one short merged list ([source] title — url (date) + "
            "snippet), `limit` hits per source. Then open a hit with the source's own "
            f"tools. Default sources: {', '.join(DEFAULT_SOURCES)}. All sources: {_GUIDE}. "
            "Short keyword queries (2-5 words) work best."
        ),
    )
    async def cern_search(
        query: str,
        sources: Annotated[
            list[SourceName] | None,
            Field(description="Subset to search; omit for the defaults."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=20, description="Hits per source.")] = 5,
    ) -> ToolEnvelope:
        """Unified multi-source search."""
        return await guarded(
            auth,
            lambda: unified_search(
                http, settings, policy, query=query, sources=list(sources or []), limit=limit
            ),
        )
