"""MCP tool registration for the Indico adapter."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import indico
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded


def register_indico_tools(
    server: MCPServer,
    settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only indico.cern.ch tools."""

    @server.tool(
        name="indico_get_user_info",
        description=(
            "Return the authenticated indico.cern.ch user profile. Requires "
            "CMCPS_INDICO_API_TOKEN and a completed cern_auth_sign_in session."
        ),
    )
    async def indico_get_user_info() -> ToolEnvelope:
        """Indico user profile for the configured API token."""
        return await guarded(auth, lambda: indico.get_user_info(http, settings))

    @server.tool(
        name="indico_search_events",
        description=(
            "Search events in an Indico category (legacy export API). Use "
            "category_id=0 for the root category. Dates use YYYY-MM-DD."
        ),
    )
    async def indico_search_events(
        *,
        category_id: str = "0",
        from_date: str | None = None,
        to_date: str | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        offset: Annotated[int, Field(ge=0)] = 0,
        only_public: bool = False,
    ) -> ToolEnvelope:
        """List events of an Indico category."""
        return await guarded(
            auth,
            lambda: indico.search_events(
                http,
                settings,
                category_id=category_id,
                from_date=from_date,
                to_date=to_date,
                limit=limit,
                offset=offset,
                only_public=only_public,
            ),
        )

    @server.tool(
        name="indico_search",
        description=(
            "Full-text search across Indico content types (categories, events, "
            "contributions, attachments, notes) via the Indico search API."
        ),
    )
    async def indico_search(
        query: str,
        limit: Annotated[int, Field(ge=1, le=50)] = 25,
    ) -> ToolEnvelope:
        """Full-text Indico search grouped by content type."""
        return await guarded(auth, lambda: indico.search(http, settings, query=query, limit=limit))

    @server.tool(
        name="indico_get_event",
        description=(
            "Fetch metadata for one indico.cern.ch event by its numeric event id, "
            "optionally including contributions and sessions."
        ),
    )
    async def indico_get_event(
        event_id: str,
        include_contributions: bool = False,
        include_sessions: bool = False,
    ) -> ToolEnvelope:
        """Indico event detail."""
        return await guarded(
            auth,
            lambda: indico.get_event(
                http,
                settings,
                event_id=event_id,
                include_contributions=include_contributions,
                include_sessions=include_sessions,
            ),
        )

    @server.tool(
        name="indico_get_event_contributions",
        description=(
            "List the contributions (talks) of an indico.cern.ch event, "
            "optionally with subcontributions."
        ),
    )
    async def indico_get_event_contributions(
        event_id: str,
        include_subcontributions: bool = False,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> ToolEnvelope:
        """Contributions of an Indico event."""
        return await guarded(
            auth,
            lambda: indico.get_event_contributions(
                http,
                settings,
                event_id=event_id,
                include_subcontributions=include_subcontributions,
                limit=limit,
            ),
        )

    @server.tool(
        name="indico_get_event_files",
        description=(
            "Enumerate all file attachments of an indico.cern.ch event and its "
            "contributions. Returns metadata and download URLs; use "
            "indico_download_file to fetch one file into local CMcpS storage."
        ),
    )
    async def indico_get_event_files(
        event_id: str,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> ToolEnvelope:
        """Files attached to an Indico event."""
        return await guarded(
            auth,
            lambda: indico.get_event_files(http, settings, event_id=event_id, limit=limit),
        )

    @server.tool(
        name="indico_download_file",
        description=(
            "Download an indico.cern.ch attachment (a download_url from "
            "indico_get_event_files) into local CMcpS download storage. "
            "Returns the local path and size; file content is not inlined."
        ),
    )
    async def indico_download_file(download_url: str) -> ToolEnvelope:
        """Bounded local download of an Indico attachment."""
        return await guarded(
            auth,
            lambda: indico.download_file(http, settings, download_url=download_url),
        )
