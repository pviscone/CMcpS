"""Read-only adapter for CMS XSDB (cross-section database).

Endpoint type: JSON API under
``https://xsecdb-xsdb-official.app.cern.ch/api/``. Discovery notes:

- Every route (including ``/api/...``) redirects anonymous clients to the
  Keycloak OIDC flow (``auth.cern.ch``). Route probes confirmed
  ``/api/search`` and ``/api/getLinkedTableNames`` answer 302 (exist) while
  made-up routes also 302, so only routes documented by XSDB users/wiki are
  called.
- Auth: CERN SSO session injected by the shared client; there is no
  anonymous read path (verified).
- ``GET /api/search?<filters>&pageNum=N&pageSize=N`` — cross-section
  records; known filter keys from the XSDB UI/wiki: ``particles``,
  ``process_name``, ``energy``, ``shower``, ``matrix_element``,
  ``order_in_alpha_s``, ``accuracy``, ``pdf_set_mean``, ``kFactor``.
  Records carry ``cross_section``, ``total_uncertainty``, ``DAS``, ``MCA``
  links, ``cuts``, ``refs``. Response is a bare JSON list; paging is
  fullness-based (no total).
- ``GET /api/getLinkedTableNames`` — table/link names the UI offers.

Limits: no pagination totals; SSO strictly required (no-CERN-SSO users get
the OIDC HTML, surfaced as an upstream error via the JSON decoder guard).
"""

from __future__ import annotations

from typing import Any

from cmcps.adapters.common import json_body
from cmcps.contracts.errors import InvalidInputError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient

SOURCE = "xsdb"

BASE = "https://xsecdb-xsdb-official.app.cern.ch"

_FILTERS = (
    "particles",
    "process_name",
    "energy",
    "shower",
    "matrix_element",
    "order_in_alpha_s",
    "accuracy",
    "pdf_set_mean",
    "kFactor",
)
_MAX_PAGE_SIZE = 100


async def search(
    client: SafeHttpClient,
    *,
    particles: str | None = None,
    process_name: str | None = None,
    energy: str | None = None,
    extra_filters: dict[str, str] | None = None,
    page_num: int = 1,
    page_size: int = 20,
) -> SearchResult:
    """Cross-section record search with the XSDB UI filter names."""
    if page_num < 1:
        raise InvalidInputError("page_num must be >= 1")
    if not 1 <= page_size <= _MAX_PAGE_SIZE:
        raise InvalidInputError(f"page_size must be between 1 and {_MAX_PAGE_SIZE}")
    params: dict[str, str | int] = {"pageNum": page_num, "pageSize": page_size}
    pairs = (("particles", particles), ("process_name", process_name), ("energy", energy))
    for key, value in pairs:
        if value:
            params[key] = value
    for key, value in (extra_filters or {}).items():
        if key not in _FILTERS:
            raise InvalidInputError(
                f"unknown xsdb filter {key!r}",
                instructions=f"Allowed: {', '.join(_FILTERS)}.",
            )
        params[key] = value
    response = await client.request("GET", f"{BASE}/api/search", params=params)
    payload = json_body(response, source="xsdb search")
    rows = payload if isinstance(payload, list) else payload.get("rows") or []
    items = [row for row in rows if isinstance(row, dict)]
    return SearchResult(
        source=SOURCE,
        query="; ".join(f"{k}={v}" for k, v in params.items() if not k.startswith("page")),
        items=items,
        total_count=None,
        page=page_num,
        size=len(items),
        has_more=len(items) >= page_size,
        note="XSDB returns a bare list without totals; page while has_more "
        "is true. Requires a completed CERN SSO session.",
    )


async def get_linked_tables(
    client: SafeHttpClient,
) -> dict[str, Any]:
    """Linked xsec summary-table names exposed by the XSDB UI."""
    response = await client.request("GET", f"{BASE}/api/getLinkedTableNames")
    payload = json_body(response, source="xsdb tables")
    rows = payload if isinstance(payload, list) else payload.get("tables") or []
    return {"source": SOURCE, "table_count": len(rows), "items": rows}
