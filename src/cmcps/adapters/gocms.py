"""Read-only adapter for the CMS shortlink service (gocms.cern.ch).

Endpoint type: FastAPI with a published OpenAPI spec
(``https://gocms.cern.ch/openapi.json``). NB: gocms.cern.ch is a shortlink
("go link") service for CMS, not the documentation wiki once assumed.

Anonymous-read endpoints (verified live, ``security: null`` in the spec):
- ``GET /api/links?search&tags&target_url&page&per_page&sort_by&sort_order``
  — paginated shortlinks; envelope ``{items, total, page, per_page,
  total_pages}``; items carry ``id, shortlink, target_url, description,
  created_by, created_at, updated_at, tags``.
- ``GET /api/links/{link_id}`` — one link by numeric id.
- ``GET /api/tags`` — all tags.
- ``GET /api/version`` — service version.

Mutating routes (POST/PUT/DELETE, auth endpoints) exist in the spec but are
never called; the safety layer permits GET only.
"""

from __future__ import annotations

from typing import Any

from cmcps.adapters.common import json_body
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient

SOURCE = "gocms"

BASE = "https://gocms.cern.ch"

_MAX_PER_PAGE = 100
_NOT_FOUND = 404


def _link_item(link: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": link.get("id"),
        "shortlink": link.get("shortlink"),
        "target_url": link.get("target_url"),
        "description": link.get("description"),
        "tags": link.get("tags") or [],
        "created_by": link.get("created_by"),
        "created_at": link.get("created_at"),
        "updated_at": link.get("updated_at"),
        "url": f"{BASE}/{link.get('shortlink')}",
    }


async def list_links(
    client: SafeHttpClient,
    *,
    search: str | None = None,
    tags: list[str] | None = None,
    page: int = 1,
    per_page: int = 20,
    sort_order: str = "desc",
) -> SearchResult:
    """Search/list CMS go-links with paginated envelope."""
    if page < 1:
        raise InvalidInputError("page must be >= 1")
    if not 1 <= per_page <= _MAX_PER_PAGE:
        raise InvalidInputError(f"per_page must be between 1 and {_MAX_PER_PAGE}")
    if sort_order not in ("asc", "desc"):
        raise InvalidInputError("sort_order must be 'asc' or 'desc'")
    params: dict[str, str | int] = {
        "page": page,
        "per_page": per_page,
        "sort_by": "updated_at",
        "sort_order": sort_order,
    }
    if search:
        params["search"] = search
    if tags:
        params["tags"] = ",".join(tags)
    response = await client.request("GET", f"{BASE}/api/links", params=params)
    payload = json_body(response, source="gocms links")
    rows = payload.get("items") if isinstance(payload, dict) else None
    if rows is None:
        raise UpstreamError(
            "gocms returned an unexpected links payload",
            instructions="Check https://gocms.cern.ch/docs for API changes.",
        )
    items = [_link_item(r) for r in rows if isinstance(r, dict)]
    total = payload.get("total")
    total_pages = payload.get("total_pages")
    return SearchResult(
        source=SOURCE,
        query=search,
        items=items,
        total_count=total if isinstance(total, int) else None,
        page=page,
        size=len(items),
        has_more=isinstance(total_pages, int) and page < total_pages,
    )


async def get_link(client: SafeHttpClient, *, link_id: int) -> dict[str, Any]:
    """One go-link by numeric id."""
    if link_id < 1:
        raise InvalidInputError("link_id must be >= 1")
    response = await client.request("GET", f"{BASE}/api/links/{link_id}")
    if response.status_code == _NOT_FOUND:
        raise UpstreamError(
            f"go-link {link_id} not found",
            instructions="List ids with gocms_list_links.",
        )
    payload = json_body(response, source=f"gocms link {link_id}")
    if not isinstance(payload, dict) or payload.get("id") is None:
        raise UpstreamError(
            f"gocms returned no link data for id {link_id}",
            instructions="List ids with gocms_list_links.",
        )
    return {"source": SOURCE, **_link_item(payload)}


async def list_tags(client: SafeHttpClient) -> dict[str, Any]:
    """All tags defined in the go-link service."""
    response = await client.request("GET", f"{BASE}/api/tags")
    payload = json_body(response, source="gocms tags")
    rows = payload if isinstance(payload, list) else payload.get("items") or []
    items = []
    for tag in rows:
        if isinstance(tag, dict):
            items.append({"tag": tag.get("name"), "id": tag.get("id")})
        else:
            items.append({"tag": tag, "id": None})
    return {"source": SOURCE, "tag_count": len(items), "items": items}
