"""MCP tool registration for the CMS HyperNews archive adapter."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import hypernews
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded

_DISCONTINUED = (
    "IMPORTANT: CMS HyperNews was DISCONTINUED at the end of 2022 in favor of "
    "CMS Talk (Discourse, cms-talk.web.cern.ch); 2022 was the overlap year "
    "when both systems ran. HyperNews therefore only contains pre-2023 "
    "discussion and is a read-only archive — for 2023+ CMS discussion search "
    "the discourse_* tools with site='cms-talk' instead. "
)


def register_hypernews_tools(
    server: MCPServer,
    _settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only hypernews.cern.ch archive tools."""

    @server.tool(
        name="hypernews_search",
        description=(
            "Full-text search over the CMS HyperNews archive "
            "(hypernews.cern.ch, SSO required): message titles, forums, "
            "posting dates, author usernames and snippets; follow a result "
            "up with hypernews_get_message using its forum + message. The "
            "backend is SQLite FTS5 (quoted phrases and AND/OR/NOT work), "
            "50 results per page; narrow slow queries with start/stop "
            "(YYYY-MM-DD, clamped to the archive range 2000-01-01..2022-12-31). " + _DISCONTINUED
        ),
    )
    async def hypernews_search(
        query: Annotated[
            str, Field(description="FTS5 query, e.g. 'laser' or '\"tracker alignment\"'")
        ],
        *,
        start: Annotated[str | None, Field(description="ISO date YYYY-MM-DD lower bound")] = None,
        stop: Annotated[str | None, Field(description="ISO date YYYY-MM-DD upper bound")] = None,
        page: Annotated[int, Field(ge=1)] = 1,
    ) -> ToolEnvelope:
        """HyperNews full-text search."""
        return await guarded(
            auth,
            lambda: hypernews.search_messages(http, query=query, start=start, stop=stop, page=page),
        )

    @server.tool(
        name="hypernews_list_forums",
        description=(
            "List CMS HyperNews forums sorted by last activity (~2900 forums), "
            "with optional client-side filters: category (case-insensitive "
            "substring of the category name, e.g. 'ECAL') and title_contains. "
            "Returns forum name (for hypernews_get_forum / hypernews_get_message), "
            "title, last-activity date, category and URL. SSO required. " + _DISCONTINUED
        ),
    )
    async def hypernews_list_forums(
        *,
        category: Annotated[
            str | None, Field(description="Case-insensitive category substring, e.g. 'Calibration'")
        ] = None,
        title_contains: Annotated[
            str | None, Field(description="Case-insensitive forum title substring")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=3000)] = 100,
    ) -> ToolEnvelope:
        """HyperNews forum listing."""
        return await guarded(
            auth,
            lambda: hypernews.list_forums(
                http, category=category, title_contains=title_contains, limit=limit
            ),
        )

    @server.tool(
        name="hypernews_get_forum",
        description=(
            "List the top-level threads of one CMS HyperNews forum by forum "
            "name (e.g. 'hnTest', values come from hypernews_list_forums or "
            "hypernews_search): per thread the message number (for "
            "hypernews_get_message), title, author, date and reply count. "
            "Threads come newest-first, bounded by limit (default 100; busy "
            "forums reach ~2000 threads) — thread_count always reports the "
            "full count. SSO required; a nonexistent forum surfaces as an "
            "upstream 500. " + _DISCONTINUED
        ),
    )
    async def hypernews_get_forum(
        forum: Annotated[str, Field(description="Forum name, e.g. 'hnTest'")],
        limit: Annotated[int, Field(ge=1, le=3000)] = 100,
    ) -> ToolEnvelope:
        """HyperNews forum thread listing."""
        return await guarded(auth, lambda: hypernews.get_forum(http, forum=forum, limit=limit))

    @server.tool(
        name="hypernews_get_message",
        description=(
            "Read one archived CMS HyperNews message by forum + message path "
            "(e.g. forum 'hnTest', path '1' or '788/2' from hypernews_search / "
            "hypernews_get_forum): title, date, author name and username "
            "(resolve with hypernews_get_member), plain-text body, direct "
            "replies, attachment URLs (for hypernews_download_file) and "
            "next-in-thread/next-in-forum flags. SSO required. " + _DISCONTINUED
        ),
    )
    async def hypernews_get_message(
        forum: Annotated[str, Field(description="Forum name, e.g. 'hnTest'")],
        path: Annotated[
            str, Field(description="Slash-separated message path, e.g. '1' or '788/2'")
        ],
    ) -> ToolEnvelope:
        """HyperNews message detail."""
        return await guarded(auth, lambda: hypernews.get_message(http, forum=forum, path=path))

    @server.tool(
        name="hypernews_get_member",
        description=(
            "Profile of one CMS HyperNews member by exact username (copied "
            "from hypernews_search results or hypernews_get_message authors, "
            "e.g. 'elmer'): display name, email, homepage URL, status and all "
            "remaining profile fields. Usernames are case-sensitive. SSO "
            "required. " + _DISCONTINUED
        ),
    )
    async def hypernews_get_member(
        username: Annotated[str, Field(description="Exact HyperNews username, e.g. 'elmer'")],
    ) -> ToolEnvelope:
        """HyperNews member profile."""
        return await guarded(auth, lambda: hypernews.get_member(http, username=username))

    @server.tool(
        name="hypernews_download_file",
        description=(
            "Download one CMS HyperNews attachment (a /HyperNews/CMS/get/AUX/… "
            "URL from hypernews_get_message) into local CMcpS storage. SSO "
            "required. " + _DISCONTINUED
        ),
    )
    async def hypernews_download_file(
        download_url: Annotated[
            str,
            Field(
                description="Attachment URL like https://hypernews.cern.ch/HyperNews/CMS/get/AUX/…"
            ),
        ],
    ) -> ToolEnvelope:
        """HyperNews attachment download."""
        return await guarded(auth, lambda: hypernews.download_file(http, download_url=download_url))
