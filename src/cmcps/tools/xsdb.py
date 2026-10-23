"""MCP tool registration for the CMS XSDB adapter."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import xsdb
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded


def register_xsdb_tools(
    server: MCPServer,
    _settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only CMS XSDB tools."""

    @server.tool(
        name="xsdb_search",
        description=(
            "Search CMS cross-section records (XSDB): 'cross section of ttbar "
            "at 13 TeV' questions. Records carry cross_section, uncertainty, "
            "DAS/MCM links — follow datasets with das_* and production requests "
            "with mcm_*. NOT for dataset locations (das_search). Filters: "
            "particles (e.g. 'ttbar'), process_name, energy (e.g. '13'). "
            "Requires CERN SSO."
        ),
    )
    async def xsdb_search(
        particles: str | None = None,
        process_name: str | None = None,
        energy: str | None = None,
        page_num: Annotated[int, Field(ge=1)] = 1,
        page_size: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> ToolEnvelope:
        """XSDB record search."""
        return await guarded(
            auth,
            lambda: xsdb.search(
                http,
                particles=particles,
                process_name=process_name,
                energy=energy,
                page_num=page_num,
                page_size=page_size,
            ),
        )

    @server.tool(
        name="xsdb_list_tables",
        description=(
            "Linked cross-section summary-table names exposed by the XSDB UI "
            "(requires CERN SSO); companion to xsdb_search."
        ),
    )
    async def xsdb_list_tables() -> ToolEnvelope:
        """XSDB linked tables."""
        return await guarded(auth, lambda: xsdb.get_linked_tables(http))
