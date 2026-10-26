"""MCP tool registration for the CERN website inventory adapter."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from cmcps.adapters import websites
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded

_AUTH_DOC = (
    " Requires CERN SSO: the portal Bearer token is minted headlessly from the "
    "stored SSO session during each inventory refresh (manual fallback: "
    "CMCPS_WEBSITES_TOKEN). The crt.sh source is public."
)
_CACHE_DOC = (
    " The merged inventory is cached in a local SQLite store and refreshed "
    "automatically when older than CMCPS_WEBSITES_CACHE_MAX_AGE_DAYS "
    "(default 30); websites_refresh forces it."
)
_SOURCES_DOC = (
    "Sources: the Web Services portal inventory (webservices-portal.web.cern.ch; "
    "all centrally hosted webeos/paas/appCatalogue/drupal/wsdb sites with owner, "
    "category and description) merged with *.cern.ch names from crt.sh "
    "certificate-transparency logs (self-managed hosts; no metadata)."
)


def register_websites_tools(
    server: MCPServer,
    settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only CERN website inventory tools."""

    @server.tool(
        name="websites_search",
        description=(
            "Ranked full-text search over the CERN hosted-website inventory — "
            "'which website covers X', 'find the site named Y'. Prefix matching "
            "over hostname, project name, description and owner; results ranked. "
            "Structured filters: environment (webeos/paas/appCatalogue/drupal/wsdb), "
            "category (Official/Personal/Test), display_type (e.g. DrupalSite, "
            "GitlabPagesSite, PaasProject), owner (CERN login). Empty query lists "
            "filtered rows. " + _SOURCES_DOC + _CACHE_DOC + _AUTH_DOC
        ),
    )
    async def websites_search(
        *,
        query: str | None = None,
        environment: str | None = None,
        category: str | None = None,
        display_type: str | None = None,
        owner: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> ToolEnvelope:
        """Website inventory search."""
        return await guarded(
            auth,
            lambda: websites.search(
                http,
                settings,
                query=query,
                environment=environment,
                category=category,
                display_type=display_type,
                owner=owner,
                limit=limit,
                offset=offset,
            ),
        )

    @server.tool(
        name="websites_lookup",
        description=(
            "Exact record(s) for one hosted website hostname (e.g. "
            "atlas.cern.ch): owner, category, environment, description, source. "
            "For fuzzy discovery use websites_search. " + _SOURCES_DOC + _CACHE_DOC + _AUTH_DOC
        ),
    )
    async def websites_lookup(hostname: str) -> ToolEnvelope:
        """Exact website record."""
        return await guarded(auth, lambda: websites.lookup(http, settings, hostname=hostname))

    @server.tool(
        name="websites_refresh",
        description=(
            "Start a background refetch of the website inventory (portal + "
            "crt.sh) rebuilding the local cache; returns immediately (a full "
            "refresh takes ~15-30 s, longer than typical MCP client timeouts). "
            "Idempotent: a running refresh is reported, not duplicated. Poll "
            "websites_status (refresh_running / refresh_error / last_refresh) "
            "for the outcome. Triggered implicitly when websites_search/"
            "websites_lookup see a cache older than "
            "CMCPS_WEBSITES_CACHE_MAX_AGE_DAYS (stale results are served while "
            "it runs). " + _SOURCES_DOC + _AUTH_DOC
        ),
    )
    async def websites_refresh() -> ToolEnvelope:
        """Forced inventory refresh."""
        return await guarded(auth, lambda: websites.refresh(http, settings))

    @server.tool(
        name="websites_status",
        description=(
            "Cache metadata of the website inventory: fetched-at timestamp, "
            "age vs CMCPS_WEBSITES_CACHE_MAX_AGE_DAYS staleness flag, per-source "
            "site counts, FTS availability. Never triggers a fetch. " + _CACHE_DOC
        ),
    )
    async def websites_status() -> ToolEnvelope:
        """Cache status."""
        return await guarded(auth, lambda: websites.status(settings))
