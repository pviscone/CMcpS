"""MCP tool registration for the CERN Admin e-guide adapter."""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import eguide
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded

_SITE_DOC = (
    "The CERN Admin e-guide (admin-eguide.web.cern.ch) documents CERN's "
    "administrative procedures (access cards, visas, contracts, taxation, "
    "departure formalities, ...). Anonymous visitors only see the few public "
    "procedures; search, the full A-Z index and all internal procedures "
    "require CERN SSO (cern_auth_sign_in). Content exists in English (en) "
    "and French (fr)."
)


def register_eguide_tools(
    server: MCPServer,
    _settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only Admin e-guide tools."""

    @server.tool(
        name="eguide_search",
        description=(
            "Full-text search over CERN Admin e-guide procedures, FAQs and "
            "content pages (Drupal core search, SSO required) — the entry "
            "point for 'how do I do X administratively at CERN' questions; "
            "returns title, URL, language, kind (procedure/faq/content), "
            "slug and snippet; follow procedures up with eguide_get_procedure. "
            "Results mix English and French pages; page is the 0-based Drupal "
            "pager index. " + _SITE_DOC
        ),
    )
    async def eguide_search(
        query: str,
        lang: Literal["en", "fr"] = "en",
        page: Annotated[int, Field(ge=0)] = 0,
    ) -> ToolEnvelope:
        """E-guide keyword search."""
        return await guarded(auth, lambda: eguide.search(http, query=query, lang=lang, page=page))

    @server.tool(
        name="eguide_list_procedures",
        description=(
            "Full A-Z index of Admin e-guide procedures with optional FAQ "
            "companion links (SSO required; anonymous visitors get the "
            "Keycloak login). title_contains is a client-side filter. Use "
            "eguide_search for keyword lookup. " + _SITE_DOC
        ),
    )
    async def eguide_list_procedures(
        lang: Literal["en", "fr"] = "en",
        title_contains: str | None = None,
    ) -> ToolEnvelope:
        """A-Z procedure index."""
        return await guarded(
            auth, lambda: eguide.list_procedures(http, lang=lang, title_contains=title_contains)
        )

    @server.tool(
        name="eguide_get_procedure",
        description=(
            "One Admin e-guide procedure page by slug (e.g. cern-access-card "
            "from eguide_search/eguide_list_procedures): title, access "
            "(public/internal), creation and last-update dates, approval "
            "process, General/Procedure sections as plain text, revision "
            "history table, contact units, and sidebar link blocks "
            "(reference documents, useful documents, other procedures). " + _SITE_DOC
        ),
    )
    async def eguide_get_procedure(
        slug: str,
        lang: Literal["en", "fr"] = "en",
    ) -> ToolEnvelope:
        """Procedure detail page."""
        return await guarded(auth, lambda: eguide.get_procedure(http, slug=slug, lang=lang))

    @server.tool(
        name="eguide_download_file",
        description=(
            "Download one file hosted on admin-eguide.web.cern.ch into local "
            "CMcpS storage; pass a URL from eguide_get_procedure "
            "related_links. Files hosted elsewhere (e.g. cds.cern.ch) must go "
            "through that service's tools (cds_download_file). " + _SITE_DOC
        ),
    )
    async def eguide_download_file(download_url: str) -> ToolEnvelope:
        """Bounded file download."""
        return await guarded(auth, lambda: eguide.download_file(http, download_url=download_url))
