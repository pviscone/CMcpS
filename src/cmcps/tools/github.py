"""MCP registration for GitHub (cms-sw/cmssw by default)."""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import github
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import enveloped

_REPO = Annotated[str, Field(description="owner/name; default cms-sw/cmssw.")]


def register_github_tools(server: MCPServer, http: SafeHttpClient) -> None:
    """Register the read-only GitHub tools (public repos; no CERN SSO needed)."""

    @server.tool(
        name="github_search",
        description=(
            "Search GitHub pull requests, issues, or code (code needs a token) of one repo, "
            "cms-sw/cmssw by default: the why/when of CMSSW changes, review discussions, "
            "where a symbol is used. Follow with github_get_thread or github_get_file."
        ),
    )
    async def github_search(
        query: str,
        kind: Literal["prs", "issues", "code"] = "prs",
        repo: _REPO = github.DEFAULT_REPO,
        limit: Annotated[int, Field(ge=1, le=30)] = 8,
    ) -> ToolEnvelope:
        return await enveloped(
            lambda: github.search(http, query=query, kind=kind, repo=repo, limit=limit)
        )

    @server.tool(
        name="github_get_thread",
        description=(
            "One GitHub PR or issue with its discussion (bots skipped, review comments with "
            "file paths), and for PRs the merge state, branches, size and changed files."
        ),
    )
    async def github_get_thread(
        number: Annotated[int, Field(ge=1)],
        repo: _REPO = github.DEFAULT_REPO,
        max_comments: Annotated[int, Field(ge=1, le=100)] = 40,
    ) -> ToolEnvelope:
        return await enveloped(
            lambda: github.get_thread(http, repo=repo, number=number, max_comments=max_comments)
        )

    @server.tool(
        name="github_get_file",
        description=(
            "A line window (max 300 lines, numbered) of one file in a GitHub repo at a "
            "branch/tag/commit (ref; default branch if omitted)."
        ),
    )
    async def github_get_file(
        path: str,
        repo: _REPO = github.DEFAULT_REPO,
        ref: str | None = None,
        start_line: Annotated[int, Field(ge=1)] = 1,
        end_line: Annotated[int | None, Field(ge=1)] = None,
    ) -> ToolEnvelope:
        return await enveloped(
            lambda: github.get_file(
                http, repo=repo, path=path, ref=ref, start_line=start_line, end_line=end_line
            )
        )
