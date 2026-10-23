"""MCP tool registration for the CMS HLT info adapter."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import hltinfo
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded

_AUTH_DOC = (
    " Requires CMCPS_HLTINFO_TOKEN (Keycloak Bearer token for the service; "
    "obtained via authenticated web login, injected as Authorization header)."
)


def register_hltinfo_tools(
    server: MCPServer,
    settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only CMS HLT info tools."""

    @server.tool(
        name="hltinfo_list_menus",
        description=("HLT menus known to the service (cmshltinfo-dev API). " + _AUTH_DOC),
    )
    async def hltinfo_list_menus() -> ToolEnvelope:
        """HLT menu list."""
        return await guarded(auth, lambda: hltinfo.list_menus(http, settings))

    @server.tool(
        name="hltinfo_get_path_summary",
        description=("Per-year HLT path summary table. " + _AUTH_DOC),
    )
    async def hltinfo_get_path_summary() -> ToolEnvelope:
        """Path summary."""
        return await guarded(auth, lambda: hltinfo.get_path_summary(http, settings))

    @server.tool(
        name="hltinfo_get_path_names",
        description=(
            "HLT path names, optionally narrowed by a simple filter "
            "(e.g. HLT_Ele, Muon). " + _AUTH_DOC
        ),
    )
    async def hltinfo_get_path_names(name_filter: str | None = None) -> ToolEnvelope:
        """Path name listing."""
        return await guarded(
            auth, lambda: hltinfo.get_path_names(http, settings, name_filter=name_filter)
        )

    @server.tool(
        name="hltinfo_get_path",
        description=(
            "Full record of one HLT path (versions, filter definitions, "
            "per-run semantics) by exact path name. " + _AUTH_DOC
        ),
    )
    async def hltinfo_get_path(path_name: str) -> ToolEnvelope:
        """HLT path detail."""
        return await guarded(auth, lambda: hltinfo.get_path(http, settings, path_name=path_name))

    @server.tool(
        name="hltinfo_get_path_prescales",
        description=(
            "Prescale values of one HLT path over a run range (min_run/max_run). " + _AUTH_DOC
        ),
    )
    async def hltinfo_get_path_prescales(
        path_name: str,
        min_run: Annotated[int, Field(ge=1)] = 1,
        max_run: Annotated[int, Field(ge=1)] = 9999999,
    ) -> ToolEnvelope:
        """Path prescales per run."""
        return await guarded(
            auth,
            lambda: hltinfo.get_path_prescales(
                http, settings, path_name=path_name, min_run=min_run, max_run=max_run
            ),
        )

    @server.tool(
        name="hltinfo_get_run_lumis",
        description=("Run/lumi ranges where one HLT path was enabled (prescale > 0). " + _AUTH_DOC),
    )
    async def hltinfo_get_run_lumis(path_name: str) -> ToolEnvelope:
        """Path run/lumi coverage."""
        return await guarded(
            auth, lambda: hltinfo.get_run_lumis_by_prescale(http, settings, path_name=path_name)
        )
