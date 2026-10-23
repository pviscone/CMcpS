"""MCP tool registration for the McM (cms-PdmV) adapter."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from cmcps.adapters import mcm
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded


def register_mcm_tools(
    server: MCPServer,
    _settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only McM public-API tools."""

    @server.tool(
        name="mcm_get_request",
        description=(
            "McM request document by prepid (e.g. B2G-RunIISummer20UL17wmLHEGEN-00001): "
            "approval, status, campaign, events, output config, history (compact view). "
            "Public API without a search endpoint; ids come from the caller."
        ),
    )
    async def mcm_get_request(request_id: str) -> ToolEnvelope:
        """McM request detail."""
        return await guarded(auth, lambda: mcm.get_request(http, request_id=request_id))

    @server.tool(
        name="mcm_get_status_and_approval",
        description=(
            "Approval+status of one McM request (e.g. submit-done, defined-new) "
            "by prepid, split into approval and status fields."
        ),
    )
    async def mcm_get_status_and_approval(prepid: str) -> ToolEnvelope:
        """McM status/approval."""
        return await guarded(auth, lambda: mcm.get_status_and_approval(http, prepid=prepid))

    @server.tool(
        name="mcm_get_output_datasets",
        description=("Output dataset names produced by one McM request, by prepid."),
    )
    async def mcm_get_output_datasets(prepid: str) -> ToolEnvelope:
        """McM output datasets."""
        return await guarded(auth, lambda: mcm.get_output_datasets(http, prepid=prepid))

    @server.tool(
        name="mcm_find_producing_requests",
        description=(
            "Find the McM request that produced an exact dataset name "
            "(/primary/processed/tier, leading slash optional). No wildcards; "
            "use cmsresults/DAS to discover datasets first."
        ),
    )
    async def mcm_find_producing_requests(dataset: str) -> ToolEnvelope:
        """Dataset → producing request lookup."""
        return await guarded(auth, lambda: mcm.find_producing_requests(http, dataset=dataset))
