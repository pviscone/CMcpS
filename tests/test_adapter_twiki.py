"""Fixture-driven contract tests for the TWiki adapter."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from cmcps.adapters import twiki
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

TOPICLIST_HTML = """
<html><body>
<ul><li><a href="/twiki/bin/view/CMSPublic/WebHome">WebHome</a></li>
<li><a href="/twiki/bin/view/CMSPublic/PhysicsResults">PhysicsResults</a></li>
<li><a href="/twiki/bin/view/Main/WebHome">Main home</a></li>
<li><a href="/twiki/bin/view/CMSPublic/WebHome">WebHome dup</a></li></ul>
</body></html>
"""

PRINT_PAGE = """<html><head><script type="text/javascript">var junk=1;</script></head>
<body><div id="patternContent"><h1>My Topic</h1>
<p>Some <b>wiki</b> text about results.</p></div>
<!--twikiAttachments--><div><table><tr>
<td><a href="/twiki/pub/CMSPublic/MyTopic/plot.pdf">plot.pdf</a></td><td>1234</td></tr><tr>
<td><a href="/twiki/pub/CMSPublic/MyTopic/table.csv">table.csv</a></td><td>789</td></tr>
</table></div><!--//twikiAttachments-->
<hr /> This topic: <a class="twikiLink" href="/twiki/bin/view/CMSPublic/WebHome">WebHome</a>
<span>&gt;</span> MyTopic
<br /> Topic revision: r12 - 2026-09-21 - JohnDoe </body></html>
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


async def test_list_topics_filters_web_and_dedupes(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=TOPICLIST_HTML))
    _settings, client = make_client(tmp_path, recorder)
    result = await twiki.list_topics(client, web="CMSPublic")
    topics = [i["topic"] for i in result.items]
    assert topics == ["WebHome", "PhysicsResults"]  # Main.WebHome excluded, dup dropped


async def test_get_page_raw_parses_print_footer(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["cover"] == "print"
        return httpx.Response(200, text=PRINT_PAGE)

    recorder = Recorder(handler)
    _settings, client = make_client(tmp_path, recorder)
    result = await twiki.get_page(client, web="CMSPublic", topic="MyTopic")
    assert result["author"] == "JohnDoe"
    assert result["version"] == "r12"
    assert result["date_epoch"] == int(datetime(2026, 9, 21, tzinfo=UTC).timestamp())
    assert result["parent_topic"] == "WebHome"
    assert "wiki text about results" in result["text"]
    assert "var junk" not in result["text"]
    att = result["attachments"][0]
    assert att["name"] == "plot.pdf"
    assert att["url"] == "https://twiki.cern.ch/twiki/pub/CMSPublic/MyTopic/plot.pdf"
    assert [a["name"] for a in result["attachments"]] == ["plot.pdf", "table.csv"]


async def test_get_page_raw_omitted_when_absent_in_footer(tmp_path: Path) -> None:
    html = PRINT_PAGE.replace("Topic revision: r12 - 2026-09-21 - JohnDoe", "no footer here")
    recorder = Recorder(lambda _r: httpx.Response(200, text=html))
    _settings, client = make_client(tmp_path, recorder)
    result = await twiki.get_page(client, web="CMSPublic", topic="MyTopic")
    assert result["author"] is None
    assert result["version"] is None
    assert result["date_epoch"] is None


async def test_get_page_raises_on_error_status(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(500, text="boom"))
    _settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await twiki.get_page(client, web="CMSPublic", topic="SecretPage")


async def test_get_page_rendered_strips_html(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=PRINT_PAGE))
    _settings, client = make_client(tmp_path, recorder)
    result = await twiki.get_page(client, web="CMSPublic", topic="MyTopic", raw=False)
    assert "<h1>" not in result["text"]
    assert "wiki text about results" in result["text"]
    assert "author" not in result


async def test_get_page_rejects_bad_names(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=""))
    _settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await twiki.get_page(client, web="CMSPublic", topic="../escape")
    assert recorder.requests == []


async def test_get_page_accepts_subweb(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=PRINT_PAGE))
    _settings, client = make_client(tmp_path, recorder)
    result = await twiki.get_page(client, web="CMS/Internal", topic="PubGuidelines")
    assert recorder.requests[0].url.path == "/twiki/bin/view/CMS/Internal/PubGuidelines"
    assert recorder.requests[0].url.params["cover"] == "print"
    assert result["web"] == "CMS/Internal"


async def test_get_page_rejects_malformed_subweb(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=""))
    _settings, client = make_client(tmp_path, recorder)
    for web in ("cms/Internal", "CMS//Internal", "CMS/Internal/", "/CMS"):
        with pytest.raises(InvalidInputError):
            await twiki.get_page(client, web=web, topic="WebHome")
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
