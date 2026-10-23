"""MCP tool registration for the CMS DAS adapter."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import das
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded


def register_das_tools(
    server: MCPServer,
    _settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only CMS DAS tools."""

    @server.tool(
        name="das_search",
        description=(
            "CMS DAS QL search (cmsweb.cern.ch/das; CERN SSO required) — "
            "dataset/block/file discovery and locations: 'which datasets match "
            "/TT*/RunII*/AODSIM', 'where is file X stored', 'blocks of dataset "
            "Y'. NOT for MC production requests (mcm_*) or cross sections "
            "(xsdb_search). Examples: dataset=/TT*/RunII*/AODSIM, block "
            "block=/X/Y/Z#..., site site=T2_CH_CERN, file file=/store/.., run "
            "run=379611. Page with offset."
        ),
    )
    async def das_search(
        query: Annotated[
            str,
            Field(
                description=(
                    "DAS QL query: 'entity attr=value', e.g. "
                    "'dataset=/TT*/RunII*/AODSIM', 'block block=/X/Y/Z#...', "
                    "'site site=T2_CH_CERN', 'file file=/store/...', "
                    "'run run=379611'"
                )
            ),
        ],
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
        offset: Annotated[int, Field(ge=0)] = 0,
        instance: Annotated[
            str,
            Field(
                description=(
                    "DBS instance to query: 'prod/global' (default, central "
                    "production data), 'prod/phys03' (user/private production), "
                    "'prod/caf' (CERN Analysis Facility)"
                )
            ),
        ] = "prod/global",
    ) -> ToolEnvelope:
        """DAS query-language search."""
        return await guarded(
            auth,
            lambda: das.search(http, query=query, limit=limit, offset=offset, instance=instance),
        )

    @server.tool(
        name="das_get_dataset_info",
        description=(
            "Attribute record of one exact CMS dataset "
            "(/primary/processed/tier, no wildcards) from DAS prod/global; "
            "follow das_search with a dataset name it returns."
        ),
    )
    async def das_get_dataset_info(dataset: str) -> ToolEnvelope:
        """Exact dataset detail."""
        return await guarded(auth, lambda: das.get_dataset_info(http, dataset=dataset))
