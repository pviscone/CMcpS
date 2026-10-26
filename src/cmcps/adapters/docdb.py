"""Read-only adapter for the CMS Document Database (cms-docdb.cern.ch).

Endpoint type: Perl CGI (modified DocDB 8.9.0); HTML-only, no JSON API.
Verified live with an SSO session (2026-10):

- ``GET /cgi-bin/DocDB/ShowDocument?docid=N[&version=M]`` — metadata page:
  title, canonical id (CMS-doc-N-vM), doc type, submitter/updater, dates,
  viewable/modifiable groups, abstract, files (RetrieveFile links), versions,
  topic paths, authors, keywords. Missing or non-visible documents render an
  HTTP 200 "Not authorized" page (no machine-readable error status).
- ``GET /cgi-bin/DocDB/Search?simpletext=Q&simple=1`` — full-text search
  (title/abstract/keywords/authors/publication info) into an HTML results
  table. ``titlesearch&titlesearchmode&mode=title`` restricts to titles;
  ``afterday/aftermonth(3-letter)/afteryear`` and the ``before`` triples
  add update-date filters (all three parts required together). The plural
  POST form in SearchForm is blocked by the Keycloak front end ("Invalid
  Request"), so only the GET variant is usable. ``XSearch`` is for *other*
  DocDB installations and useless here.
- ``GET /cgi-bin/DocDB/ListBy?days=N`` — documents modified in the last N
  days, same results table. ``ListBy?alldocs=1`` exceeds the response-size
  policy on every corpus and is intentionally not exposed.
- ``GET /cgi-bin/DocDB/RetrieveFile?docid=N&filename=F[&version=M]`` — file
  download (bounded, into CMcpS storage).

TLS note: cms-docdb.cern.ch serves a host certificate from the CERN Grid
Certification Authority, absent from the certifi root bundle; the shared
client trusts the system CA store instead (see net/client.py).
"""

from __future__ import annotations

import html as _html
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient

SOURCE = "docdb"

BASE = "https://cms-docdb.cern.ch/cgi-bin/DocDB"
SHOW_URL = f"{BASE}/ShowDocument"
SEARCH_URL = f"{BASE}/Search"
LISTBY_URL = f"{BASE}/ListBy"
RETRIEVE_URL = f"{BASE}/RetrieveFile"
PUBLIC_URL = "https://cms-docdb.cern.ch/cgi-bin/DocDB/ShowDocument?docid={docid}"

_ENCODING = "latin-1"
_TAG_RE = re.compile(r"<[^>]+>")
_MIN_ERROR_STATUS = 400
_MAX_LIMIT = 200
_MAX_DOCID = 99_999_999
_MAX_RECENT_DAYS = 3650
_TITLE_RE = re.compile(r"<title>CMS-doc-(\d+)-v?(\d*):\s*(.*?)</title>", re.S)
_INFO_BLOCK_RE = re.compile(r'<div id="BasicDocInfo">(.*?)</div>', re.S)
_DTDD_RE = re.compile(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", re.S)
_ANCHOR_RE = re.compile(r'<a\b[^>]*title="([^"]*)"[^>]*>([^<]*)</a>', re.S)
_CNT_RE = re.compile(r"Number of documents found:?\s*(\d+)")
_ROW_RE = re.compile(
    r"<tr[^>]*>\s*"
    r'<td class="Docid"><a href="[^"]*ShowDocument\?docid=(\d+)[^"]*"'
    r'\s+title="CMS-doc-\d+-v\d+"\s*>(\d+-v\d+)</a></td>\s*'
    r'<td class="Title"><a href="[^"]*"[^>]*>(.*?)</a></td>\s*'
    r'<td class="Author">(.*?)</td>\s*'
    r'<td class="Topics">(.*?)</td>\s*'
    r'<td class="Updated">(.*?)</td>\s*</tr>',
    re.S,
)
_VERSION_ROW_RE = re.compile(
    r'<a href="[^"]*ShowDocument\?docid=(\d+)[^"]*version=(\d+)[^"]*"'
    r'\s+title="CMS-doc-\d+-v\d+"[^>]*>(CMS-doc-\d+-v\d+)</a>\s*<br/>\s*([^<]*)',
    re.S,
)
_FILE_LINK_RE = re.compile(
    r'<a href="(https://cms-docdb\.cern\.ch/cgi-bin/DocDB/RetrieveFile\?[^"]+)"'
    r'\s+title="([^"]*)"[^>]*>(.*?)</a>\s*[(]([^,)]+),\s*([^)]*)[)]\s*</li>',
    re.S,
)
_FILENAME_ALLOWED_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.,+()\[\]#@'~-]{0,254}$")
_DATE_INPUT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KW_LINK_RE = re.compile(r'<a href="[^"]*keywordsearch[^"]*"[^>]*>([^<]*)</a>', re.S)
_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _decode(data: bytes) -> str:
    return data.decode(_ENCODING)


def _plain(fragment: str) -> str:
    """Strip tags and entities; collapse whitespace."""
    return " ".join(_html.unescape(_TAG_RE.sub(" ", fragment)).split())


def _check_docid(docid: int) -> int:
    if not 1 <= docid <= _MAX_DOCID:
        raise InvalidInputError("docid must be a positive integer")
    return docid


def _check_filename(filename: str) -> str:
    if not _FILENAME_ALLOWED_RE.fullmatch(filename) or ".." in filename:
        raise InvalidInputError(
            "filename must be a plain DocDB file name (no path components)",
            instructions="Take the exact filename reported by docdb_get_document.",
        )
    return filename


def _check_date(value: str, *, field: str) -> datetime:
    if not _DATE_INPUT_RE.fullmatch(value):
        raise InvalidInputError(f"{field} must be an ISO date (YYYY-MM-DD)")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidInputError(f"{field} must be a valid date (YYYY-MM-DD)") from exc


def _date_params(prefix: str, date: datetime) -> dict[str, str]:
    return {
        f"{prefix}day": str(date.day),
        f"{prefix}month": _MONTH_ABBR[date.month - 1],
        f"{prefix}year": str(date.year),
    }


def _not_found(docid: int) -> UpstreamError:
    return UpstreamError(
        f"DocDB document {docid} not found or not visible to this session",
        instructions="Check the docid; SSO-visible CMS documents only.",
    )


def _rows(page: str, *, limit: int, offset: int) -> tuple[list[dict[str, Any]], int | None]:
    """Parse DocumentTable result rows; client-side offset/limit."""
    rows: list[dict[str, Any]] = []
    for docid, canonical, title_frag, author_frag, topics_frag, updated in _ROW_RE.findall(page):
        first_author = re.sub(r"\s*<i>\s*et al\.\s*</i>\s*$", "", author_frag, flags=re.S)
        topics = [_plain(t) for _, t in _ANCHOR_RE.findall(topics_frag) if _plain(t)]
        rows.append(
            {
                "docid": int(docid),
                "canonical": canonical,
                "title": _plain(title_frag),
                "first_author": _plain(first_author) or None,
                "topics": topics,
                "updated": _plain(updated),
                "url": PUBLIC_URL.format(docid=int(docid)),
            }
        )
    count_match = _CNT_RE.search(page)
    total = int(count_match.group(1)) if count_match else len(rows)
    total = max(total, len(rows))
    sliced = rows[offset : offset + limit]
    return sliced, total


async def get_document(
    client: SafeHttpClient,
    *,
    docid: int,
    version: int | None = None,
) -> dict[str, Any]:
    """Full metadata of one DocDB document (parse of the ShowDocument page)."""
    _check_docid(docid)
    params: dict[str, str | int] = {"docid": docid}
    if version is not None:
        if version < 1:
            raise InvalidInputError("version must be a positive integer")
        params["version"] = version
    response = await client.request("GET", SHOW_URL, params=params)
    if response.status_code >= _MIN_ERROR_STATUS:
        raise UpstreamError(
            f"DocDB ShowDocument failed with status {response.status_code}",
            status_code=response.status_code,
        )
    return _parse_show_page(_decode(response.content), docid=docid, version=version)


def _parse_show_page(page: str, *, docid: int, version: int | None) -> dict[str, Any]:
    """Parse one ShowDocument HTML page into structured document metadata."""
    title_match = _TITLE_RE.search(page)
    if not title_match or "RevisionInfo" not in page:
        raise _not_found(docid)
    canonical = f"CMS-doc-{title_match.group(1)}-v{title_match.group(2) or '?'}"
    info = _basic_info(page)

    return {
        "source": SOURCE,
        "docid": docid,
        "canonical": canonical,
        "version": version,
        "title": _plain(_div(page, "DocTitle")) or None,
        "doc_type": info.get("Document type"),
        "submitted_by": info.get("Submitted by"),
        "updated_by": info.get("Updated by"),
        "created": info.get("Document Created"),
        "contents_revised": info.get("Contents Revised"),
        "metadata_revised": info.get("Metadata Revised"),
        "viewable_by": _group_list(page, "Viewable"),
        "modifiable_by": _group_list(page, "Modifiable"),
        "abstract": _abstract_section(page),
        "files": _file_entries(page),
        "versions": _version_entries(page, docid),
        "topics": _topic_entries(page),
        "authors": _author_entries(page),
        "keywords": _keyword_entries(page),
        "url": PUBLIC_URL.format(docid=docid),
    }


def _basic_info(page: str) -> dict[str, str]:
    """Label/value pairs from the BasicDocInfo definition list."""
    info: dict[str, str] = {}
    info_match = _INFO_BLOCK_RE.search(page)
    if info_match:
        for dt_frag, dd_frag in _DTDD_RE.findall(info_match.group(1)):
            label = _plain(dt_frag).rstrip(":").strip()
            info[label] = _plain(dd_frag)
    return info


def _file_entries(page: str) -> list[dict[str, Any]]:
    return [
        {
            "description": _plain(desc),
            "filename": _html.unescape(fname),
            "size": _plain(size),
            "download_url": _html.unescape(url),
        }
        for url, fname, desc, _fname2, size in _FILE_LINK_RE.findall(page)
    ]


def _version_entries(page: str, docid: int) -> list[dict[str, Any]]:
    return [
        {
            "version": int(vnum),
            "canonical": _plain(canon),
            "date": _plain(date),
        }
        for dnum, vnum, canon, date in _VERSION_ROW_RE.findall(page)
        if int(dnum) == docid
    ]


def _topic_entries(page: str) -> list[str]:
    topics: list[str] = []
    topics_block = _div_raw(page, "Topics")
    if topics_block:
        for li in re.findall(r"<li>(.*?)</li>", topics_block, re.S):
            names = [_plain(t) for _, t in _ANCHOR_RE.findall(li) if _plain(t)]
            if names:
                topics.append(" : ".join(names))
    return topics


def _author_entries(page: str) -> list[dict[str, Any]]:
    return [
        {
            "name": _plain(name),
            "authorid": int(aid),
            "institute": _html.unescape(inst) or None,
        }
        for inst, name, aid in _author_rows(page)
    ]


def _keyword_entries(page: str) -> list[str]:
    kw_block = _div_raw(page, "Keywords")
    if not kw_block:
        return []
    return [_plain(t) for t in _KW_LINK_RE.findall(kw_block) if _plain(t)]


def _abstract_section(page: str) -> str | None:
    abs_block = _div_raw(page, "Abstract")
    if not abs_block:
        return None
    dd_abs = re.search(r"<dd>(.*?)</dd>", abs_block, re.S)
    return _plain(dd_abs.group(1)) or None if dd_abs else None


def _div_raw(page: str, div_id: str) -> str | None:
    """Block content of a leaf div (these DocDB blocks never nest divs)."""
    match = re.search(rf'<div id="{div_id}">(.*?)</div>', page, re.S)
    return match.group(1) if match else None


def _div(page: str, div_id: str) -> str:
    return _div_raw(page, div_id) or ""


def _group_list(page: str, div_id: str) -> list[str]:
    block = _div_raw(page, div_id)
    if not block:
        return []
    return [_plain(t) for _, t in _ANCHOR_RE.findall(block) if _plain(t)]


def _author_rows(page: str) -> list[tuple[str, str, str]]:
    block = _div_raw(page, "Authors")
    if not block:
        return []
    rows = []
    for match in re.finditer(
        r'<a\s+href="[^"]*authorid=(\d+)[^"]*"[^>]*title="([^"]*)"[^>]*>([^<]*)</a>', block
    ):
        aid, inst, name = match.groups()
        rows.append((inst, name, aid))
    return rows


async def search_documents(
    client: SafeHttpClient,
    *,
    text: str | None = None,
    title: str | None = None,
    updated_from: str | None = None,
    updated_to: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> SearchResult:
    """Full-text (simpletext) or title search over CMS DocDB documents."""
    if not text and not title:
        raise InvalidInputError("At least one of text or title must be given")
    if not 1 <= limit <= _MAX_LIMIT:
        raise InvalidInputError(f"limit must be between 1 and {_MAX_LIMIT}")
    if offset < 0:
        raise InvalidInputError("offset must be >= 0")
    params: dict[str, str | int] = {}
    if text:
        params["simpletext"] = text
        params["simple"] = 1
    if title:
        params["titlesearch"] = title
        params["titlesearchmode"] = "allword"
        params["mode"] = "title"
    if updated_from is not None:
        params.update(_date_params("after", _check_date(updated_from, field="updated_from")))
    if updated_to is not None:
        params.update(_date_params("before", _check_date(updated_to, field="updated_to")))
    response = await client.request("GET", SEARCH_URL, params=params)
    if response.status_code >= _MIN_ERROR_STATUS:
        raise UpstreamError(
            f"DocDB search failed with status {response.status_code}",
            status_code=response.status_code,
        )
    page = _decode(response.content)
    items, total = _rows(page, limit=limit, offset=offset)
    return SearchResult(
        source=SOURCE,
        items=items,
        total_count=total,
        page=offset // limit + 1,
        size=limit,
        has_more=offset + len(items) < (total or 0),
        note="Client-side pagination over the CMS DocDB search results table",
    )


async def list_recent(
    client: SafeHttpClient,
    *,
    days: int = 20,
    limit: int = 20,
    offset: int = 0,
) -> SearchResult:
    """Documents modified in the last `days` days (DocDB 'see recent' list)."""
    if not 1 <= days <= _MAX_RECENT_DAYS:
        raise InvalidInputError(f"days must be between 1 and {_MAX_RECENT_DAYS}")
    if not 1 <= limit <= _MAX_LIMIT:
        raise InvalidInputError(f"limit must be between 1 and {_MAX_LIMIT}")
    if offset < 0:
        raise InvalidInputError("offset must be >= 0")
    response = await client.request("GET", LISTBY_URL, params={"days": days})
    if response.status_code >= _MIN_ERROR_STATUS:
        raise UpstreamError(
            f"DocDB ListBy failed with status {response.status_code}",
            status_code=response.status_code,
        )
    page = _decode(response.content)
    items, total = _rows(page, limit=limit, offset=offset)
    return SearchResult(
        source=SOURCE,
        items=items,
        total_count=total,
        page=offset // limit + 1,
        size=limit,
        has_more=offset + len(items) < (total or 0),
        note=f"Documents modified in the last {days} days",
    )


async def download_file(
    client: SafeHttpClient,
    *,
    docid: int,
    filename: str,
    version: int | None = None,
) -> dict[str, Any]:
    """Bounded download of one DocDB file into CMcpS state storage."""
    _check_docid(docid)
    _check_filename(filename)
    url = f"{RETRIEVE_URL}?docid={docid}&filename={quote(filename)}" + (
        f"&version={version}" if version is not None else ""
    )
    final = await client.download(url, Path(filename))
    return {
        "source": SOURCE,
        "docid": docid,
        "filename": filename,
        "version": version,
        "download_url": url,
        "path": str(final),
        "size_bytes": final.stat().st_size,
    }
