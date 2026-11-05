"""GitHub adapter against a mock transport."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cmsmcp.adapters import github
from cmsmcp.auth.service import AuthService
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.errors import InvalidInputError, UpstreamError
from cmsmcp.net.client import SafeHttpClient

ROUTES: dict[str, Any] = {
    "/search/issues": {
        "total_count": 12,
        "items": [
            {
                "number": 42,
                "title": "Fix tracker seeding",
                "html_url": "https://github.com/cms-sw/cmssw/pull/42",
                "updated_at": "2026-10-01T00:00:00Z",
                "state": "closed",
                "pull_request": {"merged_at": "2026-10-02T00:00:00Z"},
                "user": {"login": "dev"},
                "labels": [{"name": "reconstruction"}],
                "body": "Improves seeding.",
            }
        ],
    },
    "/repos/cms-sw/cmssw/issues/42": {
        "title": "Fix tracker seeding",
        "html_url": "https://github.com/cms-sw/cmssw/pull/42",
        "state": "closed",
        "user": {"login": "dev"},
        "pull_request": {},
        "body": "Improves seeding.",
    },
    "/repos/cms-sw/cmssw/issues/42/comments": [
        {"user": {"login": "cmsbuild"}, "body": "bot", "created_at": "2026-10-01"},
        {"user": {"login": "reviewer"}, "body": "+1", "created_at": "2026-10-02"},
    ],
    "/repos/cms-sw/cmssw/pulls/42": {
        "merged_at": "2026-10-02",
        "base": {"ref": "master"},
        "head": {"label": "dev:seed"},
        "additions": 10,
        "deletions": 2,
    },
    "/repos/cms-sw/cmssw/pulls/42/files": [{"filename": "RecoTracker/a.cc"}],
    "/repos/cms-sw/cmssw/pulls/42/comments": [
        {"user": {"login": "r2"}, "body": "nit", "created_at": "2026-10-03", "path": "a.cc"}
    ],
}


def _client(tmp_path: Path, **kw: Any) -> tuple[SafeHttpClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.startswith("/repos/cms-sw/cmssw/contents/"):
            return httpx.Response(200, content=b"line1\nline2\nline3\n")
        body = ROUTES.get(request.url.path)
        return httpx.Response(200, json=body) if body is not None else httpx.Response(404)

    settings = Settings(state_dir=tmp_path, **kw)
    return (
        SafeHttpClient(
            settings,
            AuthService(settings),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ),
        seen,
    )


async def test_pr_search_is_repo_scoped_and_compact(tmp_path: Path) -> None:
    client, seen = _client(tmp_path)
    result = await github.search(client, query="seeding")
    assert seen[0].url.params["q"] == "seeding repo:cms-sw/cmssw is:pr"
    assert "authorization" not in seen[0].headers and "cookie" not in seen[0].headers
    item = result.items[0]
    assert item["title"] == "#42 Fix tracker seeding"
    assert item["state"] == "closed (merged)"
    assert result.has_more is True


async def test_token_is_sent_and_code_search_needs_one(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    with pytest.raises(InvalidInputError):
        await github.search(client, query="x", kind="code")
    client, seen = _client(tmp_path, github_token="github_pat_x")
    await github.search(client, query="x")
    assert seen[0].headers["Authorization"] == "Bearer github_pat_x"


async def test_thread_skips_bots_and_includes_review_comments(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    thread = await github.get_thread(client, repo="cms-sw/cmssw", number=42)
    assert thread["title"] == "PR #42: Fix tracker seeding"
    assert thread["files"] == ["RecoTracker/a.cc"]
    authors = [c["author"] for c in thread["comments"]]
    assert authors == ["reviewer", "r2"]
    assert thread["comments"][1]["on"] == "a.cc"


async def test_file_window(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    out = await github.get_file(
        client, repo="cms-sw/cmssw", path="a/b.py", start_line=2, end_line=3
    )
    assert out["lines"] == "2-3 of 3"
    assert out["content"].splitlines()[0].strip().startswith("2")
    assert out["next_start_line"] is None


async def test_bad_repo_and_errors(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    with pytest.raises(InvalidInputError):
        await github.search(client, query="x", repo="not a repo")
    with pytest.raises(UpstreamError):
        await github.get_thread(client, repo="cms-sw/other", number=1)
