"""Read-only adapter for the CMS Search service (cms-search.web.cern.ch).

Endpoint type: FastAPI with a public OpenAPI spec
(``https://cms-search.web.cern.ch/openapi.json``, title "CERN Batch Docs
Search"). Every route proxies through Keycloak, so browser cookies from the
shared CERN SSO session are required; there is no anonymous surface.

Endpoints (verified live except the document route):
- ``GET /search?q&size&from&sort&date_from&date_to&indices&rank`` — unified
  search across CMS/CERN IT doc sources; envelope ``{query, count, total,
  offset, limit, rank, results: [{document_id, score, index, base_key, tags,
  title, location, url, snippet, date}]}``. ``indices`` is a repeated query
  param. ``rank`` values are backend-defined (e.g.
  ``relevance_pagerank_recent``) and passed through.
- ``GET /api/documents/{source_key}/{document_id}?offset&limit`` — full
  document content; response shape returned as-is (top-level mapping).
- ``GET /api/docs-config`` — ``{docs: {<source_key>: {index_name, tags,
  url}}}`` for the ~20-plus indexed sources.
- ``GET /api/index-stats`` — ``{stats: {<source_key>: {count,
  last_updated_at, last_updated_source, latest_document_date}}}``.
"""

from __future__ import annotations

from typing import Any

from cmcps.adapters.common import json_body
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient

SOURCE = "cmssearch"

BASE = "https://cms-search.web.cern.ch"

_MAX_SIZE = 50
_NOT_FOUND = 404


async def search(
    client: SafeHttpClient,
    *,
    query: str,
    size: int = 10,
    offset: int = 0,
    sort: str | None = None,
    rank: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    indices: list[str] | None = None,
) -> SearchResult:
    """Unified search across the indexed CMS/CERN documentation sources."""
    if not query.strip():
        raise InvalidInputError("query must not be empty")
    if not 1 <= size <= _MAX_SIZE:
        raise InvalidInputError(f"size must be between 1 and {_MAX_SIZE}")
    if offset < 0:
        raise InvalidInputError("offset must be >= 0")
    params: dict[str, Any] = {"q": query, "size": size, "from": offset}
    if sort:
        params["sort"] = sort
    if rank:
        params["rank"] = rank
    if date_from:
        params["date_from"] = date_from
    if date_to:
        params["date_to"] = date_to
    if indices:
        params["indices"] = [i for i in indices if i]
    response = await client.request("GET", f"{BASE}/search", params=params)
    payload = json_body(response, source="cmssearch search")
    rows = payload.get("results") if isinstance(payload, dict) else None
    if rows is None:
        raise UpstreamError(
            "cms-search returned an unexpected search payload",
            instructions="Check https://cms-search.web.cern.ch/openapi.json for API changes.",
        )
    items = [dict(r) for r in rows if isinstance(r, dict)]
    total = payload.get("total")
    return SearchResult(
        source=SOURCE,
        query=query,
        items=items,
        total_count=total if isinstance(total, int) else None,
        page=offset // size + 1,
        size=len(items),
        has_more=isinstance(total, int) and offset + len(items) < total,
        sort=sort or rank,
        note=f"offset={offset}",
    )


async def get_document(
    client: SafeHttpClient,
    *,
    source_key: str,
    document_id: str,
    offset: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Full content of one indexed document (ids come from search results)."""
    if not source_key.strip():
        raise InvalidInputError("source_key must not be empty")
    if not document_id.strip():
        raise InvalidInputError("document_id must not be empty")
    params: dict[str, int] = {}
    if offset is not None:
        if offset < 0:
            raise InvalidInputError("offset must be >= 0")
        params["offset"] = offset
    if limit is not None:
        if limit < 1:
            raise InvalidInputError("limit must be >= 1")
        params["limit"] = limit
    response = await client.request(
        "GET",
        f"{BASE}/api/documents/{source_key}/{document_id}",
        params=params or None,
    )
    if response.status_code == _NOT_FOUND:
        raise UpstreamError(
            f"document {document_id!r} in source {source_key!r} not found",
            instructions="Get document ids and source keys from cmssearch_search results.",
        )
    payload = json_body(response, source=f"cmssearch document {document_id}")
    if not isinstance(payload, dict):
        raise UpstreamError(
            "cms-search returned a non-mapping document payload",
            instructions="Check https://cms-search.web.cern.ch/openapi.json for API changes.",
        )
    return {
        "source": SOURCE,
        "source_key": source_key,
        "document_id": document_id,
        "document": payload,
    }


async def list_sources(client: SafeHttpClient) -> SearchResult:
    """Indexed documentation sources (source_key, index, tags, URL)."""
    response = await client.request("GET", f"{BASE}/api/docs-config")
    payload = json_body(response, source="cmssearch docs-config")
    docs = payload.get("docs") if isinstance(payload, dict) else None
    if not isinstance(docs, dict):
        raise UpstreamError(
            "cms-search returned an unexpected docs-config payload",
            instructions="Check https://cms-search.web.cern.ch/openapi.json for API changes.",
        )
    items = [
        {
            "source_key": key,
            "index_name": cfg.get("index_name"),
            "tags": cfg.get("tags") or [],
            "url": cfg.get("url"),
        }
        for key, cfg in sorted(docs.items())
        if isinstance(cfg, dict)
    ]
    return SearchResult(
        source=SOURCE,
        items=items,
        total_count=len(items),
        size=len(items),
        has_more=False,
    )


async def index_stats(client: SafeHttpClient, *, source_key: str | None = None) -> SearchResult:
    """Index sizes and update timestamps, optionally for one source only."""
    response = await client.request("GET", f"{BASE}/api/index-stats")
    payload = json_body(response, source="cmssearch index-stats")
    stats = payload.get("stats") if isinstance(payload, dict) else None
    if not isinstance(stats, dict):
        raise UpstreamError(
            "cms-search returned an unexpected index-stats payload",
            instructions="Check https://cms-search.web.cern.ch/openapi.json for API changes.",
        )
    items = [
        {
            "source_key": key,
            "count": row.get("count"),
            "last_updated_at": row.get("last_updated_at"),
            "latest_document_date": row.get("latest_document_date"),
        }
        for key, row in sorted(stats.items())
        if isinstance(row, dict) and (source_key is None or key == source_key)
    ]
    return SearchResult(
        source=SOURCE,
        items=items,
        total_count=len(items),
        size=len(items),
        has_more=False,
    )
