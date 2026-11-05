"""Unified search fan-out and the personal overview (adapters stubbed)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cmsmcp.adapters import cmssearch, indico, jira, mattermost, mine, search
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.errors import InvalidInputError, UpstreamError
from cmsmcp.contracts.results import SearchResult
from cmsmcp.policy import Policy


def _sr(source: str, items: list[dict[str, Any]], note: str | None = None) -> SearchResult:
    return SearchResult(source=source, items=items, size=len(items), note=note)


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    async def indico_search(*_a: Any, query: str, limit: int, **_k: Any) -> dict[str, Any]:
        seen["indico"] = (query, limit)
        return {
            "results": {
                "contribution": {
                    "items": [
                        {
                            "title": "Tracker talk",
                            "url": "https://indico.cern.ch/c/1",
                            "date": "2026-10-01T10:00:00Z",
                            "event": "Tracker week",
                            "snippet": "the <b>tracker</b> " + "x" * 400,
                        }
                    ]
                },
                "event": {"items": []},
            }
        }

    async def docs_search(*_a: Any, **_k: Any) -> SearchResult:
        return _sr("cmssearch", [{"title": "TrackerTwiki", "url": "https://twiki.cern.ch/T"}])

    async def failing(*_a: Any, **_k: Any) -> SearchResult:
        raise UpstreamError("backend down")

    async def teams(*_a: Any, **_k: Any) -> SearchResult:
        return _sr("mattermost", [{"team_id": "t1"}, {"team_id": "t2"}])

    async def posts(*_a: Any, team_id: str, include_direct_messages: bool, **_k: Any) -> Any:
        seen.setdefault("dms", []).append(include_direct_messages)
        post = {"post_id": "p1", "message": "tracker news", "url": "u", "created_at": "2026-11-01"}
        return _sr("mattermost", [post, {**post, "post_id": f"p-{team_id}"}])

    monkeypatch.setattr(indico, "search", indico_search)
    monkeypatch.setattr(cmssearch, "search", docs_search)
    monkeypatch.setattr(jira, "search_issues", failing)
    monkeypatch.setattr(mattermost, "list_teams", teams)
    monkeypatch.setattr(mattermost, "search_posts", posts)
    return seen


async def test_unified_search_merges_sources_and_isolates_failures(
    tmp_path: Path, stubs: dict[str, Any]
) -> None:
    policy = Policy(Settings(state_dir=tmp_path))
    result = await search.unified_search(
        None,  # type: ignore[arg-type]
        Settings(state_dir=tmp_path),
        policy,
        query=" tracker ",
        sources=["indico", "docs", "jira", "mattermost"],
        limit=3,
    )
    titles = [hit["title"] for hit in result["items"]]
    assert titles[0] == "[indico] Tracker talk (Tracker week)"
    assert "[docs] TrackerTwiki" in titles
    assert len(result["items"][0]["snippet"]) <= search.SNIPPET_CHARS
    assert result["source_errors"]["jira"].startswith("UPSTREAM_ERROR")
    # Posts seen in several teams are de-duplicated; DMs stay out by default.
    assert sum(t.startswith("[mattermost]") for t in titles) == 3
    assert stubs["dms"] == [False, False]
    assert stubs["indico"] == ("tracker", 3)
    assert {"indico", "docs", "mattermost"} <= policy.read_sources


async def test_unified_search_includes_dms_only_with_opt_in(
    tmp_path: Path, stubs: dict[str, Any]
) -> None:
    settings = Settings(state_dir=tmp_path, personal_sources=("mattermost_dm",))
    await search.unified_search(
        None,  # type: ignore[arg-type]
        settings,
        Policy(settings),
        query="x",
        sources=["mattermost"],
    )
    assert stubs["dms"] == [True, True]


async def test_unified_search_rejects_bad_input(tmp_path: Path) -> None:
    settings = Settings(state_dir=tmp_path)
    with pytest.raises(InvalidInputError):
        await search.unified_search(None, settings, Policy(settings), query="  ")  # type: ignore[arg-type]
    with pytest.raises(InvalidInputError):
        await search.unified_search(
            None,  # type: ignore[arg-type]
            settings,
            Policy(settings),
            query="x",
            sources=["nope"],
        )


async def test_mine_overview_sections(
    tmp_path: Path, stubs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    del stubs

    async def gitlab_get(_c: Any, _s: Any, path: str, params: dict[str, Any]) -> Any:
        if path == "/user":
            return {"username": "jdoe"}
        if path == "/todos":
            return [{"action_name": "mentioned", "target": {"title": "Fix"}, "target_url": "t"}]
        if params.get("reviewer_username") == "jdoe":
            return [{"title": "Review me", "web_url": "m", "references": {"full": "g/p!1"}}]
        return []

    async def jira_search(*_a: Any, jql: str, **_k: Any) -> SearchResult:
        assert "currentUser()" in jql
        return _sr("jira", [{"key": "CMS-1", "summary": "Bug", "url": "j", "status": "Open"}])

    async def get_me(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"username": "jdoe"}

    monkeypatch.setattr(mine, "_gitlab_get", gitlab_get)
    monkeypatch.setattr(jira, "search_issues", jira_search)
    monkeypatch.setattr(mattermost, "get_me", get_me)
    settings = Settings(state_dir=tmp_path)
    out = await mine.overview(None, settings, Policy(settings), limit=5)  # type: ignore[arg-type]
    assert out["gitlab"]["review_requested"][0]["title"] == "g/p!1 Review me"
    assert out["gitlab"]["todos"][0]["title"] == "mentioned: Fix"
    assert out["jira"]["assigned"][0]["title"] == "CMS-1 Bug"
    assert out["mattermost"]["user"] == "jdoe"
    assert out["mattermost"]["dms_included"] is False
    with pytest.raises(InvalidInputError):
        await mine.overview(None, settings, Policy(settings), sections=["x"])  # type: ignore[arg-type]
