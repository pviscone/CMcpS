"""Read-only adapter for CERN TWiki (twiki.cern.ch).

CERN's TWiki sits behind OIDC SSO for every script except anonymous
``view`` of public webs:

- ``GET /twiki/bin/view/<Web>/<Topic>`` — rendered skin HTML; anonymous for
  public webs (e.g. ``CMSPublic``), SSO for restricted webs.
- ``GET /twiki/bin/view/<Web>/<Topic>?raw=on`` — raw wiki source with
  ``%META:*`` records; anonymous requests redirect to auth.cern.ch (OIDC),
  so this path always uses the SSO session. Topic metadata (author, date,
  revision) and attachment manifests are only present in the raw source.
- ``GET /twiki/bin/search/<Web>/?search=...&type=keyword|literal|regex`` —
  search; SSO-protected at CERN. Results render as ``bin/view`` links inside
  the search template; the adapter extracts ``(web, topic)`` pairs. Multiple
  webs in one search are passed as ``?web=Web1,Web2`` (TWiki VarSEARCH
  comma-list) while the first web stays in the URL path.
- ``GET /twiki/bin/view/<Web>/WebTopicList`` — full topic list of a web
  (public webs anonymous).
- ``GET /twiki/pub/<Web>/<Topic>/<file>`` — attachments, SSO for protected
  webs; bounded download through the shared client.

Endpoint type: TWiki CGI scripts (documented behavior), HTML parsing of the
rendered skin; raw text is used whenever SSO is available. Auth-required
HTML is detected (OIDC redirect target or HTML where wiki text is expected)
and reported as a structured upstream error with sign-in instructions.
"""

from __future__ import annotations

import re
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

_WEB_RE = re.compile(r"^[A-Z][A-Za-z0-9-]*$")
_TOPIC_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_VIEW_URL_RE = re.compile(r"^/twiki/bin/view/([A-Z][A-Za-z0-9-]*)/([A-Za-z0-9_.-]+)$")
_META_LINE_RE = re.compile(r"^%META:([A-Z]+)\{(.*)\}%$", re.MULTILINE)
_META_FIELD_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')
_SEARCH_TYPES = ("keyword", "literal", "regex")
_OIDC_MARKER = "auth.cern.ch"
_MIN_ERROR_STATUS = 400


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
    return "This Twiki script requires CERN SSO. Sign in with cern_auth_sign_in and retry."


def _looks_like_auth_html(content: bytes) -> bool:
    head = content[:4096].lstrip()
    return head.startswith(b"<") or b"login" in head[:1024].lower()


def _check_web(web: str) -> str:
    if not _WEB_RE.fullmatch(web):
        raise InvalidInputError(
            f"invalid Twiki web {web!r}",
            instructions="Webs are CamelCase names like CMSPublic, Main, TWiki.",
        )
    return web


def _check_topic(topic: str) -> str:
    if not _TOPIC_RE.fullmatch(topic):
        raise InvalidInputError(
            f"invalid Twiki topic {topic!r}",
            instructions="Topics are WikiWord names like WebHome or Hig11024TWiki.",
        )
    return topic


def _check_webs(web: str) -> list[str]:
    """Validate one web name or a comma-separated list; returns the ordered list."""
    webs = []
    for part in web.split(","):
        name = part.strip()
        if not _WEB_RE.fullmatch(name):
            raise InvalidInputError(
                f"invalid Twiki web {name!r}",
                instructions="Webs are CamelCase names like CMS, CMSPublic, Main. Pass one web or a "
                "comma-separated list, e.g. 'CMS,CMSPublic'.",
            )
        if name in webs:
            raise InvalidInputError(
                f"duplicate Twiki web {name!r} in list",
                instructions="List each web once, e.g. 'CMS,CMSPublic'.",
            )
        webs.append(name)
    return webs


def _parse_meta_fields(body: str) -> dict[str, str]:
    return {name: value.replace('\\"', '"') for name, value in _META_FIELD_RE.findall(body)}


def _parse_meta(raw: str) -> tuple[list[tuple[str, dict[str, str]]], str]:
    meta = [(kind, _parse_meta_fields(fields)) for kind, fields in _META_LINE_RE.findall(raw)]
    body = _META_LINE_RE.sub("", raw)
    return meta, body.strip("\n")


async def search(
    client: SafeHttpClient,
    *,
    web: str,
    query: str,
    search_type: str = "keyword",
    limit: int = 50,
) -> SearchResult:
    """Full-text search of one or more comma-separated Twiki webs; results are view-link references."""
    if not query.strip():
        raise InvalidInputError(
            "query must not be empty",
            instructions="Pass a keyword, literal phrase, or regex according to search_type.",
        )
    if search_type not in _SEARCH_TYPES:
        raise InvalidInputError(
            f"search_type must be one of {_SEARCH_TYPES}",
            instructions="keyword, literal, or regex.",
        )
    webs = _check_webs(web)
    params: dict[str, Any] = {
        "search": query,
        "type": search_type,
        "scope": "text",
        "limit": limit,
    }
    if len(webs) > 1:
        # Multi-web: base web stays in the path; the full list goes to the
        # CGI's ``web`` parameter, forwarded to %SEARCH% (TWiki VarSEARCH).
        params["web"] = ",".join(webs)
    response = await client.request(
        "GET",
        f"{BASE}/search/{webs[0]}/",
        params=params,
    )
    if response.status_code >= _MIN_ERROR_STATUS:
        raise UpstreamError(
            f"twiki search failed with status {response.status_code} for {response.url}",
            status_code=response.status_code,
            instructions=_auth_instructions(),
        )
    collector = _LinkCollector()
    collector.feed(response.content.decode("utf-8", errors="replace"))
    seen: set[tuple[str, str]] = set()
    items = []
    for result_web, topic, text in collector.links:
        key = (result_web, topic)
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "web": result_web,
                "topic": topic,
                "anchor": text or None,
                "url": f"{BASE}/view/{result_web}/{topic}",
            }
        )
    return SearchResult(
        source=SOURCE,
        query=f"web={','.join(webs)} type={search_type}: {query}",
        items=items,
        total_count=len(items),
        page=1,
        size=len(items),
        has_more=len(items) >= limit,
        note="Twiki search renders result links only; fetch content with "
        "twiki_get_page. Set limit to bound the server-side result count.",
    )


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
    """One Twiki page. raw=True returns wiki source + parsed %META records
    (needs SSO); raw=False returns the anonymous rendered text."""
    web = _check_web(web)
    topic = _check_topic(topic)
    url = f"{BASE}/view/{web}/{topic}"
    if raw:
        response = await client.request("GET", url, params={"raw": "on"})
    else:
        response = await client.request("GET", url)
    if response.status_code >= _MIN_ERROR_STATUS:
        raise UpstreamError(
            f"twiki page failed with status {response.status_code} for {response.url}",
            status_code=response.status_code,
            instructions=_auth_instructions(),
        )
    if raw:
        if _looks_like_auth_html(response.content) or _OIDC_MARKER in response.url:
            raise UpstreamError(
                "twiki returned HTML instead of raw wiki text",
                instructions=_auth_instructions(),
            )
        text = response.content.decode("utf-8", errors="replace")
        meta, body = _parse_meta(text)
        info = next((fields for kind, fields in meta if kind == "TOPICINFO"), {})
        parent = next((fields for kind, fields in meta if kind == "TOPICPARENT"), {})
        attachments = [fields for kind, fields in meta if kind == "FILEATTACHMENT"]
        return {
            "source": SOURCE,
            "web": web,
            "topic": topic,
            "author": info.get("author"),
            "version": info.get("version"),
            "date_epoch": int(info["date"]) if info.get("date", "").isdigit() else None,
            "parent_topic": parent.get("name"),
            "raw": body,
            "attachments": [
                {
                    "name": att.get("name") or att.get("attachment"),
                    "size_bytes": int(att["size"]) if att.get("size", "").isdigit() else None,
                    "comment": att.get("comment"),
                    "url": f"{PUB_BASE}/{web}/{topic}/{att.get('name') or att.get('attachment')}",
                }
                for att in attachments
            ],
            "url": url,
        }
    return {
        "source": SOURCE,
        "web": web,
        "topic": topic,
        "text": html_to_text(response.content.decode("utf-8", errors="replace")),
        "url": url,
    }


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
