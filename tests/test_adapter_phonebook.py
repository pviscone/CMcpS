"""Fixture-driven contract tests for the CERN Phonebook adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cmcps.adapters import phonebook
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

TEST_TOKEN = "unit-test-token"  # noqa: S105 - mock credential, not a real secret

PERSON = {
    "type": "PERSON",
    "name": "Steffen DOEBERT",
    "firstName": "Steffen",
    "lastName": "DOEBERT",
    "email": "steffen.doebert@cern.ch",
    "organicUnit": "SY-RF-MKS",
    "location": "18/2-008",
    "phone1": {"displayNumber": "72574", "fullNumber": "+41227672574"},
    "phone2": None,
    "mobilePhone": {"displayNumber": "164045", "fullNumber": "+41754114045"},
    "postBox": "E34220",
    "preferredLanguage": "EN",
    "snowId": None,
}
SERVICE = {
    "type": "SERVICE",
    "name": "Security Service",
    "firstName": None,
    "lastName": None,
    "email": "sites-security@cern.ch",
    "organicUnit": None,
    "location": None,
    "phone1": None,
    "phone2": None,
    "mobilePhone": None,
    "postBox": None,
    "preferredLanguage": None,
    "snowId": "b068a78df06cce80c7133d85b5657c30",
}
PAGE = {
    "content": [PERSON, SERVICE],
    "empty": False,
    "first": True,
    "last": True,
    "number": 0,
    "numberOfElements": 2,
    "size": 10,
    "totalElements": 2,
    "totalPages": 1,
}


class Recorder:
    """Mock-transport handler that records every request."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def make_client(
    tmp_path: Path, recorder: Recorder, *, token: str | None = TEST_TOKEN
) -> tuple[Settings, SafeHttpClient]:
    settings = Settings(
        state_dir=tmp_path,
        require_sso=False,
        browser_executable=None,
        phonebook_token=token,
    )
    auth = AuthService(settings)
    transport = httpx.MockTransport(recorder)
    client = SafeHttpClient(
        settings, auth_service=auth, client=httpx.AsyncClient(transport=transport)
    )
    return settings, client


async def test_search_hits_api_with_bearer_and_zero_based_page(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == f"Bearer {TEST_TOKEN}"
        assert request.url.path == "/api/search"
        assert request.url.params["query"] == "doe"
        assert request.url.params["page"] == "1"  # 1-based tool arg → 0-based backend
        return httpx.Response(200, json={**PAGE, "number": 1, "first": False, "last": True})

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await phonebook.search(client, settings, query="doe", page=2)
    assert result.source == "phonebook"
    assert result.page == 2
    assert result.size == 10
    assert result.total_count == 2
    assert result.has_more is False
    assert result.query == "doe"
    assert result.items[0]["type"] == "PERSON"
    assert result.items[1]["snowId"] == "b068a78df06cce80c7133d85b5657c30"


async def test_search_has_more_when_not_last(tmp_path: Path) -> None:
    page = {**PAGE, "last": False}
    recorder = Recorder(lambda _r: httpx.Response(200, json=page))
    settings, client = make_client(tmp_path, recorder)
    result = await phonebook.search(client, settings, query="doe")
    assert result.has_more is True


async def test_search_missing_last_flag_means_done(tmp_path: Path) -> None:
    page = {k: v for k, v in PAGE.items() if k != "last"}
    recorder = Recorder(lambda _r: httpx.Response(200, json=page))
    settings, client = make_client(tmp_path, recorder)
    result = await phonebook.search(client, settings, query="doe")
    assert result.has_more is False


async def test_search_rejects_empty_query_without_request(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await phonebook.search(client, settings, query="   ")
    assert recorder.requests == []


async def test_search_rejects_page_zero_without_request(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await phonebook.search(client, settings, query="doe", page=0)
    assert recorder.requests == []


async def test_missing_token_rejected_without_request(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={}))
    settings, client = make_client(tmp_path, recorder, token=None)
    with pytest.raises(InvalidInputError, match="Bearer token"):
        await phonebook.search(client, settings, query="doe")
    assert recorder.requests == []


async def test_401_maps_to_helpful_upstream(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(401, json={}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await phonebook.search(client, settings, query="doe")


async def test_search_rejects_non_page_payload(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={"unexpected": True}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError, match="unexpected search payload"):
        await phonebook.search(client, settings, query="doe")


async def test_last_update(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == f"Bearer {TEST_TOKEN}"
        assert request.url.path == "/api/search/lastUpdate"
        return httpx.Response(200, json={"lastUpdate": "2026-10-26T12:00:39.376Z"})

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await phonebook.last_update(client, settings)
    assert result == {"source": "phonebook", "last_update": "2026-10-26T12:00:39.376Z"}


async def test_last_update_rejects_bad_payload(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={"lastUpdate": 42}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError, match="unexpected lastUpdate payload"):
        await phonebook.last_update(client, settings)
