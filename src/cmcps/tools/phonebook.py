"""MCP tool registration for the CERN Phonebook adapter."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import phonebook
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded

_AUTH_DOC = (
    " Requires the stored CERN SSO session (the phonebook Keycloak Bearer "
    "token is minted automatically from it on first use); set "
    "CMCPS_PHONEBOOK_TOKEN as a manual fallback."
)


def register_phonebook_tools(
    server: MCPServer,
    settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only CERN Phonebook tools."""

    @server.tool(
        name="phonebook_search",
        description=(
            "Search the CERN Phonebook (directory.web.cern.ch backends "
            "phonebook.cern.ch) — 'who is X', phone number, office location, "
            "email and organic unit of a CERN person, or the email of a CERN "
            "service (type PERSON or SERVICE). Returns name, first/last name, "
            "email, organicUnit, office (location), landlines phone1/phone2, "
            "mobilePhone ({displayNumber, fullNumber}), postBox, and "
            "preferredLanguage per hit, paged 10/page. NOT for presence/"
            "membership status (use glance_membership_search_members for CMS)." + _AUTH_DOC
        ),
    )
    async def phonebook_search(
        query: Annotated[
            str,
            Field(
                description="Name, email fragment, or service name, e.g. "
                "'John Doe', 'doebert', 'Security Service'."
            ),
        ],
        page: Annotated[
            int,
            Field(description="1-based result page (10 hits per page).", ge=1),
        ] = 1,
    ) -> ToolEnvelope:
        """People/services search."""
        return await guarded(auth, lambda: phonebook.search(http, settings, query=query, page=page))

    @server.tool(
        name="phonebook_last_update",
        description=(
            "Last-update timestamp of the CERN Phonebook directory database — "
            "how fresh a phonebook_search result is. " + _AUTH_DOC
        ),
    )
    async def phonebook_last_update() -> ToolEnvelope:
        """Directory database freshness."""
        return await guarded(auth, lambda: phonebook.last_update(http, settings))
