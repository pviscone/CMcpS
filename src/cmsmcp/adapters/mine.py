""" "What needs my attention": one compact overview across personal work queues.

Sections (each optional, fetched in parallel, failures reported per section):

- GitLab: merge requests awaiting your review, assigned to you, and opened by
  you; issues assigned to you; pending to-dos.
- Jira: unresolved issues assigned to you / reported by you.
- Mattermost: recent posts mentioning you (direct/group messages only with
  the ``mattermost_dm`` opt-in).

Uses the same credentials as the per-service tools (SSO session, plus the
GitLab/Jira tokens when configured). Read-only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from cmsmcp.adapters import jira, mattermost
from cmsmcp.adapters.common import json_body
from cmsmcp.adapters.search import items_of
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.errors import CMSMCPError, InvalidInputError, UpstreamError
from cmsmcp.net.client import SafeHttpClient
from cmsmcp.policy import Policy

SECTIONS = ("gitlab", "jira", "mattermost")
_OK = 200


def _gitlab_headers(settings: Settings) -> dict[str, str] | None:
    return {"PRIVATE-TOKEN": settings.gitlab_token} if settings.gitlab_token else None


async def _gitlab_get(
    client: SafeHttpClient, settings: Settings, path: str, params: dict[str, Any]
) -> Any:
    response = await client.request(
        "GET",
        f"{settings.gitlab_base_url.rstrip('/')}/api/v4{path}",
        params=params,
        service_headers=_gitlab_headers(settings),
    )
    if response.status_code != _OK:
        raise UpstreamError(
            f"GitLab answered HTTP {response.status_code} for {path}",
            instructions="Sign in (cmsmcp login) or add a GitLab token (`cmsmcp setup gitlab`).",
        )
    return json_body(response, source="gitlab")


def _mr(item: dict[str, Any]) -> dict[str, Any]:
    refs = item.get("references") or {}
    return {
        "title": f"{refs.get('full') or ''} {item.get('title') or ''}".strip(),
        "url": item.get("web_url"),
        "date": item.get("updated_at"),
        "author": (item.get("author") or {}).get("username"),
        "draft": item.get("draft") or None,
        "status": item.get("detailed_merge_status"),
    }


def _issue(item: dict[str, Any]) -> dict[str, Any]:
    refs = item.get("references") or {}
    return {
        "title": f"{refs.get('full') or ''} {item.get('title') or ''}".strip(),
        "url": item.get("web_url"),
        "date": item.get("updated_at"),
        "labels": item.get("labels") or None,
    }


async def gitlab_section(client: SafeHttpClient, settings: Settings, limit: int) -> dict[str, Any]:
    me = await _gitlab_get(client, settings, "/user", {})
    username = me.get("username") if isinstance(me, dict) else None
    if not username:
        raise UpstreamError("GitLab did not return your user")
    base = {"state": "opened", "per_page": limit, "order_by": "updated_at"}
    queries = {
        "review_requested": (
            "/merge_requests",
            {**base, "scope": "all", "reviewer_username": username},
        ),
        "assigned_mrs": ("/merge_requests", {**base, "scope": "assigned_to_me"}),
        "my_open_mrs": ("/merge_requests", {**base, "scope": "created_by_me"}),
        "assigned_issues": ("/issues", {**base, "scope": "assigned_to_me"}),
        "todos": ("/todos", {"state": "pending", "per_page": limit}),
    }
    results = await asyncio.gather(
        *(_gitlab_get(client, settings, path, params) for path, params in queries.values()),
        return_exceptions=True,
    )
    out: dict[str, Any] = {"user": username}
    for name, result in zip(queries, results, strict=True):
        if isinstance(result, BaseException):
            out[name] = f"unavailable ({_why(result)})"
        elif name == "todos":
            out[name] = [
                {
                    "title": f"{t.get('action_name')}: {(t.get('target') or {}).get('title')}",
                    "url": t.get("target_url"),
                    "date": t.get("created_at"),
                }
                for t in result
                if isinstance(t, dict)
            ]
        else:
            convert = _issue if name == "assigned_issues" else _mr
            out[name] = [convert(i) for i in result if isinstance(i, dict)]
    return out


async def jira_section(client: SafeHttpClient, limit: int) -> dict[str, Any]:
    queries = {
        "assigned": "assignee = currentUser() AND resolution = Unresolved ORDER BY updated DESC",
        "reported": "reporter = currentUser() AND resolution = Unresolved ORDER BY updated DESC",
    }
    results = await asyncio.gather(
        *(jira.search_issues(client, jql=jql, max_results=limit) for jql in queries.values()),
        return_exceptions=True,
    )
    out: dict[str, Any] = {}
    for name, result in zip(queries, results, strict=True):
        if isinstance(result, BaseException):
            out[name] = f"unavailable ({_why(result)})"
            continue
        if result.note:
            out["note"] = "anonymous Jira session: sign in or add a Jira token"
        out[name] = [
            {
                "title": f"{i.get('key')} {i.get('summary')}",
                "url": i.get("url"),
                "date": i.get("updated_at"),
                "status": i.get("status"),
            }
            for i in result.items
        ]
    return out


async def mattermost_section(
    client: SafeHttpClient, settings: Settings, policy: Policy, limit: int, days: int
) -> dict[str, Any]:
    me = await mattermost.get_me(client, settings)
    username = me.get("username")
    if not username:
        raise UpstreamError("Mattermost did not return your user")
    since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    teams = await mattermost.list_teams(client, settings)
    include_dms = policy.personal_allowed("mattermost_dm")
    searches = await asyncio.gather(
        *(
            mattermost.search_posts(
                client,
                settings,
                team_id=team["team_id"],
                terms=f'"@{username}" after:{since}',
                include_direct_messages=include_dms,
            )
            for team in items_of(teams)
            if team.get("team_id")
        ),
        return_exceptions=True,
    )
    posts: dict[str, dict[str, Any]] = {}
    for result in searches:
        if isinstance(result, BaseException):
            continue
        for post in items_of(result):
            posts.setdefault(str(post.get("post_id")), dict(post))
    newest = sorted(posts.values(), key=lambda p: str(p.get("created_at") or ""), reverse=True)
    return {
        "user": username,
        "since": since,
        "dms_included": include_dms,
        "mentions": [
            {
                "title": str(p.get("message") or "")[:200],
                "url": p.get("url"),
                "date": p.get("created_at"),
            }
            for p in newest[:limit]
        ],
    }


def _why(exc: BaseException) -> str:
    if isinstance(exc, CMSMCPError):
        return exc.message[:160]
    if isinstance(exc, TimeoutError):
        return "timed out"
    return type(exc).__name__


async def overview(
    client: SafeHttpClient,
    settings: Settings,
    policy: Policy,
    *,
    sections: list[str] | None = None,
    limit: int = 10,
    days: int = 14,
) -> dict[str, Any]:
    chosen = list(dict.fromkeys(sections or SECTIONS))
    unknown = [s for s in chosen if s not in SECTIONS]
    if unknown:
        raise InvalidInputError(f"unknown sections {unknown}; choose from {list(SECTIONS)}")
    runners: dict[str, Callable[[], Awaitable[dict[str, Any]]]] = {
        "gitlab": lambda: gitlab_section(client, settings, limit),
        "jira": lambda: jira_section(client, limit),
        "mattermost": lambda: mattermost_section(client, settings, policy, limit, days),
    }
    results = await asyncio.gather(
        *(asyncio.wait_for(runners[name](), 30) for name in chosen), return_exceptions=True
    )
    out: dict[str, Any] = {}
    for name, result in zip(chosen, results, strict=True):
        if isinstance(result, BaseException):
            out[name] = f"unavailable ({_why(result)})"
        else:
            out[name] = result
            policy.note_read(name)
    return out
