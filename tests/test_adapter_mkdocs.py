"""Fixture-driven contract tests for the MkDocs adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cmcps.adapters import mkdocs
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

BASE = "https://docs.example.cern.ch"

INDEX = {
    "config": {"lang": ["en"]},
    "docs": [
        {
            "location": "trigger/commissioning",
            "title": "Trigger commissioning guide",
            "text": "Comprehensive guide to commissioning the trigger system. "
            "Includes runs, menus, and prescale tables.",
            "tags": ["trigger"],
        },
        {
            "location": "daq/overview",
            "title": "DAQ overview",
            "text": "DAQ reads data. Something something trigger.",
            "tags": [],
        },
        {"location": "home", "title": "Home", "text": "welcome page", "tags": []},
    ],
}

PAGE_HTML = """<!doctype html><html><head><title>Guide &ndash; Docs</title></head>
<body><h1>Trigger commissioning guide</h1>
<p>Overview. Run <code>cmsRun conf.py</code> and check <b>prescales</b>.</p>
<pre><code>$ cmsenv
$ edmPluginDump</code></pre>
</body></html>"""

LOGIN_HTML = b'<!DOCTYPE html><html class="login-pf"><body>Sign in to CERN</body></html>'


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


async def test_search_scores_title_hits(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search/search_index.json"
        return httpx.Response(200, json=INDEX)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await mkdocs.search(client, base_url=BASE, query="trigger guide")
    assert result.total_count == 2
    top = result.items[0]
    assert top["location"] == "trigger/commissioning"
    assert top["score"] > 0
    assert top["snippet"] and "trigger" in top["snippet"].lower()
    assert result.has_more is False


async def test_search_reports_sso_page(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, content=LOGIN_HTML))
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await mkdocs.search(client, base_url=BASE, query="x")


async def test_search_rejects_bad_input(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=INDEX))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await mkdocs.search(client, base_url="http://insecure.example.com", query="x")
    with pytest.raises(InvalidInputError):
        await mkdocs.search(client, base_url=BASE, query="  ")
    assert recorder.requests == []


async def test_list_pages_dedupes_and_filters(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=INDEX))
    client = make_client(tmp_path, recorder)
    all_pages = await mkdocs.list_pages(client, base_url=BASE)
    assert all_pages.total_count == 3
    filt = await mkdocs.list_pages(client, base_url=BASE, path_filter="daq")
    assert filt.total_count == 1
    assert filt.items[0]["location"] == "daq/overview"


async def test_get_page_strips_html(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=PAGE_HTML))
    client = make_client(tmp_path, recorder)
    result = await mkdocs.get_page(client, base_url=BASE, page="trigger/commissioning")
    assert result["title"] == "Guide " + chr(0x2013) + " Docs"
    text = result["text"]
    assert "<code>" not in text
    assert "cmsenv" in text
    assert "edmPluginDump" in text
    assert "prescales" in text


async def test_get_page_rejects_traversal(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=""))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await mkdocs.get_page(client, base_url=BASE, page="../secret")
    with pytest.raises(InvalidInputError):
        await mkdocs.get_page(client, base_url=BASE, page="https://evil.example/x")
    assert recorder.requests == []


async def test_get_page_sso_html(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, content=LOGIN_HTML))
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await mkdocs.get_page(client, base_url=BASE, page="")


async def test_download_rejects_bad_url(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, content=b"x"))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await mkdocs.download_file(client, file_url="not-a-url")
    assert recorder.requests == []


async def test_download_writes_file(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, content=b"png-bytes"))
    settings = Settings(state_dir=tmp_path, require_sso=False, browser_executable=None)
    auth = AuthService(settings)
    client = SafeHttpClient(
        settings,
        auth_service=auth,
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
    )
    result = await mkdocs.download_file(
        client, file_url="https://cms-l1t-correlator.docs.cern.ch/img/plot.png"
    )
    saved = Path(result["path"])
    assert saved.read_bytes() == b"png-bytes"
    assert saved.parent == settings.download_dir
