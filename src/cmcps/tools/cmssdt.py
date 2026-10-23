"""MCP tool registration for the CMS SDT adapter."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import cmssdt
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded


async def _build_log_url(*, ib_name: str, arch: str, fwlite: bool) -> dict[str, Any]:
    return cmssdt.build_log_url(ib_name, arch=arch, fwlite=fwlite)


def register_cmssdt_tools(
    server: MCPServer,
    _settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only CMS SDT tools."""

    @server.tool(
        name="cmssdt_list_releases",
        description=(
            "List announced CMSSW releases per architecture from the SDT "
            "ReleasesXML feed — the entry point for 'which CMSSW releases exist "
            "for X' questions. For integration-build status use "
            "cmssdt_list_ib_queues/cmssdt_ib_summary; NOT for source code "
            "search (gitlab_*). Filters: arch_contains (e.g. 'el9_amd64'), "
            "label_contains (e.g. 'CMSSW_15_0'), state (e.g. 'Announced'). "
            "Public; the interactive IB dashboards are SSO-only."
        ),
    )
    async def cmssdt_list_releases(
        arch_contains: str | None = None,
        label_contains: str | None = None,
        state: str | None = None,
        limit: Annotated[int, Field(ge=1, le=2000)] = 100,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> ToolEnvelope:
        """CMSSW release listing."""
        return await guarded(
            auth,
            lambda: cmssdt.list_releases(
                http,
                arch_contains=arch_contains,
                label_contains=label_contains,
                state=state,
                limit=limit,
                offset=offset,
            ),
        )

    @server.tool(
        name="cmssdt_list_ib_queues",
        description=(
            "List IB (integration build) queues and their release family from "
            "the SDT dashboard structure.json. Optional 'family' filter "
            "(case-insensitive substring, e.g. 'CMSSW_20'). The note field "
            "reports the dashboard default release. Authenticated (Keycloak)."
        ),
    )
    async def cmssdt_list_ib_queues(
        family: Annotated[
            str | None,
            Field(
                description=(
                    "Case-insensitive CMSSW release family substring filter, "
                    "e.g. 'CMSSW_15_0' or 'CMSSW_20'"
                )
            ),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=2000)] = 100,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> ToolEnvelope:
        """IB queue listing."""
        return await guarded(
            auth,
            lambda: cmssdt.list_ib_queues(http, family=family, limit=limit, offset=offset),
        )

    @server.tool(
        name="cmssdt_ib_summary",
        description=(
            "Latest IB name and build status per queue/architecture from "
            "LatestIBsSummary.json. Filters: queue (substring, "
            "case-insensitive), arch (substring), status (exact, e.g. "
            "'passed'/'failed'/'error'). Authenticated (Keycloak)."
        ),
    )
    async def cmssdt_ib_summary(
        queue: str | None = None,
        arch: Annotated[
            str | None,
            Field(description="Architecture name substring, e.g. el8_amd64_gcc12"),
        ] = None,
        status: str | None = None,
        limit: Annotated[int, Field(ge=1, le=2000)] = 100,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> ToolEnvelope:
        """Latest IB summary rows."""
        return await guarded(
            auth,
            lambda: cmssdt.ib_summary(
                http, queue=queue, arch=arch, status=status, limit=limit, offset=offset
            ),
        )

    @server.tool(
        name="cmssdt_get_ib_details",
        description=(
            "Bounded details of one IB comparison for a queue (e.g. "
            "CMSSW_20_1_X). Select with comparison_index (0 = newest, "
            "comparisons are newest-first) or exact ib_date "
            "(e.g. '2026-10-22-1100'). Returns base fields, status strings "
            "(builds/relvals/hlt/...), counts for large list fields, and the "
            "merged cmssw/cmsdist PR lists. Authenticated (Keycloak)."
        ),
    )
    async def cmssdt_get_ib_details(
        queue: str,
        comparison_index: Annotated[int, Field(ge=0)] = 0,
        ib_date: str | None = None,
    ) -> ToolEnvelope:
        """One IB comparison details."""
        return await guarded(
            auth,
            lambda: cmssdt.get_ib_details(
                http, queue=queue, comparison_index=comparison_index, ib_date=ib_date
            ),
        )

    @server.tool(
        name="cmssdt_list_cmssw_labels",
        description=(
            "List CMSSW GitHub labels and their colors as used by the SDT "
            "PR dashboards. Optional 'prefix' filter (case-insensitive). "
            "Authenticated (Keycloak)."
        ),
    )
    async def cmssdt_list_cmssw_labels(
        prefix: str | None = None,
        limit: Annotated[int, Field(ge=1, le=2000)] = 100,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> ToolEnvelope:
        """CMSSW label listing."""
        return await guarded(
            auth,
            lambda: cmssdt.list_cmssw_labels(http, prefix=prefix, limit=limit, offset=offset),
        )

    @server.tool(
        name="cmssdt_list_open_prs",
        description=(
            "List pull requests tracked for a release family (e.g. "
            "CMSSW_20_1). Filters: repo ('cmssw'/'cmsdist', default both), "
            "state (default 'open'; pass empty/none semantics via state=null "
            "for all), label substring. Items sorted newest first. "
            "Authenticated (Keycloak)."
        ),
    )
    async def cmssdt_list_open_prs(
        family: Annotated[
            str,
            Field(
                description=(
                    "CMSSW release family without the trailing _X, e.g. CMSSW_15_0 or CMSSW_20_1"
                )
            ),
        ],
        *,
        repo: str | None = None,
        state: str | None = "open",
        label: str | None = None,
        limit: Annotated[int, Field(ge=1, le=2000)] = 100,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> ToolEnvelope:
        """PR listing for a release family."""
        return await guarded(
            auth,
            lambda: cmssdt.list_open_prs(
                http,
                family=family,
                repo=repo,
                state=state,
                label=label,
                limit=limit,
                offset=offset,
            ),
        )

    @server.tool(
        name="cmssdt_relval_archs",
        description=(
            "List which architectures have RelVal results available per full "
            "IB name (e.g. CMSSW_20_1_X_2026-10-22-1100). Optional "
            "'ib_name_contains' substring filter. Authenticated (Keycloak)."
        ),
    )
    async def cmssdt_relval_archs(
        ib_name_contains: str | None = None,
        limit: Annotated[int, Field(ge=1, le=2000)] = 100,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> ToolEnvelope:
        """RelVal arch availability per IB."""
        return await guarded(
            auth,
            lambda: cmssdt.relval_archs(
                http, ib_name_contains=ib_name_contains, limit=limit, offset=offset
            ),
        )

    @server.tool(
        name="cmssdt_build_log_url",
        description=(
            "Construct the SDT showBuildLogs URL for a full IB name "
            "(e.g. CMSSW_20_1_X_2026-10-22-1100) and architecture (e.g. "
            "el9_amd64_gcc13). Pure URL derivation — performs no network "
            "call. Set fwlite=true for the fwlite build logs."
        ),
    )
    async def cmssdt_build_log_url(
        ib_name: str,
        arch: Annotated[
            str,
            Field(description="Full architecture name, e.g. el8_amd64_gcc12"),
        ],
        fwlite: bool = False,
    ) -> ToolEnvelope:
        """Build-log URL derivation."""
        return await guarded(
            auth,
            lambda: _build_log_url(ib_name=ib_name, arch=arch, fwlite=fwlite),
        )
