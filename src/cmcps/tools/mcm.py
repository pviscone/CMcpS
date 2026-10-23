"""MCP tool registration for the McM (cms-PdmV) adapter."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import mcm
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import enveloped


def register_mcm_tools(
    server: MCPServer,
    _settings: Settings,
    http: SafeHttpClient,
) -> None:
    """Register the read-only McM public-API tools (anonymous, no SSO)."""

    @server.tool(
        name="mcm_get_request",
        description=(
            "McM Monte-Carlo production request document (cms-PdmV): approval, "
            "status, campaign, events, output config, history (compact view). "
            "Use it for the lifecycle of one MC production request; NOT for "
            "dataset physics content (das_search) or cross sections "
            "(xsdb_search). McM's public API has no search endpoint — ids come "
            "from the caller or from mcm_find_producing_requests."
        ),
    )
    async def mcm_get_request(
        request_id: Annotated[
            str,
            Field(
                description=(
                    "PdmV pre-identifier (prepid) of the request, e.g. "
                    "B2G-RunIISummer20UL17wmLHEGEN-00001; this is the "
                    "'prepid' field of a McM request, not a numeric id"
                )
            ),
        ],
    ) -> ToolEnvelope:
        """McM request detail."""
        return await enveloped(lambda: mcm.get_request(http, request_id=request_id))

    @server.tool(
        name="mcm_get_status_and_approval",
        description=(
            "Approval+status of one McM production request (e.g. submit-done, "
            "defined-new); follow mcm_get_request. Split into separate approval "
            "and status fields."
        ),
    )
    async def mcm_get_status_and_approval(
        prepid: Annotated[
            str,
            Field(
                description=(
                    "PdmV pre-identifier of the request, e.g. "
                    "B2G-RunIISummer20UL17wmLHEGEN-00001 (same value as "
                    "mcm_get_request's request_id)"
                )
            ),
        ],
    ) -> ToolEnvelope:
        """McM status/approval."""
        return await enveloped(lambda: mcm.get_status_and_approval(http, prepid=prepid))

    @server.tool(
        name="mcm_get_output_datasets",
        description=(
            "Output dataset names produced by one McM production request; "
            "follow mcm_get_request; check them with das_get_dataset_info."
        ),
    )
    async def mcm_get_output_datasets(
        prepid: Annotated[
            str,
            Field(
                description=(
                    "PdmV pre-identifier of the request, e.g. "
                    "B2G-RunIISummer20UL17wmLHEGEN-00001 (same value as "
                    "mcm_get_request's request_id)"
                )
            ),
        ],
    ) -> ToolEnvelope:
        """McM output datasets."""
        return await enveloped(lambda: mcm.get_output_datasets(http, prepid=prepid))

    @server.tool(
        name="mcm_find_producing_requests",
        description=(
            "Reverse lookup: find the McM request that produced an exact "
            "dataset name (/primary/processed/tier, leading slash optional). "
            "No wildcards; use das_search to discover dataset names first, "
            "then mcm_get_request for the request details."
        ),
    )
    async def mcm_find_producing_requests(dataset: str) -> ToolEnvelope:
        """Dataset → producing request lookup."""
        return await enveloped(lambda: mcm.find_producing_requests(http, dataset=dataset))
