"""Fixture-driven contract tests for the CMS XSDB adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cmcps.adapters import xsdb
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

RECORDS = [
    {
        "id": 1,
        "energy": 13,
        "process_name": "ttbar",
        "cross_section": 834.0,
        "total_uncertainty": 4.2,
        "accuracy": "NNLO",
        "DAS": "/TTbar/mc/AODSIM",
        "refs": "arXiv:2001.00001",
    }
]


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


async def test_search_builds_params(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        assert params["particles"] == "ttbar"
        assert params["energy"] == "13"
        assert params["pageNum"] == "2"
        assert params["pageSize"] == "20"
        return httpx.Response(200, json=RECORDS)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await xsdb.search(client, particles="ttbar", energy="13", page_num=2)
    assert result.source == "xsdb"
    assert result.size == 1
    assert result.items[0]["cross_section"] == 834.0
    assert result.items[0]["DAS"] == "/TTbar/mc/AODSIM"


async def test_search_rejects_bad_args(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=[]))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await xsdb.search(client, page_num=0)
    with pytest.raises(InvalidInputError):
        await xsdb.search(client, page_size=0)
    with pytest.raises(InvalidInputError):
        await xsdb.search(client, extra_filters={"bogus_filter": "x"})
    assert recorder.requests == []


async def test_search_extra_filters(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["shower"] == "Pythia8"
        return httpx.Response(200, json=RECORDS)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await xsdb.search(client, extra_filters={"shower": "Pythia8"})
    assert result.size == 1


async def test_search_has_more(tmp_path: Path) -> None:
    rows = [dict(r, id=i) for i, r in enumerate(RECORDS * 3)]
    recorder = Recorder(lambda _r: httpx.Response(200, json=rows))
    client = make_client(tmp_path, recorder)
    result = await xsdb.search(client, page_size=3)
    assert result.has_more is True
    result = await xsdb.search(client, page_size=10)
    assert result.has_more is False


async def test_get_linked_tables(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=["tableA", "tableB"]))
    client = make_client(tmp_path, recorder)
    result = await xsdb.get_linked_tables(client)
    assert result["table_count"] == 2
    assert result["items"] == ["tableA", "tableB"]


async def test_html_oidc_response_maps_upstream(tmp_path: Path) -> None:
    recorder = Recorder(
        lambda _r: httpx.Response(200, text="<html><body>Sign in to CERN</body></html>")
    )
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await xsdb.search(client, particles="ttbar")
