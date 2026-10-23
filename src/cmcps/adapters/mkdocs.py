"""Read-only adapter for MkDocs sites (docs.cern.ch and elsewhere).

Endpoint type: standard MkDocs (Material) static output, discovered live on
``cms-l1t-correlator.docs.cern.ch`` (SSO-gated; CERN session cookies
suffice) — the whole ``*.docs.cern.ch`` fleet sits behind Keycloak and the
shared cookie session unlocks it.

- ``GET <base>/search/search_index.json`` — full-text index
  ``{"config": {...}, "docs": [{"location", "title", "text", "tags?"}]}``.
  Client-side scoring: substring hits in title weigh 10x text hits.
- ``GET <base>/<location>`` — rendered page HTML; stripped to plain text
  via the shared extractor (code blocks, tables survive as text).
- ``GET <base>/<asset>`` — images/PDFs/ROOT files referenced from pages;
  bounded download, host-restricted by the safety layer.

There is no raw-markdown endpoint in stock MkDocs; the reference
``mkdocs-mcp`` design (search index + HTML page fetch) is the standard
fallback and what this adapter implements. SSO-protected sites return the
Keycloak HTML instead of JSON/HTML page content; detected and surfaced with
sign-in instructions.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from cmcps.adapters.common import html_to_text, json_body
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient

SOURCE = "mkdocs"

_MIN_ERROR_STATUS = 400
_MAX_PAGE_TEXT = 20000
_MAX_SEARCH_LIMIT = 100
_TITLE_HIT_WEIGHT = 10
_SNIPPET_RADIUS = 120

_UNSAFE_RE = re.compile(r"(\.\.|^/|^https?://|\\\\)")


def _check_base(base_url: str) -> str:
    cleaned = base_url.strip().rstrip("/")
    parts = urlsplit(cleaned)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise InvalidInputError(
            f"invalid mkdocs base url {base_url!r}",
            instructions="Use an https site root like https://sftroot.docs.cern.ch",
        )
    return cleaned


def _check_page(page: str) -> str:
    cleaned = page.strip().strip("/")
    if _UNSAFE_RE.search(cleaned):
        raise InvalidInputError(
            f"invalid page path {page!r}",
            instructions="Use the 'location' value from mkdocs_search/mkdocs_list_pages.",
        )
    return cleaned


def _authish(content: bytes) -> bool:
    head = content[:2048].lower()
    return b"login-pf" in head or b"sign in to cern" in head


def _sign_in_note(html: bool) -> str:
    return (
        "The site answered with the CERN SSO login page; call cern_auth_sign_in and retry."
        if html
        else "Complete cern_auth_sign_in for SSO-protected docs sites."
    )


async def _fetch_search_index(client: SafeHttpClient, base: str) -> dict[str, Any]:
    response = await client.request("GET", f"{base}/search/search_index.json")
    if response.status_code >= _MIN_ERROR_STATUS:
        raise UpstreamError(
            f"mkdocs search index failed with status {response.status_code} for {base}",
            status_code=response.status_code,
            instructions="Verify the base URL points at a MkDocs site root.",
        )
    if _authish(response.content):
        raise UpstreamError(
            f"{base} is SSO-protected and returned the login page",
            instructions=_sign_in_note(True),
        )
    payload = json_body(response, source=f"mkdocs {base}")
    if not isinstance(payload, dict) or not isinstance(payload.get("docs"), list):
        raise UpstreamError(
            f"{base} did not return a MkDocs search index",
            instructions="Check the URL; expected <site>/search/search_index.json to exist.",
        )
    return payload


def _snippet(text: str, needle: str) -> str | None:
    idx = text.lower().find(needle.lower())
    if idx < 0:
        return None
    start = max(0, idx - _SNIPPET_RADIUS)
    end = min(len(text), idx + len(needle) + _SNIPPET_RADIUS)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}".strip()


def _score(doc: dict[str, Any], terms: list[str]) -> int:
    title = str(doc.get("title") or "").lower()
    text = str(doc.get("text") or "").lower()
    score = 0
    for term in terms:
        score += title.count(term) * _TITLE_HIT_WEIGHT
        score += text.count(term)
    return score


def _doc_item(base: str, doc: dict[str, Any], *, snippet: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "location": doc.get("location") or "",
        "title": doc.get("title"),
        "tags": doc.get("tags") or [],
        "url": urljoin(f"{base}/", str(doc.get("location") or "")),
    }
    if snippet:
        item["snippet"] = snippet
    return item


async def search(
    client: SafeHttpClient,
    *,
    base_url: str,
    query: str,
    limit: int = 20,
) -> SearchResult:
    """Client-side full-text search over the MkDocs search index."""
    base = _check_base(base_url)
    terms = [t for t in re.split(r"\s+", query.strip().lower()) if t]
    if not terms:
        raise InvalidInputError("query must not be empty")
    if not 1 <= limit <= _MAX_SEARCH_LIMIT:
        raise InvalidInputError(f"limit must be between 1 and {_MAX_SEARCH_LIMIT}")
    payload = await _fetch_search_index(client, base)
    scored = []
    for doc in payload["docs"]:
        if not isinstance(doc, dict):
            continue
        score = _score(doc, terms)
        if score <= 0:
            continue
        text = str(doc.get("text") or "")
        first = next((t for t in terms), "")
        scored.append((score, _doc_item(base, doc, snippet=_snippet(text, first))))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    items = [{**item, "score": score} for score, item in scored[:limit]]
    return SearchResult(
        source=SOURCE,
        query=f"{base} :: {' '.join(terms)}",
        items=items,
        total_count=len(scored),
        page=1,
        size=len(items),
        has_more=len(scored) > limit,
        note="Client-side scoring over the site search index (title hits weigh 10x text hits).",
    )


async def list_pages(
    client: SafeHttpClient,
    *,
    base_url: str,
    path_filter: str | None = None,
) -> SearchResult:
    """All pages of a MkDocs site (from the search index locations)."""
    base = _check_base(base_url)
    payload = await _fetch_search_index(client, base)
    items = []
    seen: set[str] = set()
    for doc in payload["docs"]:
        if not isinstance(doc, dict):
            continue
        location = str(doc.get("location") or "")
        if location in seen:
            continue
        if path_filter and path_filter.lower() not in location.lower():
            continue
        seen.add(location)
        items.append(_doc_item(base, doc))
    return SearchResult(
        source=SOURCE,
        query=f"{base} pages" + (f" ~{path_filter!r}" if path_filter else ""),
        items=items,
        total_count=len(items),
        page=1,
        size=len(items),
        has_more=False,
    )


async def get_page(
    client: SafeHttpClient,
    *,
    base_url: str,
    page: str = "",
) -> dict[str, Any]:
    """One rendered page as plain text (HTML stripped, code preserved)."""
    base = _check_base(base_url)
    location = _check_page(page)
    url = urljoin(f"{base}/", location)
    response = await client.request("GET", url)
    if response.status_code >= _MIN_ERROR_STATUS:
        raise UpstreamError(
            f"mkdocs page failed with status {response.status_code} for {url}",
            status_code=response.status_code,
            instructions="Check the page location from mkdocs_search or mkdocs_list_pages.",
        )
    content = response.content
    if _authish(content):
        raise UpstreamError(
            f"{base} is SSO-protected and returned the login page",
            instructions=_sign_in_note(True),
        )
    html = content.decode("utf-8", errors="replace")
    title_match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    title = html_to_text(title_match.group(1)) if title_match else None
    return {
        "source": SOURCE,
        "location": location,
        "title": title,
        "text": html_to_text(html, limit=_MAX_PAGE_TEXT),
        "url": url,
    }


async def download_file(
    client: SafeHttpClient,
    *,
    file_url: str,
) -> dict[str, Any]:
    """Bounded download of a static asset hosted on the docs site."""
    cleaned = file_url.strip()
    parts = urlsplit(cleaned)
    if parts.scheme != "https" or not parts.hostname:
        raise InvalidInputError(
            f"invalid file url {file_url!r}",
            instructions="Pass an absolute https URL of the docs site.",
        )
    filename = parts.path.rsplit("/", 1)[-1] or None
    final = await client.download(cleaned, Path(filename) if filename else None)
    return {
        "source": SOURCE,
        "download_url": cleaned,
        "path": str(final),
        "size_bytes": final.stat().st_size,
    }
