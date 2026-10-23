"""Read-only adapter for CERN TWiki (twiki.cern.ch).

Live behavior (verified 2026-10):

- ``GET /twiki/bin/view/<Web>/<Topic>?cover=print`` — rendered page with
  the low-noise print cover; anonymous for every web, restricted ones
  included (e.g. ``CMS``, ``CMS/Internal``). CERN TWiki no longer serves the
  wiki source: ``?raw=on`` returns the rendered page (200, HTML) for every
  web and session, so page content is always taken from the render. The
  metadata the wiki source used to carry (``%META`` records) is parsed from
  the print-cover footer instead: ``Topic revision: r<n> - YYYY-MM-DD -
  <user>``, the ``This topic:`` breadcrumb (immediate parent), and the
  marked ``twikiAttachments`` section (attachment names/URLs).
- ``GET /twiki/bin/viewauth/<Web>/<Topic>`` — forces the SSO handshake;
  visited at sign-in so the TWiki session cookie lands in the jar for
  scripts that do gate on login (e.g. attachments in protected webs).
- ``GET /twiki/bin/view/<Web>/WebTopicList`` — full topic list of a web
  (public webs anonymous).
- ``GET /twiki/pub/<Web>/<Topic>/<file>`` — attachments, SSO for protected
  webs; bounded download through the shared client.

Webs are hierarchical: subwebs appear as ``<Web>/<Subweb>`` segments in the
URL path (e.g. the ``Internal`` subweb of ``CMS`` is addressed as
``CMS/Internal``).

Endpoint type: TWiki CGI scripts (documented behavior), HTML parsing of the
rendered print cover.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cmcps.adapters.common import html_to_text
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient

SOURCE = "twiki"

BASE = "https://twiki.cern.ch/twiki/bin"
PUB_BASE = "https://twiki.cern.ch/twiki/pub"

# Webs are hierarchical: CamelCase segments joined by "/" (e.g. CMS/Internal).
_WEB_SEGMENT = r"[A-Z][A-Za-z0-9-]*"
_WEB_RE = re.compile(rf"^{_WEB_SEGMENT}(?:/{_WEB_SEGMENT})*$")
_TOPIC_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_VIEW_URL_RE = re.compile(
    rf"^/twiki/bin/view/({_WEB_SEGMENT}(?:/{_WEB_SEGMENT})*)/([A-Za-z0-9_.-]+)$"
)
_MIN_ERROR_STATUS = 400
_PAGE_TEXT_LIMIT = 200_000
# Print-cover footer metadata (replaces the wiki-source %META records; CERN
# TWiki no longer serves raw source — see module docstring).
_REVISION_RE = re.compile(
    r"Topic revision:\s*(r\d+)\s*-\s*(\d{4}-\d{2}-\d{2})\s*-\s*([A-Za-z0-9]+)"
)
_ATTACH_SECTION_RE = re.compile(r"<!--twikiAttachments-->(.*?)<!--//twikiAttachments-->", re.DOTALL)
_BREADCRUMB_MARK = "This topic:"
_BREADCRUMB_SPAN = 3000
_BREADCRUMB_LINK_RE = re.compile(r'href="/twiki/bin/view/([^"]+)/([A-Za-z0-9_.-]+)"')


class _LinkCollector(HTMLParser):
    """Collect ``/twiki/bin/view/...`` links with their anchor text."""

    _BLOCK_TAGS: frozenset[str] = frozenset({"searchinput", "head"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str, str]] = []
        self._current: tuple[str, str] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = dict(attrs).get("href") or ""
            match = _VIEW_URL_RE.match(urlsplit(href).path)
            if match:
                self._current = (match.group(1), match.group(2))
                self._text = []

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current is not None:
            text = " ".join("".join(self._text).split())
            web, topic = self._current
            self.links.append((web, topic, text))
            self._current = None
            self._text = []


def _auth_instructions() -> str:
    return (
        "This Twiki path requires a signed-in Twiki session. Sign in with "
        "cern_auth_sign_in — it visits twiki.cern.ch to establish the session — and retry."
    )


def _check_web(web: str) -> str:
    if not _WEB_RE.fullmatch(web):
        raise InvalidInputError(
            f"invalid Twiki web {web!r}",
            instructions=(
                "Webs are CamelCase names like CMSPublic, Main, TWiki, optionally with "
                "subwebs joined by '/' like CMS/Internal."
            ),
        )
    return web


def _check_topic(topic: str) -> str:
    if not _TOPIC_RE.fullmatch(topic):
        raise InvalidInputError(
            f"invalid Twiki topic {topic!r}",
            instructions="Topics are WikiWord names like WebHome or Hig11024TWiki.",
        )
    return topic


def _parent_from_breadcrumb(html: str) -> str | None:
    """Immediate parent topic from the 'This topic:' breadcrumb links."""
    mark = html.find(_BREADCRUMB_MARK)
    if mark < 0:
        return None
    segment = html[mark : mark + _BREADCRUMB_SPAN]
    links = _BREADCRUMB_LINK_RE.findall(segment)
    return links[-1][1] if links else None


def _attachments_from_print(html: str, web: str, topic: str) -> list[dict[str, Any]]:
    """Attachment names/URLs from the marked twikiAttachments section."""
    match = _ATTACH_SECTION_RE.search(html)
    if match is None:
        return []
    prefix = f"/twiki/pub/{web}/{topic}/"
    attachments: list[dict[str, Any]] = []
    seen: set[str] = set()
    for href in re.finditer(r'href="(/twiki/pub/[^"]+)"', match.group(1)):
        path = href.group(1)
        if not path.startswith(prefix):
            continue
        name = path[len(prefix) :]
        if not name or name in seen:
            continue
        seen.add(name)
        attachments.append({"name": name, "url": f"{PUB_BASE}/{web}/{topic}/{name}"})
    return attachments


def _meta_from_print(html: str, web: str, topic: str) -> dict[str, Any]:
    """Find revision/author/date/parent/attachments in the print-cover footer."""
    revision = _REVISION_RE.search(html)
    date_epoch = None
    if revision is not None:
        try:
            date_epoch = int(
                datetime.strptime(revision.group(2), "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
            )
        except ValueError:
            date_epoch = None
    return {
        "author": revision.group(3) if revision else None,
        "version": revision.group(1) if revision else None,
        "date_epoch": date_epoch,
        "parent_topic": _parent_from_breadcrumb(html),
        "attachments": _attachments_from_print(html, web, topic),
    }


async def list_topics(
    client: SafeHttpClient,
    *,
    web: str,
    limit: int = 200,
) -> SearchResult:
    """Topic list of one public web (via the web's WebTopicList page)."""
    web = _check_web(web)
    response = await client.request("GET", f"{BASE}/view/{web}/WebTopicList")
    if response.status_code >= _MIN_ERROR_STATUS:
        raise UpstreamError(
            f"twiki topic list failed with status {response.status_code}",
            status_code=response.status_code,
            instructions=_auth_instructions(),
        )
    collector = _LinkCollector()
    collector.feed(response.content.decode("utf-8", errors="replace"))
    seen: set[str] = set()
    items = []
    for result_web, topic, _text in collector.links:
        if result_web != web or topic in seen or not topic[0].isalnum():
            continue
        seen.add(topic)
        items.append({"web": web, "topic": topic, "url": f"{BASE}/view/{web}/{topic}"})
        if len(items) >= limit:
            break
    return SearchResult(
        source=SOURCE,
        query=f"web={web} WebTopicList",
        items=items,
        total_count=len(items),
        page=1,
        size=len(items),
        has_more=len(items) >= limit,
    )


async def get_page(
    client: SafeHttpClient,
    *,
    web: str,
    topic: str,
    raw: bool = True,
) -> dict[str, Any]:
    """One Twiki page, fetched with the low-noise print cover (``cover=print``).

    CERN TWiki serves rendered HTML for every access mode (``?raw=on``
    included), so both raw values return the rendered content as plain text;
    ``raw=True`` additionally returns the metadata parsed from the
    print-cover footer (revision/author/date, parent topic, attachments).
    """
    web = _check_web(web)
    topic = _check_topic(topic)
    url = f"{BASE}/view/{web}/{topic}"
    response = await client.request("GET", url, params={"cover": "print"})
    if response.status_code >= _MIN_ERROR_STATUS:
        raise UpstreamError(
            f"twiki page failed with status {response.status_code} for {response.url}",
            status_code=response.status_code,
            instructions=_auth_instructions(),
        )
    html = response.content.decode("utf-8", errors="replace")
    result: dict[str, Any] = {
        "source": SOURCE,
        "web": web,
        "topic": topic,
        "text": html_to_text(html, limit=_PAGE_TEXT_LIMIT),
        "url": url,
    }
    if raw:
        result.update(_meta_from_print(html, web, topic))
    return result


async def download_attachment(
    client: SafeHttpClient,
    *,
    attachment_url: str,
) -> dict[str, Any]:
    """Bounded download of one /twiki/pub attachment."""
    parts = urlsplit(attachment_url)
    if parts.hostname != "twiki.cern.ch" or not parts.path.startswith("/twiki/pub/"):
        raise InvalidInputError(
            "twiki_download_attachment only accepts /twiki/pub/... URLs",
            instructions="Pass a URL from twiki_get_page attachments.",
        )
    filename = parts.path.rsplit("/", 1)[-1] or None
    final = await client.download(attachment_url, Path(filename) if filename else None)
    return {
        "source": SOURCE,
        "download_url": attachment_url,
        "path": str(final),
        "size_bytes": final.stat().st_size,
    }
