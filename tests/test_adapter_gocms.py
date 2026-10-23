"""Fixture-driven contract tests for the goCMS shortlink adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cmcps.adapters import gocms
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

LINKS_PAYLOAD = {
    "items": [
        {
            "id": 29,
            "shortlink": "rctools",
            "target_url": "https://cms-rctools.web.cern.ch/rctools/",
            "description": "Run Coordination Tools",
            "created_by": "clange",
            "created_at": "2024-01-02T00:00:00",
            "updated_at": "2024-02-03T00:00:00",
            "tags": [{"name": "operations", "id": 3}],
        }
    ],
    "total": 29,
    "page": 2,
    "per_page": 10,
    "total_pages": 3,
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


async def test_list_links_envelope(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        assert params["search"] == "run"
        assert params["page"] == "2"
        assert params["per_page"] == "10"
        return httpx.Response(200, json=LINKS_PAYLOAD)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await gocms.list_links(client, search="run", page=2, per_page=10)
    assert result.total_count == 29
    assert result.has_more is True
    item = result.items[0]
    assert item["shortlink"] == "rctools"
    assert item["url"] == "https://gocms.cern.ch/rctools"
    assert item["tags"] == [{"name": "operations", "id": 3}]


async def test_list_links_rejects_bad_args(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={"items": []}))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await gocms.list_links(client, page=0)
    with pytest.raises(InvalidInputError):
        await gocms.list_links(client, per_page=0)
    with pytest.raises(InvalidInputError):
        await gocms.list_links(client, sort_order="sideways")
    assert recorder.requests == []


async def test_get_link(tmp_path: Path) -> None:
    link = dict(LINKS_PAYLOAD["items"][0])
    recorder = Recorder(lambda _r: httpx.Response(200, json=link))
    client = make_client(tmp_path, recorder)
    result = await gocms.get_link(client, link_id=29)
    assert result["id"] == 29
    assert result["target_url"].endswith("/rctools/")


async def test_get_link_404(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(404, json={"detail": "nf"}))
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await gocms.get_link(client, link_id=9999)


async def test_get_link_rejects_zero(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={}))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await gocms.get_link(client, link_id=0)
    assert recorder.requests == []


async def test_list_tags(tmp_path: Path) -> None:
    payload = [{"name": "operations", "id": 3}, {"name": "docs", "id": 4}]
    recorder = Recorder(lambda _r: httpx.Response(200, json=payload))
    client = make_client(tmp_path, recorder)
    result = await gocms.list_tags(client)
    assert result["tag_count"] == 2
    assert result["items"][0] == {"tag": "operations", "id": 3}
