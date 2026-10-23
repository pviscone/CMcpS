"""MCP tool registration for the CERN Jira adapter."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import jira
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded


def register_jira_tools(
    server: MCPServer,
    _settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only its.cern.ch/jira tools."""

    @server.tool(
        name="jira_search",
        description=(
            "JQL search over CERN Jira issues (its.cern.ch). Example: "
            "project = CMSTRACK AND text ~ 'calibration'. Returns issue "
            "summaries with status, assignee, dates; page with start_at."
        ),
    )
    async def jira_search(
        jql: str,
        start_at: Annotated[int, Field(ge=0)] = 0,
        max_results: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> ToolEnvelope:
        """JQL issue search."""
        return await guarded(
            auth,
            lambda: jira.search_issues(http, jql=jql, start_at=start_at, max_results=max_results),
        )

    @server.tool(
        name="jira_get_issue",
        description=(
            "Full record of one Jira issue by key (e.g. CMSTRACK-123): "
            "description, comments, attachments, status, people, dates."
        ),
    )
    async def jira_get_issue(issue_key: str) -> ToolEnvelope:
        """Issue detail."""
        return await guarded(auth, lambda: jira.get_issue(http, issue_key=issue_key))

    @server.tool(
        name="jira_get_project",
        description=("Metadata of one CERN Jira project: lead, components, issue types."),
    )
    async def jira_get_project(project_key: str) -> ToolEnvelope:
        """Project metadata."""
        return await guarded(auth, lambda: jira.get_project(http, project_key=project_key))
