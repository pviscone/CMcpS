"""MCP registration for CMS Rucio (grid proxy; read-only)."""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmsmcp.adapters import rucio
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.results import ToolEnvelope
from cmsmcp.net.client import SafeHttpClient
from cmsmcp.tools.common import enveloped

_SETUP = "Needs a grid proxy and a Rucio account (`cmsmcp setup grid` / `cmsmcp setup rucio`)."
_ACCOUNT = Annotated[str | None, Field(description="Rucio account; default: the user's.")]


def register_rucio_tools(server: MCPServer, settings: Settings, http: SafeHttpClient) -> None:
    """Register Rucio read tools."""

    @server.tool(
        name="rucio_list_rules",
        description=(
            "The user's (or an account's) Rucio replication rules: dataset, RSE expression, "
            "state, lock counts, expiry — 'where are my samples being transferred', 'which "
            "rules are STUCK'. " + _SETUP
        ),
    )
    async def rucio_list_rules(
        account: _ACCOUNT = None,
        state: Literal["OK", "REPLICATING", "STUCK", "SUSPENDED", "WAITING_APPROVAL"] | None = None,
        name_contains: str | None = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
    ) -> ToolEnvelope:
        return await enveloped(
            lambda: rucio.list_rules(
                http,
                settings,
                account=account,
                state=state,
                name_contains=name_contains,
                limit=limit,
            )
        )

    @server.tool(
        name="rucio_get_rule",
        description="One Rucio rule by 32-hex id (state, locks, error, expiry). " + _SETUP,
    )
    async def rucio_get_rule(rule_id: str, account: _ACCOUNT = None) -> ToolEnvelope:
        return await enveloped(
            lambda: rucio.get_rule(http, settings, rule_id=rule_id, account=account)
        )

    @server.tool(
        name="rucio_get_did",
        description=(
            "Metadata of one Rucio data identifier (scope:name, e.g. a CMS dataset "
            "cms:/A/B/TIER) with size, file count and the rules placing it. " + _SETUP
        ),
    )
    async def rucio_get_did(did: str, account: _ACCOUNT = None) -> ToolEnvelope:
        return await enveloped(lambda: rucio.get_did(http, settings, did=did, account=account))
