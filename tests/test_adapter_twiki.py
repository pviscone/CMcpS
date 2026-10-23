"""Fixture-driven contract tests for the TWiki adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cmcps.adapters import twiki
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

SEARCH_HTML = """
<html><body>
<div class="twikiSearchResults">
<h3><a href="/twiki/bin/view/CMSPublic/Hig11024TWiki">Hig11024TWiki</a></h3>
<div class="twikiSearchResult"><a href="/twiki/bin/view/CMSPublic/Hig11024TWiki">
Hig11024TWiki</a> ... higgs search results ...</div>
<div class="twikiSearchResult"><a href="https://twiki.cern.ch/twiki/bin/view/CMSPublic/PhysicsResults">
PhysicsResults</a> ...</div>
</div>
</body></html>
"""

TOPICLIST_HTML = """
<html><body>
<ul><li><a href="/twiki/bin/view/CMSPublic/WebHome">WebHome</a></li>
<li><a href="/twiki/bin/view/CMSPublic/PhysicsResults">PhysicsResults</a></li>
<li><a href="/twiki/bin/view/Main/WebHome">Main home</a></li>
<li><a href="/twiki/bin/view/CMSPublic/WebHome">WebHome dup</a></li></ul>
</body></html>
"""

RAW_PAGE = """%META:TOPICINFO{author="JohnDoe" date="1700000000" format="1.1" version="1.12"}%
%META:TOPICPARENT{name="WebHome"}%
---+ My Topic
Some *wiki* text about $x^2$ results.
%META:FILEATTACHMENT\
{name="plot.pdf" attachment="plot.pdf" attr="" comment="main plot" date="1700000100"\
 size="123456" user="JohnDoe" version="1"}%
%META:FILEATTACHMENT\
{name="table.csv" attachment="table.csv" comment="" date="1700000110" size="789"\
 user="JaneDoe"}%
"""

RENDERED_PAGE = """<html><head><title> MyTopic &lt; CMSPublic &lt; TWiki</title></head>
<body><div class="twikiContent"><h1>My Topic</h1>
<p>Some <b>wiki</b> text about results.</p></div></body></html>
"""


class Recorder:
    """Mock-transport handler that records every request."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def make_client(tmp_path: Path, recorder: Recorder) -> tuple[Settings, SafeHttpClient]:
    settings = Settings(state_dir=tmp_path, require_sso=False, browser_executable=None)
    auth = AuthService(settings)
    transport = httpx.MockTransport(recorder)
    client = SafeHttpClient(
        settings, auth_service=auth, client=httpx.AsyncClient(transport=transport)
    )
    return settings, client


async def test_search_dedupes_links(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/twiki/bin/search/CMSPublic/"
        assert request.url.params["search"] == "higgs"
        assert request.url.params["type"] == "keyword"
        return httpx.Response(200, text=SEARCH_HTML)

    recorder = Recorder(handler)
    _settings, client = make_client(tmp_path, recorder)
    result = await twiki.search(client, web="CMSPublic", query="higgs")
    assert result.total_count == 2  # duplicate Hig11024TWiki collapsed
    assert result.items[0]["topic"] == "Hig11024TWiki"
    assert result.items[1]["url"].endswith("/view/CMSPublic/PhysicsResults")


async def test_search_rejects_bad_args(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=""))
    _settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await twiki.search(client, web="CMSPublic", query="   ")
    with pytest.raises(InvalidInputError):
        await twiki.search(client, web="lowerweb", query="x")
    with pytest.raises(InvalidInputError):
        await twiki.search(client, web="CMSPublic", query="x", search_type="fuzzy")
    with pytest.raises(InvalidInputError):
        await twiki.search(client, web="CMS,,CMSPublic", query="x")
    with pytest.raises(InvalidInputError):
        await twiki.search(client, web="CMS,CMS", query="x")
    with pytest.raises(InvalidInputError):
        await twiki.search(client, web="all", query="x")
    assert recorder.requests == []


async def test_search_multi_web_sends_web_list(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/twiki/bin/search/CMS/":
            assert request.url.params["web"] == "CMS,CMSPublic"
            assert request.url.params["limit"] == "5"
        else:
            assert request.url.path == "/twiki/bin/search/CMSPublic/"
            assert "web" not in request.url.params
        return httpx.Response(200, text=SEARCH_HTML)

    recorder = Recorder(handler)
    _settings, client = make_client(tmp_path, recorder)
    result = await twiki.search(client, web="CMS, CMSPublic", query="higgs", limit=5)
    assert result.query == "web=CMS,CMSPublic type=keyword: higgs"
    single = await twiki.search(client, web="CMSPublic", query="higgs")
    assert single.query == "web=CMSPublic type=keyword: higgs"


async def test_list_topics_filters_web_and_dedupes(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=TOPICLIST_HTML))
    _settings, client = make_client(tmp_path, recorder)
    result = await twiki.list_topics(client, web="CMSPublic")
    topics = [i["topic"] for i in result.items]
    assert topics == ["WebHome", "PhysicsResults"]  # Main.WebHome excluded, dup dropped


async def test_get_page_raw_parses_meta(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["raw"] == "on"
        return httpx.Response(200, content=RAW_PAGE.encode())

    recorder = Recorder(handler)
    _settings, client = make_client(tmp_path, recorder)
    result = await twiki.get_page(client, web="CMSPublic", topic="MyTopic")
    assert result["author"] == "JohnDoe"
    assert result["version"] == "1.12"
    assert result["date_epoch"] == 1700000000
    assert result["parent_topic"] == "WebHome"
    assert result["raw"].startswith("---+ My Topic")
    assert "%META:" not in result["raw"]
    att = result["attachments"][0]
    assert att["name"] == "plot.pdf"
    assert att["size_bytes"] == 123456
    assert att["url"].endswith("/twiki/pub/CMSPublic/MyTopic/plot.pdf")
    assert result["attachments"][1]["comment"] == ""


async def test_get_page_raw_refuses_html(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text="<html><body>login</body></html>"))
    _settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await twiki.get_page(client, web="CMSPublic", topic="SecretPage")


async def test_get_page_rendered_strips_html(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=RENDERED_PAGE))
    _settings, client = make_client(tmp_path, recorder)
    result = await twiki.get_page(client, web="CMSPublic", topic="MyTopic", raw=False)
    assert "<h1>" not in result["text"]
    assert "wiki text about results" in result["text"]


async def test_get_page_rejects_bad_names(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=""))
    _settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await twiki.get_page(client, web="CMSPublic", topic="../escape")
    assert recorder.requests == []


async def test_download_rejects_non_pub_url(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, content=b"x"))
    _settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await twiki.download_attachment(
            client, attachment_url="https://twiki.cern.ch/twiki/bin/view/X/Y"
        )
    assert recorder.requests == []


async def test_download_writes_file(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, content=b"pdf-bytes"))
    settings, client = make_client(tmp_path, recorder)
    result = await twiki.download_attachment(
        client, attachment_url="https://twiki.cern.ch/twiki/pub/CMSPublic/My/plot.pdf"
    )
    assert Path(result["path"]).read_bytes() == b"pdf-bytes"
    assert Path(result["path"]).parent == settings.download_dir
