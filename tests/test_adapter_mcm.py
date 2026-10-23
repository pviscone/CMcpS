"""Fixture-driven contract tests for the McM adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cmcps.adapters import mcm
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

REQ_DOC = {
    "prepid": "B2G-RunIISummer20UL17wmLHEGEN-00001",
    "approval": "submit",
    "status": "done",
    "pwg": "B2G",
    "member_of_campaign": "RunIISummer20UL17wmLHEGEN",
    "dataset_name": "TT_Mtt-700to1000_TuneCP5_13TeV-powheg-pythia8",
    "total_events": 100,
    "completed_events": 100,
    "history": [{"action": "created"}] * 25,
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


async def test_get_request_full_doc(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/requests/get/B2G-RunIISummer20UL17wmLHEGEN-00001")
        return httpx.Response(200, json={"results": REQ_DOC})

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await mcm.get_request(client, request_id="B2G-RunIISummer20UL17wmLHEGEN-00001")
    assert result["request_id"] == "B2G-RunIISummer20UL17wmLHEGEN-00001"
    assert result["status"] == "done"
    assert result["dataset_name"] == "TT_Mtt-700to1000_TuneCP5_13TeV-powheg-pythia8"
    assert len(result["history"]) == mcm._HISTORY_KEEP
    assert result["history_truncated"] is True


async def test_get_request_empty_results_raises(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={"results": {}}))
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await mcm.get_request(client, request_id="BOGUS-00001")


async def test_get_request_rejects_bad_id(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={}))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await mcm.get_request(client, request_id="bad id with spaces")
    assert recorder.requests == []


async def test_status_and_approval_split(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={"B2G-00001": "submit-done"}))
    client = make_client(tmp_path, recorder)
    result = await mcm.get_status_and_approval(client, prepid="B2G-00001")
    assert result["approval"] == "submit"
    assert result["status"] == "done"
    assert result["approval_status"] == "submit-done"


async def test_status_missing_raises(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={}))
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await mcm.get_status_and_approval(client, prepid="B2G-00001")


async def test_output_datasets(tmp_path: Path) -> None:
    recorder = Recorder(
        lambda _r: httpx.Response(200, json={"B2G-00001": ["/TT/x/AODSIM", "/TT/y/MINIAODSIM"]})
    )
    client = make_client(tmp_path, recorder)
    result = await mcm.get_output_datasets(client, prepid="B2G-00001")
    assert result["dataset_count"] == 2
    assert result["datasets"][0] == "/TT/x/AODSIM"


async def test_produces_strips_slash_and_wraps(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/requests/produces/TT/bar/AODSIM")
        return httpx.Response(200, json={"results": REQ_DOC})

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await mcm.find_producing_requests(client, dataset="/TT/bar/AODSIM/")
    assert result.total_count == 1
    assert result.items[0]["prepid"] == "B2G-RunIISummer20UL17wmLHEGEN-00001"


async def test_produces_empty_is_no_results(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={"results": {}}))
    client = make_client(tmp_path, recorder)
    result = await mcm.find_producing_requests(client, dataset="/TT/x/NONE")
    assert result.total_count == 0
    assert result.items == []


async def test_adapter_calls_need_no_sso_session(tmp_path: Path) -> None:
    """McM hits a public API: all four calls must work with require_sso on and no session."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/requests/get_status_and_approval/" in path:
            return httpx.Response(200, json={"B2G-00001": "submit-done"})
        if "/requests/output/" in path:
            return httpx.Response(200, json={"B2G-00001": ["/TT/x/AODSIM"]})
        if "/requests/produces/" in path:
            return httpx.Response(200, json={"results": REQ_DOC})
        return httpx.Response(200, json={"results": REQ_DOC})

    recorder = Recorder(handler)
    settings = Settings(state_dir=tmp_path, require_sso=True, browser_executable=None)
    auth = AuthService(settings)
    client = SafeHttpClient(
        settings,
        auth_service=auth,
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
    )
    request = await mcm.get_request(client, request_id="B2G-00001")
    assert request["request_id"] == "B2G-RunIISummer20UL17wmLHEGEN-00001"
    status = await mcm.get_status_and_approval(client, prepid="B2G-00001")
    assert status["approval_status"] == "submit-done"
    output = await mcm.get_output_datasets(client, prepid="B2G-00001")
    assert output["datasets"] == ["/TT/x/AODSIM"]
    producing = await mcm.find_producing_requests(client, dataset="/TT/bar/AODSIM")
    assert producing.total_count == 1
    assert len(recorder.requests) == 4
    assert all("Cookie" not in r.headers for r in recorder.requests)


async def test_produces_rejects_malformed_dataset(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={}))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await mcm.find_producing_requests(client, dataset="NoSlashes")
    assert recorder.requests == []
