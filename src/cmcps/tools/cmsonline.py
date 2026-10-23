"""MCP tool registration for the CMS Online (OMS) adapter."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import cmsonline
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded

_AUTH_DOC = (
    " The OMS API is CERN-authenticated (inside the CERN network or via "
    "CERN SSO); sign in with cern_auth_sign_in first."
)


def register_cmsonline_tools(
    server: MCPServer,
    _settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only CMS Online (OMS API) tools."""

    @server.tool(
        name="cmsonline_get_version",
        description="OMS API version (cmsoms.cern.ch)." + _AUTH_DOC,
    )
    async def cmsonline_get_version() -> ToolEnvelope:
        """OMS version probe."""
        return await guarded(auth, lambda: cmsonline.get_version(http))

    @server.tool(
        name="cmsonline_list_runs",
        description=(
            "List CMS data-taking runs with attributes (start/end time, "
            "fill, sequence, components). Optional exact filters on "
            "run_number and fill_number." + _AUTH_DOC
        ),
    )
    async def cmsonline_list_runs(
        run_number: Annotated[int, Field(ge=1)] | None = None,
        fill_number: Annotated[int, Field(ge=1)] | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> ToolEnvelope:
        """Run listing with filters."""
        return await guarded(
            auth,
            lambda: cmsonline.list_runs(
                http,
                run_number=run_number,
                fill_number=fill_number,
                limit=limit,
                offset=offset,
            ),
        )

    @server.tool(
        name="cmsonline_get_run",
        description="Full attribute record of one CMS run." + _AUTH_DOC,
    )
    async def cmsonline_get_run(
        run_number: Annotated[int, Field(ge=1)],
    ) -> ToolEnvelope:
        """Run detail."""
        return await guarded(auth, lambda: cmsonline.get_run(http, run_number=run_number))

    @server.tool(
        name="cmsonline_list_fills",
        description=(
            "List LHC fills (stable beams, durations, energies). Optional "
            ">= lower bound on fill_number." + _AUTH_DOC
        ),
    )
    async def cmsonline_list_fills(
        fill_number_min: Annotated[int, Field(ge=1)] | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> ToolEnvelope:
        """Fill listing."""
        return await guarded(
            auth,
            lambda: cmsonline.list_fills(
                http, fill_number_min=fill_number_min, limit=limit, offset=offset
            ),
        )

    @server.tool(
        name="cmsonline_list_lumisections",
        description=(
            "Lumisection records of one run (luminosity, rates). Heavy "
            "runs return thousands of rows; page with offset." + _AUTH_DOC
        ),
    )
    async def cmsonline_list_lumisections(
        run_number: Annotated[int, Field(ge=1)],
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> ToolEnvelope:
        """Lumisection listing of one run."""
        return await guarded(
            auth,
            lambda: cmsonline.list_lumisections(
                http, run_number=run_number, limit=limit, offset=offset
            ),
        )

    @server.tool(
        name="cmsonline_get_shift_schedule",
        description=(
            "CMS Online shift schedule (shifter grid of one date): blocks of "
            "role assignments per system (Central, DAQ, HGCAL, TC, ...) with "
            "shifter name, id, start time, and block length. date is ISO "
            "YYYY-MM-DD (omit for the portal's currently selected day; pass "
            "an explicit date to be precise). Requires CERN SSO + portal "
            "session."
        ),
    )
    async def cmsonline_get_shift_schedule(date: str | None = None) -> ToolEnvelope:
        """Shift schedule for one date."""
        return await guarded(auth, lambda: cmsonline.get_shift_schedule(http, date=date))
