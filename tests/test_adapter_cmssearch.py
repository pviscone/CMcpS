"""Fixture-driven contract tests for the CMS Search adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cmcps.adapters import cmssearch
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

SEARCH_PAYLOAD = {
    "query": "calibration",
    "count": 2,
    "total": 5492,
    "offset": 10,
    "limit": 2,
    "rank": "relevance_pagerank_recent",
    "results": [
        {
            "document_id": "c5b9995084199b55c231a75ea9caed63bbfd4954",
            "score": 70.433136,
            "index": "cms-jme-public",
            "base_key": "cms-jme-public",
            "tags": ["CMS"],
            "title": "Calibration files",
            "location": "dp-notes/CMS-DP-2023-044/#calibration-files",
            "url": "https://cms-jme-public.docs.cern.ch/dp-notes/CMS-DP-2023-044/#calibration-files",
            "snippet": "The correction factors are provided in correctionlib JSON format.",
            "date": "2026-08-18 00:00",
        },
        {
            "document_id": "1bacd8f0e8c949c623bc46ae0b598d0a1621be23",
            "score": 58.283195,
            "index": "ph2acf",
            "base_key": "ph2acf",
            "tags": ["CMS"],
            "title": "Calibration result",
            "location": "innerTracker/calibrations/ThresholdAdjust.html#calibration-result",
            "url": "https://ph2acf.docs.cern.ch/innerTracker/calibrations/ThresholdAdjust.html#calibration-result",
            "snippet": "Threshold distribution from S-Curve measurement.",
            "date": "2026-09-24 00:00",
        },
    ],
}

DOCS_CONFIG_PAYLOAD = {
    "docs": {
        "cms-jme-public": {
            "index_name": "cms-jme-public-index",
            "tags": ["CMS", "JME"],
            "url": "https://cms-jme-public.docs.cern.ch/",
        },
        "ph2acf": {
            "index_name": "ph2acf-index",
            "tags": ["CMS"],
            "url": "https://ph2acf.docs.cern.ch/",
        },
    }
}

INDEX_STATS_PAYLOAD = {
    "stats": {
        "cms-jme-public": {
            "count": 1234,
            "last_updated_at": "2026-10-20T08:00:00",
            "last_updated_source": "gitlab",
            "latest_document_date": "2026-10-18 00:00",
        },
        "ph2acf": {
            "count": 567,
            "last_updated_at": "2026-10-19T08:00:00",
            "last_updated_source": "gitlab",
            "latest_document_date": "2026-10-01 00:00",
        },
    }
}

DOCUMENT_PAYLOAD = {
    "document_id": "abc123",
    "title": "Calibration files",
    "content": "line 1\nline 2\n",
    "offset": 0,
    "limit": 100,
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


async def test_search_envelope_and_params(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=SEARCH_PAYLOAD))
    client = make_client(tmp_path, recorder)
    result = await cmssearch.search(
        client,
        query="calibration",
        size=2,
        offset=10,
        rank="relevance_pagerank_recent",
        date_from="2026-01-01",
        date_to="2026-12-31",
        indices=["cms-jme-public", "ph2acf"],
    )
    assert result.total_count == 5492
    assert result.size == 2
    assert result.has_more is True
    assert result.page == 6
    first = result.items[0]
    assert first["document_id"] == "c5b9995084199b55c231a75ea9caed63bbfd4954"
    request = recorder.requests[0]
    assert request.url.path == "/search"
    params = request.url.params
    assert params["q"] == "calibration"
    assert params["size"] == "2"
    assert params["from"] == "10"
    assert params["rank"] == "relevance_pagerank_recent"
    assert params["date_from"] == "2026-01-01"
    assert params["date_to"] == "2026-12-31"
    assert params.get_list("indices") == ["cms-jme-public", "ph2acf"]


async def test_search_rejects_empty_query(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=SEARCH_PAYLOAD))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await cmssearch.search(client, query="   ")
    with pytest.raises(InvalidInputError):
        await cmssearch.search(client, query="x", size=0)
    with pytest.raises(InvalidInputError):
        await cmssearch.search(client, query="x", size=51)
    with pytest.raises(InvalidInputError):
        await cmssearch.search(client, query="x", offset=-1)
    assert recorder.requests == []


async def test_search_last_page_no_more(tmp_path: Path) -> None:
    payload = dict(SEARCH_PAYLOAD, total=12)
    recorder = Recorder(lambda _r: httpx.Response(200, json=payload))
    client = make_client(tmp_path, recorder)
    result = await cmssearch.search(client, query="calibration", size=2, offset=10)
    assert result.has_more is False
    assert result.total_count == 12


async def test_search_unexpected_payload(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={"nope": []}))
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await cmssearch.search(client, query="x")


async def test_get_document(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=DOCUMENT_PAYLOAD))
    client = make_client(tmp_path, recorder)
    result = await cmssearch.get_document(
        client, source_key="cms-jme-public", document_id="abc123", offset=0, limit=100
    )
    assert result["source"] == "cmssearch"
    assert result["document"]["content"].startswith("line 1")
    request = recorder.requests[0]
    assert request.url.path == "/api/documents/cms-jme-public/abc123"
    assert request.url.params["offset"] == "0"
    assert request.url.params["limit"] == "100"


async def test_get_document_internal_credential_401(tmp_path: Path) -> None:
    recorder = Recorder(
        lambda _r: httpx.Response(401, json={"detail": "Invalid internal service credential."})
    )
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError) as excinfo:
        await cmssearch.get_document(client, source_key="cms-twiki", document_id="abc123")
    assert "internal-credential" in excinfo.value.message
    assert "twiki_get_page" in (excinfo.value.instructions or "")


async def test_get_document_other_401_keeps_generic_error(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(401, json={"detail": "session expired"}))
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError) as excinfo:
        await cmssearch.get_document(client, source_key="cms-twiki", document_id="abc123")
    assert "internal-credential" not in excinfo.value.message


async def test_get_document_404(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(404, json={"detail": "nf"}))
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError, match="not found"):
        await cmssearch.get_document(client, source_key="s", document_id="missing")


async def test_get_document_rejects_bad_args(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={}))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await cmssearch.get_document(client, source_key="", document_id="x")
    with pytest.raises(InvalidInputError):
        await cmssearch.get_document(client, source_key="s", document_id="x", offset=-1)
    with pytest.raises(InvalidInputError):
        await cmssearch.get_document(client, source_key="s", document_id="x", limit=0)
    assert recorder.requests == []


async def test_list_sources(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=DOCS_CONFIG_PAYLOAD))
    client = make_client(tmp_path, recorder)
    result = await cmssearch.list_sources(client)
    assert result.total_count == 2
    keys = [row["source_key"] for row in result.items]
    assert keys == ["cms-jme-public", "ph2acf"]
    assert result.items[0]["index_name"] == "cms-jme-public-index"
    assert result.items[0]["tags"] == ["CMS", "JME"]
    assert recorder.requests[0].url.path == "/api/docs-config"


async def test_index_stats_and_filter(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=INDEX_STATS_PAYLOAD))
    client = make_client(tmp_path, recorder)
    result = await cmssearch.index_stats(client)
    assert result.total_count == 2
    assert result.items[0]["count"] == 1234
    filtered = await cmssearch.index_stats(client, source_key="ph2acf")
    assert filtered.total_count == 1
    assert filtered.items[0]["source_key"] == "ph2acf"
    assert filtered.items[0]["latest_document_date"] == "2026-10-01 00:00"
    missing = await cmssearch.index_stats(client, source_key="nope")
    assert missing.items == []
