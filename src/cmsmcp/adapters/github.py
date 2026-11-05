"""GitHub (cms-sw/cmssw and other public repos) via the REST API.

Ported from cernscout's GitHub source (Apache-2.0, same author). PR and issue
discussions are where CMSSW changes are explained, so this is for what a model
cannot know reliably: why/when something changed, review threads, and the
exact current code on a branch. Output is compact on purpose: PR/issue search
hits, threads without bot comments, and line windows rather than whole files.

Anonymous access works for search and reads (GitHub rate limits: ~10
searches/min, 60 requests/hour); code search needs a token
(``cmsmcp setup github``: fine-grained, public repositories, no permissions).
The token goes only to api.github.com; CERN cookies are never sent.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

from cmsmcp.config.settings import Settings
from cmsmcp.contracts.errors import InvalidInputError, UpstreamError, setup_hint
from cmsmcp.contracts.results import SearchResult
from cmsmcp.net.client import SafeHttpClient, SafeResponse

SOURCE = "github"
API = "https://api.github.com"
DEFAULT_REPO = "cms-sw/cmssw"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BOTS = frozenset({"cmsbuild", "github-actions[bot]", "dependabot[bot]"})
_OK = 200
_RATE_LIMITED = 403
_MAX_LINES = 300


def check_repo(repo: str) -> str:
    if not _REPO_RE.fullmatch(repo or ""):
        raise InvalidInputError(
            f"invalid repo {repo!r}", instructions="Use owner/name, e.g. cms-sw/cmssw."
        )
    return repo


def _headers(settings: Settings, accept: str) -> dict[str, str]:
    headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return headers


async def _get(
    client: SafeHttpClient,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    accept: str = "application/vnd.github+json",
) -> SafeResponse:
    response = await client.request(
        "GET",
        f"{API}{path}",
        params=params,
        authenticated=False,
        service_headers=_headers(client.settings, accept),
    )
    if response.status_code == _RATE_LIMITED:
        raise UpstreamError(
            "GitHub refused the request (rate limit?)",
            status_code=response.status_code,
            instructions="Anonymous GitHub allows ~10 searches/min. "
            + setup_hint("github", "A GitHub token (higher limits)"),
        )
    if response.status_code != _OK:
        raise UpstreamError(
            f"GitHub answered HTTP {response.status_code} for {path}",
            status_code=response.status_code,
        )
    return response


def _json(response: SafeResponse) -> Any:
    try:
        return json.loads(response.content)
    except ValueError as exc:
        raise UpstreamError("GitHub returned invalid JSON") from exc


async def search(
    client: SafeHttpClient,
    *,
    query: str,
    kind: str = "prs",
    repo: str = DEFAULT_REPO,
    limit: int = 8,
) -> SearchResult:
    repo = check_repo(repo)
    if kind in {"prs", "issues"}:
        q = f"{query} repo:{repo} {'is:pr' if kind == 'prs' else 'is:issue'}"
        data = _json(
            await _get(client, "/search/issues", {"q": q, "per_page": limit, "sort": "updated"})
        )
        items = []
        for item in (data.get("items") or [])[:limit]:
            merged = (item.get("pull_request") or {}).get("merged_at")
            items.append(
                {
                    "title": f"#{item.get('number')} {item.get('title')}",
                    "url": item.get("html_url"),
                    "updated_at": item.get("updated_at"),
                    "state": f"{item.get('state')}{' (merged)' if merged else ''}",
                    "author": (item.get("user") or {}).get("login"),
                    "labels": [lb.get("name") for lb in item.get("labels") or []][:8],
                    "snippet": (item.get("body") or "")[:300],
                }
            )
        total = data.get("total_count")
    elif kind == "code":
        if not client.settings.github_token:
            raise InvalidInputError(
                "GitHub code search needs a token",
                instructions=setup_hint("github", "GitHub code search"),
            )
        data = _json(
            await _get(
                client,
                "/search/code",
                {"q": f"{query} repo:{repo}", "per_page": limit},
                accept="application/vnd.github.text-match+json",
            )
        )
        items = [
            {
                "title": f"{(i.get('repository') or {}).get('full_name')}: {i.get('path')}",
                "url": i.get("html_url"),
                "snippet": " … ".join(
                    m.get("fragment", "") for m in (i.get("text_matches") or [])[:2]
                )[:400],
            }
            for i in (data.get("items") or [])[:limit]
        ]
        total = data.get("total_count")
    else:
        raise InvalidInputError("kind must be prs, issues or code")
    return SearchResult(
        source=SOURCE,
        query=f"{query} ({kind} in {repo})",
        items=items,
        size=len(items),
        total_count=total if isinstance(total, int) else None,
        has_more=isinstance(total, int) and total > len(items),
    )


async def get_thread(
    client: SafeHttpClient, *, repo: str, number: int, max_comments: int = 40
) -> dict[str, Any]:
    repo = check_repo(repo)
    issue = _json(await _get(client, f"/repos/{repo}/issues/{number}"))
    is_pr = "pull_request" in issue
    out: dict[str, Any] = {
        "title": f"{'PR' if is_pr else 'Issue'} #{number}: {issue.get('title')}",
        "url": issue.get("html_url"),
        "state": issue.get("state"),
        "author": (issue.get("user") or {}).get("login"),
        "created_at": issue.get("created_at"),
        "labels": [lb.get("name") for lb in issue.get("labels") or []],
        "body": (issue.get("body") or "")[:4_000],
    }
    comments = _json(
        await _get(client, f"/repos/{repo}/issues/{number}/comments", {"per_page": 100})
    )
    if is_pr:
        pr = _json(await _get(client, f"/repos/{repo}/pulls/{number}"))
        files = _json(await _get(client, f"/repos/{repo}/pulls/{number}/files", {"per_page": 100}))
        out.update(
            {
                "merged_at": pr.get("merged_at"),
                "base": (pr.get("base") or {}).get("ref"),
                "head": (pr.get("head") or {}).get("label"),
                "changes": f"+{pr.get('additions')}/-{pr.get('deletions')}",
                "files": [f.get("filename") for f in files][:40],
            }
        )
        review = _json(
            await _get(client, f"/repos/{repo}/pulls/{number}/comments", {"per_page": 100})
        )
        comments += [dict(c, _path=c.get("path")) for c in review]
    human = [
        c
        for c in comments
        if (c.get("user") or {}).get("login", "").lower() not in _BOTS
        and (c.get("user") or {}).get("type") != "Bot"
    ]
    human.sort(key=lambda c: c.get("created_at", ""))
    out["comments_omitted"] = max(0, len(human) - max_comments) or None
    out["comments"] = [
        {
            "author": (c.get("user") or {}).get("login"),
            "date": c.get("created_at"),
            "on": c.get("_path"),
            "body": (c.get("body") or "")[:800],
        }
        for c in human[-max_comments:]
    ]
    return out


async def get_file(
    client: SafeHttpClient,
    *,
    repo: str,
    path: str,
    ref: str | None = None,
    start_line: int = 1,
    end_line: int | None = None,
) -> dict[str, Any]:
    repo = check_repo(repo)
    clean = path.strip("/")
    if not clean or ".." in clean.split("/"):
        raise InvalidInputError(f"bad path {path!r}")
    params = {"ref": ref} if ref else None
    response = await _get(
        client,
        f"/repos/{repo}/contents/{quote(clean)}",
        params,
        accept="application/vnd.github.raw",
    )
    lines = response.content.decode(errors="replace").splitlines()
    total = len(lines)
    start = max(1, start_line)
    end = min(total, end_line or start + _MAX_LINES - 1, start + _MAX_LINES - 1)
    text = "\n".join(f"{i:>6}  {lines[i - 1]}" for i in range(start, end + 1))
    return {
        "url": f"https://github.com/{repo}/blob/{quote(ref or 'HEAD')}/{quote(clean)}",
        "lines": f"{start}-{end} of {total}",
        "content": text,
        "next_start_line": end + 1 if end < total else None,
    }
