"""Read-only adapter for CMS Data Aggregation System (cmsweb.cern.ch/das).

Endpoint type: DAS REST API (documented at
https://github.com/dmwm/DAS):

- ``GET /das/request?view=list&limit=..&idx=..&input=<DAS QL>&instance=...``
  — one endpoint for every query; DAS QL examples:
  ``dataset=/TT*/RunII*/AODSIM``, ``dataset dataset=/X/Y/Z``,
  ``site site=T2_CH_CERN``, ``file file=...``, ``block block=...``.
  Anonymous probing returns ``401`` — cmsweb is fronted by CERN SSO; the
  shared client injects the session cookies.
- Response: ``{"status", "mongo_query", "nresults", "timestamp", "data"}``
  where ``data[i]`` is a dict whose key is the entity name
  (``dataset``/``block``/``file``/``site``/``run``/``lumi``/``summary``) and
  whose value is a list of attribute dicts. Duplicate rows arrive per
  replica; the adapter deduplicates on the primary key (``name``).

Limits: 429 responses are treated as retryable upstream errors; ``limit``
is capped at 50 per call (DAS throttles aggressively), ``idx`` is the
offset for paging.
"""

from __future__ import annotations

import re
from typing import Any

from cmcps.adapters.common import json_body
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient

SOURCE = "das"

BASE = "https://cmsweb.cern.ch/das/request"

_MAX_LIMIT = 50
_DATASET_RE = re.compile(r"^/?[A-Za-z0-9_*][A-Za-z0-9_.*~-]*/[^/]+/[A-Za-z0-9_*-]+/?$")
_UNSAFE_QL_RE = re.compile(r"[;`'\"$]")


def _check_query(query: str) -> str:
    cleaned = query.strip()
    if not cleaned:
        raise InvalidInputError(
            "query must not be empty",
            instructions="Use DAS QL, e.g. dataset=/TT*/RunII*/AODSIM or dataset dataset=/X/Y/Z.",
        )
    if _UNSAFE_QL_RE.search(cleaned):
        raise InvalidInputError(
            "query contains unsafe characters",
            instructions="Pass a plain DAS QL expression; no shell metacharacters.",
        )
    return cleaned


async def search(
    client: SafeHttpClient,
    *,
    query: str,
    limit: int = 10,
    offset: int = 0,
    instance: str = "prod/global",
) -> SearchResult:
    """DAS QL search (datasets, blocks, files, sites, runs, lumis)."""
    ql = _check_query(query)
    if not 1 <= limit <= _MAX_LIMIT:
        raise InvalidInputError(
            f"limit must be between 1 and {_MAX_LIMIT}",
            instructions="DAS throttles large pages; page with offset (idx).",
        )
    if offset < 0:
        raise InvalidInputError("offset must be >= 0")
    if not instance or any(c in instance for c in " ;'\""):
        raise InvalidInputError(
            f"invalid DAS instance {instance!r}",
            instructions="Common values: prod/global, prod/phys01, prod/phys02, prod/phys03.",
        )
    response = await client.request(
        "GET",
        BASE,
        params={
            "view": "list",
            "limit": limit,
            "idx": offset,
            "input": ql,
            "instance": instance,
        },
    )
    payload = json_body(response, source="das")
    if not isinstance(payload, dict):
        raise UpstreamError(
            "das returned a non-object payload",
            instructions="The query may be malformed; DAS errors are string responses.",
        )
    rows = payload.get("data") or []
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not row:
            continue
        entity, values = next(iter(row.items()))
        attrs = values[0] if isinstance(values, list) and values else values
        if not isinstance(attrs, dict):
            continue
        name = attrs.get("name") or attrs.get("block_name") or attrs.get("file")
        key = f"{entity}:{name}"
        if name is not None and key in seen:
            continue
        seen.add(key)
        items.append({"entity": entity, **attrs})
    nresults = payload.get("nresults")
    return SearchResult(
        source=SOURCE,
        query=f"[{instance}] {ql}",
        items=items,
        total_count=nresults if isinstance(nresults, int) else None,
        page=(offset // limit) + 1,
        size=len(items),
        has_more=isinstance(nresults, int) and offset + len(items) < nresults,
        note="Rows are deduplicated per replica. Entities: dataset, block, "
        "file, site, run, lumi, summary.",
    )


async def get_dataset_info(
    client: SafeHttpClient,
    *,
    dataset: str,
) -> dict[str, Any]:
    """Attribute record of one exact dataset (/primary/processed/tier)."""
    cleaned = dataset.strip()
    if not _DATASET_RE.fullmatch(cleaned) or "*" in cleaned:
        raise InvalidInputError(
            f"invalid exact dataset name {dataset!r}",
            instructions="Format: /primary/processed/tier without wildcards.",
        )
    if not cleaned.startswith("/"):
        cleaned = f"/{cleaned}"
    result = await search(
        client, query=f"dataset={cleaned}", limit=_MAX_LIMIT, instance="prod/global"
    )
    if not result.items:
        raise UpstreamError(
            f"dataset {cleaned} not found in DAS (prod/global)",
            instructions="Check the name; try das_search with a wildcard query.",
        )
    rows = [item for item in result.items if item.get("name") == cleaned]
    if not rows:
        raise UpstreamError(
            f"das returned no exact match for {cleaned}",
            instructions="Try das_search with the wildcard form.",
        )
    item = rows[0]
    item["source"] = SOURCE
    item["url"] = f"https://cmsweb.cern.ch/das/request?input=dataset%3D{cleaned}"
    return item
