"""Fixture-driven contract tests for the CMS DAS adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cmcps.adapters import das
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

DAS_PAYLOAD = {
    "status": "ok",
    "nresults": 137,
    "timestamp": 1700000000.0,
    "mongo_query": {},
    "data": [
        {
            "dataset": [
                {
                    "name": "/TTbar/RunIISummer20UL17SIM-v1/SIM",
                    "nevents": 1000000,
                    "nfiles": 50,
                    "datatype": "mc",
                }
            ]
        },
        {
            "dataset": [
                {
                    "name": "/TTbar/RunIISummer20UL17SIM-v1/SIM",
                    "nevents": 1000000,
                    "nfiles": 50,
                    "datatype": "mc",
                }
            ]
        },  # replica duplicate
        {"site": [{"name": "T2_CH_CERN", "kind": "Disk"}]},
    ],
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


async def test_search_builds_das_params(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        assert params["view"] == "list"
        assert params["input"] == "dataset=/TTbar/*/SIM"
        assert params["idx"] == "10"
        assert params["instance"] == "prod/global"
        return httpx.Response(200, json=DAS_PAYLOAD)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await das.search(client, query="dataset=/TTbar/*/SIM", offset=10)
    assert result.total_count == 137
    assert result.has_more is True
    assert result.size == 2  # duplicate dataset row collapsed
    names = [i["name"] for i in result.items]
    assert names.count("/TTbar/RunIISummer20UL17SIM-v1/SIM") == 1
    assert result.items[0]["entity"] == "dataset"
    assert result.items[1]["entity"] == "site"


async def test_search_rejects_bad_args(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={}))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await das.search(client, query="   ")
    with pytest.raises(InvalidInputError):
        await das.search(client, query="dataset=/X/Y/Z; DROP TABLE")
    with pytest.raises(InvalidInputError):
        await das.search(client, query="dataset=/X/Y/Z", limit=0)
    with pytest.raises(InvalidInputError):
        await das.search(client, query="dataset=/X/Y/Z", instance="evil host")
    assert recorder.requests == []


async def test_get_dataset_info_exact_match(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["input"] == "dataset=/TTbar/RunIISummer20UL17SIM-v1/SIM"
        return httpx.Response(200, json=DAS_PAYLOAD)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await das.get_dataset_info(client, dataset="/TTbar/RunIISummer20UL17SIM-v1/SIM")
    assert result["name"] == "/TTbar/RunIISummer20UL17SIM-v1/SIM"
    assert result["nevents"] == 1000000
    assert result["datatype"] == "mc"


async def test_get_dataset_info_rejects_wildcards(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={}))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await das.get_dataset_info(client, dataset="/TT*/RunII*/AODSIM")
    assert recorder.requests == []


async def test_get_dataset_info_missing_raises(tmp_path: Path) -> None:
    payload = {"data": [], "nresults": 0}
    recorder = Recorder(lambda _r: httpx.Response(200, json=payload))
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await das.get_dataset_info(client, dataset="/NoSuch/Thing-v1/NONE")


async def test_search_401_maps_upstream(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(401, text="Unauthorized"))
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await das.search(client, query="dataset=/X/Y/Z")
