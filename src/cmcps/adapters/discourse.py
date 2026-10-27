"""Read-only adapter for CERN-hosted Discourse forums.

Covers cms-talk.web.cern.ch, cms-pub-talk.web.cern.ch, root-forum.cern.ch,
and geant4-forum.web.cern.ch. All four run stock Discourse with the public
JSON API (``.json`` suffixed routes):

- ``GET /search.json?q=...&page=N`` — posts+topics search (lazy format).
- ``GET /latest.json?page=N`` / ``GET /top.json?period=...&page=N``.
- ``GET /c/{category_id}.json?page=N`` — topics of one category.
- ``GET /categories.json`` — category list with counts.
- ``GET /t/{topic_id}.json?page=N`` — topic detail, ``post_stream.posts``
  chunks of ~20 posts per page.
- ``GET /posts/{post_id}.json`` — single post (includes ``raw`` when public).
- ``GET /u/{username}.json`` — user profile summary.
- ``GET /uploads/...`` — attachments/images (bounded download).

Endpoint type: documented public Discourse JSON API. root-forum and
geant4-forum are anonymously readable; every page of the two CMS forums
redirects to ``/auth/oauth2_basic`` (anonymous 403), so their session
cookies (``_forum_session``/``_t``) are captured by the SSO sign-in flow
(``cmcps.auth.browser._COOKIE_ONLY_SITES``) and injected by the shared
cookie client. No native search-relevance scores or global offset cursors
exist; paging is 1-based ``page`` with ``more_full_page_results`` /
``more_topics_url`` flags.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cmcps.adapters.common import html_to_text, json_body
from cmcps.contracts.errors import AuthRequiredError, InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient, SafeResponse

SOURCE_PREFIX = "discourse"

SITES: dict[str, str] = {
    "cms-talk": "https://cms-talk.web.cern.ch",
    "cms-pub-talk": "https://cms-pub-talk.web.cern.ch",
    "root-forum": "https://root-forum.cern.ch",
    "geant4-forum": "https://geant4-forum.web.cern.ch",
}

# The two CMS forums are fully login-required (anonymous 403 everywhere);
# session cookies are captured into the SSO storage state by
# `cmcps.auth.browser._COOKIE_ONLY_SITES`.
_SSO_SITES = frozenset({"cms-talk", "cms-pub-talk"})


def _guard_session(response: SafeResponse, *, site: str) -> None:
    """Translate an SSO-forum 401/403 into an actionable auth error."""
    if site in _SSO_SITES and response.status_code in (401, 403):
        raise AuthRequiredError(
            f"{site} rejected the request with status {response.status_code}: "
            "the stored SSO state has no (expired) Discourse session cookies"
        )


_LISTINGS = ("latest", "top")
_TOP_PERIODS = ("all", "yearly", "quarterly", "monthly", "weekly", "daily")
_UPLOAD_RE = re.compile(r'(?:href|src)="([^"]*/uploads/[^"]+)"')


def _site_box(site: str) -> str:
    base = SITES.get(site)
    if base is None:
        raise InvalidInputError(
            f"unknown Discourse site {site!r}",
            instructions=f"Pick one of: {', '.join(SITES)}.",
        )
    return base


def _topic_url(base: str, topic: dict[str, Any]) -> str | None:
    topic_id = topic.get("id")
    slug = topic.get("slug")
    if topic_id is None or not slug:
        return None
    return f"{base}/t/{slug}/{topic_id}"


def _topic_item(base: str, topic: dict[str, Any]) -> dict[str, Any]:
    return {
        "topic_id": topic.get("id"),
        "title": topic.get("title"),
        "slug": topic.get("slug"),
        "category_id": topic.get("category_id"),
        "tags": topic.get("tags") or [],
        "posts_count": topic.get("posts_count"),
        "reply_count": topic.get("reply_count"),
        "like_count": topic.get("like_count"),
        "views": topic.get("views"),
        "created_at": topic.get("created_at"),
        "last_posted_at": topic.get("last_posted_at"),
        "closed": topic.get("closed"),
        "has_accepted_answer": topic.get("has_accepted_answer"),
        "url": _topic_url(base, topic),
        "excerpt": topic.get("excerpt"),
    }


def _search_items(base: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    topics = payload.get("topics") or []
    titles: dict[int, dict[str, Any]] = {
        t["id"]: t for t in topics if isinstance(t, dict) and isinstance(t.get("id"), int)
    }
    items = []
    for post in payload.get("posts") or []:
        if not isinstance(post, dict):
            continue
        topic = titles.get(post.get("topic_id") or -1, {})
        topic_id = post.get("topic_id")
        items.append(
            {
                "post_id": post.get("id"),
                "topic_id": topic_id,
                "topic_title": topic.get("title"),
                "post_number": post.get("post_number"),
                "username": post.get("username"),
                "created_at": post.get("created_at"),
                "like_count": post.get("like_count"),
                "excerpt": post.get("blurb"),
                "url": (
                    f"{base}/t/{topic.get('slug') or 'topic'}/{topic_id}/{post.get('post_number')}"
                    if topic_id is not None
                    else None
                ),
            }
        )
    return items


async def search(
    client: SafeHttpClient,
    *,
    site: str,
    query: str,
    page: int = 1,
) -> SearchResult:
    """Full-text search of one forum's posts and topics."""
    base = _site_box(site)
    response = await client.request("GET", f"{base}/search.json", params={"q": query, "page": page})
    _guard_session(response, site=site)
    payload = json_body(response, source=f"{site} search")
    items = _search_items(base, payload if isinstance(payload, dict) else {})
    grouped = payload.get("grouped_search_result") or {}
    return SearchResult(
        source=f"{SOURCE_PREFIX}:{site}",
        query=query,
        items=items,
        page=page,
        size=len(items),
        total_count=None,
        has_more=bool(grouped.get("more_full_page_results"))
        or any(bool(grouped.get(key)) for key in ("more_posts", "more_users")),
        note=(
            "No total count is exposed by Discourse lazy search; page "
            "with page=N until has_more is false."
        ),
    )


async def list_topics(
    client: SafeHttpClient,
    *,
    site: str,
    listing: str = "latest",
    period: str = "monthly",
    category_id: int | None = None,
    page: int = 1,
) -> SearchResult:
    """Paged topic listing (latest/top, or one category's topics)."""
    base = _site_box(site)
    if category_id is not None:
        url = f"{base}/c/{category_id}.json"
        params: dict[str, str | int] = {"page": page}
    else:
        if listing not in _LISTINGS:
            raise InvalidInputError(
                f"listing must be one of {_LISTINGS}",
                instructions="Use listing='latest' or listing='top'.",
            )
        if listing == "top" and period not in _TOP_PERIODS:
            raise InvalidInputError(
                f"period must be one of {_TOP_PERIODS}",
                instructions="Only meaningful for listing='top'.",
            )
        url = f"{base}/{listing}.json"
        params = {"page": page}
        if listing == "top":
            params["period"] = period
    response = await client.request("GET", url, params=params)
    _guard_session(response, site=site)
    payload = json_body(response, source=f"{site} topic list")
    topic_list = payload.get("topic_list") or {}
    topics = topic_list.get("topics") or []
    items = [_topic_item(base, t) for t in topics if isinstance(t, dict)]
    return SearchResult(
        source=f"{SOURCE_PREFIX}:{site}",
        query=f"{listing} (category {category_id})" if category_id is not None else listing,
        items=items,
        page=page,
        size=len(items),
        total_count=None,
        has_more=bool(topic_list.get("more_topics_url")),
    )


async def list_categories(
    client: SafeHttpClient,
    *,
    site: str,
) -> dict[str, Any]:
    """Category tree of one forum, with topic/post counts."""
    base = _site_box(site)
    response = await client.request("GET", f"{base}/categories.json")
    _guard_session(response, site=site)
    payload = json_body(response, source=f"{site} categories")
    categories = (payload.get("category_list") or {}).get("categories") or []
    items = [
        {
            "category_id": c.get("id"),
            "name": c.get("name"),
            "slug": c.get("slug"),
            "description": c.get("description_excerpt") or c.get("description_text"),
            "topic_count": c.get("topic_count"),
            "post_count": c.get("post_count"),
            "subcategory_ids": c.get("subcategory_ids") or [],
            "read_restricted": c.get("read_restricted"),
            "url": f"{base}/c/{c.get('slug')}/{c.get('id')}" if c.get("id") is not None else None,
        }
        for c in categories
        if isinstance(c, dict)
    ]
    return {
        "source": f"{SOURCE_PREFIX}:{site}",
        "site": site,
        "category_count": len(items),
        "items": items,
    }


def _post_item(base: str, post: dict[str, Any], *, include_text: bool) -> dict[str, Any]:
    cooked = post.get("cooked") or ""
    uploads = sorted({match for match in _UPLOAD_RE.findall(cooked)})
    item: dict[str, Any] = {
        "post_id": post.get("id"),
        "post_number": post.get("post_number"),
        "username": post.get("username"),
        "name": post.get("name"),
        "created_at": post.get("created_at"),
        "updated_at": post.get("updated_at"),
        "like_count": post.get("like_count"),
        "reply_to_post_number": post.get("reply_to_post_number"),
        "accepted_answer": bool(post.get("accepted_answer")),
        "uploads": [f"{base}{u}" if u.startswith("/") else u for u in uploads],
        "url": f"{base}/p/{post.get('id')}" if post.get("id") is not None else None,
    }
    if include_text:
        item["text"] = html_to_text(cooked)
    return item


async def get_topic(
    client: SafeHttpClient,
    *,
    site: str,
    topic_id: int,
    page: int = 1,
) -> dict[str, Any]:
    """Topic detail plus one page of rendered posts (chunked by ~20)."""
    base = _site_box(site)
    response = await client.request("GET", f"{base}/t/{topic_id}.json", params={"page": page})
    _guard_session(response, site=site)
    payload = json_body(response, source=f"{site} topic {topic_id}")
    if not isinstance(payload, dict) or "post_stream" not in payload:
        raise UpstreamError(
            f"{site} returned no topic data for topic_id={topic_id}",
            instructions="Check the topic id, e.g. from discourse_search results.",
        )
    stream = payload.get("post_stream") or {}
    posts = stream.get("posts") or []
    items = [_post_item(base, p, include_text=True) for p in posts if isinstance(p, dict)]
    stream_ids = stream.get("stream") or []
    return {
        "source": f"{SOURCE_PREFIX}:{site}",
        "topic_id": payload.get("id"),
        "title": payload.get("title"),
        "category_id": payload.get("category_id"),
        "tags": payload.get("tags") or [],
        "created_at": payload.get("created_at"),
        "last_posted_at": payload.get("last_posted_at"),
        "posts_count": payload.get("posts_count"),
        "reply_count": payload.get("reply_count"),
        "like_count": payload.get("like_count"),
        "views": payload.get("views"),
        "closed": payload.get("closed"),
        "has_accepted_answer": payload.get("has_accepted_answer"),
        "participant_count": payload.get("participant_count"),
        "url": _topic_url(base, payload),
        "page": page,
        "posts_in_page": len(items),
        "total_posts": len(stream_ids) or len(items),
        "has_more": len(items) > 0
        and items[-1]["post_number"] < (payload.get("highest_post_number") or 0),
        "posts": items,
    }


async def get_post(
    client: SafeHttpClient,
    *,
    site: str,
    post_id: int,
) -> dict[str, Any]:
    """Single post detail; includes the raw markdown source when public."""
    base = _site_box(site)
    response = await client.request("GET", f"{base}/posts/{post_id}.json")
    _guard_session(response, site=site)
    payload = json_body(response, source=f"{site} post {post_id}")
    if not isinstance(payload, dict) or payload.get("id") is None:
        raise UpstreamError(
            f"{site} returned no post data for post_id={post_id}",
            instructions="Check the post id from discourse_search or discourse_get_topic.",
        )
    item = _post_item(base, payload, include_text=True)
    item["topic_id"] = payload.get("topic_id")
    item["topic_slug"] = payload.get("topic_slug")
    if isinstance(payload.get("raw"), str):
        item["raw"] = payload["raw"]
    item["source"] = f"{SOURCE_PREFIX}:{site}"
    return item


async def get_user(
    client: SafeHttpClient,
    *,
    site: str,
    username: str,
) -> dict[str, Any]:
    """Public user profile summary (no private fields)."""
    base = _site_box(site)
    response = await client.request("GET", f"{base}/u/{username}.json")
    _guard_session(response, site=site)
    payload = json_body(response, source=f"{site} user {username}")
    user = payload.get("user") if isinstance(payload, dict) else None
    if not isinstance(user, dict):
        raise UpstreamError(
            f"{site} returned no user data for username={username!r}",
            instructions="Check the username spelling (Discourse usernames are exact).",
        )
    return {
        "source": f"{SOURCE_PREFIX}:{site}",
        "username": user.get("username"),
        "name": user.get("name"),
        "title": user.get("title"),
        "bio": html_to_text(user.get("bio_cooked") or "") or None,
        "created_at": user.get("created_at"),
        "last_posted_at": user.get("last_posted_at"),
        "last_seen_at": user.get("last_seen_at"),
        "post_count": user.get("post_count"),
        "topic_count": user.get("topic_count"),
        "likes_given": user.get("likes_given"),
        "likes_received": user.get("likes_received"),
        "trust_level": user.get("trust_level"),
        "moderator": user.get("moderator"),
        "admin": user.get("admin"),
        "url": f"{base}/u/{user.get('username')}" if user.get("username") else None,
    }


async def download_file(
    client: SafeHttpClient,
    *,
    site: str,
    download_url: str,
) -> dict[str, Any]:
    """Bounded download of one attachment/image hosted on the forum."""
    base = _site_box(site)
    base_host = urlsplit(base).hostname
    host = urlsplit(download_url).hostname
    if not host or host != base_host:
        raise InvalidInputError(
            f"discourse_download_file only accepts URLs on {base_host}",
            instructions="Pass a URL from a post's uploads list (discourse_get_topic).",
        )
    filename = urlsplit(download_url).path.rsplit("/", 1)[-1] or None
    final = await client.download(download_url, Path(filename) if filename else None)
    return {
        "source": f"{SOURCE_PREFIX}:{site}",
        "download_url": download_url,
        "path": str(final),
        "size_bytes": final.stat().st_size,
    }
