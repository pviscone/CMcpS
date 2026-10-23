"""MCP server assembly for CMcpS."""

from __future__ import annotations

import logging
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps import __version__
from cmcps.auth.service import AuthService
from cmcps.config.settings import (
    MAX_SSO_TIMEOUT_SECONDS,
    MIN_SSO_TIMEOUT_SECONDS,
    Settings,
    get_settings,
)
from cmcps.contracts.errors import CMCPSError
from cmcps.contracts.results import ErrorCode, ErrorPayload, HealthResult, ToolEnvelope
from cmcps.net.client import SafeHttpClient
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


def build_server(
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
            "then retry protected tools. Use cern_auth_status to inspect safe session metadata."
        ),
        version=runtime_settings.server_version or __version__,
    )

    @server.tool(
        name="cern_auth_sign_in",
        description=(
            "Open a visible browser for CERN SSO and wait for the authenticated "
            "Keycloak session. The storage state is stored locally with owner-only permissions. "
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

    http = SafeHttpClient(runtime_settings, auth_service=runtime_auth)
    register_adapter_tools(server, runtime_settings, runtime_auth, http)

    return server


def main() -> None:
    """Run CMcpS over MCP stdio."""
    # httpx logs request URLs at INFO, which would leak OIDC/SAML artifacts from
    # redirect chains into stderr. Policy handles redaction for our own logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    build_server().run("stdio")
