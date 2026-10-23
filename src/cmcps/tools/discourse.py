"""MCP tool registration for the CERN Discourse forums adapter."""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import discourse
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded

Site = Literal["cms-talk", "cms-pub-talk", "root-forum", "geant4-forum"]

_SITE_DOC = (
    "site is one of: cms-talk (cms-talk.web.cern.ch, CMS internal, SSO), "
    "cms-pub-talk (cms-pub-talk.web.cern.ch, CMS public announcements, SSO), "
    "root-forum (root-forum.cern.ch, public), geant4-forum "
    "(geant4-forum.web.cern.ch, public)."
)


def register_discourse_tools(
    server: MCPServer,
    _settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only Discourse forum tools."""

    @server.tool(
        name="discourse_search",
        description=(
            "Full-text search across posts and topics of a CERN Discourse forum. " + _SITE_DOC
        ),
    )
    async def discourse_search(
        site: Site,
        query: str,
        page: Annotated[int, Field(ge=1)] = 1,
    ) -> ToolEnvelope:
        """Forum post/topic search."""
        return await guarded(
            auth,
            lambda: discourse.search(http, site=site, query=query, page=page),
        )

    @server.tool(
        name="discourse_list_topics",
        description=(
            "Paged topic listing of a forum: latest or top (with period), or "
            "the topics of one category via category_id (see "
            "discourse_list_categories). " + _SITE_DOC
        ),
    )
    async def discourse_list_topics(
        site: Site,
        listing: Literal["latest", "top"] = "latest",
        period: Literal["all", "yearly", "quarterly", "monthly", "weekly", "daily"] | None = None,
        category_id: int | None = None,
        page: Annotated[int, Field(ge=1)] = 1,
    ) -> ToolEnvelope:
        """Latest/top/category topic listing."""
        return await guarded(
            auth,
            lambda: discourse.list_topics(
                http,
                site=site,
                listing=listing,
                period=period or "monthly",
                category_id=category_id,
                page=page,
            ),
        )

    @server.tool(
        name="discourse_list_categories",
        description=(
            "Category tree of one forum with topic/post counts; use the "
            "category_id values with discourse_list_topics. " + _SITE_DOC
        ),
    )
    async def discourse_list_categories(site: Site) -> ToolEnvelope:
        """Category listing."""
        return await guarded(auth, lambda: discourse.list_categories(http, site=site))

    @server.tool(
        name="discourse_get_topic",
        description=(
            "Topic detail (title, tags, counts) plus one page of posts as "
            "plain text (HTML stripped). Posts arrive in chunks of ~20; "
            "advance with page while has_more is true. Post attachments are "
            "listed per post as uploads. " + _SITE_DOC
        ),
    )
    async def discourse_get_topic(
        site: Site,
        topic_id: Annotated[int, Field(ge=1)],
        page: Annotated[int, Field(ge=1)] = 1,
    ) -> ToolEnvelope:
        """Topic detail with rendered posts."""
        return await guarded(
            auth,
            lambda: discourse.get_topic(http, site=site, topic_id=topic_id, page=page),
        )

    @server.tool(
        name="discourse_get_post",
        description=(
            "Single post by id: plain-text body, metadata, uploads, and the "
            "raw markdown source when the forum exposes it publicly. " + _SITE_DOC
        ),
    )
    async def discourse_get_post(
        site: Site,
        post_id: Annotated[int, Field(ge=1)],
    ) -> ToolEnvelope:
        """Single post detail."""
        return await guarded(auth, lambda: discourse.get_post(http, site=site, post_id=post_id))

    @server.tool(
        name="discourse_get_user",
        description=(
            "Public user profile summary of one forum (post counts, trust level, bio). " + _SITE_DOC
        ),
    )
    async def discourse_get_user(site: Site, username: str) -> ToolEnvelope:
        """User profile summary."""
        return await guarded(auth, lambda: discourse.get_user(http, site=site, username=username))

    @server.tool(
        name="discourse_download_file",
        description=(
            "Download one attachment/image hosted on the forum into local "
            "CMcpS download storage; pass a URL from a post's uploads list. " + _SITE_DOC
        ),
    )
    async def discourse_download_file(site: Site, download_url: str) -> ToolEnvelope:
        """Bounded attachment download."""
        return await guarded(
            auth,
            lambda: discourse.download_file(http, site=site, download_url=download_url),
        )
