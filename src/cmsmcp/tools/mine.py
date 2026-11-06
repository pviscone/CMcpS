"""MCP registration for the personal work-queue overview."""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmsmcp.adapters import mine
from cmsmcp.auth.service import AuthService
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.results import ToolEnvelope
from cmsmcp.net.client import SafeHttpClient
from cmsmcp.policy import Policy
from cmsmcp.tools.common import guarded


def register_mine_tools(
    server: MCPServer,
    settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
    policy: Policy,
) -> None:
    """Register ``mine_overview``."""

    @server.tool(
        name="mine_overview",
        description=(
            "What needs the user's attention, in one call: GitLab merge requests awaiting "
            "their review / assigned / opened by them, assigned issues and to-dos; Jira "
            "issues assigned to or reported by them; recent Mattermost posts mentioning them. "
            "Sections run in parallel; an unavailable section says why."
        ),
    )
    async def mine_overview(
        sections: Annotated[
            list[Literal["gitlab", "jira", "mattermost"]] | None,
            Field(description="Subset of sections; omit for all."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=50, description="Items per list.")] = 10,
        days: Annotated[int, Field(ge=1, le=90, description="Mattermost lookback.")] = 14,
    ) -> ToolEnvelope:
        """Personal work queues."""
        return await guarded(
            auth,
            lambda: mine.overview(
                http, settings, policy, sections=list(sections or []), limit=limit, days=days
            ),
        )
