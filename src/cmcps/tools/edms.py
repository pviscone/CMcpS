"""MCP tool registration for the CERN EDMS adapter."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import edms, edms_ui
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded


def register_edms_tools(
    server: MCPServer,
    _settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only edms.cern.ch tools."""

    @server.tool(
        name="edms_search_text",
        description=(
            "Full-text search over CERN EDMS documents (edms.cern.ch, SSO "
            "required) — the only text search available for EDMS, since the "
            "REST WS has none. Renders the deep-linkable UI search results "
            "page in a headless browser through the SSO session "
            "(auto-clicks 'Sign in' when needed) and parses the document "
            "grid. DOM-COUPLED fallback: an EDMS UI redeploy may break the "
            "parser (empty result instead of mis-parse). Page 1 only: up "
            "to 100 documents; narrow the query when has_more is true. The "
            "hit totals of other result sections (Projects, Items, "
            "Assets, ...) are reported in the note. Each item: "
            "document_number + version (follow up with edms_get_document), "
            "formal code (e.g. CMS-EC-SG-0004), title, status, authors, "
            "last-modified date, external reference, text snippet, and "
            "files (exact names + document numbers feed "
            "edms_download_file). ACCESSIBILITY: the search index is "
            "global metadata, so results can list documents you cannot "
            "open — those rows carry the UI's own access-denied marker and "
            "come back flagged accessible=false for free. Rows WITHOUT the "
            "marker (accessible=null) are openable in practice; pass "
            "check_access to REST-probe them into true/false, or pass "
            "access='accessible'/'restricted' to filter (implies the probe "
            "of unmarked rows; documents without numeric ids drop out of "
            "both filtered views)."
        ),
    )
    async def edms_search_text(
        query: Annotated[
            str,
            Field(description='Your EDMS UI search words (e.g. "ECAL milestones")'),
        ],
        latest_version: Annotated[
            bool,
            Field(description="Match against only the latest version of each document"),
        ] = False,
        hide_obsolete: Annotated[
            bool, Field(description="Exclude obsolete documents from the results")
        ] = True,
        check_access: Annotated[
            bool,
            Field(description="Probe every returned document number for readability (slower)"),
        ] = False,
        access: Annotated[
            str | None,
            Field(
                pattern="^(any|accessible|restricted)$",
                description="'any' (default), 'accessible', or 'restricted' — implies check_access",
            ),
        ] = None,
    ) -> ToolEnvelope:
        """EDMS UI full-text search (browser scrape)."""
        return await guarded(
            auth,
            lambda: edms_ui.search_text(
                auth,
                http,
                query=query,
                latest_version=latest_version,
                hide_obsolete=hide_obsolete,
                check_access=check_access,
                access=access or "any",
            ),
        )

    @server.tool(
        name="edms_search_documents",
        description=(
            "List CERN EDMS documents attached to a structure parent "
            "(edms.cern.ch, SSO required): parent_object_type + "
            "parent_object_id scope the listing (type 'A' with an asset id "
            "for documents attached to an equipment item), and "
            "status/type/context/release_procedure/attributes/obsolete "
            "narrow it further. IMPORTANT upstream caveat: WITHOUT a parent "
            "scope the endpoint returns EMPTY for ordinary accounts "
            "(verified live 2026-10) — this is a scoped structure listing, "
            "not a global filter search, and there is no full-text search "
            "in the EDMS REST WS (use the EDMS UI, then come back with a "
            "document number for edms_get_document). The parent-scoped "
            "listing can include documents whose detail your account "
            "cannot open (edms_get_document then reports 'not found or "
            "not visible'). Returns edms_id + version (follow up with "
            "edms_get_document), title, dates, ext_ref, context, type, "
            "status, obsolescence and the public URL. Pagination is "
            "client-side (offset/limit)."
        ),
    )
    async def edms_search_documents(
        *,
        parent_object_type: Annotated[
            str | None,
            Field(description="Parent object type (e.g. D - document, J - job, A - asset)"),
        ] = None,
        parent_object_id: Annotated[
            str | None,
            Field(description="Parent object id (e.g. an asset id like HCMB__A025-10004120)"),
        ] = None,
        parent_object_version: Annotated[
            str | None, Field(description="Parent object version (optional)")
        ] = None,
        asset_and_jobs: Annotated[
            bool, Field(description="Include assets and jobs in a parent-scoped search")
        ] = False,
        type: Annotated[
            str | None,
            Field(description="Document type filter (names from edms_list_document_types)"),
        ] = None,
        attributes: Annotated[str | None, Field(description="Type-attribute filter")] = None,
        context: Annotated[
            str | None, Field(description="Context filter (ids from edms_list_contexts)")
        ] = None,
        status: Annotated[
            str | None, Field(description="Status filter (e.g. 'Released', 'In Work')")
        ] = None,
        release_procedure: Annotated[
            str | None, Field(description="Release procedure filter (e.g. 'DOC-OWNER')")
        ] = None,
        obsolete: Annotated[bool | None, Field(description="Filter by obsolescence flag")] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> ToolEnvelope:
        """EDMS document filter search."""
        return await guarded(
            auth,
            lambda: edms.search_documents(
                http,
                parent_object_type=parent_object_type,
                parent_object_id=parent_object_id,
                parent_object_version=parent_object_version,
                asset_and_jobs=asset_and_jobs,
                type=type,
                attributes=attributes,
                context=context,
                status=status,
                release_procedure=release_procedure,
                obsolete=obsolete,
                limit=limit,
                offset=offset,
            ),
        )

    @server.tool(
        name="edms_get_document",
        description=(
            "Full metadata of one CERN EDMS document version "
            "(edms.cern.ch, SSO required) by numeric document number "
            "(e.g. 1525125 from an EDMS URL): title, authors, "
            "creation/modification dates, context, type, release procedure, "
            "status and status color, visibility, keywords, ext_ref, the "
            "canonical document URL, and the files list (name, size, last "
            "modified, optional converted PDF URL) — file names feed "
            "edms_download_file. version selects a version index ('1', "
            "'1.2') or 'LAST_RELEASED'; omitted = latest version."
        ),
    )
    async def edms_get_document(
        document_number: Annotated[
            str, Field(description="Numeric EDMS document id (e.g. 188033)")
        ],
        version: Annotated[
            str | None,
            Field(description="Version token ('1', '1.2', letter 'AI') or 'LAST_RELEASED'"),
        ] = None,
    ) -> ToolEnvelope:
        """EDMS document version detail."""
        return await guarded(
            auth,
            lambda: edms.get_document(http, document_number=document_number, version=version),
        )

    @server.tool(
        name="edms_download_file",
        description=(
            "Download one file of a CERN EDMS document (edms.cern.ch, SSO "
            "required) into local CMcpS storage: pass document_number, the "
            "exact filename from the files list of edms_get_document, and "
            "optionally version ('1', '1.2', default 'LAST_RELEASED'). "
            "Returns the local path and size; content is not inlined."
        ),
    )
    async def edms_download_file(
        document_number: Annotated[
            str, Field(description="Numeric EDMS document id (e.g. 1525125)")
        ],
        filename: Annotated[
            str,
            Field(description="Exact file name from edms_get_document (e.g. Searching_EDMS.pdf)"),
        ],
        version: Annotated[
            str | None,
            Field(description="Version token ('1', '1.2', letter 'AI') or 'LAST_RELEASED'"),
        ] = None,
    ) -> ToolEnvelope:
        """Bounded EDMS file download."""
        return await guarded(
            auth,
            lambda: edms.download_file(
                http, document_number=document_number, filename=filename, version=version
            ),
        )

    @server.tool(
        name="edms_list_document_types",
        description=(
            "List EDMS document types (typeId/typeName/typeDesc) usable as "
            "the 'type' filter of edms_search_documents within one context "
            "(context_id is required by EDMS — e.g. 'ORG-003107'; context "
            "ids appear in document details). Optionally restricted to a "
            "cadim_type category (TEXTFILE, DRAWING, LAYOUT, SCHEMATIC, "
            "3D_MODEL). SSO required."
        ),
    )
    async def edms_list_document_types(
        context_id: Annotated[str, Field(description="EDMS context id (e.g. ORG-003107)")],
        cadim_type: Annotated[
            str | None,
            Field(description="Category filter: TEXTFILE, DRAWING, LAYOUT, SCHEMATIC or 3D_MODEL"),
        ] = None,
    ) -> ToolEnvelope:
        """EDMS document types."""
        return await guarded(
            auth,
            lambda: edms.list_document_types(http, context_id=context_id, cadim_type=cadim_type),
        )

    @server.tool(
        name="edms_get_current_user",
        description=(
            "EDMS identity mapped to the current SSO session (uic, "
            "username, email, display name) — useful to confirm which "
            "account EDMS visibility/rights apply to after "
            "cern_auth_sign_in. SSO required."
        ),
    )
    async def edms_get_current_user() -> ToolEnvelope:
        """EDMS current user."""
        return await guarded(auth, lambda: edms.get_current_user(http))
