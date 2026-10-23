"""Read-only adapter for cds.cern.ch.

Covers the search, record, and file-listing tools backed by the legacy
Invenio v1 ``/search`` endpoint with ``of=recjson``. The reference CDS MCP
server's OIDC client-credentials flow is known-broken, so CMcpS relies on
the shared SSO cookie jar injected by ``SafeHttpClient`` to pass the Anubis
challenge.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cmcps.adapters.common import json_body
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient, SafeResponse

SOURCE = "cds"
_DATE_FORMAT = "%Y-%m-%d"
_MAX_PAGE_SIZE = 100
_ERROR_STATUS_MIN = 400
_SEARCH_NOTE = "Total hit count is not exposed by the CDS search endpoint"
_BASE_URL = "https://cds.cern.ch"

_SORT_FIELDS = {
    "mostrecent": "d",
    "bestmatch": "r",
    "mostcited": "c",
}

_RECID_PREFIX = "cds:"
_ANUBIS_INSTRUCTIONS = (
    "CDS is serving an Anubis bot challenge; complete cern_auth_sign_in in a browser and retry."
)


def _url(settings: Settings, path: str) -> str:
    return f"{settings.cds_base_url.rstrip('/')}{path}"


def _cds_json(response: SafeResponse) -> Any:
    """Decode a CDS response, mapping Anubis challenge pages to a clear error."""
    body = response.content.decode("utf-8", errors="replace")
    content_type = response.headers.get("content-type", "").lower()
    if response.status_code < _ERROR_STATUS_MIN and (
        "text/html" in content_type or "anubis" in body.lower()
    ):
        raise UpstreamError(
            f"cds served an HTML page instead of JSON (content-type: {content_type or 'unknown'})",
            instructions=_ANUBIS_INSTRUCTIONS,
        )
    return json_body(response, source=SOURCE)


def _check_date(value: str, *, name: str) -> None:
    try:
        datetime.strptime(value, _DATE_FORMAT)
    except ValueError as exc:
        raise InvalidInputError(f"{name} must use YYYY-MM-DD, got {value!r}") from exc


def _year(value: str) -> str:
    return value[:4]


def _parse_recid(recid: str) -> int:
    raw = str(recid).strip()
    if raw.lower().startswith(_RECID_PREFIX):
        raw = raw[len(_RECID_PREFIX) :]
    if not re.fullmatch(r"\d+", raw):
        raise InvalidInputError(
            f"recid must be a numeric CDS record id, got {recid!r}",
            instructions="Pass a plain number (e.g. '12345') or 'cds:12345'.",
        )
    return int(raw)


def _build_query(
    query: str,
    *,
    experiment: str | None,
    doc_type: str | None,
    from_date: str | None,
    until_date: str | None,
) -> str:
    parts: list[str] = []
    if query and query.strip():
        parts.append(query.strip())
    if experiment:
        parts.append(f"experiment:{experiment}")
    if doc_type:
        parts.append(f"collection:{doc_type}")
    if from_date:
        _check_date(from_date, name="from_date")
    if until_date:
        _check_date(until_date, name="until_date")
    if from_date or until_date:
        start = _year(from_date) if from_date else _year(until_date)  # type: ignore[arg-type]
        end = _year(until_date) if until_date else _year(from_date)  # type: ignore[arg-type]
        parts.append(f"year:{start}-{end}")
    return " AND ".join(parts)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _author_affiliation(author: dict[str, Any]) -> list[str] | None:
    affiliation = author.get("affiliation")
    if affiliation is None:
        return None
    if isinstance(affiliation, list):
        return [str(item) for item in affiliation]
    return [str(affiliation)]


def _abstract(record: dict[str, Any]) -> str | None:
    abstract = record.get("abstract")
    if isinstance(abstract, dict):
        summary = abstract.get("summary")
        return str(summary) if summary is not None else None
    if abstract is not None:
        return str(abstract)
    return None


def _experiments(record: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for entry in _as_list(record.get("accelerator_experiment")):
        if isinstance(entry, dict):
            value = entry.get("experiment")
            if value and str(value) not in seen:
                seen.append(str(value))
    return seen


def _doc_types(record: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for entry in _as_list(record.get("collection")):
        if isinstance(entry, dict):
            value = entry.get("primary")
            if value and str(value) not in seen:
                seen.append(str(value))
    return seen


def _subjects(record: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for entry in _as_list(record.get("subject")):
        value = entry.get("term") if isinstance(entry, dict) else entry
        if value and str(value) not in seen:
            seen.append(str(value))
    return seen


def _mime_type(raw_file: dict[str, Any]) -> str | None:
    magic = raw_file.get("magic")
    if isinstance(magic, list) and len(magic) > 1 and magic[1]:
        return str(magic[1])
    return None


def _file_items(record: dict[str, Any]) -> list[dict[str, Any]]:
    recid = record.get("recid")
    items: list[dict[str, Any]] = []
    for raw_file in _as_list(record.get("files")):
        if not isinstance(raw_file, dict):
            continue
        full_name = raw_file.get("full_name") or raw_file.get("name")
        size = raw_file.get("size")
        size_bytes = int(size) if isinstance(size, (int, float)) else None
        items.append(
            {
                "name": full_name,
                "size_bytes": size_bytes,
                "size_mb": round(size_bytes / (1024 * 1024), 3) if size_bytes else None,
                "mime_type": _mime_type(raw_file),
                "url": f"{_BASE_URL}/record/{recid}/files/{full_name}"
                if full_name and recid is not None
                else None,
            }
        )
    return items


def _document(record: dict[str, Any]) -> dict[str, Any]:
    recid = record.get("recid")
    title = record.get("title")
    authors = record.get("authors") or []
    experiments = _experiments(record)
    doc_types = _doc_types(record)
    subjects = _subjects(record)
    files = _file_items(record)
    arxiv_id = record.get("arxiv_id") or record.get("arxiv")
    return {
        "cds_id": recid,
        "mcp_id": f"{_RECID_PREFIX}{recid}" if recid is not None else None,
        "title": title.get("title") if isinstance(title, dict) else title,
        "authors": [
            str(author.get("full_name"))
            for author in authors
            if isinstance(author, dict) and author.get("full_name")
        ],
        "abstract": _abstract(record),
        "experiment": "; ".join(experiments) if experiments else None,
        "doc_type": "; ".join(doc_types) if doc_types else None,
        "created": record.get("creation_date"),
        "updated": record.get("version_id"),
        "url": f"{_BASE_URL}/record/{recid}" if recid is not None else None,
        "file_count": len(files),
        "keywords": subjects,
        "subjects": subjects,
        "doi": record.get("doi"),
        "arxiv_id": str(arxiv_id) if arxiv_id else None,
    }


async def _fetch_recjson(
    client: SafeHttpClient,
    settings: Settings,
    params: dict[str, str | int],
) -> list[dict[str, Any]]:
    params = {"of": "recjson", **params}
    response = await client.request("GET", _url(settings, "/search"), params=params)
    data = _cds_json(response)
    if not isinstance(data, list):
        raise UpstreamError(
            "cds returned an unexpected recjson payload",
            instructions="The CDS search endpoint should return a JSON list of records.",
        )
    return [record for record in data if isinstance(record, dict)]


async def search(
    client: SafeHttpClient,
    settings: Settings,
    *,
    query: str,
    experiment: str | None = None,
    doc_type: str | None = None,
    from_date: str | None = None,
    until_date: str | None = None,
    size: int = 10,
    page: int = 1,
    sort: str = "mostrecent",
) -> SearchResult:
    """Search CDS records via the legacy Invenio recjson endpoint."""
    if not 1 <= size <= _MAX_PAGE_SIZE:
        raise InvalidInputError(f"size must be between 1 and {_MAX_PAGE_SIZE}, got {size}")
    if page < 1:
        raise InvalidInputError(f"page must be >= 1, got {page}")
    if sort not in _SORT_FIELDS:
        allowed = ", ".join(sorted(_SORT_FIELDS))
        raise InvalidInputError(f"sort must be one of: {allowed}, got {sort!r}")
    query_string = _build_query(
        query,
        experiment=experiment,
        doc_type=doc_type,
        from_date=from_date,
        until_date=until_date,
    )
    if not query_string:
        raise InvalidInputError(
            "a query or at least one filter (experiment, doc_type, dates) is required"
        )
    records = await _fetch_recjson(
        client,
        settings,
        {
            "p": query_string,
            "rg": size,
            "jrec": (page - 1) * size + 1,
            "sf": _SORT_FIELDS[sort],
        },
    )
    items = [_document(record) for record in records]
    return SearchResult(
        source=SOURCE,
        items=items,
        total_count=None,
        page=page,
        size=size,
        has_more=len(items) == size,
        query=query_string,
        sort=sort,
        note=_SEARCH_NOTE,
    )


async def _fetch_record(
    client: SafeHttpClient, settings: Settings, *, recid: str
) -> dict[str, Any]:
    numeric = _parse_recid(recid)
    records = await _fetch_recjson(client, settings, {"p": f"recid:{numeric}", "rg": 1, "jrec": 1})
    if not records:
        raise InvalidInputError(
            f"CDS record {numeric} was not found",
            instructions="Check the record id on cds.cern.ch and retry.",
        )
    return records[0]


async def get_record(client: SafeHttpClient, settings: Settings, *, recid: str) -> dict[str, Any]:
    """Fetch the full metadata of one CDS record."""
    record = await _fetch_record(client, settings, recid=recid)
    document = _document(record)
    authors = record.get("authors") or []
    document["authors_detailed"] = [
        {
            "name": str(author.get("full_name")),
            "affiliation": _author_affiliation(author),
            "orcid": None,
        }
        for author in authors
        if isinstance(author, dict) and author.get("full_name")
    ]
    document["files"] = _file_items(record)
    return document


async def get_files(client: SafeHttpClient, settings: Settings, *, recid: str) -> dict[str, Any]:
    """List the files attached to one CDS record (links only, never content)."""
    record = await _fetch_record(client, settings, recid=recid)
    files = _file_items(record)
    items = [
        {
            "name": item["name"],
            "size_bytes": item["size_bytes"],
            "size_mb": item["size_mb"],
            "mime_type": item["mime_type"],
            "download_url": item["url"],
        }
        for item in files
    ]
    recid_value = record.get("recid")
    return {
        "cds_id": recid_value,
        "mcp_id": f"{_RECID_PREFIX}{recid_value}" if recid_value is not None else None,
        "file_count": len(items),
        "items": items,
    }


_ICMS_STUB_MARKER = b"iCMS_login.jsp"
_PROBE_BYTES = 4096


async def download_file(
    client: SafeHttpClient,
    settings: Settings,
    *,
    download_url: str,
) -> dict[str, Any]:
    """Download one CDS attachment into CMcpS download storage.

    CDS internal-note files delegate to legacy iCMS, which answers with a
    login stub (``iCMS_login.jsp``) instead of the document. That wrapper is
    detected and rejected so login HTML is never delivered as a file.
    """
    host = urlsplit(download_url).hostname
    cds_host = urlsplit(settings.cds_base_url).hostname
    if not host or host != cds_host:
        raise InvalidInputError(
            f"cds_download_file only accepts URLs on {cds_host}",
            instructions="Pass a download_url returned by cds_get_files.",
        )
    filename = Path(urlsplit(download_url).path).name or None
    destination = Path(filename) if filename else None
    final = await client.download(download_url, destination)
    head = final.read_bytes()[:_PROBE_BYTES]
    if _ICMS_STUB_MARKER in head:
        final.unlink()
        raise UpstreamError(
            "The file is served by legacy iCMS (CMSFence) and cannot be fetched directly",
            instructions=(
                "This attachment routes through cmsfence.cern.ch, which has no documented "
                "read API yet; access it via the CDS record page in a browser."
            ),
        )
    if b"anubis" in head[:1024].lower():
        final.unlink()
        raise UpstreamError(
            "CDS answered with an Anubis bot challenge instead of the file",
            instructions=_ANUBIS_INSTRUCTIONS,
        )
    return {
        "source": SOURCE,
        "download_url": download_url,
        "path": str(final),
        "size_bytes": os.stat(final).st_size,
    }
