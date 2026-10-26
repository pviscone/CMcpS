"""MCP tool registration for the CMS Document Database (DocDB) adapter."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import docdb
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded


def register_docdb_tools(
    server: MCPServer,
    _settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only cms-docdb.cern.ch tools."""

    @server.tool(
        name="docdb_get_document",
        description=(
            "Full metadata of one CMS DocDB document (cms-docdb.cern.ch, SSO "
            "required) by numeric docid (e.g. 14372 from a DocDB URL), parsed "
            "from the ShowDocument page: canonical id (CMS-doc-N-vM), title, "
            "type, submitter/updater, created/revised dates, viewable and "
            "modifiable groups, abstract, files with download URLs for "
            "docdb_download_file, version history, topic paths, authors, "
            "keywords. Missing or non-visible documents report an error. "
            "version selects an older revision of the document."
        ),
    )
    async def docdb_get_document(
        docid: Annotated[int, Field(description="Numeric DocDB document id (e.g. 14372)", gt=0)],
        version: Annotated[int | None, Field(gt=0)] = None,
    ) -> ToolEnvelope:
        """DocDB document metadata page."""
        return await guarded(auth, lambda: docdb.get_document(http, docid=docid, version=version))

    @server.tool(
        name="docdb_search",
        description=(
            "Search internal CMS documents on cms-docdb.cern.ch (SSO "
            "required): DocDB simple search (simpletext, across "
            "title/abstract/keywords/authors) and/or title-only search; "
            "updated_from/updated_to (YYYY-MM-DD) restrict the modification "
            "date. Returns one row per document: docid (follow up with "
            "docdb_get_document), canonical id, title, first author, topics, "
            "last-update date. Pagination is client-side via offset/limit."
        ),
    )
    async def docdb_search(
        *,
        text: Annotated[
            str | None, Field(description="Full-text query (DocDB simple search)")
        ] = None,
        title: Annotated[str | None, Field(description="Title-only query words")] = None,
        updated_from: Annotated[str | None, Field(description="ISO date YYYY-MM-DD")] = None,
        updated_to: Annotated[str | None, Field(description="ISO date YYYY-MM-DD")] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> ToolEnvelope:
        """DocDB document search (simple and/or title)."""
        return await guarded(
            auth,
            lambda: docdb.search_documents(
                http,
                text=text,
                title=title,
                updated_from=updated_from,
                updated_to=updated_to,
                limit=limit,
                offset=offset,
            ),
        )

    @server.tool(
        name="docdb_list_recent",
        description=(
            "List CMS DocDB documents modified in the last `days` days "
            "(SSO required; DocDB 'see recent' list), same row shape as "
            "docdb_search: docid (follow up with docdb_get_document), "
            "title, first author, topics, last-update date."
        ),
    )
    async def docdb_list_recent(
        days: Annotated[int, Field(ge=1, le=3650)] = 20,
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> ToolEnvelope:
        """Recently modified DocDB documents."""
        return await guarded(
            auth, lambda: docdb.list_recent(http, days=days, limit=limit, offset=offset)
        )

    @server.tool(
        name="docdb_download_file",
        description=(
            "Download one CMS DocDB file (RetrieveFile) into local CMcpS "
            "storage; take docid, filename (and optionally version) from the "
            "files list reported by docdb_get_document. SSO required."
        ),
    )
    async def docdb_download_file(
        docid: Annotated[int, Field(description="Numeric DocDB document id", gt=0)],
        filename: Annotated[str, Field(description="Exact file name from docdb_get_document")],
        version: Annotated[int | None, Field(gt=0)] = None,
    ) -> ToolEnvelope:
        """Bounded DocDB file download."""
        return await guarded(
            auth,
            lambda: docdb.download_file(http, docid=docid, filename=filename, version=version),
        )
