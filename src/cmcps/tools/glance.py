"""MCP tool registration for the GLANCE/CMSFence adapter."""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import glance
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded

_AUTH_DOC = (
    " Requires a per-app Keycloak Bearer token (audiences cms-alcm-api-prod and "
    "cms-membership-api-prod), captured automatically by cern_auth_sign_in from "
    "the cmsfence.cern.ch app roots, or set via CMCPS_GLANCE_ALCM_TOKEN / "
    "CMCPS_GLANCE_MEMBERSHIP_TOKEN."
)


def register_glance_tools(  # noqa: C901
    server: MCPServer,
    settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only GLANCE/CMSFence tools."""

    @server.tool(
        name="glance_alcm_user",
        description=("Current GLANCE ALCM user record (roles, appointments). " + _AUTH_DOC),
    )
    async def glance_alcm_user() -> ToolEnvelope:
        """ALCM me."""
        return await guarded(auth, lambda: glance.alcm_user(http, settings))

    @server.tool(
        name="glance_membership_user",
        description=(
            "Current GLANCE Membership user record incl. primary employment. " + _AUTH_DOC
        ),
    )
    async def glance_membership_user() -> ToolEnvelope:
        """Membership me."""
        return await guarded(auth, lambda: glance.membership_user(http, settings))

    @server.tool(
        name="glance_search_analyses",
        description=(
            "Search INTERNAL CMS analyses registered in GLANCE (CADI): the "
            "right tool for 'which CMS analysis/group is studying X', 'status "
            "of analysis EXO-24-010', or finding an internal analysis by "
            "identifier, title, stage, working group, or proponent name. NOT "
            "for published paper records (inspire_search_papers) or approved "
            "public result pages (cmsresults_*). Filters mirror the ALCM "
            "analysis page: custom (custom identifier, e.g. 'EXO-24'), code "
            "(paper identifier), title, stage, working_group, contacts "
            "(proponent name), start_date/end_date (registration date, "
            "YYYY-MM-DD), sort_by ('title'/'registrationDate'), sort_order "
            "('asc'/'desc'). Filters are client-side (the API has no server "
            "search); results are paginated with limit/offset. " + _AUTH_DOC
        ),
    )
    async def glance_search_analyses(
        *,
        custom: str | None = None,
        code: str | None = None,
        title: str | None = None,
        stage: str | None = None,
        working_group: str | None = None,
        contacts: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        sort_by: str = "registrationDate",
        sort_order: str = "desc",
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> ToolEnvelope:
        """Internal analysis search."""
        return await guarded(
            auth,
            lambda: glance.list_analyses(
                http,
                settings,
                custom=custom,
                code=code,
                title=title,
                stage=stage,
                working_group=working_group,
                contacts=contacts,
                start_date=start_date,
                end_date=end_date,
                sort_by=sort_by,
                sort_order=sort_order,
                limit=limit,
                offset=offset,
            ),
        )

    @server.tool(
        name="glance_list_published_analyses",
        description=(
            "List CMS analyses that reached the published stage in GLANCE — a "
            "bridge between internal analysis records and the published "
            "literature; for the paper record itself follow up with inspire_*. "
            "Filtered client-side by identifier/title/working_group and "
            "has_arxiv (true/false to keep only entries with/without an arXiv "
            "number). Paginated. " + _AUTH_DOC
        ),
    )
    async def glance_list_published_analyses(
        *,
        identifier: str | None = None,
        title_contains: str | None = None,
        working_group: str | None = None,
        has_arxiv: bool | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> ToolEnvelope:
        """Published analysis search."""
        return await guarded(
            auth,
            lambda: glance.list_published_analyses(
                http,
                settings,
                identifier=identifier,
                title_contains=title_contains,
                working_group=working_group,
                has_arxiv=has_arxiv,
                limit=limit,
                offset=offset,
            ),
        )

    @server.tool(
        name="glance_list_note_types",
        description=(
            "GLANCE ALCM note types (e.g. Analysis Note, Conference Report) — "
            "the exact type_name values accepted by glance_search_notes. " + _AUTH_DOC
        ),
    )
    async def glance_list_note_types() -> ToolEnvelope:
        """Note type list."""
        return await guarded(auth, lambda: glance.list_note_types(http, settings))

    @server.tool(
        name="glance_search_notes",
        description=(
            "Search INTERNAL CMS analysis notes and conference reports stored "
            "in GLANCE (ANs); the right tool for 'the analysis note documenting "
            "X'. NOT for published references (inspire_*) or CERN Document "
            "Server records/theses (cds_search). Filters: title_contains, "
            "working_group (exact code), type_name (exact note type, see "
            "glance_list_note_types), project (exact name). The upstream index "
            "is a single ~15 MB payload fetched in full on every call (requires "
            "CMCPS_MAX_RESPONSE_BYTES >= ~20 MB) and filtered client-side; "
            "output is bounded by limit/offset. " + _AUTH_DOC
        ),
    )
    async def glance_search_notes(
        *,
        title_contains: str | None = None,
        working_group: str | None = None,
        type_name: str | None = None,
        project: str | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> ToolEnvelope:
        """Notes search."""
        return await guarded(
            auth,
            lambda: glance.search_notes(
                http,
                settings,
                title_contains=title_contains,
                working_group=working_group,
                type_name=type_name,
                project=project,
                limit=limit,
                offset=offset,
            ),
        )

    @server.tool(
        name="glance_get_analysis",
        description=(
            "Full detail page of one internal CMS analysis; follow "
            "glance_search_analyses with a code it returns. Includes identity, "
            "dates, authors, abstract, ARC review committee (chair/LEs/members), "
            "paper information, workflow (current/target stage, entry "
            "conditions, past events), analysis history, attachments (documents "
            "with versions, GitLab repos, Indico contributions), hashtags. " + _AUTH_DOC
        ),
    )
    async def glance_get_analysis(
        analysis: Annotated[
            str,
            Field(
                description=(
                    "CADI analysis identifier (e.g. 'EXO-24-010', also partial "
                    "custom identifiers like 'EXO-24') or the numeric GLANCE id; "
                    "codes come from glance_search_analyses."
                )
            ),
        ],
    ) -> ToolEnvelope:
        """Analysis detail page."""
        return await guarded(auth, lambda: glance.get_analysis(http, settings, analysis=analysis))

    @server.tool(
        name="glance_get_note",
        description=(
            "Internal CMS analysis/conference note detail (abstract, authors, "
            "approvers, stage) by numeric id; follow glance_search_notes with "
            "an id it returns. " + _AUTH_DOC
        ),
    )
    async def glance_get_note(note_id: Annotated[int, Field(ge=1)]) -> ToolEnvelope:
        """Note detail."""
        return await guarded(auth, lambda: glance.get_note(http, settings, note_id=note_id))

    @server.tool(
        name="glance_list_projects",
        description=("GLANCE ALCM projects with their e-groups. " + _AUTH_DOC),
    )
    async def glance_list_projects() -> ToolEnvelope:
        """Project list."""
        return await guarded(auth, lambda: glance.list_projects(http, settings))

    @server.tool(
        name="glance_list_alcm_working_groups",
        description=(
            "GLANCE ALCM working groups. kind='physics' (B2G, TOP, ...), "
            "'non-physics', or 'all' (both lists). " + _AUTH_DOC
        ),
    )
    async def glance_list_alcm_working_groups(
        kind: Annotated[
            Literal["physics", "non-physics", "all"],
            Field(
                description=(
                    "'physics' = physics working groups (B2G, TOP, EXO, ...), "
                    "'non-physics' = non-physics groups, 'all' = both lists."
                )
            ),
        ] = "all",
    ) -> ToolEnvelope:
        """Working-group list."""
        return await guarded(auth, lambda: glance.list_working_groups(http, settings, kind=kind))

    @server.tool(
        name="glance_alcm_members",
        description=(
            "GLANCE ALCM member roster: 'who is registered in GLANCE', with "
            "substring name filter, paginated (full ~1.1 MB roster fetched per "
            "call). For CMS collaboration members with institute/authorship "
            "status use glance_membership_search_members. " + _AUTH_DOC
        ),
    )
    async def glance_alcm_members(
        name_contains: str | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> ToolEnvelope:
        """ALCM member roster."""
        return await guarded(
            auth,
            lambda: glance.alcm_members(
                http, settings, name_contains=name_contains, limit=limit, offset=offset
            ),
        )

    @server.tool(
        name="glance_membership_search_members",
        description=(
            "Search CMS collaboration members in GLANCE Membership by name "
            "(required query): 'who is X / institute and CMS authorship status "
            "of X'; returns institute, authorship status, appointments; "
            "paginated client-side over the upstream result set. NOT for "
            "paper authorship — use inspire_*. " + _AUTH_DOC
        ),
    )
    async def glance_membership_search_members(
        query: str,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> ToolEnvelope:
        """Membership member search."""
        return await guarded(
            auth,
            lambda: glance.membership_search_members(
                http, settings, query=query, limit=limit, offset=offset
            ),
        )
