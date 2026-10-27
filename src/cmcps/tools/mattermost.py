"""MCP tool registration for the CERN Mattermost adapter."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import mattermost
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded

_ID_DOC = (
    "Mattermost ids are 26 lowercase-alphanumeric strings; get them from "
    "mattermost_list_teams, mattermost_list_channels, mattermost_search_posts, "
    "or mattermost_get_posts."
)

_PER_PAGE = Annotated[int, Field(ge=1, le=200, description="Items per page (max 200).")]
_PAGE = Annotated[int, Field(ge=1, description="1-based page number.")]


def register_mattermost_tools(
    server: MCPServer,
    settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only Mattermost tools."""

    @server.tool(
        name="mattermost_get_me",
        description=(
            "The Mattermost user the current SSO/PAT session authenticates as "
            "(mattermost.web.cern.ch) — confirms which account the other "
            "mattermost_* tools act as. SSO session required: sign in with "
            "cern_auth_sign_in first (the MMAUTHTOKEN cookie is minted "
            "automatically); a personal access token in CMCPS_MATTERMOST_TOKEN "
            "is an optional fallback."
        ),
    )
    async def mattermost_get_me() -> ToolEnvelope:
        """Current user probe."""
        return await guarded(auth, lambda: mattermost.get_me(http, settings))

    @server.tool(
        name="mattermost_get_user",
        description=(
            "Public Mattermost user profile by exact username or by user_id; "
            "follow mattermost_search_posts/mattermost_get_posts, which return "
            "user_id values (privacy config may hide the email field). " + _ID_DOC
        ),
    )
    async def mattermost_get_user(
        username: Annotated[str | None, Field(description="Exact Mattermost username.")] = None,
        user_id: Annotated[str | None, Field(description="26-char user id.")] = None,
    ) -> ToolEnvelope:
        """User profile by username or id."""
        return await guarded(
            auth,
            lambda: mattermost.get_user(http, settings, username=username, user_id=user_id),
        )

    @server.tool(
        name="mattermost_list_teams",
        description=(
            "List the Mattermost teams the current user is a member of, with "
            "team_id, URL name, display name, and type — the entry point for "
            "every other mattermost_* tool (they all take team_id or ids "
            "derived from it). The server returns the full list in one call "
            "(no paging on the CERN fork)."
        ),
    )
    async def mattermost_list_teams() -> ToolEnvelope:
        """Team listing."""
        return await guarded(auth, lambda: mattermost.list_teams(http, settings))

    @server.tool(
        name="mattermost_list_channels",
        description=(
            "List the channels of one Mattermost team the current user is in: "
            "channel_id, URL name, display name, type (O public, P private), "
            "purpose/header, last post time, message count, and web URL. "
            "name_contains filters by substring of name/display_name "
            "(case-insensitive). Note: the CERN endpoint returns the "
            "account-wide channel list unpaged; direct/group conversations "
            "(D/G) are filtered out client-side, so the full team channel "
            "list is already in the result. " + _ID_DOC
        ),
    )
    async def mattermost_list_channels(
        team_id: str,
        name_contains: Annotated[
            str | None, Field(description="Case-insensitive substring of name/display_name.")
        ] = None,
    ) -> ToolEnvelope:
        """Channel listing of one team."""
        return await guarded(
            auth,
            lambda: mattermost.list_channels(
                http,
                settings,
                team_id=team_id,
                name_contains=name_contains,
            ),
        )

    @server.tool(
        name="mattermost_get_channel",
        description=(
            "One Mattermost channel's detail by channel_id alone, or by "
            "team_id + channel_name (the exact URL name from "
            "mattermost_list_channels). " + _ID_DOC
        ),
    )
    async def mattermost_get_channel(
        channel_id: Annotated[str | None, Field(description="26-char channel id.")] = None,
        team_id: Annotated[str | None, Field(description="26-char team id.")] = None,
        channel_name: Annotated[
            str | None, Field(description="Exact URL name of the channel.")
        ] = None,
    ) -> ToolEnvelope:
        """Channel detail."""
        return await guarded(
            auth,
            lambda: mattermost.get_channel(
                http,
                settings,
                channel_id=channel_id,
                team_id=team_id,
                channel_name=channel_name,
            ),
        )

    @server.tool(
        name="mattermost_search_posts",
        description=(
            "Full-text search of Mattermost posts within one team (SSO "
            "required). Use it for community Q&A: troubleshooting, how-to "
            "answers, release/incident discussion. terms supports the "
            "Mattermost filters from:, in:, before:, after:, on:; the default "
            "matches all given words — set is_or_search=true to match any. "
            "Results are newest-first posts with message text, permalink URL, "
            "user_id (follow with mattermost_get_user) and file_ids (follow "
            "mattermost_download_file). The backend exposes no hit "
            "count and clamps results to ~100 hits with its own page size; "
            "advance page until an empty/short page. "
            "NOT for published papers (inspire_search_papers) or wiki docs "
            "(cmssearch_search). " + _ID_DOC
        ),
    )
    async def mattermost_search_posts(
        team_id: str,
        terms: str,
        is_or_search: bool = False,
        page: _PAGE = 1,
    ) -> ToolEnvelope:
        """Post search within a team."""
        return await guarded(
            auth,
            lambda: mattermost.search_posts(
                http,
                settings,
                team_id=team_id,
                terms=terms,
                is_or_search=is_or_search,
                page=page,
            ),
        )

    @server.tool(
        name="mattermost_get_posts",
        description=(
            "Read the history of one Mattermost channel: one page of posts, "
            "newest first, as plain text with author user_id, timestamps, "
            "reply_count, hashtags and file_ids. Page with page, or with "
            "post-id cursors: before=<id of the last post of this page> for "
            "the next older page (has_more tells whether one exists), "
            "after=<id of the first post> for newer history; "
            "prev_post_id/next_post_id are returned as the boundary "
            "indicators. Root posts with replies are read in full via "
            "mattermost_get_thread. " + _ID_DOC
        ),
    )
    async def mattermost_get_posts(
        channel_id: str,
        page: _PAGE = 1,
        per_page: _PER_PAGE = 60,
        before: Annotated[str | None, Field(description="Only posts before this post id.")] = None,
        after: Annotated[str | None, Field(description="Only posts after this post id.")] = None,
    ) -> ToolEnvelope:
        """Channel post history."""
        return await guarded(
            auth,
            lambda: mattermost.get_posts(
                http,
                settings,
                channel_id=channel_id,
                page=page,
                per_page=per_page,
                before=before,
                after=after,
            ),
        )

    @server.tool(
        name="mattermost_get_thread",
        description=(
            "Read one Mattermost post with every reply in its thread, oldest "
            "first; typically follows mattermost_search_posts or "
            "mattermost_get_posts when a root post carries reply_count > 0. " + _ID_DOC
        ),
    )
    async def mattermost_get_thread(post_id: str) -> ToolEnvelope:
        """Thread detail."""
        return await guarded(auth, lambda: mattermost.get_thread(http, settings, post_id=post_id))

    @server.tool(
        name="mattermost_download_file",
        description=(
            "Download one Mattermost file attachment into local CMcpS "
            "download storage (size-bounded); pass a file_id from post "
            "results (mattermost_search_posts / mattermost_get_posts). The "
            "server-side file metadata (name, mime type, extension) is "
            "returned alongside the local path. " + _ID_DOC
        ),
    )
    async def mattermost_download_file(file_id: str) -> ToolEnvelope:
        """Bounded file download."""
        return await guarded(
            auth, lambda: mattermost.download_file(http, settings, file_id=file_id)
        )
