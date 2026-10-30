"""MCP server assembly for CMcpS."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps import __version__
from cmcps.auth.browser import (
    FETCH_DEFAULT_MAX_CHARS,
    FETCH_DEFAULT_TIMEOUT_SECONDS,
    FETCH_MAX_CHARS_LIMIT,
    FETCH_MAX_TIMEOUT_SECONDS,
)
from cmcps.auth.service import AuthService
from cmcps.config.settings import (
    MAX_SSO_TIMEOUT_SECONDS,
    MIN_SSO_TIMEOUT_SECONDS,
    Settings,
    get_settings,
)
from cmcps.contracts.errors import CMCPSError, InvalidInputError, UpstreamError
from cmcps.contracts.results import (
    ErrorCode,
    ErrorPayload,
    HealthResult,
    PageFetchResult,
    ToolEnvelope,
)
from cmcps.net.client import SafeHttpClient
from cmcps.net.safety import validate_url
from cmcps.tools import register_adapter_tools


def _failure(exc: Exception) -> ToolEnvelope:
    if isinstance(exc, CMCPSError):
        return ToolEnvelope.failure(exc.to_payload())
    return ToolEnvelope.failure(
        ErrorPayload(
            code=ErrorCode.INTERNAL_ERROR,
            message="The operation failed without exposing internal details",
            retryable=False,
        )
    )


async def _cern_fetch_page_payload(
    settings: Settings,
    auth: AuthService,
    *,
    url: str,
    wait_for: str | None,
    timeout_seconds: int,
    max_chars: int,
) -> ToolEnvelope:
    """Handler for the cern_fetch_page tool (kept out of build_server)."""
    try:
        validate_url(url, allowed_hosts=settings.allowed_hosts)
        await auth.ensure_authenticated()
        payload = await auth.fetch_page(
            url,
            wait_for=wait_for,
            timeout_seconds=timeout_seconds,
            max_chars=max_chars,
        )
        if payload is None:
            raise UpstreamError(
                "the page could not be rendered headlessly",
                instructions=(
                    "The stored SSO session may be stale, the page failed to "
                    "load, or no browser is available. Retry after "
                    "cern_auth_sign_in."
                ),
            )
        if payload.get("content_kind") == "binary":
            raise UpstreamError(
                "the URL serves a binary file "
                f"({payload.get('content_type') or 'unknown content type'}), "
                "not a renderable page",
                instructions="Download it with cern_download_file instead.",
            )
        return ToolEnvelope.success(PageFetchResult(**payload, marker=wait_for))
    except Exception as exc:
        return _failure(exc)


async def _cern_download_file_payload(
    http: SafeHttpClient,
    *,
    url: str,
    filename: str | None,
) -> ToolEnvelope:
    """Handler for the cern_download_file tool (kept out of build_server)."""
    try:
        destination: Path | None = None
        if filename is not None:
            cleaned = filename.strip()
            if not cleaned or Path(cleaned).name != cleaned:
                raise InvalidInputError(
                    "filename must be a plain file name, not a path",
                    instructions="Pass e.g. 'CR2026_251_v4.pdf'; the download "
                    "directory is fixed to CMcpS storage.",
                )
            destination = Path(cleaned)
        final = await http.download(url, destination)
        return ToolEnvelope.success(
            {"url": url, "path": str(final), "size_bytes": final.stat().st_size}
        )
    except Exception as exc:
        return _failure(exc)


def build_server(  # noqa: C901 - per-tool registration blocks dominate the branch count
    settings: Settings | None = None,
    *,
    auth_service: AuthService | None = None,
) -> MCPServer:
    """Build the M1 server and register auth-gated tools."""
    runtime_settings = settings or get_settings()
    runtime_auth = auth_service or AuthService(runtime_settings)
    server: MCPServer = MCPServer(
        name=runtime_settings.server_name,
        title="CMcpS",
        description="Read-only CERN/CMS MCP server.",
        instructions=(
            "Call cern_auth_sign_in first, complete the CERN browser login, "
            "then retry protected tools. Use cern_auth_status to inspect safe session metadata. "
            "Tool namespaces (when to use what): "
            "indico_* = meetings, conferences, agendas, presentations/slides. "
            "inspire_* = published HEP literature, citations, BibTeX, author metrics. "
            "cds_* = CERN Document Server records (notes, theses, archived docs). "
            "cmsresults_* = CMS-approved PUBLIC results pages (plots, papers per physics group). "
            "glance_* = INTERNAL CMS analyses (CADI), analysis notes, member rosters. "
            "discourse_* = community Q&A forums; site meanings: cms-talk = internal CMS "
            "discussion; cms-pub-talk = internal CMS publication-review discussion (per-paper "
            "threads, NOT public content); root-forum = ROOT software help (anonymous); "
            "geant4-forum = Geant4 help (anonymous). "
            "twiki_* = legacy CMS internal wiki (docs not yet migrated). "
            "mkdocs_* = generic reader for CMS MkDocs documentation sites. "
            "cmssearch_* = unified full-text search over CMS batch documentation. "
            "gocms_* = CMS shortlink directory — find pages/URLs when you only know a name. "
            "gitlab_* = CERN GitLab repos, code, repo issues/wikis. "
            "cmssdt_* = CMSSW releases, integration builds, open PRs per release. "
            "jira_* = CERN issue trackers (service requests, project bugs). "
            "cmsonline_* / cmsonline_elog_* = run/fill/lumi data, shift schedules, "
            "shift-log entries. "
            "hltinfo_* = HLT trigger menus, paths, prescales. "
            "das_* = dataset/block/file locations (DBS). "
            "mcm_* = Monte-Carlo production requests (PdmV prepids). "
            "xsdb_* = cross-section records. "
            "websites_* = searchable inventory of centrally hosted CERN websites "
            "(Web Services portal + crt.sh; hostname/description/owner search). "
            "For a CMS publication's lifecycle use glance_* (internal analysis) -> "
            "cms-pub-talk (publication-review discussion) -> cmsresults_* (approved public "
            "page); for the published paper record itself use inspire_*."
        ),
        version=runtime_settings.server_version or __version__,
    )

    @server.tool(
        name="cern_auth_sign_in",
        description=(
            "Open a visible browser for CERN SSO and wait for the authenticated "
            "Keycloak session. Only the login itself is visible: per-service "
            "Bearer tokens are minted lazily on first API use, and cookie-only "
            "site sessions are re-warmed on demand from the stored identity. "
            "The storage state is stored locally with owner-only permissions. "
            "No cookies or tokens are returned."
        ),
    )
    async def cern_auth_sign_in(
        timeout_seconds: Annotated[
            int,
            Field(ge=MIN_SSO_TIMEOUT_SECONDS, le=MAX_SSO_TIMEOUT_SECONDS),
        ]
        | None = None,
    ) -> ToolEnvelope:
        """Authenticate with CERN before any protected CMcpS tool is used."""
        try:
            result = await runtime_auth.sign_in(timeout_seconds)
            return ToolEnvelope.success(result)
        except Exception as exc:
            return _failure(exc)

    @server.tool(
        name="cern_auth_reauthenticate",
        description=(
            "Force a fresh CERN SSO login: the stored session cookies and captured "
            "service Bearer tokens are discarded first, then a visible browser opens "
            "for CERN SSO. Use this when cern_auth_sign_in keeps reusing a broken session."
        ),
    )
    async def cern_auth_reauthenticate(
        timeout_seconds: Annotated[
            int,
            Field(ge=MIN_SSO_TIMEOUT_SECONDS, le=MAX_SSO_TIMEOUT_SECONDS),
        ]
        | None = None,
    ) -> ToolEnvelope:
        """Force a fresh CERN login, ignoring any stored session state."""
        try:
            result = await runtime_auth.reauthenticate(timeout_seconds)
            return ToolEnvelope.success(result)
        except Exception as exc:
            return _failure(exc)

    @server.tool(
        name="cern_auth_status",
        description=(
            "Show safe CERN SSO session metadata, including whether the auth guard "
            "will allow protected tools. Session cookies and tokens are never returned."
        ),
    )
    async def cern_auth_status() -> ToolEnvelope:
        """Return safe authentication/session metadata."""
        try:
            return ToolEnvelope.success(runtime_auth.status())
        except Exception as exc:
            return _failure(exc)

    @server.tool(
        name="cern_health",
        description=(
            "Protected M1 smoke check. It exercises the SSO guard and read-only/runtime "
            "policy after sign-in. Returns AUTH_REQUIRED until cern_auth_sign_in completes."
        ),
    )
    async def cern_health() -> ToolEnvelope:
        """Protected health check proving server-side auth enforcement."""
        try:
            session = await runtime_auth.ensure_authenticated()
            result = HealthResult(
                server=runtime_settings.server_name,
                version=runtime_settings.server_version or __version__,
                authenticated=session.authenticated,
                auth_bypass=session.bypass,
                max_response_bytes=runtime_settings.max_response_bytes,
                max_download_bytes=runtime_settings.max_download_bytes,
            )
            return ToolEnvelope.success(result)
        except Exception as exc:
            return _failure(exc)

    @server.tool(
        name="cern_fetch_page",
        description=(
            "Render one CERN page in a headless browser through the stored SSO session "
            "and return its visible text. Covers cookie-based sites and Keycloak-PKCE "
            "single-page apps (the app's own token exchange completes silently), which "
            "plain GET-based tools cannot read. Pass wait_for — a case-insensitive "
            "substring of the expected content — to wait until SPA data appears; "
            "without it the page is read once its text stabilizes. auth_divert=true "
            "means the page landed on the SSO login: the stored session is stale, "
            "re-run cern_auth_sign_in."
        ),
    )
    async def cern_fetch_page(
        url: Annotated[
            str,
            Field(description="Page URL; must pass the CERN host allowlist."),
        ],
        wait_for: Annotated[
            str | None,
            Field(
                description="Case-insensitive text to wait for in the rendered page, "
                "e.g. a table header or name."
            ),
        ] = None,
        timeout_seconds: Annotated[
            int,
            Field(ge=5, le=FETCH_MAX_TIMEOUT_SECONDS),
        ] = FETCH_DEFAULT_TIMEOUT_SECONDS,
        max_chars: Annotated[
            int,
            Field(ge=1_000, le=FETCH_MAX_CHARS_LIMIT),
        ] = FETCH_DEFAULT_MAX_CHARS,
    ) -> ToolEnvelope:
        """Browse one SSO-protected page headlessly and return its visible text."""
        return await _cern_fetch_page_payload(
            runtime_settings,
            runtime_auth,
            url=url,
            wait_for=wait_for,
            timeout_seconds=timeout_seconds,
            max_chars=max_chars,
        )

    http = SafeHttpClient(runtime_settings, auth_service=runtime_auth)

    @server.tool(
        name="cern_download_file",
        description=(
            "Download one file (PDF, plot, archive, ...) hosted on an allowlisted "
            "CERN site into local CMcpS download storage through the stored SSO "
            "session, with cookies re-scoped on every redirect hop. Use it when "
            "cern_fetch_page reports the URL serves a binary file instead of a "
            "renderable page. Returns the local path and size; the file content "
            "is never inlined."
        ),
    )
    async def cern_download_file(
        url: Annotated[
            str,
            Field(description="File URL; must pass the CERN host allowlist."),
        ],
        filename: Annotated[
            str | None,
            Field(
                description="Optional destination file name (a plain name, not a "
                "path); defaults to the server-provided name."
            ),
        ] = None,
    ) -> ToolEnvelope:
        """Download one CERN-hosted file through the SSO session."""
        return await _cern_download_file_payload(http, url=url, filename=filename)

    register_adapter_tools(server, runtime_settings, runtime_auth, http)

    return server


def main() -> None:
    """Run CMcpS over MCP stdio."""
    # httpx logs request URLs at INFO, which would leak OIDC/SAML artifacts from
    # redirect chains into stderr. Policy handles redaction for our own logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    build_server().run("stdio")
