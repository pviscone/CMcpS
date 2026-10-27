"""MCP tool registration for the CMS Run Registry adapter."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import runregistry
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded

_AUTH_DOC = " SSO required (Keycloak-proxied); sign in with cern_auth_sign_in first."
_COMPONENT_DOC = (
    "Each entry 'workspace-column:STATUS' filters to runs/datasets having at "
    "least one lumisection certified STATUS on that component — e.g. "
    "'tracker-strip:GOOD', 'dt-dt:GOOD', 'csc-csc:GOOD', 'l1t-l1tcalo:GOOD', "
    "'hlt-hlt:GOOD' (STATUS: GOOD/BAD/STANDBY/EXCLUDED/NOTSET/EMPTY)."
)


def register_runregistry_tools(
    server: MCPServer,
    _settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only CMS Run Registry tools."""

    @server.tool(
        name="runregistry_search_runs",
        description=(
            "Search CMS Run Registry (cmsrunregistry.web.cern.ch) run records — "
            "data certification semantics: which runs are significant, their RR "
            "class/state, and lumisection-level component statuses — questions "
            "like 'collision runs between X and Y with GOOD tracker status'. "
            "Each item carries oms_attributes (fill, energy, hlt_key, times, "
            "lumi) and rr_attributes (class, state, significant, stop_reason) "
            "plus triplet_summary status counts. For beam rates/exact "
            "luminosity use cmsonline_* (OMS); for golden-JSON inputs use the "
            "dataset/lumisection tools in this namespace. " + _COMPONENT_DOC + _AUTH_DOC
        ),
    )
    async def runregistry_search_runs(  # noqa: PLR0917
        run_number: Annotated[int, Field(ge=1)] | None = None,
        run_from: Annotated[int, Field(ge=1)] | None = None,
        run_to: Annotated[int, Field(ge=1)] | None = None,
        fill_number: Annotated[int, Field(ge=1)] | None = None,
        significant: bool | None = None,
        class_name: Annotated[str, Field(min_length=1)] | None = None,
        hlt_key_contains: Annotated[str, Field(min_length=1)] | None = None,
        component_statuses: list[str] | None = None,
        page: Annotated[int, Field(ge=0)] = 0,
        page_size: Annotated[int, Field(ge=1, le=250)] = 50,
    ) -> ToolEnvelope:
        """Run listing with certification filters."""
        return await guarded(
            auth,
            lambda: runregistry.search_runs(
                http,
                run_number=run_number,
                run_from=run_from,
                run_to=run_to,
                fill_number=fill_number,
                significant=significant,
                class_name=class_name,
                hlt_key_contains=hlt_key_contains,
                component_statuses=component_statuses or (),
                page=page,
                page_size=page_size,
            ),
        )

    @server.tool(
        name="runregistry_get_run",
        description=(
            "Full Run Registry record of one run (oms_attributes + "
            "rr_attributes + triplet_summary); follow runregistry_search_runs "
            "with a run number it returns." + _AUTH_DOC
        ),
    )
    async def runregistry_get_run(
        run_number: Annotated[int, Field(ge=1)],
    ) -> ToolEnvelope:
        """Run detail."""
        return await guarded(auth, lambda: runregistry.get_run(http, run_number=run_number))

    @server.tool(
        name="runregistry_list_run_datasets",
        description=(
            "List the certification dataset names Run Registry recorded for "
            "one run ('online' plus e.g. '/PromptReco/Collisions2026A/DQM') — "
            "get the dataset_name values accepted by runregistry_get_dataset "
            "and runregistry_get_lumisection_ranges. NOTE: these are RR "
            "certification records, NOT CMS DAS datasets (for those use "
            "das_search)." + _AUTH_DOC
        ),
    )
    async def runregistry_list_run_datasets(
        run_number: Annotated[int, Field(ge=1)],
    ) -> ToolEnvelope:
        """Dataset-name listing of one run."""
        return await guarded(
            auth, lambda: runregistry.list_run_dataset_names(http, run_number=run_number)
        )

    @server.tool(
        name="runregistry_search_datasets",
        description=(
            "Search Run Registry per-run certification records (run number + "
            "dataset name like '/PromptReco/Collisions2026A/DQM') with "
            "workspace offline states and component status filters — e.g. "
            "'PromptReco DQM records of runs X..Y with tracker-strip GOOD'. "
            + _COMPONENT_DOC
            + _AUTH_DOC
        ),
    )
    async def runregistry_search_datasets(  # noqa: PLR0917
        run_number: Annotated[int, Field(ge=1)] | None = None,
        run_from: Annotated[int, Field(ge=1)] | None = None,
        run_to: Annotated[int, Field(ge=1)] | None = None,
        dataset_name_contains: Annotated[str, Field(min_length=1)] | None = None,
        component_statuses: list[str] | None = None,
        page: Annotated[int, Field(ge=0)] = 0,
        page_size: Annotated[int, Field(ge=1, le=250)] = 50,
    ) -> ToolEnvelope:
        """Certification-record listing with filters."""
        return await guarded(
            auth,
            lambda: runregistry.search_datasets(
                http,
                run_number=run_number,
                run_from=run_from,
                run_to=run_to,
                dataset_name_contains=dataset_name_contains,
                component_statuses=component_statuses or (),
                page=page,
                page_size=page_size,
            ),
        )

    @server.tool(
        name="runregistry_get_dataset",
        description=(
            "One Run Registry certification record by run number and exact "
            "dataset name ('online' or e.g. '/PromptReco/Collisions2026A/DQM' "
            "from runregistry_list_run_datasets): workspace states "
            "(dt_state/global_state/...), triplet status counts, and metadata." + _AUTH_DOC
        ),
    )
    async def runregistry_get_dataset(
        run_number: Annotated[int, Field(ge=1)],
        dataset_name: Annotated[str, Field(min_length=1)],
    ) -> ToolEnvelope:
        """Certification-record detail."""
        return await guarded(
            auth,
            lambda: runregistry.get_dataset(http, run_number=run_number, dataset_name=dataset_name),
        )

    @server.tool(
        name="runregistry_get_lumisection_ranges",
        description=(
            "Lumisection certification ranges of one Run Registry dataset of "
            "one run: [start, end] ranges with per-component "
            "status/comment/cause (the building blocks of golden JSONs). "
            "dataset_name='online' (default) selects the online record; use "
            "runregistry_list_run_datasets for other names. Set "
            "by_component=true to get ranges grouped per 'workspace-column' "
            "key instead of one range list. For OMS rates/luminosity per "
            "lumisection use cmsonline_list_lumisections." + _AUTH_DOC
        ),
    )
    async def runregistry_get_lumisection_ranges(
        run_number: Annotated[int, Field(ge=1)],
        dataset_name: Annotated[str, Field(min_length=1)] = "online",
        by_component: bool = False,
    ) -> ToolEnvelope:
        """Lumisection certification ranges."""
        return await guarded(
            auth,
            lambda: runregistry.get_lumisection_ranges(
                http,
                run_number=run_number,
                dataset_name=dataset_name,
                by_component=by_component,
            ),
        )
