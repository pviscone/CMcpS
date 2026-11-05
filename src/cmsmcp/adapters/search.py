"""Unified search: one query fanned out over many CERN/CMS sources in parallel.

The single most token-efficient entry point: instead of the agent calling
eight search tools (eight tool-call round trips, eight result envelopes), one
call returns a short merged list, ``limit`` hits per source, each reduced to
``[source] title — url (date)`` plus a clipped snippet. Sources that fail or
time out report one line each and never fail the whole search.

Personal content follows the policy: Mattermost direct/group messages are
included only when the user opted into ``mattermost_dm``.
"""

from __future__ import annotations

import asyncio
import html
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from cmsmcp.adapters import (
    cds,
    cmssearch,
    discourse,
    docdb,
    glance,
    gocms,
    hypernews,
    indico,
    inspire,
    jira,
    mattermost,
    servicenow,
)
from cmsmcp.adapters import gitlab as gitlab_adapter
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.errors import CMSMCPError, InvalidInputError
from cmsmcp.net.client import SafeHttpClient
from cmsmcp.output import DATE_KEYS, TITLE_KEYS, URL_KEYS
from cmsmcp.policy import Policy

SNIPPET_CHARS = 240
SOURCE_TIMEOUT_SECONDS = 25.0
_SNIPPET_KEYS = ("snippet", "excerpt", "abstract", "description", "message", "summary", "content")

Hit = dict[str, Any]


@dataclass(frozen=True)
class Source:
    name: str
    guide: str
    default: bool


SOURCES: tuple[Source, ...] = (
    Source("indico", "meetings: agendas, talks, slides, minutes", True),
    Source("docs", "CMS documentation incl. TWiki (cms-search index)", True),
    Source("gitlab", "CERN GitLab projects and issues", True),
    Source("mattermost", "chat posts in your teams (DMs only if opted in)", True),
    Source("cms-talk", "CMS Talk forum discussions", True),
    Source("jira", "CERN Jira tickets (slow: full-text over all projects, ~20 s)", False),
    Source("glance", "internal CMS analyses (CADI) by title", True),
    Source("inspire", "published HEP papers", True),
    Source("cds", "CERN Document Server records", False),
    Source("docdb", "CMS DocDB documents", False),
    Source("servicenow", "CERN Service Portal / knowledge base", False),
    Source("gocms", "CMS shortlinks (go/...)", False),
    Source("hypernews", "CMS HyperNews archive (to 2022)", False),
    Source("cms-pub-talk", "publication-review discussions", False),
    Source("root-forum", "ROOT forum", False),
)
SOURCE_NAMES = tuple(s.name for s in SOURCES)
DEFAULT_SOURCES = tuple(s.name for s in SOURCES if s.default)


def _first(item: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str | int) and str(value).strip():
            return str(value).strip()
    return None


_TAG_RE = re.compile(r"<[^>]+>")


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub("", text))).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def to_hit(
    source: str, item: Mapping[str, Any], *, title: str | None = None, snippet: bool = True
) -> Hit:
    """Reduce one backend item to the shared hit shape (HTML stripped, clipped)."""
    hit: Hit = {
        "title": f"[{source}] {_clip(title or _first(item, TITLE_KEYS) or '(untitled)', 160)}",
        "url": _first(item, URL_KEYS),
        "date": _first(item, DATE_KEYS),
    }
    text = _first(item, _SNIPPET_KEYS) if snippet else None
    if text and text != title:
        hit["snippet"] = _clip(text, SNIPPET_CHARS)
    return {k: v for k, v in hit.items() if v}


def items_of(result: Any) -> list[Mapping[str, Any]]:
    items = getattr(result, "items", None)
    if items is None and isinstance(result, Mapping):
        items = result.get("items")
    return [i for i in items or [] if isinstance(i, Mapping)]


class Searcher:
    """Runs the per-source searches with shared client/settings/policy."""

    def __init__(self, client: SafeHttpClient, settings: Settings, policy: Policy) -> None:
        self.client = client
        self.settings = settings
        self.policy = policy

    async def indico(self, query: str, limit: int) -> list[Hit]:
        result = await indico.search(self.client, self.settings, query=query, limit=limit)
        groups = result.get("results") or {}
        hits: list[Hit] = []
        for kind in ("event", "contribution", "attachment", "event_note", "category"):
            for item in (groups.get(kind) or {}).get("items") or []:
                title = item.get("title")
                if item.get("event") and item.get("event") != title:
                    title = f"{title} ({item['event']})"
                hits.append(to_hit("indico", item, title=title))
        return hits[:limit]

    async def docs(self, query: str, limit: int) -> list[Hit]:
        result = await cmssearch.search(self.client, query=query, size=limit)
        return [to_hit("docs", i) for i in items_of(result)]

    async def gitlab(self, query: str, limit: int) -> list[Hit]:
        projects, issues = await asyncio.gather(
            gitlab_adapter.search_projects(self.client, self.settings, query=query, per_page=limit),
            gitlab_adapter.search_issues(
                self.client, self.settings, search_term=query, per_page=limit
            ),
            return_exceptions=True,
        )
        hits: list[Hit] = []
        if not isinstance(projects, BaseException):
            hits += [
                to_hit("gitlab", i, title=i.get("path_with_namespace")) for i in items_of(projects)
            ]
        if not isinstance(issues, BaseException):
            hits += [to_hit("gitlab-issue", i) for i in items_of(issues)]
        if isinstance(projects, BaseException) and isinstance(issues, BaseException):
            raise projects
        return hits[: limit * 2]

    async def mattermost(self, query: str, limit: int) -> list[Hit]:
        teams = await mattermost.list_teams(self.client, self.settings)
        include_dms = self.policy.personal_allowed("mattermost_dm")
        searches = await asyncio.gather(
            *(
                mattermost.search_posts(
                    self.client,
                    self.settings,
                    team_id=team["team_id"],
                    terms=query,
                    include_direct_messages=include_dms,
                )
                for team in items_of(teams)
                if team.get("team_id")
            ),
            return_exceptions=True,
        )
        seen: dict[str, Mapping[str, Any]] = {}
        for result in searches:
            if isinstance(result, BaseException):
                continue
            for post in items_of(result):
                seen.setdefault(str(post.get("post_id")), post)
        posts = sorted(seen.values(), key=lambda p: str(p.get("created_at") or ""), reverse=True)
        return [
            to_hit("mattermost", p, title=str(p.get("message") or ""), snippet=False)
            for p in posts[:limit]
        ]

    async def _discourse(self, site: str, query: str, limit: int) -> list[Hit]:
        result = await discourse.search(self.client, site=site, query=query)
        return [to_hit(site, i) for i in items_of(result)[:limit]]

    async def jira(self, query: str, limit: int) -> list[Hit]:
        escaped = query.replace("\\", "\\\\").replace('"', '\\"')
        result = await jira.search_issues(
            self.client,
            jql=f'text ~ "{escaped}" ORDER BY updated DESC',
            max_results=limit,
            warm=False,
        )
        return [
            to_hit("jira", i, title=f"{i.get('key')} {i.get('summary') or ''} [{i.get('status')}]")
            for i in items_of(result)
        ]

    async def glance(self, query: str, limit: int) -> list[Hit]:
        result = await glance.list_analyses(self.client, self.settings, title=query, limit=limit)
        return [
            to_hit("glance", i, title=f"{i.get('code') or ''} {i.get('title') or ''}".strip())
            for i in items_of(result)
        ]

    async def inspire(self, query: str, limit: int) -> list[Hit]:
        result = await inspire.search_papers(self.client, self.settings, query=query, size=limit)
        return [to_hit("inspire", i) for i in items_of(result)]

    async def cds(self, query: str, limit: int) -> list[Hit]:
        result = await cds.search(self.client, self.settings, query=query, size=limit)
        return [to_hit("cds", i) for i in items_of(result)]

    async def docdb(self, query: str, limit: int) -> list[Hit]:
        result = await docdb.search_documents(self.client, text=query, limit=limit)
        return [to_hit("docdb", i) for i in items_of(result)]

    async def servicenow(self, query: str, limit: int) -> list[Hit]:
        result = await servicenow.search_portal(self.client, query=query, limit=limit)
        return [to_hit("servicenow", i) for i in items_of(result)]

    async def gocms(self, query: str, limit: int) -> list[Hit]:
        result = await gocms.list_links(self.client, search=query, per_page=limit)
        return [to_hit("gocms", i) for i in items_of(result)]

    async def hypernews(self, query: str, limit: int) -> list[Hit]:
        result = await hypernews.search_messages(self.client, query=query)
        return [to_hit("hypernews", i) for i in items_of(result)[:limit]]

    def runner(self, name: str) -> Callable[[str, int], Awaitable[list[Hit]]]:
        if name in {"cms-talk", "cms-pub-talk", "root-forum"}:
            return lambda q, n: self._discourse(name, q, n)
        return getattr(self, name)


def _error_line(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timed out"
    if isinstance(exc, CMSMCPError):
        return str(exc.code) + ": " + _clip(exc.message, 160)
    return type(exc).__name__


async def unified_search(
    client: SafeHttpClient,
    settings: Settings,
    policy: Policy,
    *,
    query: str,
    sources: list[str] | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Search several sources at once; ``limit`` hits per source."""
    query = query.strip()
    if not query:
        raise InvalidInputError("query must not be empty")
    chosen = list(dict.fromkeys(sources or DEFAULT_SOURCES))
    unknown = [s for s in chosen if s not in SOURCE_NAMES]
    if unknown:
        raise InvalidInputError(
            f"unknown sources {unknown}",
            instructions=f"Choose from: {', '.join(SOURCE_NAMES)}.",
        )
    searcher = Searcher(client, settings, policy)
    outcomes = await asyncio.gather(
        *(
            asyncio.wait_for(searcher.runner(name)(query, limit), SOURCE_TIMEOUT_SECONDS)
            for name in chosen
        ),
        return_exceptions=True,
    )
    items: list[Hit] = []
    errors: dict[str, str] = {}
    for name, outcome in zip(chosen, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            errors[name] = _error_line(outcome)
        else:
            items.extend(outcome)
            policy.note_read(name)
    result: dict[str, Any] = {
        "source": "cern_search",
        "query": query,
        "size": len(items),
        "items": items,
    }
    if errors:
        result["source_errors"] = errors
    return result
