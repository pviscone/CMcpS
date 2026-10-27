"""Read-only adapter for CERN EDMS (edms.cern.ch).

Endpoint type: documented JSON REST WS ("EDMS REST WS 1.0.0", apidoc 0.29.0
at https://edms.cern.ch/ws/apidoc/index.html). Discovered and verified live
with an SSO session (2026-10):

- ``GET /ws/api/documents/{number}[/{version}]`` — document detail: title,
  authors, dates, context, type, release procedure, status, visibility,
  keywords/extRef and the ``files`` list (name/size/lastModified*, optional
  ``convertedPdfUrl``). ``version`` is optional (latest version); the literal
  ``LAST_RELEASED`` resolves to the latest released version. Documents the
  caller cannot read (restricted visibility) are masked exactly like missing
  ones — HTTP 400, mapped to "not found or not visible" (verified live with
  an AT-MEL corpus doc: listed by the parent-scoped search, detail denied).
- ``GET /ws/api/documents?<filters>`` — parent-structure listing (no
  full-text query): ``parentObjectType/parentObjectId[/
  parentObjectVersion]/assetAndJobs`` scope it; ``type/context/status/
  releaseProcedure/attributes/obsolete`` narrow it further. WITHOUT a
  parent scope the endpoint returns EMPTY for ordinary accounts (verified
  live 2026-10). Envelope ``{"exitInfo": [], "documents": [...]}`` with no
  server-side paging or totals — pagination is client-side. The listing
  can show metadata of documents whose detail the caller cannot read.
- ``GET /ws/api/files/{number}/{version}/{fileName}`` — raw file download
  (bounded, into CMcpS storage).
- ``GET /ws/api/documentTypes?contextId=&cadimType=`` — document types
  (bare JSON array of ``typeId/typeName/typeDesc``). ``contextId`` is
  mandatory (400 otherwise, despite the docs listing it among filters).
- ``GET /ws/api/users/current`` — the SSO caller's EDMS identity.

``GET /ws/api/contexts`` is documented but answers HTTP 500 for every
parameter combination (bare and all ``template`` values, verified live
2026-10-27 and cross-checked through a full browser session), so no
context-listing tool exists; context ids/names (e.g. ``ORG-003107``,
``EDMS-SERVICE-PUBLIC``, ``PLM``) are read from document details.

Auth: the whole ``/ws`` tree is an OAuth2 client (``edms_oidc_ws_prod``)
fronted by CERN Keycloak; anonymous requests answer ``302`` into the silent
OIDC handshake, which the shared client follows with the stored SSO cookies —
no per-service token or bearer exchange is needed.

Documented but intentionally not exposed (creation/write UX, not useful for
read-only lookup): assets, config profiles/properties, document type
attributes, equipment codes, release procedures, visibilities, structure
views, thumbnails. All mutating routes (create document/version, status
changes, attach/detach, file upload) are never called; the safety layer
permits GET only.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from cmcps.adapters.common import json_body
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient

SOURCE = "edms"

BASE = "https://edms.cern.ch"
API = f"{BASE}/ws/api"
DOCUMENTS_URL = f"{API}/documents"
FILES_URL = f"{API}/files"
DOCUMENT_TYPES_URL = f"{API}/documentTypes"
CURRENT_USER_URL = f"{API}/users/current"

_MAX_NUMBER = 99_999_999_999
_NOT_FOUND = 404
_BAD_REQUEST = 400
_LAST_RELEASED = "LAST_RELEASED"

# Live version tokens include drawing-letter releases (AI, AC, AG, ...) and
# decimals: they are opaque path segments — keep traversal chars out only.
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]*$")
# Real EDMS file names include leading-dash drawing files ("----atl109460004-v.AI.pdf");
# leading dots stay rejected (dotfiles/traversal), '..' and slashes are rejected separately.
_FILENAME_ALLOWED_RE = re.compile(r"^[A-Za-z0-9-][A-Za-z0-9 _.,+()\[\]#@'~-]{0,254}$")
_CADIM_TYPES = frozenset({"TEXTFILE", "DRAWING", "LAYOUT", "SCHEMATIC", "3D_MODEL"})

_FALSE = "false"
_TRUE = "true"


def _check_document_number(document_number: str) -> str:
    value = str(document_number).strip()
    if not value.isdigit() or not 1 <= int(value) <= _MAX_NUMBER:
        raise InvalidInputError(
            "document_number must be a numeric EDMS document id (e.g. 188033)",
            instructions=(
                "Find ids in EDMS URLs (edms.cern.ch/document/<id>/<version>) "
                "or via edms_search_documents."
            ),
        )
    return value


def _check_version(version: str | None) -> str | None:
    if version is None:
        return None
    value = version.strip()
    if value.upper() == _LAST_RELEASED:
        return _LAST_RELEASED
    if not _VERSION_RE.fullmatch(value):
        raise InvalidInputError(
            "version must be a version token like '1', '1.2' or 'AI', or 'LAST_RELEASED'",
            instructions=(
                "Omit version for the latest version; EDMS drawing versions can be "
                "letters too ('AI', 'AC', ...)."
            ),
        )
    return value


def _check_filename(filename: str) -> str:
    value = filename.strip()
    if (
        not _FILENAME_ALLOWED_RE.fullmatch(value)
        or value.startswith(".")
        or ".." in value
        or "/" in value
        or "\\" in value
    ):
        raise InvalidInputError(
            "filename must be a plain EDMS file name (no directories or '..')",
            instructions="Copy the exact name from the files list of edms_get_document.",
        )
    return value


def _document_item(doc: dict[str, Any]) -> dict[str, Any]:
    edms_id = doc.get("edmsId")
    version = doc.get("version")
    item = {
        "edms_id": edms_id,
        "version": version,
        "title": doc.get("title"),
        "creation_date": doc.get("creationDate"),
        "modification_date": doc.get("modificationDate"),
        "ext_ref": doc.get("extRef"),
        "context": doc.get("context"),
        "type": doc.get("type"),
        "type_attributes": doc.get("typeAttributes"),
        "release_proc": doc.get("releaseProc"),
        "status": doc.get("status"),
        "obsolete": doc.get("obsolete"),
    }
    if edms_id and version:
        item["url"] = f"{BASE}/document/{edms_id}/{version}"
    return item


async def search_documents(
    client: SafeHttpClient,
    *,
    parent_object_type: str | None = None,
    parent_object_id: str | None = None,
    parent_object_version: str | None = None,
    asset_and_jobs: bool = False,
    type: str | None = None,
    attributes: str | None = None,
    context: str | None = None,
    status: str | None = None,
    release_procedure: str | None = None,
    obsolete: bool | None = None,
    limit: int = 20,
    offset: int = 0,
) -> SearchResult:
    """Parent-scoped EDMS document listing with narrowing filters."""
    if not limit >= 1:
        raise InvalidInputError("limit must be >= 1")
    if offset < 0:
        raise InvalidInputError("offset must be >= 0")
    filters = {
        "parentObjectType": parent_object_type,
        "parentObjectId": parent_object_id,
        "parentObjectVersion": parent_object_version,
        "type": type,
        "attributes": attributes,
        "context": context,
        "status": status,
        "releaseProcedure": release_procedure,
    }
    params: dict[str, str | int] = {
        key: value for key, value in filters.items() if value is not None
    }
    if not params and obsolete is None:
        raise InvalidInputError(
            "at least one filter is required (parent_object_type/parent_object_id, "
            "status, type, context, release_procedure, attributes, or obsolete)",
            instructions=(
                "For a useful result pass a parent scope, e.g. parent_object_type='A' "
                "with an asset id — without a scope this endpoint returns empty "
                "for ordinary accounts (discovery happens in the EDMS UI)."
            ),
        )
    if asset_and_jobs:
        params["assetAndJobs"] = _TRUE
    if obsolete is not None:
        params["obsolete"] = _TRUE if obsolete else _FALSE
    response = await client.request("GET", DOCUMENTS_URL, params=params)
    payload = json_body(response, source="edms documents")
    if not isinstance(payload, dict) or not isinstance(payload.get("documents"), list):
        raise UpstreamError(
            "edms returned an unexpected documents payload",
            instructions="Check https://edms.cern.ch/ws/apidoc/index.html for API changes.",
        )
    documents = payload["documents"]
    items = [_document_item(doc) for doc in documents if isinstance(doc, dict)]
    total = len(items)
    windowed = items[offset : offset + limit]
    return SearchResult(
        source=SOURCE,
        items=windowed,
        total_count=total,
        page=offset // limit + 1,
        size=limit,
        has_more=offset + len(windowed) < total,
        note=(
            "EDMS REST search is a parent-structure listing with narrowing filters "
            "(no full-text query, no server-side paging/totals); without a parent "
            "scope it returns empty for ordinary accounts. Pagination is "
            "client-side. Detail access may be denied for listed documents you "
            "are not allowed to read."
        ),
    )


async def get_document(
    client: SafeHttpClient,
    *,
    document_number: str,
    version: str | None = None,
) -> dict[str, Any]:
    """Full metadata of one EDMS document version."""
    number = _check_document_number(document_number)
    version_value = _check_version(version)
    url = f"{DOCUMENTS_URL}/{number}"
    if version_value is not None:
        url = f"{url}/{quote(version_value, safe='')}"
    response = await client.request("GET", url)
    if response.status_code in (_BAD_REQUEST, _NOT_FOUND):
        # Unknown document/version: 400 with a JSON {"exitInfo": [...]} body
        # (verified live); 404 kept for defense in depth.
        raise UpstreamError(
            f"edms document {number} (version {version_value or 'latest'}) "
            "not found or not visible to this session",
            instructions="Check the document number and version; SSO-visible documents only.",
        )
    payload = json_body(response, source=f"edms document {number}")
    if not isinstance(payload, dict) or payload.get("edmsId") is None:
        raise UpstreamError(
            f"edms returned an unexpected payload for document {number}",
            instructions="Check https://edms.cern.ch/ws/apidoc/index.html for API changes.",
        )
    return {"source": SOURCE, **payload}


async def download_file(
    client: SafeHttpClient,
    *,
    document_number: str,
    filename: str,
    version: str | None = None,
) -> dict[str, Any]:
    """Bounded download of one file of an EDMS document."""
    number = _check_document_number(document_number)
    name = _check_filename(filename)
    version_value = _check_version(version) or _LAST_RELEASED
    url = f"{FILES_URL}/{number}/{quote(version_value, safe='')}/{quote(name, safe='')}"
    final = await client.download(url, Path(name))
    return {
        "source": SOURCE,
        "document_number": number,
        "version": version_value,
        "filename": name,
        "download_url": url,
        "path": str(final),
        "size_bytes": final.stat().st_size,
    }


async def list_document_types(
    client: SafeHttpClient,
    *,
    context_id: str,
    cadim_type: str | None = None,
) -> SearchResult:
    """EDMS document types of one context (contextId is mandatory upstream)."""
    if not context_id or not context_id.strip():
        raise InvalidInputError(
            "context_id is required (e.g. 'ORG-003107')",
            instructions="List context ids with edms_list_contexts.",
        )
    params: dict[str, str] = {"contextId": context_id.strip()}
    if cadim_type:
        value = cadim_type.upper()
        if value not in _CADIM_TYPES:
            raise InvalidInputError(
                f"cadim_type must be one of {sorted(_CADIM_TYPES)}",
                instructions="Omit cadim_type to list every document type.",
            )
        params["cadimType"] = value
    response = await client.request("GET", DOCUMENT_TYPES_URL, params=params)
    payload = json_body(response, source="edms document types")
    if not isinstance(payload, list):
        raise UpstreamError(
            "edms returned an unexpected document-types payload",
            instructions="Check https://edms.cern.ch/ws/apidoc/index.html for API changes.",
        )
    items = [item for item in payload if isinstance(item, dict)]
    return SearchResult(
        source=SOURCE,
        items=items,
        total_count=len(items),
        size=len(items),
        has_more=False,
    )


async def get_current_user(client: SafeHttpClient) -> dict[str, Any]:
    """EDMS identity of the SSO caller (uic, username, email, display name)."""
    response = await client.request("GET", CURRENT_USER_URL)
    payload = json_body(response, source="edms current user")
    if not isinstance(payload, dict) or payload.get("username") is None:
        raise UpstreamError(
            "edms returned an unexpected current-user payload",
            instructions="The session may lack an EDMS account; check the apidoc for API changes.",
        )
    return {"source": SOURCE, **payload}
