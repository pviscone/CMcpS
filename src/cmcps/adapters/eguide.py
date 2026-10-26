"""Read-only adapter for the CERN Admin e-guide (admin-eguide.web.cern.ch).

Drupal 9 site with CERN SSO (Keycloak). Anonymous visitors see only the
public procedures listed on the home page; the search form
(``/{lang}/search/node?keys=...``), the full A-Z index
(``/{lang}/index-procedures``) and all internal procedures redirect to
Keycloak without a session. Content exists in English (``/en``) and French
(``/fr``); search returns both languages regardless of the prefix.

There is no public JSON API or sitemap (``/sitemap.xml`` is 404), so this
adapter parses the rendered HTML:

- ``search``: Drupal core node search result list.
- ``list_procedures``: A-Z index view (letter headings + rows with an
  optional ``(FAQ)`` companion link).
- ``get_procedure``: procedure node page (Drupal ``views-field-*`` fields
  for general/procedure text, dates, access, revisions table, contacts and
  the sidebar link blocks).
- ``download_file``: bounded download of files hosted on the site itself.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cmcps.adapters.common import html_to_text
from cmcps.contracts.errors import (
    AuthRequiredError,
    InvalidInputError,
    UpstreamError,
)
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient

SOURCE = "admin-eguide"
BASE = "https://admin-eguide.web.cern.ch"

_LANGS = ("en", "fr")
_MIN_ERROR_STATUS = 400
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_LOGIN_TITLE_RE = re.compile(r"<title>\s*Sign in to CERN", re.IGNORECASE)
_TITLE_RE = re.compile(r"<h1[^>]*class=\"page-header\"[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_TAG_TITLE_RE = re.compile(r"<title>(.*?)\s*\|\s*Admin e-guide", re.IGNORECASE | re.DOTALL)

_SEARCH_BLOCK_RE = re.compile(
    r"<h2>\s*Search results\s*</h2><ol>(.*?)</ol>", re.IGNORECASE | re.DOTALL
)
_SEARCH_ITEM_RE = re.compile(
    r'<h3(?:\s+lang="(?P<lang>\w+)")?\s*>\s*'
    r'<a href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>\s*</h3>\s*'
    r"<p[^>]*>(?P<snippet>.*?)</p>",
    re.IGNORECASE | re.DOTALL,
)
_SEARCH_PAGER_RE = re.compile(r"(?:[?&]|&amp;)page=\d+")

_INDEX_TOKEN_RE = re.compile(
    r"(?:<h3>\s*(?P<letter>[A-Za-z0-9 *])\s*</h3>)"
    r"|(?:<a href=\"/(?P<lang>en|fr)/procedure/(?P<slug>[a-z0-9-]+)\"[^>]*>"
    r"\s*(?P<title>[^<]+?)\s*</a>"
    r"(?:\s*<span class=\"field-content\"><a href=\"(?P<faq>[^\"]+)\"[^>]*>"
    r"\(FAQ\)</a></span>)?)",
    re.IGNORECASE | re.DOTALL,
)
_VIEW_CONTENT_RE = re.compile(r'<div class="view-content clearfix">(.*)', re.IGNORECASE | re.DOTALL)

_FIELD_RE = {
    "access": re.compile(
        r'views-field-field-access">\s*<span class="views-label[^"]*">[^<]*</span>\s*'
        r'<span class="field-content">(.*?)</span>',
        re.IGNORECASE | re.DOTALL,
    ),
    "created": re.compile(
        r'views-field-field-date-creation">\s*<span class="views-label[^"]*">[^<]*</span>\s*'
        r'<span class="field-content">(.*?)</span>',
        re.IGNORECASE | re.DOTALL,
    ),
    "updated": re.compile(
        r'views-field-changed">\s*<span class="views-label[^"]*">[^<]*</span>\s*'
        r'<span class="field-content">(.*?)</span>',
        re.IGNORECASE | re.DOTALL,
    ),
    "approval_process": re.compile(
        r'views-field-field-processus-approbation">\s*'
        r'<span class="views-label[^"]*">[^<]*</span>\s*'
        r'<span class="field-content">(.*?)</span>',
        re.IGNORECASE | re.DOTALL,
    ),
}
_GENERAL_TEXT_RE = re.compile(
    r'views-label-body[^"]*">[^<]*</h2>\s*<div class="field-content">(.*?)'
    r'</div></div><div class="views-field',
    re.IGNORECASE | re.DOTALL,
)
_PROCEDURE_TEXT_RE = re.compile(
    r'views-label-field-procedure[^"]*">[^<]*</h2>\s*<div class="field-content">(.*?)'
    r'</div></div><div class="views-field',
    re.IGNORECASE | re.DOTALL,
)
_REVISION_TABLE_RE = re.compile(
    r'<div class="field-content revision-table">(.*?)</table>', re.IGNORECASE | re.DOTALL
)
_REVISION_ROW_RE = re.compile(
    r"<tr>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*</tr>",
    re.IGNORECASE | re.DOTALL,
)
_CONTACTS_START_RE = re.compile(r"views-field-field-ref-contacts", re.IGNORECASE)
_CONTACT_ARTICLE_RE = re.compile(
    r'<article[^>]*about="(?P<path>/(?:en|fr)/contact/[^"]+)"[^>]*>.*?'
    r"<a href=\"[^\"]*\"[^>]*>\s*<span>(?P<name>.*?)</span>",
    re.IGNORECASE | re.DOTALL,
)
_SIDEBAR_H1_RE = re.compile(
    r'<h1 class="mb-3 mt-3 text-center">(.*?)</h1>', re.IGNORECASE | re.DOTALL
)
_ANCHOR_RE = re.compile(r'<a href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)


def _check_lang(lang: str) -> str:
    if lang not in _LANGS:
        raise InvalidInputError(
            f"Unsupported language {lang!r}", instructions=f"Use one of {_LANGS}."
        )
    return lang


def _check_slug(slug: str) -> str:
    slug = slug.strip().strip("/")
    for prefix in ("en/procedure/", "fr/procedure/", "procedure/"):
        if slug.startswith(prefix):
            slug = slug[len(prefix) :]
    if not _SLUG_RE.fullmatch(slug):
        raise InvalidInputError(
            f"Invalid procedure slug {slug!r}",
            instructions="Pass the slug from eguide_search or eguide_list_procedures.",
        )
    return slug


async def _get_html(client: SafeHttpClient, url: str, **kwargs: Any) -> str:
    response = await client.request("GET", url, **kwargs)
    html = response.content.decode("utf-8", errors="replace")
    if "auth.cern.ch" in response.url or _LOGIN_TITLE_RE.search(html):
        raise AuthRequiredError()
    if response.status_code >= _MIN_ERROR_STATUS:
        raise UpstreamError(
            f"{SOURCE} returned {response.status_code} for {url}",
            status_code=response.status_code,
        )
    return html


def _field(html: str, name: str) -> str | None:
    match = _FIELD_RE[name].search(html)
    if not match:
        return None
    value = html_to_text(match.group(1)).strip()
    return value or None


def _path_parts(url: str) -> tuple[str | None, str, str] | None:
    """Split an e-guide URL into (lang, kind, slug); None for off-site links.

    Drupal serves French aliases without a language prefix
    (``/procedure/visas-...``), so the prefix is optional. ``lang`` is None
    for those; callers keep the search hit's own ``lang`` attribute then.
    """
    path = urlsplit(url).path
    match = re.match(
        r"^/(?:(?P<lang>en|fr)/)?(?P<kind>procedure|faq|content|contact)(?:/(?P<slug>.+))?$",
        path,
    )
    if not match:
        return None
    return (match.group("lang"), match.group("kind"), match.group("slug") or "")


async def search(
    client: SafeHttpClient,
    *,
    query: str,
    lang: str = "en",
    page: int = 0,
) -> SearchResult:
    """Drupal core node search over procedures, FAQs and content pages."""
    _check_lang(lang)
    query = query.strip()
    if not query:
        raise InvalidInputError("query must not be empty", instructions="Pass non-empty text.")
    html = await _get_html(
        client, f"{BASE}/{lang}/search/node", params={"keys": query, "page": page}
    )
    block = _SEARCH_BLOCK_RE.search(html)
    items: list[dict[str, Any]] = []
    if block:
        after = html[block.end() :]
        has_more = bool(_SEARCH_PAGER_RE.search(after))
        for match in _SEARCH_ITEM_RE.finditer(block.group(1)):
            url = match.group("url")
            parts = _path_parts(url)
            items.append(
                {
                    "title": html_to_text(match.group("title")).strip(),
                    "url": url,
                    "lang": match.group("lang") or "en",
                    "kind": parts[1] if parts else None,
                    "slug": parts[2] if parts else None,
                    "snippet": html_to_text(match.group("snippet")).strip() or None,
                }
            )
    else:
        has_more = False
    return SearchResult(
        source=SOURCE,
        items=items,
        total_count=None,
        page=page,
        size=len(items),
        has_more=has_more,
        query=query,
        note=(
            "Drupal core search; results include both English and French pages "
            "regardless of the URL language prefix."
        ),
    )


async def list_procedures(
    client: SafeHttpClient,
    *,
    lang: str = "en",
    title_contains: str | None = None,
) -> SearchResult:
    """A-Z index of administrative procedures (internal entries need SSO)."""
    _check_lang(lang)
    html = await _get_html(client, f"{BASE}/{lang}/index-procedures")
    view = _VIEW_CONTENT_RE.search(html)
    segment = view.group(1) if view else html
    items: list[dict[str, Any]] = []
    letter: str | None = None
    for token in _INDEX_TOKEN_RE.finditer(segment):
        if token.group("letter"):
            letter = token.group("letter")
            continue
        if token.group("lang") != lang:
            continue
        items.append(
            {
                "letter": letter,
                "title": token.group("title").strip(),
                "slug": token.group("slug"),
                "path": f"/{lang}/procedure/" + token.group("slug"),
                "faq_path": token.group("faq"),
            }
        )
    if title_contains:
        needle = title_contains.casefold()
        items = [item for item in items if needle in item["title"].casefold()]
    return SearchResult(
        source=SOURCE,
        items=items,
        total_count=None,
        page=1,
        size=len(items),
        has_more=False,
        note=(
            "Full A-Z index visible to authenticated users; anonymous visitors "
            "are redirected to the CERN SSO login."
        ),
    )


def _sidebar_links(html: str) -> list[dict[str, Any]]:
    """Sidebar blocks headed by an h1 (Reference documents, Other procedures, ...)."""
    markers = list(_SIDEBAR_H1_RE.finditer(html))
    links: list[dict[str, Any]] = []
    for i, marker in enumerate(markers):
        section = html_to_text(marker.group(1)).strip()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(html)
        for anchor in _ANCHOR_RE.finditer(html[marker.end() : end]):
            title = html_to_text(anchor.group(2)).strip()
            if not title:
                continue
            links.append({"section": section, "title": title, "url": anchor.group(1)})
    return links


def _contacts(html: str) -> list[dict[str, str]]:
    """Contact teasers (name + /contact/<slug> path) inside the ref-contacts field."""
    start = _CONTACTS_START_RE.search(html)
    if not start:
        return []
    tail = html[start.end() :]
    ends = [
        pos
        for marker in ('<div class="views-field', "mb-3 mt-3 text-center", "</section>")
        if (pos := tail.find(marker)) != -1
    ]
    block = tail[: min(ends)] if ends else tail
    contacts = []
    for article in _CONTACT_ARTICLE_RE.finditer(block):
        name = html_to_text(article.group("name")).strip()
        if name:
            contacts.append({"name": name, "path": article.group("path")})
    return contacts


async def get_procedure(
    client: SafeHttpClient,
    *,
    slug: str,
    lang: str = "en",
) -> dict[str, Any]:
    """One procedure page: text sections, dates, access, revisions, links."""
    _check_lang(lang)
    slug = _check_slug(slug)
    url = f"{BASE}/{lang}/procedure/{slug}"
    html = await _get_html(client, url)
    title_match = _TITLE_RE.search(html) or _TAG_TITLE_RE.search(html)
    title = html_to_text(title_match.group(1)).strip() if title_match else None

    general = None
    general_match = _GENERAL_TEXT_RE.search(html)
    if general_match:
        general = html_to_text(general_match.group(1)) or None
    procedure_text = None
    procedure_match = _PROCEDURE_TEXT_RE.search(html)
    if procedure_match:
        procedure_text = html_to_text(procedure_match.group(1)) or None

    revisions: list[dict[str, str]] = []
    table_match = _REVISION_TABLE_RE.search(html)
    if table_match:
        for no, changes, approved in _REVISION_ROW_RE.findall(table_match.group(1)):
            revisions.append(
                {
                    "no": html_to_text(no).strip(),
                    "changes": html_to_text(changes).strip(),
                    "approved": html_to_text(approved).strip(),
                }
            )

    contacts = _contacts(html) or None

    if title is None and general is None and procedure_text is None:
        raise UpstreamError(f"{SOURCE} did not render a procedure page for slug {slug!r}")

    return {
        "source": SOURCE,
        "url": url,
        "slug": slug,
        "lang": lang,
        "title": title,
        "access": _field(html, "access"),
        "created": _field(html, "created"),
        "updated": _field(html, "updated"),
        "approval_process": _field(html, "approval_process"),
        "general": general,
        "procedure": procedure_text,
        "revisions": revisions,
        "contacts": contacts,
        "related_links": _sidebar_links(html),
    }


async def download_file(
    client: SafeHttpClient,
    *,
    download_url: str,
) -> dict[str, Any]:
    """Bounded download of one file hosted on admin-eguide.web.cern.ch."""
    base_host = urlsplit(BASE).hostname
    host = urlsplit(download_url).hostname
    if not host or host != base_host:
        raise InvalidInputError(
            f"eguide_download_file only accepts URLs on {base_host}",
            instructions="Pass a URL returned by eguide_get_procedure related_links.",
        )
    filename = urlsplit(download_url).path.rsplit("/", 1)[-1] or None
    final = await client.download(download_url, Path(filename) if filename else None)
    return {
        "source": SOURCE,
        "download_url": download_url,
        "path": str(final),
        "size_bytes": final.stat().st_size,
    }
