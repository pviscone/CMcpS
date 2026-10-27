"""Read-only adapter for CERN Mattermost (mattermost.web.cern.ch).

CERN runs a stock Mattermost fork (observed ``x-version-id:
11.7.12...cern``), exposing the documented API v4 under ``/api/v4``:

- ``GET /api/v4/users/me`` — current user (also the auth probe; anonymous
  answers ``401 {"id":"api.context.session_expired.app_error"}``, verified).
- ``GET /api/v4/users/{user_id}`` / ``GET /api/v4/users/username/{name}``.
- ``GET /api/v4/users/me/teams`` — teams of the current user (array
  payload; the CERN fork ignores ``page``/``per_page`` and returns the full
  list, verified).
- ``GET /api/v4/users/me/teams/{team_id}/channels`` — channel list; the
  CERN fork ignores paging and returns the account-wide list including
  direct/group conversations, so results are filtered to the requested
  team client-side (verified).
- ``GET /api/v4/teams/{team_id}`` — team detail (name, display name); also
  resolves the team name for channel/permalink URLs.
- ``GET /api/v4/channels/{channel_id}`` and
  ``GET /api/v4/teams/{team_id}/channels/name/{name}`` — channel detail.
- ``GET /api/v4/channels/{channel_id}/posts?page=&per_page=&before=&after=``
  — channel history, ``{order, posts, prev_post_id, next_post_id}``.
- ``GET /api/v4/posts/{post_id}/thread`` — a post with its replies.
- ``POST /api/v4/teams/{team_id}/posts/search`` — full-text post search
  (read-only semantics; the only POST, allowlisted by the shared client, and
  sent with ``X-Requested-With: XMLHttpRequest`` — the CERN fork instantly
  401s cookie-authenticated POSTs without it, verified 2026-10). Verified
  quirks: results clamp to 100 hits, ``per_page`` is ignored, ``page``
  advances through the capped window.
- ``GET /api/v4/files/{file_id}/info`` and ``GET /api/v4/files/{file_id}``
  — file metadata and bounded download.

Endpoint type: documented public Mattermost REST API v4. Authentication is
the ``MMAUTHTOKEN`` session cookie minted by the OAuth handshake at
``/oauth/gitlab/login`` (mm-auth.web.cern.ch brokers to auth.cern.ch, so the
stored KEYCLOAK_IDENTITY completes it silently — captured/warmed through
``cmcps.auth.browser._COOKIE_ONLY_SITES``); ``CMCPS_MATTERMOST_TOKEN`` is an
optional personal-access-token fallback sent as ``Authorization: Bearer``.
Tool-facing pages are 1-based (the backend is 0-based). Timestamps
are converted from epoch milliseconds to ISO-8601.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from cmcps.adapters.common import json_body
from cmcps.config.settings import Settings
from cmcps.contracts.errors import AuthRequiredError, InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient, SafeResponse

SOURCE = "mattermost"

# Mattermost ids are 26 lowercase-alphanumeric characters (base32-ish).
_ID_RE = re.compile(r"[a-z0-9]{26}")
_DEFAULT_PER_PAGE = 60
_UNAUTHORIZED = 401
# The CERN fork rejects cookie-authenticated POSTs without the
# X-Requested-With header (verified: plain POST → instant 401
# api.context.session_expired; with the header → 200). Harmless under
# Bearer/PAT auth, so it is sent unconditionally on the search POST.
_SEARCH_HEADERS = {"X-Requested-With": "XMLHttpRequest"}
# Verified on the CERN fork (11.7.x): post search clamps results to 100 hits
# total, ignores per_page, and `page` advances through the capped window
# (page=1 of a ≤100-hit set returns empty). has_more therefore means "this
# page hit the observed cap".
_SEARCH_RESULT_CAP = 100


def _api(settings: Settings, path: str) -> str:
    return f"{settings.mattermost_base_url.rstrip('/')}/api/v4{path}"


def _base(settings: Settings) -> str:
    return settings.mattermost_base_url.rstrip("/")


def _search_allowlist(settings: Settings) -> tuple[str, ...]:
    host = urlsplit(_base(settings)).hostname
    if not host:
        raise InvalidInputError(
            "CMCPS_MATTERMOST_BASE_URL has no usable host",
            instructions="Set it to e.g. https://mattermost.web.cern.ch.",
        )
    return (host,)


def _service_headers(settings: Settings) -> dict[str, str] | None:
    if settings.mattermost_token:
        return {"Authorization": f"Bearer {settings.mattermost_token}"}
    return None


def _guard_session(response: SafeResponse) -> None:
    """Translate an expired/absent session (401) into an actionable auth error.

    403 stays an upstream permission error: Mattermost uses it for channel or
    team access denial while logged in (e.g. ``api.team.get_member...``).
    """
    if response.status_code == _UNAUTHORIZED:
        raise AuthRequiredError(
            "Mattermost rejected the request with status 401: the stored session "
            "has no valid MMAUTHTOKEN cookie"
        )


def _check_id(value: str, kind: str) -> str:
    if not _ID_RE.fullmatch(value):
        raise InvalidInputError(
            f"invalid Mattermost {kind}_id {value!r}",
            instructions=(
                "Mattermost ids are 26 lowercase alphanumeric characters; copy "
                "them from a previous mattermost_* tool result."
            ),
        )
    return value


def _iso(epoch_ms: Any) -> str | None:
    """Convert a Mattermost epoch-milliseconds timestamp to ISO-8601 UTC."""
    if not isinstance(epoch_ms, int | float) or epoch_ms <= 0:
        return None
    return (
        datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _user_item(user: dict[str, Any]) -> dict[str, Any]:
    """Public user fields; email/names depend on server privacy config."""
    return {
        "user_id": user.get("id"),
        "username": user.get("username"),
        "nickname": user.get("nickname") or None,
        "first_name": user.get("first_name") or None,
        "last_name": user.get("last_name") or None,
        "email": user.get("email") or None,
        "position": user.get("position") or None,
        "roles": user.get("roles"),
        "created_at": _iso(user.get("create_at")),
    }


def _team_item(base: str, team: dict[str, Any]) -> dict[str, Any]:
    return {
        "team_id": team.get("id"),
        "name": team.get("name"),
        "display_name": team.get("display_name"),
        "description": team.get("description") or None,
        "type": team.get("type"),
        "deleted": bool(team.get("delete_at")),
        "created_at": _iso(team.get("create_at")),
        "url": f"{base}/{quote(str(team['name']), safe='')}" if team.get("name") else None,
    }


def _channel_item(base: str, channel: dict[str, Any], *, team_name: str | None) -> dict[str, Any]:
    # The team URL applies only to the channels that belong to a team; direct
    # and group messages (empty team_id) must not inherit it.
    url = None
    if channel.get("team_id") and team_name and channel.get("name"):
        url = f"{base}/{quote(team_name, safe='')}/channels/{quote(str(channel['name']), safe='')}"
    return {
        "channel_id": channel.get("id"),
        "team_id": channel.get("team_id"),
        "name": channel.get("name"),
        "display_name": channel.get("display_name"),
        "type": channel.get("type"),  # O public, P private, D direct, G group
        "purpose": channel.get("purpose") or None,
        "header": channel.get("header") or None,
        "created_at": _iso(channel.get("create_at")),
        "last_post_at": _iso(channel.get("last_post_at")),
        "total_msg_count": channel.get("total_msg_count"),
        "url": url,
    }


def _post_item(base: str, post: dict[str, Any], *, team_name: str | None) -> dict[str, Any]:
    url = None
    if team_name and post.get("id"):
        url = f"{base}/{quote(team_name, safe='')}/pl/{post['id']}"
    return {
        "post_id": post.get("id"),
        "channel_id": post.get("channel_id"),
        "user_id": post.get("user_id"),
        "message": post.get("message") or "",
        "type": post.get("type") or "",
        "created_at": _iso(post.get("create_at")),
        "edited": bool(post.get("edit_at")),
        "reply_count": post.get("reply_count"),
        "root_id": post.get("root_id") or None,
        "hashtags": post.get("hashtags") or None,
        "file_ids": post.get("file_ids") or [],
        "url": url,
    }


def _ordered_posts(
    base: str, payload: dict[str, Any], *, team_name: str | None
) -> list[dict[str, Any]]:
    """Order a Mattermost ``{order, posts}`` payload by its ``order`` list."""
    posts = payload.get("posts") if isinstance(payload, dict) else None
    if not isinstance(posts, dict):
        return []
    seen: set[str] = set()
    ordered: list[dict[str, Any]] = []
    for pid in payload.get("order") or []:
        post = posts.get(pid)
        if isinstance(post, dict):
            seen.add(pid)
            ordered.append(post)
    for pid, post in posts.items():
        if pid not in seen and isinstance(post, dict):
            ordered.append(post)
    return [_post_item(base, p, team_name=team_name) for p in ordered]


async def _team_name(client: SafeHttpClient, settings: Settings, team_id: str) -> str:
    """Resolve a team id to its URL name; raises when the team is unreachable."""
    response = await client.request(
        "GET",
        _api(settings, f"/teams/{team_id}"),
        service_headers=_service_headers(settings),
    )
    _guard_session(response)
    payload = json_body(response, source="mattermost team lookup")
    if isinstance(payload, dict) and isinstance(payload.get("name"), str):
        return payload["name"]
    raise UpstreamError(
        f"mattermost returned no team name for team_id={team_id!r}",
        instructions="Check the team id (from mattermost_list_teams).",
    )


async def _team_name_or_none(
    client: SafeHttpClient, settings: Settings, team_id: Any
) -> str | None:
    """Best-effort team name for URL building; never fails the caller's read."""
    if not isinstance(team_id, str) or not _ID_RE.fullmatch(team_id):
        return None
    try:
        return await _team_name(client, settings, team_id)
    except UpstreamError:
        return None


async def get_me(client: SafeHttpClient, settings: Settings) -> dict[str, Any]:
    """Current Mattermost user; doubles as the session/auth probe."""
    response = await client.request(
        "GET",
        _api(settings, "/users/me"),
        service_headers=_service_headers(settings),
    )
    _guard_session(response)
    payload = json_body(response, source="mattermost current user")
    if not isinstance(payload, dict) or payload.get("id") is None:
        raise UpstreamError("mattermost returned no current user")
    return {"source": SOURCE, **_user_item(payload)}


async def get_user(
    client: SafeHttpClient,
    settings: Settings,
    *,
    username: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """One user profile, resolved by exact username or by 26-char id."""
    if (username is None) == (user_id is None):
        raise InvalidInputError(
            "pass exactly one of username or user_id",
            instructions="Mattermost usernames are exact; user ids come from earlier results.",
        )
    if username is not None:
        path = f"/users/username/{quote(username, safe='')}"
        label = f"username {username!r}"
    else:
        assert user_id is not None  # narrowed by the XOR check above
        path = f"/users/{_check_id(user_id, 'user')}"
        label = f"user_id {user_id!r}"
    response = await client.request(
        "GET",
        _api(settings, path),
        service_headers=_service_headers(settings),
    )
    _guard_session(response)
    payload = json_body(response, source=f"mattermost user {label}")
    if not isinstance(payload, dict) or payload.get("id") is None:
        raise UpstreamError(
            f"mattermost returned no user data for {label}",
            instructions="Check the spelling; usernames and ids are exact.",
        )
    return {"source": SOURCE, **_user_item(payload)}


async def list_teams(
    client: SafeHttpClient,
    settings: Settings,
) -> SearchResult:
    """Teams the current user is a member of.

    The CERN fork ignores ``page``/``per_page`` and returns the full list in
    one call (verified), so paging is not exposed at all.
    """
    response = await client.request(
        "GET",
        _api(settings, "/users/me/teams"),
        service_headers=_service_headers(settings),
    )
    _guard_session(response)
    payload = json_body(response, source="mattermost team list")
    teams = payload if isinstance(payload, list) else []
    items = [_team_item(_base(settings), t) for t in teams if isinstance(t, dict)]
    return SearchResult(
        source=SOURCE,
        query="teams of the current user",
        items=items,
        page=1,
        size=len(items),
        total_count=None,
        has_more=False,
        note=(
            "Only teams you are a member of are listed (full list in one call; "
            "the CERN fork offers no paging); use the team_id/name here with "
            "the other mattermost_* tools."
        ),
    )


async def list_channels(
    client: SafeHttpClient,
    settings: Settings,
    *,
    team_id: str,
    name_contains: str | None = None,
) -> SearchResult:
    """Channels of one team the current user is in; optional name filter.

    The CERN fork ignores ``page``/``per_page`` and returns the account-wide
    channel list (every direct/group conversation included), so paging is
    not exposed and the result is filtered to the requested team.
    """
    team_id = _check_id(team_id, "team")
    team_name = await _team_name_or_none(client, settings, team_id)
    response = await client.request(
        "GET",
        _api(settings, f"/users/me/teams/{team_id}/channels"),
        service_headers=_service_headers(settings),
    )
    _guard_session(response)
    payload = json_body(response, source=f"mattermost channels of team {team_id}")
    channels = payload if isinstance(payload, list) else []
    # Verified on the CERN fork: this endpoint ignores page/per_page and
    # returns the account-wide channel list, mixing every direct/group
    # conversation in. Keep only the channels that belong to the requested
    # team — semantically correct and avoids echoing the user's DM graph.
    team_channels = [c for c in channels if isinstance(c, dict) and c.get("team_id") == team_id]
    items = [_channel_item(_base(settings), c, team_name=team_name) for c in team_channels]
    if name_contains:
        needle = name_contains.lower()
        items = [
            item
            for item in items
            if needle in str(item.get("name") or "").lower()
            or needle in str(item.get("display_name") or "").lower()
        ]
    return SearchResult(
        source=SOURCE,
        query=f"channels of team {team_id}",
        items=items,
        page=1,
        size=len(items),
        total_count=None,
        has_more=False,
        note=(
            "Direct and group conversations are filtered out client-side: the "
            "CERN endpoint returns the account-wide channel list and ignores "
            "paging, so the full team list is already here. Channel types: "
            "O = public, P = private."
        ),
    )


async def get_channel(
    client: SafeHttpClient,
    settings: Settings,
    *,
    channel_id: str | None = None,
    team_id: str | None = None,
    channel_name: str | None = None,
) -> dict[str, Any]:
    """One channel by id, or by team id + exact URL name."""
    if channel_id is None:
        if team_id is None or channel_name is None:
            raise InvalidInputError(
                "pass channel_id, or team_id + channel_name",
                instructions="Mattermost channel names are exact URL names (from list_channels).",
            )
        path = f"/teams/{_check_id(team_id, 'team')}/channels/name/{quote(channel_name, safe='')}"
    else:
        if team_id is not None or channel_name is not None:
            raise InvalidInputError(
                "pass channel_id alone, or team_id + channel_name",
                instructions="channel_id identifies the channel on its own.",
            )
        path = f"/channels/{_check_id(channel_id, 'channel')}"
    response = await client.request(
        "GET",
        _api(settings, path),
        service_headers=_service_headers(settings),
    )
    _guard_session(response)
    payload = json_body(response, source="mattermost channel")
    if not isinstance(payload, dict) or payload.get("id") is None:
        raise UpstreamError("mattermost returned no channel data")
    team_name = await _team_name_or_none(client, settings, payload.get("team_id"))
    return {
        "source": SOURCE,
        **_channel_item(_base(settings), payload, team_name=team_name),
    }


async def search_posts(
    client: SafeHttpClient,
    settings: Settings,
    *,
    team_id: str,
    terms: str,
    is_or_search: bool = False,
    page: int = 1,
) -> SearchResult:
    """Full-text post search within one team (read-only JSON POST).

    Mattermost has no GET search; ``POST
    /teams/{team_id}/posts/search`` is the documented backend (same precedent
    as the ServiceNow portal search). ``terms`` supports the Mattermost
    filters ``from:``, ``in:``, ``before:``, ``after:``, ``on:``. The CERN
    fork ignores ``per_page`` and clamps results to ~100 hits, so paging
    advances with ``page`` only.
    """
    if not terms.strip():
        raise InvalidInputError(
            "search terms must not be empty",
            instructions="Pass words to match; use from:, in:, before:, after:, on: to filter.",
        )
    team_id = _check_id(team_id, "team")
    team_name = await _team_name(client, settings, team_id)
    response = await client.request_json_post(
        _api(settings, f"/teams/{team_id}/posts/search"),
        json_body={
            "terms": terms,
            "is_or_search": bool(is_or_search),
            "include_deleted_channels": False,
            "time_zone": "UTC",
            "page": page - 1,
        },
        allowlist=_search_allowlist(settings),
        headers=_SEARCH_HEADERS,
        service_headers=_service_headers(settings),
    )
    _guard_session(response)
    payload = json_body(response, source=f"mattermost post search in team {team_id}")
    items = _ordered_posts(
        _base(settings), payload if isinstance(payload, dict) else {}, team_name=team_name
    )
    return SearchResult(
        source=SOURCE,
        query=terms,
        items=items,
        page=page,
        size=len(items),
        total_count=None,
        has_more=len(items) >= _SEARCH_RESULT_CAP,
        note=(
            "Search covers the channels of this team you may read. The backend "
            "exposes no hit count and clamps results to ~100 hits (its own "
            "page size, per_page is ignored); an empty/short page means the "
            "end. Items carry user_id, not usernames — follow with "
            "mattermost_get_user; file_ids follow with "
            "mattermost_download_file."
        ),
    )


async def get_posts(
    client: SafeHttpClient,
    settings: Settings,
    *,
    channel_id: str,
    page: int = 1,
    per_page: int = _DEFAULT_PER_PAGE,
    before: str | None = None,
    after: str | None = None,
) -> dict[str, Any]:
    """One page of channel history, newest first, with cursor navigation."""
    channel_id = _check_id(channel_id, "channel")
    params: dict[str, str | int] = {"page": page - 1, "per_page": per_page}
    if before is not None:
        params["before"] = _check_id(before, "post")
    if after is not None:
        params["after"] = _check_id(after, "post")
    response = await client.request(
        "GET",
        _api(settings, f"/channels/{channel_id}/posts"),
        params=params,
        service_headers=_service_headers(settings),
    )
    _guard_session(response)
    payload = json_body(response, source=f"mattermost posts of channel {channel_id}")
    data = payload if isinstance(payload, dict) else {}
    items = _ordered_posts(_base(settings), data, team_name=None)
    # Verified: ``next_post_id``/``prev_post_id`` are the boundary ids toward
    # newer/older history (next == the ``before`` cursor used to get here).
    # On the newest page next_post_id is empty even when older posts exist, so
    # "more history" ⇔ prev_post_id non-empty.
    prev_post_id = data.get("prev_post_id") or None
    return {
        "source": SOURCE,
        "channel_id": channel_id,
        "page": page,
        "posts_in_page": len(items),
        "has_more": bool(prev_post_id),
        "next_post_id": data.get("next_post_id") or None,
        "prev_post_id": prev_post_id,
        "note": (
            "Posts are newest first; pass before=<post_id of the last post of "
            "this page> for the next older page, after=<post_id of the first "
            "post> for newer history. Root posts carry reply_count — follow "
            "long threads with mattermost_get_thread."
        ),
        "posts": items,
    }


async def get_thread(
    client: SafeHttpClient,
    settings: Settings,
    *,
    post_id: str,
) -> dict[str, Any]:
    """A post and every reply in its thread, oldest first."""
    post_id = _check_id(post_id, "post")
    response = await client.request(
        "GET",
        _api(settings, f"/posts/{post_id}/thread"),
        service_headers=_service_headers(settings),
    )
    _guard_session(response)
    payload = json_body(response, source=f"mattermost thread of post {post_id}")
    items = _ordered_posts(
        _base(settings), payload if isinstance(payload, dict) else {}, team_name=None
    )
    if not items:
        raise UpstreamError(
            f"mattermost returned no thread for post_id={post_id!r}",
            instructions=(
                "Check the post id (from mattermost_search_posts or mattermost_get_posts)."
            ),
        )
    # The server's order list is not time-sorted (verified); present the root
    # post first and the replies in actual creation order.
    root = next((item for item in items if item.get("post_id") == post_id), items[0])
    replies = sorted(
        (item for item in items if item is not root),
        key=lambda item: str(item.get("created_at") or ""),
    )
    items = [root, *replies]
    return {
        "source": SOURCE,
        "post_id": post_id,
        "channel_id": items[0].get("channel_id"),
        "reply_count": max(0, len(items) - 1),
        "note": "posts[0] is the root post; the rest are its replies, oldest first.",
        "posts": items,
    }


def _safe_filename(raw_name: Any, file_id: str) -> str:
    """Reduce server-supplied metadata to a bare, non-empty file name."""
    name = Path(str(raw_name or "")).name.strip()
    return name or f"mattermost-{file_id}"


async def download_file(
    client: SafeHttpClient,
    settings: Settings,
    *,
    file_id: str,
) -> dict[str, Any]:
    """Bounded download of one file attachment into CMcpS download storage."""
    file_id = _check_id(file_id, "file")
    info_response = await client.request(
        "GET",
        _api(settings, f"/files/{file_id}/info"),
        service_headers=_service_headers(settings),
    )
    _guard_session(info_response)
    info = json_body(info_response, source=f"mattermost file {file_id} info")
    if not isinstance(info, dict):
        info = {}
    name = _safe_filename(info.get("name"), file_id)
    final = await client.download(
        _api(settings, f"/files/{file_id}"),
        Path(name),
        service_headers=_service_headers(settings),
    )
    return {
        "source": SOURCE,
        "file_id": file_id,
        "name": name,
        "mime_type": info.get("mime_type"),
        "extension": info.get("extension"),
        "size_bytes": final.stat().st_size,
        "path": str(final),
    }
