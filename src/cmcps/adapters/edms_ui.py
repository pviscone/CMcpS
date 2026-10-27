"""Full-text search for edms.cern.ch by scraping the EDMS GWT UI (fallback).

The documented EDMS REST WS has NO full-text search (see
``cmcps/adapters/edms.py``), and the UI search itself is GWT-RPC (POST,
binary serialization, obfuscated per redeploy) — unusable read-only. What
works: the search results page is deep-linkable, so this adapter renders

    https://edms.cern.ch/ui/#!master/search/view?objecttype=document&query=...

headlessly through the stored SSO session (see ``fetch_page_html_text`` in
``cmcps/auth/browser.py``) and parses the GXT results grid (100 rows of
documents per page).

Fragility note: this is a marked HTML-scraping fallback coupled to the GXT
DOM (``x-grid3-row`` rows, ``x-grid3-td-<field>`` cell classes,
``SearchColumnRenderer*.teaser`` snippets). An EDMS UI redeploy may change
those classes — the parser then returns an empty result instead of
mis-parsing, which the result note reports.

Verified live (2026-10-27, SSO session): query 'ECAL milestones' returns
'CMS-ECAL Milestones' (EDMS 350221 v.6) via the grid; anonymous first load
triggers the interact hook: clicking Sign in completes the OIDC handshake
silently and the results reload with session visibility.
"""

from __future__ import annotations

import asyncio
import re
from html import unescape as _unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, unquote, urlsplit

try:  # hard dependency in practice; kept import-guarded for parser-only tests
    from playwright.async_api import Error as PlaywrightError
except Exception:  # pragma: no cover
    PlaywrightError = Exception  # type: ignore[assignment,misc]

from cmcps.adapters.edms import get_document
from cmcps.auth.service import AuthService
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient

SOURCE = "edms-ui"

BASE = "https://edms.cern.ch"
UI_URL = f"{BASE}/ui/"
RESULTS_MARKER = "per page"

_TOTAL_RE = re.compile(
    r"(Docs & Files|Documents|Projects|Items|Assets|F\. Positions|Systems|Locations)\s*"
    r"\((\d+\+?)\)"
)
_FILE_URL_RE = re.compile(r"^/file/(\d+)/([^/]+)/(.+)$")
_ID_CELL_RE = re.compile(r"^(.+?)\s+v\.(.+)$")
_ROW_CLASS = "x-grid3-row"
_CELL_PREFIX = "x-grid3-td-"
_SHOW_MORE = "show more results"
_SIGN_IN_ANCHOR_RE = re.compile(r">\s*Sign in\s*</a>", re.IGNORECASE)
# Logged-in pages mark restricted rows with these classes (grey anchor +
# red circle-minus icon); anonymous pages carry NEITHER (the banner's
# generic "You are not logged in" tooltip is unrelated and not a marker).
_DENIED_CLASS_RE = re.compile(r"accessdenied|anchornoaccess", re.IGNORECASE)
_DENIED_TITLES = frozenset({"access denied"})
_MAX_ITEMS = 100
_SNIPPET_LIMIT = 600


def search_url(query: str, *, latest_version: bool = False, hide_obsolete: bool = True) -> str:
    """Deep-link URL of the EDMS UI search results page for ``query``."""
    return (
        f"{UI_URL}#!master/search/view?objecttype=document&query={quote(query, safe='')}"
        f"&latestversion={'true' if latest_version else 'false'}"
        f"&hideobsolete={'true' if hide_obsolete else 'false'}"
        "&dadvanced=false&global=true"
    )


_SIGN_IN_SETTLE_SECONDS = 20.0
# GWT paints the results shell ("Per page") seconds before the grid hydrates.
_GRID_SETTLE_SECONDS = 5.0
# Accessibility provenance: the logged-in UI marks restricted rows with
# per-row classes (see _DENIED_CLASS_RE above; cross-validated against REST
# on live pages, both directions). Rows WITHOUT the marker are openable in
# practice (6/6 sample) but strictly only the REST detail endpoint (masked
# 400) proves it — check/access probing covers those and re-verifies marks.
_ACCESS_MODES = frozenset({"any", "accessible", "restricted"})
_ACCESS_CONCURRENCY = 8


async def _sign_in_interact(page: Any) -> bool:
    """Click the UI's Sign-in anchor and wait out the silent OIDC round trip.

    Must not return before the handshake finished — the caller re-navigates
    to the results URL the moment this returns True, which would otherwise
    abort the redirect chain before the UI session cookie is planted.
    """
    try:
        link = page.locator("a", has_text="Sign in").first
        if await link.count() == 0:
            return False
        await link.click()
        deadline = asyncio.get_running_loop().time() + _SIGN_IN_SETTLE_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(1.0)
            if urlsplit(page.url).hostname != "edms.cern.ch":
                continue  # mid-handshake on auth.cern.ch
            if await link.count() > 0:
                continue  # still the anonymous page
            if await page.locator("input.x-form-field").count() > 0:
                return True  # app shell back, signed-in
    except PlaywrightError:
        return False
    return True


class _ResultsParser(HTMLParser):
    """Extract the EDMS UI document grid (and section totals) from the page HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, Any]] = []
        self.page_text: list[str] = []
        self._depth = 0  # >0 while inside a grid row
        self._row: dict[str, Any] | None = None
        self._cell: str | None = None
        self._cell_depth = 0
        self._cell_buf: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_buf: list[str] = []
        self._teaser_buf: list[str] | None = None

    # -- streaming ----------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = dict(attrs)
        cls = attrs_d.get("class", "") or ""
        if self._depth > 0 and self._row is not None and not self._row["denied_marker"]:
            icon_title = (attrs_d.get("title") or "").strip().casefold()
            if _DENIED_CLASS_RE.search(cls) or icon_title in _DENIED_TITLES:
                self._row["denied_marker"] = True
        if tag == "div":
            self._start_div(cls)
        elif tag == "td":
            self._start_cell(cls)
        elif tag == "a" and self._depth > 0:
            self._start_anchor(attrs_d.get("href", "") or "")

    def _start_div(self, cls: str) -> None:
        if self._row is None and _ROW_CLASS in cls.split():
            # rows open wherever they appear; reset depth absolutely so an
            # unbalanced fragment before the grid cannot swallow a row
            self._row = {"cells": {}, "files": [], "snippets": [], "denied_marker": False}
            self._depth = 1
            return
        if self._depth > 0:
            self._depth += 1
            if "teaser" in cls.casefold():
                self._teaser_buf = []

    def _start_cell(self, cls: str) -> None:
        if self._depth == 0:
            return
        if self._cell is not None:
            self._cell_depth += 1  # presentational table nested inside the cell
            return
        for token in cls.split():
            if token.startswith(_CELL_PREFIX):
                self._cell = token[len(_CELL_PREFIX) :]
                self._cell_buf = []
                self._cell_depth = 1
                break

    def _start_anchor(self, href: str) -> None:
        if href and href != "javascript:;" and self._row is not None:
            file_entry = _parse_file_link(href)
            if file_entry is not None:
                self._row["files"].append(file_entry)
                return
        if self._anchor_href is None:  # shallowest anchor wins its text
            self._anchor_href = href or "javascript:;"
            self._anchor_buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._depth > 0:
            self._depth -= 1
            if self._teaser_buf is not None and self._row is not None:
                snippet = _norm(" ".join(self._teaser_buf))
                if snippet:
                    self._row["snippets"].append(snippet)
                self._teaser_buf = None
            if self._depth == 0 and self._row is not None:
                self.rows.append(self._row)
                self._row = None
        elif tag == "td" and self._cell is not None and self._row is not None:
            self._cell_depth -= 1
            if self._cell_depth > 0:
                return  # inner presentational table closing, still in the cell
            self._row["cells"][self._cell] = _norm(" ".join(self._cell_buf))
            self._cell = None
            self._cell_buf = []
        elif tag == "a" and self._anchor_href is not None and self._row is not None:
            text = _norm("".join(self._anchor_buf))
            if text:
                # Id cells look like "CMS-EC-SG-0004 v.6" / "3481902 v.0.1";
                # any other plain anchor in a row is the title. Both can sit
                # in unclassed <td>s, so classify by content, not column.
                key = "anchor_id" if _ID_CELL_RE.match(text) else "anchor_title"
                self._row["cells"].setdefault(key, text)
            self._anchor_href = None
            self._anchor_buf = []

    def handle_data(self, data: str) -> None:
        self.page_text.append(data)
        if self._anchor_href is not None:
            self._anchor_buf.append(data)
        if self._teaser_buf is not None:
            self._teaser_buf.append(data)
        if self._cell is not None:
            self._cell_buf.append(data)


def _norm(text: str) -> str:
    return " ".join(_unescape(text).split())


def _parse_file_link(href: str) -> dict[str, Any] | None:
    path = urlsplit(href).path
    match = _FILE_URL_RE.match(path)
    if not match:
        return None
    doc_number, version, name = match.groups()
    return {
        "document_number": int(doc_number),
        "version": unquote(version),
        "name": unquote(name),
        "url": f"{BASE}{path}",
    }


def _row_to_item(row: dict[str, Any]) -> dict[str, Any]:
    cells = row["cells"]
    id_text = cells.get("anchor_id", "")
    code = id_text
    version = None
    if match := _ID_CELL_RE.match(id_text):
        code, version = match.groups()
    files = row["files"]
    document_number: int | None = None
    if code.isdigit():
        document_number = int(code)
    if document_number is None and files:
        document_number = files[0]["document_number"]
    snippet = " | ".join(s for s in row["snippets"] if s)[:_SNIPPET_LIMIT] or None
    item = {
        "document_number": document_number,
        "code": code or None,
        "version": version or (files[0]["version"] if files else None),
        # UI's access-denied marker classifies immediately (cross-validated
        # against REST probes on live pages); None = unmarked, unprobed.
        "accessible": False if row["denied_marker"] else None,
        "title": cells.get("anchor_title") or cells.get("title") or None,
        "status": cells.get("statusCode") or None,
        "authors": cells.get("author") or None,
        "last_modified": cells.get("lastModifiedDate") or None,
        "external_ref": cells.get("externalRef") or None,
        "context": cells.get("contextName") or None,
        "keywords": cells.get("keywords") or None,
        "type": cells.get("type") or None,
        "snippet": snippet,
        "files": files,
    }
    if document_number is not None and item["version"]:
        item["url"] = f"{BASE}/document/{document_number}/{item['version']}"
    return item


def parse_results(html: str) -> dict[str, Any]:
    """Parse one rendered EDMS UI results page into items + section totals."""
    parser = _ResultsParser()
    parser.feed(html)
    parser.close()
    items: list[dict[str, Any]] = []
    seen: dict[tuple[str | None, str | None], int] = {}
    for row in parser.rows:
        item = _row_to_item(row)
        key = (item["code"], item["version"])
        if key[0] is None:
            continue  # non-document row (filter chips, empty placeholders)
        if key in seen:
            target = items[seen[key]]
            known = {f["name"] for f in target["files"]}
            target["files"].extend(f for f in item["files"] if f["name"] not in known)
            continue
        if len(items) >= _MAX_ITEMS:
            break
        seen[key] = len(items)
        items.append(item)
    for item in items:
        if item["document_number"] is None and item["files"]:
            # First row of the document carried no file link; recover the
            # numeric id from a merged file row's /file/<id>/<version> URL.
            item["document_number"] = item["files"][0]["document_number"]
            if item["version"]:
                item["url"] = f"{BASE}/document/{item['document_number']}/{item['version']}"
    text = " ".join(parser.page_text)
    totals = {label: count for label, count in _TOTAL_RE.findall(_norm(text))}
    return {
        "items": items,
        "totals": totals,
        "has_more": _SHOW_MORE in text.casefold(),
        # Anonymous pages carry the banner's "Sign in" anchor; after the OIDC
        # handshake it is replaced by the user name. Tooltip text never
        # reaches text nodes, so detect the anchor in the raw HTML.
        "ui_logged_in": _SIGN_IN_ANCHOR_RE.search(html) is None,
    }


async def _probe_access(
    http: SafeHttpClient,
    items: list[dict[str, Any]],
) -> int:
    """Set item['accessible'] True/False via the masked REST detail endpoint.

    Rows already classified by the UI access-denied marker (accessible is
    False) and code-only rows (no numeric id) are skipped. Returns the
    total number of numbered documents.
    """
    semaphore = asyncio.Semaphore(_ACCESS_CONCURRENCY)

    async def probe(item: dict[str, Any]) -> None:
        number = item.get("document_number")
        if number is None or item.get("accessible") is not None:
            return  # code-only rows are unprobable; UI-denied rows need no probe
        version = item.get("version")
        async with semaphore:
            try:
                await get_document(
                    http,
                    document_number=str(number),
                    version=str(version) if version else None,
                )
                item["accessible"] = True
            except UpstreamError:
                # masked 400 for denied-or-missing — treated identically
                item["accessible"] = False

    if any(item.get("accessible") is None and item.get("document_number") for item in items):
        await asyncio.gather(*(probe(item) for item in items))
    return sum(1 for item in items if item.get("document_number") is not None)


async def _render_results(
    service: AuthService, url: str, timeout_seconds: int | None
) -> dict[str, Any]:
    """Render the results URL and parse it; re-renders signed-in when anonymous."""
    payload = await service.fetch_page_html(
        url,
        wait_for=RESULTS_MARKER,
        interact=_sign_in_interact,
        settle_seconds=_GRID_SETTLE_SECONDS,
        timeout_seconds=timeout_seconds,
    )
    if payload is None:
        raise UpstreamError(
            "the EDMS UI search page could not be rendered through the SSO session",
            instructions=(
                "Sign in with cern_auth_sign_in and retry; the EDMS UI is a GWT app and "
                "must be reachable in the headless browser."
            ),
        )
    if payload.get("auth_divert"):
        raise UpstreamError(
            "the SSO session expired while loading the EDMS UI search",
            instructions="Re-sign in with cern_auth_reauthenticate and retry.",
        )
    parsed = parse_results(str(payload.get("html") or ""))
    if parsed["ui_logged_in"]:
        return parsed
    # Anonymous page: the results shell markers already match, so the
    # ordinary interact path never fires — force the Sign-in click for a
    # second pass with session visibility (the UI session cookie then
    # persists in the stored state for later searches).
    re_payload = await service.fetch_page_html(
        url,
        wait_for=RESULTS_MARKER,
        interact=_sign_in_interact,
        interact_even_if_found=True,
        settle_seconds=_GRID_SETTLE_SECONDS,
        timeout_seconds=timeout_seconds,
    )
    if re_payload and not re_payload.get("auth_divert"):
        re_parsed = parse_results(str(re_payload.get("html") or ""))
        if re_parsed["ui_logged_in"]:
            return re_parsed
    return parsed


async def _apply_access(
    http: SafeHttpClient,
    items: list[dict[str, Any]],
    mode: str,
) -> tuple[list[dict[str, Any]], str]:
    """Probe item accessibility via the REST detail endpoint and filter on it."""
    probed = await _probe_access(http, items)
    unknown = sum(1 for item in items if item["accessible"] is None)
    accessible = sum(1 for item in items if item["accessible"] is True)
    restricted = sum(1 for item in items if item["accessible"] is False)
    if mode == "accessible":
        items = [item for item in items if item["accessible"] is True]
    elif mode == "restricted":
        items = [item for item in items if item["accessible"] is False]
    summary = (
        f"Access probe over {probed} numbered documents: {accessible} accessible, "
        f"{restricted} restricted, {unknown} without numeric id; "
        f"after access='{mode}' filter: {len(items)} items. "
    )
    return items, summary


async def search_text(
    service: AuthService,
    http: SafeHttpClient | None = None,
    *,
    query: str,
    latest_version: bool = False,
    hide_obsolete: bool = True,
    check_access: bool = False,
    access: str = "any",
    timeout_seconds: int | None = None,
) -> SearchResult:
    """Full-text EDMS search by rendering the UI results page headlessly."""
    value = (query or "").strip()
    mode = (access or "any").strip().casefold()
    if mode not in _ACCESS_MODES:
        raise InvalidInputError(
            f"access must be one of {sorted(_ACCESS_MODES)}",
            instructions="Use 'any' to skip probing, 'accessible' or 'restricted' to filter.",
        )
    if (check_access or mode != "any") and http is None:
        raise InvalidInputError(
            "check_access/access filtering requires the shared HTTP client",
            instructions="This is a wiring error; report it.",
        )
    if not value:
        raise UpstreamError(
            "query must not be empty",
            instructions="Pass the words you would type in the EDMS UI search box.",
        )
    url = search_url(value, latest_version=latest_version, hide_obsolete=hide_obsolete)
    parsed = await _render_results(service, url, timeout_seconds)
    items = parsed["items"]
    access_counts = ""
    if check_access or mode != "any":
        items, access_counts = await _apply_access(http, items, mode)
    counts = ", ".join(f"{label}: {count}" for label, count in parsed["totals"].items())
    note = (
        "EDMS full-text search via headless scrape of the GWT UI results page "
        "(DOM-coupled fallback; an EDMS UI redeploy may break the parser). "
        "Page 1 only, up to 100 documents. "
        "document_number links to edms_get_document; file names/document numbers "
        "link to edms_download_file. Results respect session visibility: "
        f"{'logged-in search' if parsed['ui_logged_in'] else 'anonymous visibility only'}. "
        "The search index is global metadata, so it lists documents you "
        "cannot open; those restricted rows carry the UI's access-denied "
        "marker and are flagged accessible=false for free. Rows WITHOUT the "
        "marker are openable in practice but only probed (REST detail) when "
        "you pass check_access or an access filter."
    )
    if counts:
        note = f"Hit totals per section — {counts}. " + note
    if not items and not counts:
        note = ("No results parsed — the query had no hits or the UI markup changed. ") + note
    return SearchResult(
        source=SOURCE,
        query=value,
        items=items,
        total_count=None,
        size=len(items),
        has_more=parsed["has_more"],
        note=access_counts + note,
    )
