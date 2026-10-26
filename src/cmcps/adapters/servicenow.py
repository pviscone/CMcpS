"""Read-only adapter for the CERN IT Service-Now portal (cern.service-now.com).

Covers the Service Portal search surface and the Knowledge Base article store.

Endpoint types (verified 2026-10 with a live SSO session):

- Anonymous GET of ``/service-portal/`` returns the public SPA shell (200)
  without any SSO redirect, so an unauthenticated HTTP client never
  establishes a ServiceNow session on its own. A headless browser drive of a
  protected classic page (``/service-portal/kb_search.do`` redirects through
  ``saml_redirector.do`` into the silent CERN SAML handshake) drops the
  ServiceNow session cookies (``JSESSIONID``, ``glide_session_store``), which
  every endpoint below honors. Keep ``_SESSION_WARM_URLS`` in sync with the
  warm-up entry in ``cmcps.auth.browser._COOKIE_ONLY_SITES``.
- ``POST /api/now/sp/search`` — the Service-Portal search backend used by the
  SPA itself (captured from authenticated browser traffic, replayed with plain
  session cookies: 201 + JSON). JSON body only; GET answers 405. Read-only
  semantics (query dispatch), routed through ``SafeHttpClient.request_json_post``
  which gates it behind an explicit host allowlist. Sources observed:
  ``kb`` (knowledge articles), ``actions``/``items`` (service-catalog items),
  ``service_elements``, ``outages``, ``external_actions``, ``information``.
  Limits: pagination unsupported (``pagination_supported`` is false and
  ``total_number_results`` is always 0 — verified), so results are
  relevance-ranked and bounded (30/call, backend cap). Each call returns at
  most ~30 hits across ``result.results`` + ``result.additionalResults``.
- ``GET /{table}.do?JSONv2&sysparm_query=...`` — legacy ServiceNow JSONv2
  read API, works with the portal session (unlike ``/api/now/v1/table/*``,
  which requires a dedicated REST role and answers 401 even with a session).
  Used for KB article detail (``kb_knowledge``) and category resolution
  (``kb_category``). Limits: ``sysparm_fields``/``sysparm_limit`` are NOT
  honored (verified) — responses always carry every field of up to 50
  records, so only exact-key lookups (number/sys_id) are safe here.
- KB article portal address: the ``u_portal_page`` field of the record,
  falling back to ``<portal>?id=kb_article&n=KB…``.

Anonymous/expiration behavior: expired sessions answer 401 with a JSON
"User is not authenticated" — the adapter re-warms the ServiceNow session
once via a headless SSO drive, then retries once.
"""

from __future__ import annotations

import re
from typing import Any

from cmcps.adapters.common import html_to_text, json_body
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient, SafeResponse

SOURCE = "servicenow"

BASE = "https://cern.service-now.com"
PORTAL_URL = f"{BASE}/service-portal"
# CERN Service Portal id + the portal search page id, captured from
# authenticated SPA traffic (``ng-init="portal_id = …"``); the page id is a
# context hint for the search backend, a query runs identically without it.
_PORTAL_ID = "af91927edb821740ab34731b8c961927"
_SEARCH_PAGE_ID = "87466b63c3223100c8b837659bba8feb"
_SEARCH_URL = f"{BASE}/api/now/sp/search"
_POST_ALLOWLIST = ("cern.service-now.com",)
# Classic page whose anonymous hit triggers the SAML handshake (observed
# saml_redirector.do in browser traffic). Keep in sync with the warm-up entry
# in `cmcps.auth.browser._COOKIE_ONLY_SITES`.
SESSION_WARM_URLS = (f"{PORTAL_URL}/kb_search.do",)

_KNOWLEDGE_TABLE_URL = f"{BASE}/kb_knowledge.do"
_CATEGORY_TABLE_URL = f"{BASE}/kb_category.do"

_KB_NUMBER_RE = re.compile(r"^KB\d{7}$")
_SYS_ID_RE = re.compile(r"^[0-9a-f]{32}$")

SEARCH_SOURCES = (
    "kb",
    "actions",
    "service_elements",
    "items",
    "outages",
    "external_actions",
    "information",
)

_DEFAULT_LIMIT = 10
_MAX_LIMIT = 30  # Service-Portal search backend accepts at most ~30 hits/call
_SNIPPET_LIMIT = 400
_TEXT_LIMIT = 20000
_UNAUTHORIZED_STATUSES = frozenset({401, 403})
_HTTP_OK = 200

_PAGINATION_NOTE = (
    "The Service-Portal search backend does not support pagination or result "
    "totals (verified); results are relevance-ranked and capped. Narrow the "
    "query instead of paging."
)
_AUTH_INSTRUCTIONS = (
    "The ServiceNow session expired. Call cern_auth_sign_in "
    "(or cern_auth_reauthenticate) with a browser available, then retry."
)


def _absolutize(link: Any) -> str | None:
    """Turn a Service-Portal relative link into a full URL."""
    if not isinstance(link, str) or not link:
        return None
    if link.startswith("?"):
        return f"{PORTAL_URL}/{link}"
    if link.startswith("/"):
        return f"{BASE}{link}"
    return link


def _snippet(value: Any, *, limit: int = _SNIPPET_LIMIT) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = html_to_text(value, limit=limit)
    return text or None


def _search_item(raw: dict[str, Any]) -> dict[str, Any]:
    label = raw.get("label") or raw.get("name") or raw.get("primary")
    return {
        "type": raw.get("__search_source_id__"),
        "table": raw.get("table"),
        "number": raw.get("number") or None,
        "name": raw.get("name") or None,
        "label": label or None,
        "description": _snippet(raw.get("short_description")),
        "snippet": _snippet(raw.get("text")),
        "score": raw.get("score") or None,
        "published_at": raw.get("publishedUTC") or None,
        "sys_id": raw.get("sys_id") or None,
        "url": _absolutize(raw.get("link") or raw.get("url")),
    }


_SESSION_PROBE_QUERY = "number=CMCPSSENTINEL0"


async def _session_ok(client: SafeHttpClient) -> bool:
    """Cheap session check: the KB JSONv2 table read is 200 in-session, 401 out."""
    response = await client.request(
        "GET",
        _KNOWLEDGE_TABLE_URL,
        params={"JSONv2": "", "sysparm_query": _SESSION_PROBE_QUERY},
    )
    return response.status_code == _HTTP_OK


async def _ensure_session(client: SafeHttpClient) -> None:
    """Guarantee an authenticated ServiceNow session before searching.

    Anonymous guests get a *successful-looking* empty result set from the
    Service-Portal search (verified: anonymous POST answers 201 with
    ``results: []``), so the search path cannot rely on 401 detection: probe
    the session explicitly, re-warming it once through a headless SSO drive.
    """
    if await _session_ok(client):
        return
    if await client.warm_sites(SESSION_WARM_URLS) and await _session_ok(client):
        return
    raise UpstreamError(
        "CERN Service-Now session unavailable; search results would be silently empty",
        instructions=_AUTH_INSTRUCTIONS,
    )


async def _post_search(client: SafeHttpClient, body: dict[str, Any]) -> SafeResponse:
    """POST one portal search, re-warming the ServiceNow session once on 401/403."""
    response = await client.request_json_post(
        _SEARCH_URL, json_body=body, allowlist=_POST_ALLOWLIST
    )
    if response.status_code in _UNAUTHORIZED_STATUSES and await client.warm_sites(
        SESSION_WARM_URLS
    ):
        response = await client.request_json_post(
            _SEARCH_URL, json_body=body, allowlist=_POST_ALLOWLIST
        )
    if response.status_code in _UNAUTHORIZED_STATUSES:
        raise UpstreamError(
            "CERN Service-Now rejected the search request (invalid session)",
            instructions=_AUTH_INSTRUCTIONS,
        )
    return response


async def _get_jsonv2(
    client: SafeHttpClient, url: str, query: str, *, context: str
) -> dict[str, Any]:
    """GET one exact-key JSONv2 lookup, re-warming the session once on 401/403."""
    params = {"JSONv2": "", "sysparm_query": query}
    response = await client.request("GET", url, params=params)
    if response.status_code in _UNAUTHORIZED_STATUSES and await client.warm_sites(
        SESSION_WARM_URLS
    ):
        response = await client.request("GET", url, params=params)
    if response.status_code in _UNAUTHORIZED_STATUSES:
        raise UpstreamError(
            f"CERN Service-Now refused the {context} request (invalid session)",
            instructions=_AUTH_INSTRUCTIONS,
        )
    payload = json_body(response, source=f"servicenow {context}")
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise UpstreamError(
            f"CERN Service-Now returned malformed {context} data",
            instructions="Retry; the legacy JSONv2 endpoint may be temporarily unavailable.",
        )
    return payload


def _validate_sources(sources: list[str] | None) -> list[str] | None:
    if sources is None:
        return None
    unknown = sorted(set(sources) - set(SEARCH_SOURCES))
    if unknown:
        raise InvalidInputError(
            f"unknown search sources: {', '.join(unknown)}",
            instructions=f"Valid sources: {', '.join(SEARCH_SOURCES)}.",
        )
    return list(dict.fromkeys(sources))


def _validate_limit(limit: int) -> int:
    if not 1 <= limit <= _MAX_LIMIT:
        raise InvalidInputError(
            f"limit must be between 1 and {_MAX_LIMIT}",
            instructions="The Service-Portal search backend caps results per call.",
        )
    return limit


async def search_portal(
    client: SafeHttpClient,
    *,
    query: str,
    sources: list[str] | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> SearchResult:
    """Relevance-ranked Service-Portal search (KB, catalog, outages, ...)."""
    query = query.strip()
    if not query:
        raise InvalidInputError(
            "query must not be empty",
            instructions="Pass keywords, e.g. 'grid certificate'.",
        )
    limit = _validate_limit(limit)
    sources = _validate_sources(sources)
    await _ensure_session(client)
    body: dict[str, Any] = {
        "query": query,
        "query_start_location": 0,
        "count": limit,
        "portal": _PORTAL_ID,
        "page": _SEARCH_PAGE_ID,
        "include_facets": False,
        "include_suggestions": False,
    }
    if sources:
        body["source"] = sources
    response = await _post_search(client, body)
    payload = json_body(response, source="servicenow search")
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise UpstreamError(
            "CERN Service-Now returned malformed search data",
            instructions="Retry; the Service-Portal search API may be unavailable.",
        )
    rows: list[dict[str, Any]] = []
    for bucket in (result.get("results"), result.get("additionalResults")):
        if isinstance(bucket, list):
            rows.extend(item for item in bucket if isinstance(item, dict))
    seen: set[str] = set()
    items = []
    for row in rows:
        key = str(row.get("sys_id") or id(row))
        if key in seen:
            continue
        seen.add(key)
        items.append(_search_item(row))
        if len(items) >= limit:
            break
    return SearchResult(
        source=SOURCE,
        query=query,
        items=items,
        total_count=None,
        page=1,
        size=len(items),
        has_more=False,
        note=_PAGINATION_NOTE,
    )


async def _resolve_category(client: SafeHttpClient, sys_id: str) -> dict[str, str | None]:
    """Resolve a kb_category sys_id to label/path when readable."""
    payload = await _get_jsonv2(
        client, _CATEGORY_TABLE_URL, f"sys_id={sys_id}", context="kb_category"
    )
    if not payload["records"]:
        return {"category": None, "category_path": None}
    record = payload["records"][0]
    if not isinstance(record, dict):
        return {"category": None, "category_path": None}
    return {
        "category": record.get("label") or None,
        "category_path": record.get("full_category") or None,
    }


def _article_item(record: dict[str, Any], category: dict[str, str | None]) -> dict[str, Any]:
    body_html = record.get("text") or ""
    text = html_to_text(body_html, limit=_TEXT_LIMIT) if isinstance(body_html, str) else ""
    if isinstance(body_html, str) and len(text) >= _TEXT_LIMIT:
        text += "\n[truncated]"
    helpful: int | None = None
    try:
        helpful = int(str(record.get("helpful_count") or ""))
    except ValueError:
        helpful = None
    number = record.get("number") or ""
    portal = record.get("u_portal_page")
    meta = record.get("meta")
    return {
        "source": SOURCE,
        "number": number or None,
        "sys_id": record.get("sys_id") or None,
        "title": record.get("short_description") or None,
        "summary": record.get("meta_description") or None,
        "keywords": meta.split() if isinstance(meta, str) and meta.strip() else [],
        **category,
        "knowledge_base_sys_id": record.get("kb_knowledge_base") or None,
        "published_at": record.get("published") or None,
        "updated_at": record.get("sys_updated_on") or None,
        "status": record.get("workflow_state") or None,
        "helpful_count": helpful,
        "url": portal
        if isinstance(portal, str) and portal
        else f"{PORTAL_URL}?id=kb_article&n={number}",
        "text": text,
    }


async def get_kb_article(client: SafeHttpClient, *, reference: str) -> dict[str, Any]:
    """Full KB article by number (KB0000705) or 32-hex sys_id."""
    reference = reference.strip()
    if _KB_NUMBER_RE.fullmatch(reference.upper()):
        query_ref = f"number={reference.upper()}"
    elif _SYS_ID_RE.fullmatch(reference):
        query_ref = f"sys_id={reference}"
    else:
        raise InvalidInputError(
            f"invalid KB article reference {reference!r}",
            instructions="Pass a KB number like KB0000705 or a 32-hex sys_id from search results.",
        )
    payload = await _get_jsonv2(client, _KNOWLEDGE_TABLE_URL, query_ref, context="kb article")
    records = payload["records"]
    if not records:
        raise UpstreamError(
            f"KB article {reference} not found or not visible",
            instructions="Check the reference from servicenow_search_kb results.",
        )
    record = records[0]
    if not isinstance(record, dict):
        raise UpstreamError("CERN Service-Now returned malformed kb article data")
    category_id = record.get("kb_category")
    category = (
        await _resolve_category(client, str(category_id))
        if isinstance(category_id, str) and _SYS_ID_RE.fullmatch(category_id)
        else {"category": None, "category_path": None}
    )
    return _article_item(record, category)
