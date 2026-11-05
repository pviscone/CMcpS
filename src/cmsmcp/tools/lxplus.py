"""MCP registration for lxplus (Kerberos SSH): read tools and confirmed actions."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from cmsmcp.adapters import lxplus
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.results import ToolEnvelope
from cmsmcp.policy import Effect, Policy
from cmsmcp.tools.common import enveloped

_SETUP = "Needs a Kerberos ticket and `cmsmcp setup lxplus`."
_APPROVAL = Annotated[
    str | None,
    Field(description="Only after the user ran `cmsmcp approve <id>`: that id."),
]


def register_lxplus_tools(  # noqa: C901 - one closure per tool
    server: MCPServer, settings: Settings, policy: Policy
) -> None:
    """Register lxplus tools (reads always; actions only when enabled)."""

    @server.tool(
        name="lxplus_eos_ls",
        description=(
            "List a directory on EOS (/eos/...) or AFS (/afs/...) as the user, via lxplus. "
            + _SETUP
        ),
    )
    async def lxplus_eos_ls(path: str) -> ToolEnvelope:
        return await enveloped(lambda: lxplus.eos_ls(settings, path=path))

    @server.tool(
        name="lxplus_read_file",
        description=(
            "Read the start of a text file on EOS/AFS/CVMFS (configs, logs, small CSV/JSON). "
            "Binary files (ROOT, PNG, PDF) must use lxplus_download_file. " + _SETUP
        ),
    )
    async def lxplus_read_file(
        path: str,
        max_bytes: Annotated[int, Field(ge=100, le=200_000)] = 20_000,
    ) -> ToolEnvelope:
        return await enveloped(lambda: lxplus.read_file(settings, path=path, max_bytes=max_bytes))

    @server.tool(
        name="lxplus_download_file",
        description=(
            "Copy one EOS/AFS file (ROOT, plot, PDF, ...) into local cmsmcp storage; returns "
            "the local path, never the content. " + _SETUP
        ),
    )
    async def lxplus_download_file(path: str) -> ToolEnvelope:
        return await enveloped(lambda: lxplus.download_file(settings, path=path))

    @server.tool(
        name="lxplus_quota",
        description="The user's EOS and AFS quota and usage. " + _SETUP,
    )
    async def lxplus_quota() -> ToolEnvelope:
        return await enveloped(lambda: lxplus.quota(settings))

    @server.tool(
        name="lxplus_condor_queue",
        description=(
            "The user's HTCondor jobs in the queue (idle/running/held), optionally one "
            "cluster. " + _SETUP
        ),
    )
    async def lxplus_condor_queue(
        cluster: Annotated[str | None, Field(description="Cluster id, e.g. 1234.")] = None,
    ) -> ToolEnvelope:
        return await enveloped(lambda: lxplus.condor_queue(settings, cluster=cluster))

    @server.tool(
        name="lxplus_condor_history",
        description="The user's recently finished HTCondor jobs with exit codes. " + _SETUP,
    )
    async def lxplus_condor_history(
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
    ) -> ToolEnvelope:
        return await enveloped(lambda: lxplus.condor_history(settings, limit=limit))

    # ------------------------------------------------------------- actions
    @server.tool(
        name="lxplus_condor_submit",
        effect=Effect.ACT_OWN,
        description=(
            "Submit an existing HTCondor submit file (on EOS/AFS) from its directory on "
            "lxplus. The user confirms every submission. " + _SETUP
        ),
    )
    async def lxplus_condor_submit(
        submit_file: str, ctx: Context, approval_id: _APPROVAL = None
    ) -> ToolEnvelope:
        async def run() -> dict:
            path = lxplus.check_path(submit_file)
            approval = await policy.confirm(
                ctx,
                tool="lxplus_condor_submit",
                summary=f"condor_submit {path} on lxplus",
                details={"submit_file": path},
                hosts=("lxplus",),
                approval_id=approval_id,
            )
            return await lxplus.condor_submit(settings, submit_file=path, approval=approval)

        return await enveloped(run)

    @server.tool(
        name="lxplus_condor_remove",
        effect=Effect.ACT_OWN,
        description="Remove one of the user's HTCondor jobs/clusters (confirmed). " + _SETUP,
    )
    async def lxplus_condor_remove(
        job_id: str, ctx: Context, approval_id: _APPROVAL = None
    ) -> ToolEnvelope:
        async def run() -> dict:
            job = lxplus.check_job_id(job_id)
            approval = await policy.confirm(
                ctx,
                tool="lxplus_condor_remove",
                summary=f"condor_rm {job} on lxplus",
                details={"job_id": job},
                hosts=("lxplus",),
                approval_id=approval_id,
            )
            return await lxplus.condor_remove(settings, job_id=job, approval=approval)

        return await enveloped(run)

    @server.tool(
        name="lxplus_run",
        effect=Effect.ACT_EXTERNAL,
        description=(
            "Run one shell command on lxplus as the user, after the user confirms the exact "
            "command. Prefer the specific lxplus_* tools; use this for anything else "
            "(e.g. `dasgoclient`, `crab status -d DIR`, `ls` in a work area). Commands "
            "touching credentials are refused. " + _SETUP
        ),
    )
    async def lxplus_run(
        command: str,
        ctx: Context,
        timeout_seconds: Annotated[int, Field(ge=5, le=900)] = 120,
        approval_id: _APPROVAL = None,
    ) -> ToolEnvelope:
        async def run() -> dict:
            checked = lxplus.check_generic_command(command)
            approval = await policy.confirm(
                ctx,
                tool="lxplus_run",
                summary="run a command on lxplus",
                details={"command": checked, "timeout_seconds": timeout_seconds},
                hosts=("lxplus",),
                approval_id=approval_id,
            )
            return await lxplus.run_command_on_lxplus(
                settings, command=checked, approval=approval, timeout_seconds=timeout_seconds
            )

        return await enveloped(run)
