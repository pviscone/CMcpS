"""Read-only adapter for CMS public physics results (cms-results.web.cern.ch).

The native search backend (`cms-results-search.app.cern.ch/publications`,
DataTables API with a client-side read token) is not used: the server was
persistently unavailable during adapter discovery, and its token story is
unsuitable for a read-only MCP layer. This adapter is a **documented HTML
fallback** over the static public-results pages, whose structure is stable
and machine-generated:

- ``{BASE}/publications/index.html`` — links to ``publications/<WG>/index.html``
  per working group (also ``publications/PR/PR.html``, ignored).
- ``{BASE}/publications/<WG>/index.html`` and
  ``{BASE}/preliminary-results/<WG>/index.html`` — one ``<tr>`` per paper
  wrapped in ``<!-- BEGIN <ID> -->`` / ``<!-- END -->`` comments with
  ``td.cadi`` (paper link), ``td.title``, ``td.status`` (journal + DOI), and
  ``td.date`` cells.
- ``{BASE}/publications/<ID>/index.html`` — paper detail page with
  ``td.titleline`` (title), ``td.journalline`` (DOI), ``td.textline``
  (``Abstract: ...``), a ``td.link`` cell with arXiv/PDF/CDS/INSPIRE/twiki
  links, and a figures table with downloadable plots/tables.

All endpoints are plain GET HTML; no authentication (public).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from cmcps.adapters.common import html_to_text
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient

SOURCE = "cms-results"

BASE = "https://cms-results.web.cern.ch/cms-results/public-results"

_KINDS = ("publications", "preliminary-results")

_MIN_ERROR_STATUS = 400

_GROUP_RE = re.compile(
    r'href="[^"]*public-results/(?:publications|preliminary-results)'
    r'/([A-Za-z0-9-]+)/index\.html"'
)
_ID_RE = re.compile(r"^[A-Za-z]{2,4}-\d{2}-\d{3}$")
_GROUP_CODE_RE = re.compile(r"^[A-Z]{2,6}$")
_TD_RE = {
    field: re.compile(rf'<td[^>]*class="{field}"[^>]*>(.*?)</td>', re.DOTALL | re.IGNORECASE)
    for field in ("cadi", "title", "status", "date")
}
_LINK_RE = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE)
_FIG_RE = re.compile(r'href="(CMS-[A-Za-z0-9-]+_(?:Figure|Table)[^"]*?)"')
_FIG_THUMB_RE = re.compile(r"-thumb\.")


def _links(html: str, *, base: str) -> list[dict[str, str]]:
    out = []
    for url, text in _LINK_RE.findall(html):
        label = html_to_text(text)
        if not label or label.startswith("<"):
            continue
        out.append({"label": label, "url": urljoin(base, url)})
    return out


async def _get_page(client: SafeHttpClient, url: str, *, source: str) -> str:
    response = await client.request("GET", url)
    if response.status_code >= _MIN_ERROR_STATUS:
        raise UpstreamError(
            f"{source} request failed with status {response.status_code} for {response.url}",
            status_code=response.status_code,
            instructions="Check the arguments; group and paper ids are case-sensitive.",
        )
    return response.content.decode("utf-8", errors="replace")


def _kind_url(kind: str, group: str) -> str:
    return f"{BASE}/{kind}/{group.upper()}/index.html"


async def list_groups(client: SafeHttpClient) -> dict[str, Any]:
    """Working groups listed on the public-results index page."""
    html = await _get_page(client, f"{BASE}/publications/index.html", source=f"{SOURCE} index")
    groups: dict[str, dict[str, str]] = {}
    for code in _GROUP_RE.findall(html):
        if not _GROUP_CODE_RE.fullmatch(code):
            continue
        groups.setdefault(
            code,
            {
                "group": code,
                "publications_url": f"{BASE}/publications/{code}/index.html",
                "preliminary_url": f"{BASE}/preliminary-results/{code}/index.html",
            },
        )
    items = sorted(groups.values(), key=lambda g: g["group"])
    return {
        "source": SOURCE,
        "group_count": len(items),
        "items": items,
        "note": "kind='publications' holds journal-accepted results; "
        "kind='preliminary-results' holds PAS/conference results.",
    }


def _paper_entry(block: str, *, kind: str, group: str) -> dict[str, Any]:
    cells = {name: pattern.search(block) for name, pattern in _TD_RE.items()}
    if cells["cadi"] is None:
        return {}
    paper_id_match = re.search(r"([A-Za-z]{2,4}-\d{2}-\d{3})", block)
    if paper_id_match is None:
        return {}
    paper_id = paper_id_match.group(1)
    doi = None
    if cells["status"] is not None:
        doi_match = re.search(r'dx\.doi\.org/([^"]+)', cells["status"].group(1))
        doi = doi_match.group(1) if doi_match else None
    return {
        "paper_id": paper_id,
        "group": group,
        "kind": kind,
        "title": html_to_text(cells["title"].group(1)) if cells["title"] else None,
        "journal": html_to_text(cells["status"].group(1)) if cells["status"] else None,
        "doi": doi,
        "date": html_to_text(cells["date"].group(1)) if cells["date"] else None,
        "url": f"{BASE}/{kind}/{paper_id}/index.html",
    }


async def list_papers(
    client: SafeHttpClient,
    *,
    group: str,
    kind: str = "publications",
    year: int | None = None,
    title_contains: str | None = None,
) -> SearchResult:
    """Papers of one working group, with client-side year/title filters."""
    if kind not in _KINDS:
        raise InvalidInputError(
            f"kind must be one of {_KINDS}",
            instructions="Use kind='publications' or kind='preliminary-results'.",
        )
    if not group or not re.fullmatch(r"[A-Za-z0-9-]+", group):
        raise InvalidInputError(
            f"invalid working-group code {group!r}",
            instructions="Take codes from cmsresults_list_groups (e.g. HIG, SMP, TOP).",
        )
    group_upper = group.upper()
    html = await _get_page(
        client, _kind_url(kind, group_upper), source=f"{SOURCE} {kind}/{group_upper}"
    )
    items = []
    for block in re.split(r"<!--\s*BEGIN\s+", html)[1:]:
        entry = _paper_entry("<!-- BEGIN " + block, kind=kind, group=group_upper)
        if not entry:
            continue
        if year is not None and (entry["date"] or "").find(str(year)) == -1:
            continue
        if title_contains and title_contains.lower() not in (entry["title"] or "").lower():
            continue
        items.append(entry)
    items.sort(key=lambda e: e["date"] or "", reverse=True)
    return SearchResult(
        source=SOURCE,
        query=f"{kind}/{group_upper}"
        + (f" year={year}" if year else "")
        + (f" title~{title_contains!r}" if title_contains else ""),
        items=items,
        total_count=len(items),
        page=1,
        size=len(items),
        has_more=False,
        note="Static index page; full listing (no server-side pagination). "
        "year and title_contains are client-side filters.",
    )


def _cell_text(html: str, field: str) -> str | None:
    match = re.search(rf'<td[^>]*class="{field}"[^>]*>(.*?)</td>', html, re.DOTALL | re.IGNORECASE)
    return html_to_text(match.group(1)) if match else None


async def get_paper(
    client: SafeHttpClient,
    *,
    paper_id: str,
    kind: str | None = None,
) -> dict[str, Any]:
    """Detail page of one paper (title, abstract, links, figure/table URLs)."""
    if not _ID_RE.fullmatch(paper_id.strip()):
        raise InvalidInputError(
            f"invalid paper id {paper_id!r}",
            instructions="Expected format like HIG-11-024 (from cmsresults_list_papers).",
        )
    pid = paper_id.strip().upper()
    kinds = (kind,) if kind else _KINDS
    html = None
    used_kind = None
    for candidate in kinds:
        if candidate not in _KINDS:
            raise InvalidInputError(
                f"kind must be one of {_KINDS}",
                instructions="Use kind='publications' or kind='preliminary-results'.",
            )
        response = await client.request("GET", f"{BASE}/{candidate}/{pid}/index.html")
        if response.status_code < _MIN_ERROR_STATUS:
            html = response.content.decode("utf-8", errors="replace")
            used_kind = candidate
            break
    if html is None or used_kind is None:
        raise UpstreamError(
            f"no detail page found for {pid} in {list(kinds)}",
            instructions="Check the id; preliminary results live under kind='preliminary-results'.",
        )
    title = _cell_text(html, "titleline")
    abstract_raw = _cell_text(html, "textline")
    abstract = None
    if abstract_raw:
        abstract = re.sub(r"^Abstract:\s*", "", abstract_raw).strip() or None
    journal_html = re.search(
        r'<td[^>]*class="journalline"[^>]*>(.*?)</td>', html, re.DOTALL | re.IGNORECASE
    )
    reference_lines: list[str] = []
    for match in re.finditer(
        r'<td[^>]*class="dateline"[^>]*>(.*?)</td>', html, re.DOTALL | re.IGNORECASE
    ):
        text = html_to_text(match.group(1))
        if text:
            reference_lines.append(text)
    link_cell = re.search(r'<td[^>]*class="link"[^>]*>(.*?)</td>', html, re.DOTALL | re.IGNORECASE)
    links = (
        _links(link_cell.group(1), base=f"{BASE}/{used_kind}/{pid}/index.html") if link_cell else []
    )
    figures = sorted(
        {
            urljoin(f"{BASE}/{used_kind}/{pid}/index.html", u)
            for u in _FIG_RE.findall(html)
            if not _FIG_THUMB_RE.search(u)
        }
    )
    doi = None
    if journal_html is not None:
        doi_match = re.search(r'dx\.doi\.org/([^"]+)', journal_html.group(1))
        doi = doi_match.group(1) if doi_match else None
    return {
        "source": SOURCE,
        "paper_id": pid,
        "kind": used_kind,
        "title": title,
        "collaboration": reference_lines[0] if reference_lines else None,
        "date": reference_lines[1] if len(reference_lines) > 1 else None,
        "journal": (html_to_text(journal_html.group(1)) if journal_html is not None else None),
        "doi": doi,
        "abstract": abstract,
        "links": links,
        "figures_and_tables": figures,
        "url": f"{BASE}/{used_kind}/{pid}/index.html",
    }


async def download_file(
    client: SafeHttpClient,
    *,
    download_url: str,
) -> dict[str, Any]:
    """Bounded download of one figure/table/PDF hosted on cms-results."""
    base_host = urlsplit(BASE).hostname
    host = urlsplit(download_url).hostname
    if not host or host != base_host:
        raise InvalidInputError(
            f"cmsresults_download_file only accepts URLs on {base_host}",
            instructions="Pass a URL from cmsresults_get_paper figures_and_tables.",
        )
    filename = urlsplit(download_url).path.rsplit("/", 1)[-1] or None
    final = await client.download(download_url, Path(filename) if filename else None)
    return {
        "source": SOURCE,
        "download_url": download_url,
        "path": str(final),
        "size_bytes": final.stat().st_size,
    }
