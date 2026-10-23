"""MCP tool registration for the CMS Online E-Log adapter."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import elog
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded


def register_elog_tools(
    server: MCPServer,
    _settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only CMS Online E-Log tools."""

    @server.tool(
        name="cmsonline_elog_search",
        description=(
            "Search the CMS Online E-Log (shifter logbook). text searches "
            "entry bodies; optional subject/author filters, date range "
            "(from_date/to_date YYYY-MM-DD), numeric sub_id restricts to one "
            "logbook (empty = every logbook, via "
            "cmsonline_elog_list_logbooks). Page with offset."
        ),
    )
    async def cmsonline_elog_search(
        query: str,
        *,
        sub_id: str = "",
        author: str = "",
        subject: str = "",
        from_date: str = "2015-01-01",
        to_date: str = "2030-01-01",
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> ToolEnvelope:
        """E-Log entry search."""
        return await guarded(
            auth,
            lambda: elog.search(
                http,
                query=query,
                sub_id=sub_id,
                author=author,
                subject=subject,
                from_date=from_date,
                to_date=to_date,
                limit=limit,
                offset=offset,
            ),
        )

    @server.tool(
        name="cmsonline_elog_get_detail",
        description=(
            "Full CMS Online E-Log entry by msg_id: full body text and "
            "attachment links (from cmsonline_elog_search results)."
        ),
    )
    async def cmsonline_elog_get_detail(
        msg_id: Annotated[int, Field(ge=1)],
        sub_id: Annotated[int, Field(ge=1)] | None = None,
    ) -> ToolEnvelope:
        """E-Log entry detail."""
        return await guarded(auth, lambda: elog.get_detail(http, msg_id=msg_id, sub_id=sub_id))

    @server.tool(
        name="cmsonline_elog_list_logbooks",
        description=(
            "E-Log category/tree ids (cat_id/sub_id pairs identifying "
            "logbooks; sub_id feedback for cmsonline_elog_search)."
        ),
    )
    async def cmsonline_elog_list_logbooks() -> ToolEnvelope:
        """E-Log logbook listing."""
        return await guarded(auth, lambda: elog.list_categories(http))

    @server.tool(
        name="cmsonline_elog_download_attachment",
        description=(
            "Download one E-Log attachment (cmsonline.cern.ch/Elog/... URL "
            "from cmsonline_elog_get_detail) into local CMcpS storage."
        ),
    )
    async def cmsonline_elog_download_attachment(attachment_url: str) -> ToolEnvelope:
        """E-Log attachment download."""
        return await guarded(
            auth, lambda: elog.download_attachment(http, attachment_url=attachment_url)
        )
