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
    "site is one of: cms-talk (cms-talk.web.cern.ch, internal CMS discussion, "
    "SSO), cms-pub-talk (cms-pub-talk.web.cern.ch, internal CMS publication-review "
    "discussion — one category per publication, NOT public announcements; SSO), "
    "root-forum (root-forum.cern.ch, ROOT software help, anonymously readable), "
    "geant4-forum (geant4-forum.web.cern.ch, Geant4 help, anonymously readable)."
)

SiteParam = Annotated[
    Site,
    Field(
        description=(
            "Forum to query: cms-talk = internal CMS discussion (SSO); "
            "cms-pub-talk = internal CMS publication-review discussion, one category "
            "per publication (SSO, NOT public announcements); root-forum = ROOT "
            "software help (anonymously readable); geant4-forum = Geant4 help "
            "(anonymously readable)."
        )
    ),
]


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
            "Full-text search across posts and topics of a CERN Discourse forum. "
            "Use it for community Q&A: tutorials, how-to questions, bug help, "
            "best-practice and discussion threads. It is NOT for published "
            "papers (inspire_search_papers), internal unpublished CMS analyses "
            "(glance_search_analyses), official CMS public results pages "
            "(cmsresults_*), or wiki documentation (cmssearch_search indices "
            "'cms-twiki'/'cms-public-twiki', mkdocs_search). " + _SITE_DOC
        ),
    )
    async def discourse_search(
        site: SiteParam,
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
            "Browse one forum instead of searching: paged listing of latest or "
            "top (with period) topics, or the topics of one category via "
            "category_id (see discourse_list_categories). Use it to see what a "
            "forum currently discusses; for a specific term use discourse_search. " + _SITE_DOC
        ),
    )
    async def discourse_list_topics(
        site: SiteParam,
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
            "Category tree of one forum with topic/post counts; call before "
            "discourse_list_topics to get the category_id values of one forum. " + _SITE_DOC
        ),
    )
    async def discourse_list_categories(site: SiteParam) -> ToolEnvelope:
        """Category listing."""
        return await guarded(auth, lambda: discourse.list_categories(http, site=site))

    @server.tool(
        name="discourse_get_topic",
        description=(
            "Read one forum topic (title, tags, counts) plus one page of posts "
            "as plain text (HTML stripped); follow discourse_search or "
            "discourse_list_topics with a topic_id they return. Posts arrive in "
            "chunks of ~20; advance with page while has_more is true. Post "
            "attachments are listed per post as uploads. " + _SITE_DOC
        ),
    )
    async def discourse_get_topic(
        site: SiteParam,
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
            "Read one forum post by id: plain-text body, metadata, uploads, "
            "and the raw markdown source when the forum exposes it publicly; "
            "typically follows discourse_get_topic. " + _SITE_DOC
        ),
    )
    async def discourse_get_post(
        site: SiteParam,
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
    async def discourse_get_user(site: SiteParam, username: str) -> ToolEnvelope:
        """User profile summary."""
        return await guarded(auth, lambda: discourse.get_user(http, site=site, username=username))

    @server.tool(
        name="discourse_download_file",
        description=(
            "Download one attachment/image hosted on the forum into local "
            "CMcpS download storage; pass an uploads URL returned by "
            "discourse_get_topic or discourse_get_post. " + _SITE_DOC
        ),
    )
    async def discourse_download_file(site: SiteParam, download_url: str) -> ToolEnvelope:
        """Bounded attachment download."""
        return await guarded(
            auth,
            lambda: discourse.download_file(http, site=site, download_url=download_url),
        )
