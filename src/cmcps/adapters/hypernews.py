"""Read-only adapter for CMS HyperNews (hypernews.cern.ch).

CMS HyperNews was the collaboration's forum/mailing-list system (SLAC
HyperNews, ~2000-2022). It was discontinued at the end of 2022 in favor of
CMS Talk (Discourse, cms-talk.web.cern.ch), with 2022 as the overlap year;
what remains is a read-only archive served by HyperNewsViewer
(github.com/henryiii/hypernewsviewer). The viewer has no JSON API — every
page is server-rendered HTML, so this adapter is an explicit HTML-parsing
fallback, like the TWiki/DocDB adapters. The whole site sits behind an
oauth2-proxy fronting CERN SSO, so the shared cookie session suffices.

Surfaces used (all GET, all confirmed live 2026-10-26):

- ``/HyperNews/CMS/search?query=…&start=…&stop=…&page=N`` — SQLite FTS5
  full-text search, 50 results per page, server-side date range anchored to
  2000-01-01..2022-12-31 (the archive freeze date).
- ``/HyperNews/CMS/index`` — all forums sorted by last activity.
- ``/HyperNews/CMS/cindex`` — forums grouped by category.
- ``/HyperNews/CMS/get/<forum>[.html]`` — forum title + top-level threads.
- ``/HyperNews/CMS/get/<forum>/<msg>[/<sub>…][.html]`` — one message:
  header (title/forum/date/author), body, direct replies, navigation flags.
- ``/HyperNews/CMS/view-member.pl?<username>`` — member profile rendered as
  flat ``key: value<br>`` lines. The route reads the first query *key* as
  the username, so the parameter name must be the username itself.
- ``/HyperNews/CMS/get/AUX/<path>`` — attachment files (bounded downloads).

Missing objects: a missing forum or member makes the viewer raise (HTTP
500); a missing message inside an existing forum returns HTTP 200 with the
plain text "Unable to find message: …". Both map to UpstreamError.
"""

from __future__ import annotations

import html as _html
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cmcps.adapters.common import html_to_text
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient

SOURCE = "hypernews"

BASE = "https://hypernews.cern.ch"
GET_BASE = f"{BASE}/HyperNews/CMS/get"
SEARCH_URL = f"{BASE}/HyperNews/CMS/search"
INDEX_URL = f"{BASE}/HyperNews/CMS/index"
CINDEX_URL = f"{BASE}/HyperNews/CMS/cindex"
MEMBER_URL = f"{BASE}/HyperNews/CMS/view-member.pl"
AUX_PATH_PREFIX = "/HyperNews/CMS/get/AUX/"

_MIN_ERROR_STATUS = 400
_SEARCH_PAGE_SIZE = 50
_MAX_LIMIT = 3000
_BODY_TEXT_LIMIT = 200_000
_NOT_FOUND_MARK = "Unable to find message"

_FORUM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
_MSG_PATH_RE = re.compile(r"^\d+(?:/\d+)*$")
_MEMBER_RE = re.compile(r"^[A-Za-z0-9._@-]{1,128}$")
_DATE_INPUT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TITLE_TAG_RE = re.compile(r"<title>(.*?)</title>", re.S)

# index rows: <b><a href="https://…/HyperNews/CMS/get/NAME.html">TITLE</a> </b> (YYYY-MM-DD)
_INDEX_ROW_RE = re.compile(
    r'<b>\s*<a\s+href="[^"]*/HyperNews/CMS/get/([^"/]+?)(?:\.html)?"\s*>'
    r"(.*?)\s*</a>\s*</b>\s*\((\d{4}-\d{2}-\d{2})\)",
    re.S,
)
# cindex: category headers and forum links, matched positionally in one pass.
_CINDEX_TOKEN_RE = re.compile(
    r'<div class="categoryheader"><a\s+name="\d+"></a>Category:\s*(.*?)</div>'
    r'|<div class="categoryforum"><a\s+class="catlink"\s+href="[^"]*/get/([^"/]+?)'
    r'(?:\.html)?"\s*>\s*(.*?)\s*</a>\s*</div>',
    re.S,
)
# One thread/reply item inside <div class="listing"><ol>.
_LISTING_HEAD_RE = re.compile(r'<li\s+value="(\d+)">')
_LISTING_LINK_RE = re.compile(r'<a\s+href="(/HyperNews/CMS/get/[^"]+)"\s*>\s*(.*?)\s*</a>', re.S)
_LISTING_EM_RE = re.compile(r"<em>\((.*?)\)</em>", re.S)
_LISTING_REPLIES_RE = re.compile(r"(\d+)\s+repl(?:y|ies)")
# searchresult block: title anchor, posted timestamp, member anchor, snippet <p>.
_SR_BLOCK_RE = re.compile(r'<div class="searchresult">(.*?)(?=<div class="searchresult">|$)', re.S)
_SR_HEAD_RE = re.compile(
    r'<a\s+href="(/HyperNews/CMS/get/[^"]+)"\s*>\s*(.*?)\s*</a>\s*'
    r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*-\s*by\s*"
    r'<a\s+href="[^"]*view-member\.pl\?[^"]*"\s*>\s*([^<]*?)\s*</a>',
    re.S,
)
_SR_SNIPPET_RE = re.compile(r"<p>(.*?)</p>", re.S)
# Message header fields.
_MSG_TITLE_RE = re.compile(r"<h2><img[^>]*>(.*?)</h2>", re.S)
_MSG_FORUM_RE = re.compile(r"<i>Forum:</i>\s*<a\s[^>]*>\s*(.*?)\s*</a>", re.S)
_MSG_DATE_RE = re.compile(r"<i>Date:</i>\s*(.*?)\s*<br", re.S)
_MSG_FROM_RE = re.compile(r'<i>From:</i>\s*<a\s+href="/view-member\.pl\?([^"]+)">(.*?)&lt;', re.S)
_BODY_START = "<!--  start message body  -->"
_BODY_END = "<!--  end message body -->"
_BODY_ALT_CUTS = ('<div class="listing">', "<details>", '<hr size="5"')
# Attachment hrefs; bodies carry both absolute and root/site-relative forms,
# and the archived 1997-era markup uses uppercase tags/attributes.
_AUX_HREF_RE = re.compile(
    r'href="(?:https?://hypernews\.cern\.ch)?/?(?:HyperNews/CMS/)?get/AUX/([^"< ]+)"',
    re.IGNORECASE,
)
# Member page: flat "key: value<br>" lines.
_MEMBER_ROW_RE = re.compile(r"(?m)^\s*([A-Za-z_0-9]+):[ \t]?(.*?)<br\s*/?>")
_LISTING_OPEN = '<div class="listing">'
_NEXT_IN_THREAD_MARK = "dir=next-in-thread"
_NEXT_IN_FORUM_MARK = "dir=nextResponse"


def _plain(fragment: str) -> str:
    """Strip tags/entities and collapse whitespace on a small fragment."""
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _check_forum(forum: str) -> str:
    if not _FORUM_RE.fullmatch(forum):
        raise InvalidInputError(
            f"invalid HyperNews forum name {forum!r}",
            instructions=(
                "Forum names are single URL path segments like hnTest or mip-timing-detector."
            ),
        )
    return forum


def _check_msg_path(path: str) -> str:
    if not _MSG_PATH_RE.fullmatch(path):
        raise InvalidInputError(
            f"invalid HyperNews message path {path!r}",
            instructions="Message paths are slash-separated numbers like '337' or '337/1/2'.",
        )
    return path


def _check_member(username: str) -> str:
    if not _MEMBER_RE.fullmatch(username):
        raise InvalidInputError(
            f"invalid HyperNews username {username!r}",
            instructions="Usernames look like 'ric' or 'karlis.dreimanis@cern.ch'.",
        )
    return username


def _check_date(value: str, *, field: str) -> str:
    if not _DATE_INPUT_RE.fullmatch(value):
        raise InvalidInputError(f"{field} must be an ISO date (YYYY-MM-DD), got {value!r}")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise InvalidInputError(f"{field} is not a valid calendar date: {value!r}") from exc
    return value


def _check_response(response: Any, *, what: str) -> str:
    """Guard HTTP status and decode the page; raises UpstreamError."""
    if response.status_code >= _MIN_ERROR_STATUS:
        instructions = (
            "The archive answers 500 for objects it does not have — check the "
            "name with hypernews_list_forums / hypernews_search and retry. If "
            "the error is 401/403 instead, sign in again with cern_auth_sign_in."
        )
        raise UpstreamError(
            f"hypernews {what} failed with status {response.status_code}",
            status_code=response.status_code,
            instructions=instructions,
        )
    return response.content.decode("utf-8", errors="replace")


def _check_message_page(page: str, forum: str, path: str) -> None:
    if _NOT_FOUND_MARK in page[:2000]:
        raise UpstreamError(
            f"hypernews has no message {forum}/{path}",
            status_code=404,
            instructions=(
                "Check the thread with hypernews_get_forum or find it with hypernews_search."
            ),
        )


def _split_href(href: str) -> tuple[str, str | None]:
    """`forum` and message path of a /HyperNews/CMS/get/… href."""
    rel = href.split("/HyperNews/CMS/get/", 1)[1].lstrip("/")
    rel = rel.removesuffix(".html")
    forum, _, rest = rel.partition("/")
    return forum, rest or None


def _title_of(page: str) -> str | None:
    match = _TITLE_TAG_RE.search(page)
    return _plain(match.group(1)) or None if match else None


def _listing_items(page: str) -> list[dict[str, Any]]:
    """Parse the <div class="listing"><ol> thread/reply items of a page."""
    open_at = page.find(_LISTING_OPEN)
    if open_at < 0:
        return []
    ol_end = page.find("</ol>", open_at)
    region = page[open_at : ol_end if ol_end > 0 else len(page)]
    heads = list(_LISTING_HEAD_RE.finditer(region))
    items: list[dict[str, Any]] = []
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(region)
        chunk = region[head.start() : end]
        link = _LISTING_LINK_RE.search(chunk)
        if link is None:
            continue
        forum, msg_path = _split_href(link.group(1))
        item: dict[str, Any] = {
            "num": int(head.group(1)),
            "message": msg_path,
            "forum": forum,
            "title": _plain(link.group(2)) or None,
            "url": f"{GET_BASE}/{forum}/{msg_path}.html" if msg_path else None,
        }
        em = _LISTING_EM_RE.search(chunk)
        if em is not None:
            author, _, date = em.group(1).rpartition(" - ")
            item["author"] = _plain(author) or None
            item["date"] = _plain(date) or None
        replies = _LISTING_REPLIES_RE.search(chunk)
        if replies is not None:
            item["replies"] = int(replies.group(1))
        items.append(item)
    return items


def _body_region(page: str) -> str:
    start = page.find(_BODY_START)
    if start >= 0:
        start += len(_BODY_START)
        end = page.find(_BODY_END, start)
        return page[start : end if end >= 0 else len(page)]
    # Plain-HTML messages (content_type 'h') render the body in a bare <div>
    # after the header definition list, without the comment markers.
    if "</dl>" in page:
        after = page.split("</dl>", 1)[1]
        cut = len(after)
        for mark in _BODY_ALT_CUTS:
            at = after.find(mark)
            if 0 <= at < cut:
                cut = at
        return after[:cut]
    return ""


def _attachments_of(region: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in _AUX_HREF_RE.finditer(region):
        tail = _html.unescape(match.group(1)).strip()
        if not tail or tail in seen or ".." in tail:
            continue
        seen.add(tail)
        urls.append(f"{GET_BASE}/AUX/{tail}")
    return urls


def _categories_of(page: str) -> dict[str, str]:
    """forum name -> category name map from the cindex page."""
    mapping: dict[str, str] = {}
    category: str | None = None
    for token in _CINDEX_TOKEN_RE.finditer(page):
        if token.group(1) is not None:
            category = _plain(token.group(1))
        elif category is not None:
            mapping[token.group(2)] = category
    return mapping


async def search_messages(
    client: SafeHttpClient,
    *,
    query: str,
    start: str | None = None,
    stop: str | None = None,
    page: int = 1,
) -> SearchResult:
    """Full-text (SQLite FTS5) search over the archived posts, 50 per page."""
    if not query.strip():
        raise InvalidInputError("query must not be empty")
    params: dict[str, str | int] = {"query": query, "page": page}
    if start is not None:
        params["start"] = _check_date(start, field="start")
    if stop is not None:
        params["stop"] = _check_date(stop, field="stop")
    response = await client.request("GET", SEARCH_URL, params=params)
    page_html = _check_response(response, what="search")
    items: list[dict[str, Any]] = []
    for block in _SR_BLOCK_RE.findall(page_html):
        head = _SR_HEAD_RE.search(block)
        if head is None:
            continue
        forum, msg_path = _split_href(head.group(1))
        snippet_match = _SR_SNIPPET_RE.search(block)
        items.append(
            {
                "forum": forum,
                "message": msg_path,
                "title": _plain(head.group(2)) or None,
                "posted": head.group(3),
                "author_username": _html.unescape(head.group(4)).strip() or None,
                "snippet": _plain(snippet_match.group(1)) if snippet_match else None,
                "url": f"{GET_BASE}/{forum}/{msg_path}.html" if msg_path else None,
            }
        )
        if len(items) >= _SEARCH_PAGE_SIZE:
            break
    return SearchResult(
        source=SOURCE,
        query=query,
        items=items,
        total_count=None,
        page=page,
        size=len(items),
        has_more=len(items) >= _SEARCH_PAGE_SIZE,
        note=(
            "Archive of the discontinued CMS HyperNews (shut down end of 2022, "
            "replaced by CMS Talk; 2022 was the overlap year). Results cap at "
            "50 per page; use start/stop date filters to narrow slow queries."
        ),
    )


async def list_forums(
    client: SafeHttpClient,
    *,
    category: str | None = None,
    title_contains: str | None = None,
    limit: int = 100,
) -> SearchResult:
    """All archive forums by last activity (index page), with client-side filters."""
    if not 1 <= limit <= _MAX_LIMIT:
        raise InvalidInputError(f"limit must be between 1 and {_MAX_LIMIT}")
    response = await client.request("GET", INDEX_URL)
    index_html = _check_response(response, what="forum index")
    rows = [(_plain(t), name, d) for name, t, d in _INDEX_ROW_RE.findall(index_html)]

    categories: dict[str, str] = {}
    if category is not None:
        cat_response = await client.request("GET", CINDEX_URL)
        categories = _categories_of(_check_response(cat_response, what="category index"))
        wanted = category.casefold()
        rows = [row for row in rows if wanted in categories.get(row[1], "").casefold()]
    if title_contains is not None:
        needle = title_contains.casefold()
        rows = [row for row in rows if needle in row[0].casefold()]

    total = len(rows)
    sliced = rows[:limit]
    items = [
        {
            "forum": name,
            "title": title or None,
            "last_activity": date,
            "category": categories.get(name) if category is not None else None,
            "url": f"{GET_BASE}/{name}.html",
        }
        for title, name, date in sliced
    ]
    filter_desc = (
        None
        if (category, title_contains) == (None, None)
        else f"category={category} title_contains={title_contains}"
    )
    return SearchResult(
        source=SOURCE,
        query=filter_desc,
        items=items,
        total_count=total,
        page=1,
        size=len(items),
        has_more=total > len(items),
        note=(
            "Archive of the discontinued CMS HyperNews (shut down end of 2022, "
            "replaced by CMS Talk; 2022 was the overlap year). Sorted by last "
            "activity, most recent first."
        ),
    )


async def get_forum(
    client: SafeHttpClient,
    *,
    forum: str,
    limit: int = 100,
) -> dict[str, Any]:
    """One forum: title plus its newest top-level threads (author/date).

    Forums reach ~2000 threads (~1 MB of listing HTML), so the returned
    slice is bounded; ``thread_count`` always reports the full count.
    """
    forum = _check_forum(forum)
    if not 1 <= limit <= _MAX_LIMIT:
        raise InvalidInputError(f"limit must be between 1 and {_MAX_LIMIT}")
    response = await client.request("GET", f"{GET_BASE}/{forum}")
    page_html = _check_response(response, what=f"forum {forum!r}")
    threads = _listing_items(page_html)
    for thread in threads:
        thread.pop("forum", None)
    return {
        "source": SOURCE,
        "forum": forum,
        "title": _title_of(page_html),
        "threads": threads[:limit],
        "thread_count": len(threads),
        "truncated": len(threads) > limit,
        "url": f"{GET_BASE}/{forum}.html",
    }


async def get_message(
    client: SafeHttpClient,
    *,
    forum: str,
    path: str,
) -> dict[str, Any]:
    """One archived message: header, plain-text body, replies, attachments."""
    forum = _check_forum(forum)
    path = _check_msg_path(path)
    response = await client.request("GET", f"{GET_BASE}/{forum}/{path}")
    page_html = _check_response(response, what=f"message {forum}/{path}")
    _check_message_page(page_html, forum, path)

    title = _MSG_TITLE_RE.search(page_html)
    forum_title = _MSG_FORUM_RE.search(page_html)
    date = _MSG_DATE_RE.search(page_html)
    author = _MSG_FROM_RE.search(page_html)
    body = _body_region(page_html)
    replies = _listing_items(page_html)
    for reply in replies:
        reply.pop("forum", None)
        reply.pop("num", None)
    result: dict[str, Any] = {
        "source": SOURCE,
        "forum": forum,
        "message": path,
        "title": _plain(title.group(1)) if title else _title_of(page_html),
        "forum_title": _plain(forum_title.group(1)) or None if forum_title else None,
        "date": _plain(date.group(1)) or None if date else None,
        "author_name": _plain(author.group(2)) or None if author else None,
        "author_username": _html.unescape(author.group(1)).strip() or None if author else None,
        "text": html_to_text(body, limit=_BODY_TEXT_LIMIT),
        "replies": replies,
        "attachments": _attachments_of(body),
        "has_next_in_thread": _NEXT_IN_THREAD_MARK in page_html,
        "has_next_in_forum": _NEXT_IN_FORUM_MARK in page_html,
        "url": f"{GET_BASE}/{forum}/{path}.html",
    }
    return result


async def get_member(
    client: SafeHttpClient,
    *,
    username: str,
) -> dict[str, Any]:
    """Member profile (name, email, homepage) from the flat key:value page."""
    username = _check_member(username)
    # The route reads the first query *key* as the username, so the parameter
    # name itself must carry it (?<username>=, not ?user=<username>).
    response = await client.request("GET", MEMBER_URL, params={username: ""})
    page_html = _check_response(response, what=f"member {username!r}")
    fields: dict[str, str] = {}
    for key, value in _MEMBER_ROW_RE.findall(page_html):
        if key in fields:
            continue
        fields[key] = _plain(value)
    if not fields.get("user_id"):
        raise UpstreamError(
            f"hypernews member {username!r} not found",
            status_code=404,
            instructions=(
                "HyperNews usernames are case-sensitive; copy them from search "
                "results or message pages."
            ),
        )
    return {
        "source": SOURCE,
        "username": fields["user_id"],
        "name": fields.get("name") or None,
        "email": fields.get("email") or None,
        "email2": fields.get("email2") or None,
        "user_url": fields.get("user_url") or None,
        "status": fields.get("status") or None,
        "fields": fields,
        "url": f"{MEMBER_URL}?{username}",
    }


async def download_file(
    client: SafeHttpClient,
    *,
    download_url: str,
) -> dict[str, Any]:
    """Bounded download of one /HyperNews/CMS/get/AUX/… attachment."""
    parts = urlsplit(download_url)
    if parts.hostname != "hypernews.cern.ch" or not parts.path.startswith(AUX_PATH_PREFIX):
        raise InvalidInputError(
            "hypernews_download_file only accepts HyperNews attachment URLs",
            instructions=(
                "Pass an attachments URL from hypernews_get_message (/HyperNews/CMS/get/AUX/…)."
            ),
        )
    url = f"{BASE}{parts.path}"
    filename = parts.path.rsplit("/", 1)[-1] or None
    final = await client.download(url, Path(filename) if filename else None)
    return {
        "source": SOURCE,
        "download_url": url,
        "path": str(final),
        "size_bytes": final.stat().st_size,
    }
