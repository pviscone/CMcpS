"""MCP tool registration for the CMS DQM GUI adapter."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import dqm
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import enveloped

_INSTANCE_FIELD = Field(
    description=(
        "DQM GUI instance: 'offline' (default, real-data PromptReco/PDs), "
        "'online' (P5 online), 'relval' (release validation), "
        "'relval-test', 'offline-test', 'dev'"
    )
)

_SAMPLE_FIELD = Field(description="CMS run number, e.g. 401605")
_DATASET_FIELD = Field(
    description=(
        "Exact DQM dataset (/primary/processed/tier, e.g. "
        "/ZeroBias/Run2026A-PromptReco-v1/DQMIO); no wildcards — find it with dqm_list_samples"
    )
)
_ME_FIELD = Field(
    description=(
        "Full monitoring-element path inside the ROOT file (no leading "
        "slash), e.g. JetMET/lumisec; list paths with dqm_get_workspace"
    )
)


def register_dqm_tools(
    server: MCPServer,
    _settings: Settings,
    http: SafeHttpClient,
) -> None:
    """Register the read-only DQM GUI tools (grid-cert auth, not SSO)."""

    @server.tool(
        name="dqm_list_samples",
        description=(
            "List (run, dataset) samples of one cmsweb.cern.ch DQM GUI "
            "instance — the entry point before dqm_get_workspace / "
            "dqm_get_monitoring_element / dqm_download_plot. Auth is the "
            "local grid client certificate (CMCPS_GRID_CERT_FILE/"
            "CMCPS_GRID_KEY_FILE, default ~/.globus), NOT the CERN SSO "
            "session. At least one of match/run is required (an empty query "
            "returns ~140 MB upstream)."
        ),
    )
    async def dqm_list_samples(
        match: Annotated[
            str | None,
            Field(
                description=(
                    "Dataset REGEX (Python re.search semantics, not glob: "
                    "write .* for any characters), e.g. "
                    "/ZeroBias/Run2026A.*/DQMIO. Run2026F* matches Run2026A "
                    "because F* means zero-or-more F."
                )
            ),
        ] = None,
        run: Annotated[int | None, Field(description="Exact run number, e.g. 401605")] = None,
        instance: Annotated[str, _INSTANCE_FIELD] = "offline",
    ) -> ToolEnvelope:
        """DQM sample listing."""
        return await enveloped(
            lambda: dqm.list_samples(http, instance=instance, match=match, run=run)
        )

    @server.tool(
        name="dqm_get_workspace",
        description=(
            "Folder content of one DQM sample (run+dataset): subfolders and "
            "monitoring elements with ROOT type, entries, axis statistics and "
            "quality-test results; follow dqm_list_samples, page down folders "
            "with folder=, filter ME names with search."
        ),
    )
    async def dqm_get_workspace(
        run: Annotated[int, _SAMPLE_FIELD],
        dataset: Annotated[str, _DATASET_FIELD],
        folder: Annotated[
            str,
            Field(
                description=(
                    "Folder inside the ROOT file (no leading slash), "
                    "e.g. JetMET; empty string lists the top level"
                )
            ),
        ] = "",
        search: Annotated[
            str | None, Field(description="Substring filter on ME names in this folder")
        ] = None,
        instance: Annotated[str, _INSTANCE_FIELD] = "offline",
    ) -> ToolEnvelope:
        """DQM folder listing."""
        return await enveloped(
            lambda: dqm.get_workspace(
                http, instance=instance, run=run, dataset=dataset, folder=folder, search=search
            )
        )

    @server.tool(
        name="dqm_get_monitoring_element",
        description=(
            "One monitoring element of a DQM sample: ROOT kind/type, entries, "
            "axis stats, quality tests (from the folder listing) plus "
            "JSON-serialized bin content when the ROOT type supports it "
            "(TH1/profiles; large/multi-dim objects may not be serialized "
            "upstream — their image is fetchable via dqm_download_plot)."
        ),
    )
    async def dqm_get_monitoring_element(
        run: Annotated[int, _SAMPLE_FIELD],
        dataset: Annotated[str, _DATASET_FIELD],
        me_path: Annotated[str, _ME_FIELD],
        instance: Annotated[str, _INSTANCE_FIELD] = "offline",
    ) -> ToolEnvelope:
        """DQM monitoring-element detail."""
        return await enveloped(
            lambda: dqm.get_monitoring_element(
                http, instance=instance, run=run, dataset=dataset, me_path=me_path
            )
        )

    @server.tool(
        name="dqm_download_plot",
        description=(
            "Download the rendered PNG of one monitoring element "
            "(explicit width/height required by the GUI) into local CMcpS "
            "download storage; the ME is verified first so a nonexistent "
            "path never yields the GUI's placeholder image."
        ),
    )
    async def dqm_download_plot(  # noqa: PLR0917
        run: Annotated[int, _SAMPLE_FIELD],
        dataset: Annotated[str, _DATASET_FIELD],
        me_path: Annotated[str, _ME_FIELD],
        width: Annotated[int, Field(ge=16, le=4096)] = 1000,
        height: Annotated[int, Field(ge=16, le=4096)] = 600,
        instance: Annotated[str, _INSTANCE_FIELD] = "offline",
    ) -> ToolEnvelope:
        """DQM plot download."""
        return await enveloped(
            lambda: dqm.download_plot(
                http,
                instance=instance,
                run=run,
                dataset=dataset,
                me_path=me_path,
                width=width,
                height=height,
            )
        )
