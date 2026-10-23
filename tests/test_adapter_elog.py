"""Fixture-driven contract tests for the CMS Online E-Log adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

from cmcps.adapters import elog
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

ELOG_SHELL = (
    '<html><body><a href="x__adfpwp_action_portlet=683379043">portal</a>'
    '<a href=".../viewSubcatMessages.do?catId=591&amp;subId=30">g</a>'
    '<a href=".../viewSubcatMessages.do?catId=591&amp;subId=30">dup</a>'
    '<a href=".../viewSubcatMessages.do?catId=0&amp;subId=495">c</a>'
    "</body></html>"
)

ROW = [
    "1347902",
    "DCS",
    "<a>x</a>",
    "<a>Starting of evening shift</a>",
    "central DCS - begin of shift",
    "THU 22.OCT.26 15:44:04",
    "<pre>magnet ramping; run ok</pre>",
]

JSON_PAYLOAD = {
    "iTotalRecords": 39470,
    "iDisplayStart": 0,
    "aaData": [ROW, ROW],
}

DETAIL_HTML = """<html><body>
<div id="content"><h1>Entry detail</h1>
<pre class="messageprefull">Detailed body text of the entry, with magnets.</pre>
<a href="/Elog/htdocs/data/495/1347902_1.png">attachment</a>
<a href="/Elog/htdocs/struts/download.jsp?mid=1347902&amp;cid=425690&amp;\
cont=image/jpeg&amp;fName=plot.jpg&amp;uid=862566">photo</a>
</div>
</body></html>"""


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


def _posted_fields(recorder: Recorder) -> dict[str, str]:
    body = recorder.requests[-1].content.decode()
    return dict(parse_qsl(body, keep_blank_values=True))


async def test_search_builds_datatable_post(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/Elog/json.do"
        assert request.method == "POST"
        return httpx.Response(200, json=JSON_PAYLOAD)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await elog.search(client, query="beam", limit=10)
    fields = _posted_fields(recorder)
    assert fields["p_text"] == "beam"
    assert fields["p_subId"] == ""
    assert fields["iDisplayLength"] == "10"
    assert fields["sEcho"] == "1"
    assert fields["p_imgLink"]
    assert fields["p_msgLink"]
    assert result.total_count == 39470
    assert result.size == 2
    first = result.items[0]
    assert first["msg_id"] == 1347902
    assert first["subject"] == "Starting of evening shift"
    assert first["section"] == "DCS"
    assert "magnet ramping" in first["excerpt"]


async def test_search_rejects_bad_input(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=JSON_PAYLOAD))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await elog.search(client, query="a\nb")
    with pytest.raises(InvalidInputError):
        await elog.search(client, query="x", limit=0)
    with pytest.raises(InvalidInputError):
        await elog.search(client, query="x", offset=-1)
    with pytest.raises(InvalidInputError):
        await elog.search(client, query="x", sub_id="7;drop")
    assert recorder.requests == []


async def test_search_empty_body_maps_upstream(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, content=b""))
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await elog.search(client, query="beam")


async def test_get_detail_dispatches_portlet(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "strutsAction=" in url:
            assert "viewMessageDetails.do" in url
            assert "msgId%3D1347902" in url
            return httpx.Response(200, text=DETAIL_HTML)
        return httpx.Response(200, text=ELOG_SHELL)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await elog.get_detail(client, msg_id=1347902, sub_id=495)
    assert result["msg_id"] == 1347902
    assert "Detailed body text" in result["text"]
    assert result["attachments"] == [
        "https://cmsonline.cern.ch/Elog/htdocs/data/495/1347902_1.png",
        "https://cmsonline.cern.ch/Elog/htdocs/struts/download.jsp"
        "?mid=1347902&cid=425690&cont=image/jpeg&fName=plot.jpg&uid=862566",
    ]


async def test_get_detail_rejects_bad_id(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=ELOG_SHELL))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await elog.get_detail(client, msg_id=0)
    assert recorder.requests == []


async def test_get_detail_saml_maps_upstream(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "strutsAction=" in url:
            return httpx.Response(200, text="<body>SAMLRequest</body>")
        return httpx.Response(200, text=ELOG_SHELL)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await elog.get_detail(client, msg_id=5)


async def test_get_detail_recovers_via_render_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "strutsAction=" in url:
            return httpx.Response(200, text="<body>SAMLRequest</body>")
        return httpx.Response(200, text=ELOG_SHELL)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    rendered_urls: list[str] = []

    async def fake_render(url: str, *, interact: Any = None) -> str:
        rendered_urls.append(url)
        assert interact is None
        return DETAIL_HTML

    monkeypatch.setattr(client, "render_page", fake_render)
    result = await elog.get_detail(client, msg_id=1347902, sub_id=495)
    assert result["msg_id"] == 1347902
    assert "Detailed body text" in result["text"]
    assert result["attachments"] == [
        "https://cmsonline.cern.ch/Elog/htdocs/data/495/1347902_1.png",
        "https://cmsonline.cern.ch/Elog/htdocs/struts/download.jsp"
        "?mid=1347902&cid=425690&cont=image/jpeg&fName=plot.jpg&uid=862566",
    ]
    assert len(rendered_urls) == 1
    assert "viewMessageDetails.do" in rendered_urls[0]


async def test_list_categories(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=ELOG_SHELL))
    client = make_client(tmp_path, recorder)
    result = await elog.list_categories(client)
    assert result["category_count"] == 2  # dedupe
    labels = {(i["cat_id"], i["sub_id"]) for i in result["items"]}
    assert (591, 30) in labels
    assert (0, 495) in labels


async def test_download_attachment_validates_url(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, content=b"x"))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await elog.download_attachment(
            client, attachment_url="https://evil.example.org/Elog/htdocs/data/x"
        )
    with pytest.raises(InvalidInputError):
        await elog.download_attachment(
            client, attachment_url="https://cmsonline.cern.ch/other/a.png"
        )
    assert recorder.requests == []


async def test_download_attachment_writes_file(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, content=b"png"))
    settings = Settings(state_dir=tmp_path, require_sso=False, browser_executable=None)
    auth = AuthService(settings)
    client = SafeHttpClient(
        settings,
        auth_service=auth,
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
    )
    result = await elog.download_attachment(
        client, attachment_url="https://cmsonline.cern.ch/Elog/htdocs/data/495/a.png"
    )
    assert Path(result["path"]).read_bytes() == b"png"
    assert Path(result["path"]).parent == settings.download_dir

    result = await elog.download_attachment(
        client,
        attachment_url=(
            "https://cmsonline.cern.ch/Elog/htdocs/struts/download.jsp"
            "?mid=1334353&cid=425697&cont=image/jpeg&fName=3567_tank.jpg&uid=862573"
        ),
    )
    assert Path(result["path"]).name == "3567_tank.jpg"
    assert Path(result["path"]).read_bytes() == b"png"
