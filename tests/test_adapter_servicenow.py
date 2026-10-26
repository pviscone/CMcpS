"""Fixture-driven contract tests for the CERN Service-Now adapter."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cmcps.adapters import servicenow
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

KB_ITEM = {
    "sys_id": "7c270d430a0a8c080095de51a5dc5872",
    "number": "KB0000705",
    "label": "Information about Certificates (Grid certificates)",
    "short_description": "Information about Certificates and CERN CA",
    "text": "Please consult the complete documentation related to CERN CA",
    "__search_source_id__": "kb",
    "link": "?sys_kb_id=7c270d430a0a8c080095de51a5dc5872&id=kb_article_view",
    "publishedUTC": "2011-03-03",
    "score": "294.6",
}
ACTION_ITEM = {
    "sys_id": "975005c648b3f000de14169329d97927",
    "name": "Exceptional Request: Internal tax annual certificate",
    "short_description": "Exceptionally the EXMP can request a certificate",
    "__search_source_id__": "actions",
    "url": "?id=sc_cat_item&name=exceptional-annual-internal-tax",
    "score": "476.0",
}
SEARCH_PAYLOAD = {
    "result": {
        "results": [ACTION_ITEM, KB_ITEM],
        "additionalResults": [],
        "total_number_results": 0,
        "pagination_supported": False,
    }
}
KB_RECORD = {
    "number": "KB0000705",
    "sys_id": "7c270d430a0a8c080095de51a5dc5872",
    "short_description": "Information about Certificates (Grid certificates)",
    "meta_description": "Please consult the documentation of the CERN CA",
    "meta": "certificate grid eligibility",
    "kb_category": "2f6402bd1b3c1494c91765b0ab4bcbeb",
    "kb_knowledge_base": "1ffdc0d22bbb35007cb7971319da1512",
    "published": "2011-03-03",
    "sys_updated_on": "2025-12-15 08:32:04",
    "workflow_state": "published",
    "helpful_count": "3",
    "u_portal_page": "https://cern.service-now.com/service-portal?id=kb_article&n=KB0000705",
    "text": '<p>Please consult the <a href="https://ca.cern.ch/ca">CA page</a></p>',
}
CATEGORY_PAYLOAD = {
    "records": [
        {
            "label": "Authentication",
            "full_category": "Computing / Authentication",
            "sys_id": "2f6402bd1b3c1494c91765b0ab4bcbeb",
        }
    ]
}


class Recorder:
    """Mock-transport handler that records every request."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def make_client(tmp_path: Path, recorder: Recorder) -> SafeHttpClient:
    settings = Settings(state_dir=tmp_path, require_sso=False, browser_executable=None)
    auth = AuthService(settings)
    transport = httpx.MockTransport(recorder)
    return SafeHttpClient(
        settings, auth_service=auth, client=httpx.AsyncClient(transport=transport)
    )


def search_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/now/sp/search":
        return httpx.Response(201, json=SEARCH_PAYLOAD)
    if request.url.path == "/kb_knowledge.do":  # session probe
        return httpx.Response(200, json={"records": []})
    return httpx.Response(404)


async def test_search_post_body_and_normalization(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/kb_knowledge.do":  # session probe
            return httpx.Response(200, json={"records": []})
        body = json.loads(request.content)
        assert request.url.path == "/api/now/sp/search"
        assert body["query"] == "certificate"
        assert body["count"] == 5
        assert body["source"] == ["kb"]
        assert body["include_facets"] is False
        return httpx.Response(201, json=SEARCH_PAYLOAD)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await servicenow.search_portal(client, query="certificate", sources=["kb"], limit=5)
    assert result.source == "servicenow"
    assert result.size == 2
    assert result.total_count is None
    assert result.has_more is False
    assert result.note is not None and "pagination" in result.note
    kb = next(item for item in result.items if item["number"] == "KB0000705")
    assert kb["url"] == (
        "https://cern.service-now.com/service-portal/"
        "?sys_kb_id=7c270d430a0a8c080095de51a5dc5872&id=kb_article_view"
    )
    assert kb["published_at"] == "2011-03-03"
    action = next(item for item in result.items if item["type"] == "actions")
    assert action["url"] == (
        "https://cern.service-now.com/service-portal/"
        "?id=sc_cat_item&name=exceptional-annual-internal-tax"
    )


async def test_search_defaults_all_sources(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/kb_knowledge.do":  # session probe
            return httpx.Response(200, json={"records": []})
        body = json.loads(request.content)
        assert "source" not in body
        return httpx.Response(201, json=SEARCH_PAYLOAD)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await servicenow.search_portal(client, query="wifi")
    assert result.size == 2


async def test_search_dedupes_and_caps_at_limit(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/kb_knowledge.do":  # session probe
            return httpx.Response(200, json={"records": []})
        payload = {
            "result": {
                "results": [dict(KB_ITEM), dict(ACTION_ITEM)],
                "additionalResults": [dict(KB_ITEM)],
            }
        }
        return httpx.Response(201, json=payload)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await servicenow.search_portal(client, query="certificate", limit=1)
    assert result.size == 1
    assert result.items[0]["sys_id"] == KB_ITEM["sys_id"]


async def test_search_rejects_bad_args(tmp_path: Path) -> None:
    recorder = Recorder(search_handler)
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await servicenow.search_portal(client, query="  ")
    with pytest.raises(InvalidInputError):
        await servicenow.search_portal(client, query="x", limit=0)
    with pytest.raises(InvalidInputError):
        await servicenow.search_portal(client, query="x", limit=31)
    with pytest.raises(InvalidInputError):
        await servicenow.search_portal(client, query="x", sources=["bogus"])
    assert recorder.requests == []


async def test_search_rewarms_once_on_401(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"search": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/kb_knowledge.do":  # session probe
            return httpx.Response(200, json={"records": []})
        calls["search"] += 1
        status = 401 if calls["search"] == 1 else 201
        return httpx.Response(status, json=SEARCH_PAYLOAD if status == 201 else None)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    warmed: list[list[str] | tuple[str, ...]] = []

    async def fake_warm(urls: list[str] | tuple[str, ...]) -> bool:
        warmed.append(urls)
        return True

    monkeypatch.setattr(client, "warm_sites", fake_warm)
    result = await servicenow.search_portal(client, query="certificate")
    assert result.size == 2
    assert calls["search"] == 2
    assert warmed == [servicenow.SESSION_WARM_URLS]


async def test_search_cold_session_maps_upstream(tmp_path: Path) -> None:
    recorder = Recorder(
        lambda _r: httpx.Response(
            401, json={"error": {"message": "User is not authenticated"}, "status": "failure"}
        )
    )
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError) as excinfo:
        await servicenow.search_portal(client, query="certificate")
    assert excinfo.value.instructions is not None
    assert "cern_auth_sign_in" in excinfo.value.instructions


async def test_search_warms_session_when_probe_cold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe 401 -> headless warm-up -> probe 200 -> search runs authenticated."""
    calls = {"probe": 0, "search": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/now/sp/search":
            calls["search"] += 1
            return httpx.Response(201, json=SEARCH_PAYLOAD)
        if request.url.path == "/kb_knowledge.do":
            calls["probe"] += 1
            status = 401 if calls["probe"] == 1 else 200
            return httpx.Response(status, json={"records": []} if status == 200 else None)
        return httpx.Response(404)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    warmed: list[list[str] | tuple[str, ...]] = []

    async def fake_warm(urls: list[str] | tuple[str, ...]) -> bool:
        warmed.append(urls)
        return True

    monkeypatch.setattr(client, "warm_sites", fake_warm)
    result = await servicenow.search_portal(client, query="certificate")
    assert result.size == 2
    assert calls == {"probe": 2, "search": 1}
    assert warmed == [servicenow.SESSION_WARM_URLS]


async def test_search_raises_before_querying_when_session_stays_cold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Warm-up failing -> UpstreamError; the search POST is never attempted."""
    calls = {"search": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/now/sp/search":
            calls["search"] += 1
            return httpx.Response(201, json=SEARCH_PAYLOAD)
        return httpx.Response(401)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)

    async def cold_warm(_urls: list[str] | tuple[str, ...]) -> bool:
        return False

    monkeypatch.setattr(client, "warm_sites", cold_warm)
    with pytest.raises(UpstreamError) as excinfo:
        await servicenow.search_portal(client, query="certificate")
    assert "cern_auth_sign_in" in (excinfo.value.instructions or "")
    assert calls["search"] == 0


async def article_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/kb_knowledge.do":
        assert request.url.params["sysparm_query"] == "number=KB0000705"
        return httpx.Response(200, json={"records": [KB_RECORD]})
    if request.url.path == "/kb_category.do":
        assert request.url.params["sysparm_query"] == "sys_id=2f6402bd1b3c1494c91765b0ab4bcbeb"
        return httpx.Response(200, json=CATEGORY_PAYLOAD)
    return httpx.Response(404)


async def test_get_article_by_number(tmp_path: Path) -> None:
    recorder = Recorder(article_handler)
    client = make_client(tmp_path, recorder)
    article = await servicenow.get_kb_article(client, reference="kb0000705")
    assert article["number"] == "KB0000705"
    assert article["title"] == "Information about Certificates (Grid certificates)"
    assert article["category"] == "Authentication"
    assert article["category_path"] == "Computing / Authentication"
    assert article["published_at"] == "2011-03-03"
    assert article["updated_at"] == "2025-12-15 08:32:04"
    assert article["helpful_count"] == 3
    assert article["url"] == "https://cern.service-now.com/service-portal?id=kb_article&n=KB0000705"
    assert "CA page" in article["text"]
    assert "<a href" not in article["text"]
    assert article["keywords"] == ["certificate", "grid", "eligibility"]


async def test_get_article_by_sys_id(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/kb_knowledge.do":
            assert request.url.params["sysparm_query"] == "sys_id=7c270d430a0a8c080095de51a5dc5872"
            return httpx.Response(200, json={"records": [KB_RECORD]})
        if request.url.path == "/kb_category.do":
            return httpx.Response(200, json=CATEGORY_PAYLOAD)
        return httpx.Response(404)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    article = await servicenow.get_kb_article(client, reference="7c270d430a0a8c080095de51a5dc5872")
    assert article["number"] == "KB0000705"


async def test_get_article_rejects_bad_reference(tmp_path: Path) -> None:
    recorder = Recorder(article_handler)
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await servicenow.get_kb_article(client, reference="KB123")
    with pytest.raises(InvalidInputError):
        await servicenow.get_kb_article(client, reference="z" * 32)
    assert recorder.requests == []


async def test_get_article_not_found(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={"records": []}))
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError) as excinfo:
        await servicenow.get_kb_article(client, reference="KB9999999")
    assert "not found" in str(excinfo.value)


async def test_get_article_rewarms_once_on_403(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"article": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/kb_knowledge.do":
            calls["article"] += 1
            status = 403 if calls["article"] == 1 else 200
            return httpx.Response(status, json={"records": [KB_RECORD]} if status == 200 else None)
        if request.url.path == "/kb_category.do":
            return httpx.Response(200, json=CATEGORY_PAYLOAD)
        return httpx.Response(404)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)

    async def fake_warm(_urls: list[str] | tuple[str, ...]) -> bool:
        return True

    monkeypatch.setattr(client, "warm_sites", fake_warm)
    article = await servicenow.get_kb_article(client, reference="KB0000705")
    assert article["number"] == "KB0000705"
    assert calls["article"] == 2
