"""Fixture-driven contract tests for the CMS HyperNews archive adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from cmcps.adapters import hypernews
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

GET = "https://hypernews.cern.ch/HyperNews/CMS/get"

# Faithful excerpts of real hypernews.cern.ch pages (2026-10-26 capture),
# trimmed to the parsed regions; tag layout and nesting kept.

INDEX_PAGE = """<html><body>
<b><a href="http://hypernews.cern.ch/HyperNews/CMS/get/osg-tier3.html"
>OSG Tier3 Discussions</a> </b> (2023-07-04)<br>
<b><a href="http://hypernews.cern.ch/HyperNews/CMS/get/calibrations.html"
>Alignment/Calibration Discussions</a> </b> (2023-06-19)<br>
<b><a href="http://hypernews.cern.ch/HyperNews/CMS/get/luminosity.html"
>Luminosity Discussions</a> </b> (2023-02-07)<br>
<b><a href="http://hypernews.cern.ch/HyperNews/CMS/get/hnTest.html"
>HyperNews Test Forum</a> </b> (2022-06-13)<br>
</body></html>
"""

CINDEX_PAGE = """<html><body>
<div class="mycategory">
    <div class="categoryheader"><a name="104"></a>Category: BRIL</div>
    <div class="categoryforum"><a class="catlink"
href="http://hypernews.cern.ch/HyperNews/CMS/get/luminosity.html"
>Luminosity Discussions</a></div>
</div>
<div class="mycategory">
    <div class="categoryheader"><a name="57"></a>Category: Calibration and Alignment</div>
    <div class="categoryforum"><a class="catlink"
href="http://hypernews.cern.ch/HyperNews/CMS/get/calibrations.html"
>Alignment/Calibration Discussions</a></div>
</div>
</body></html>
"""

SEARCH_PAGE = """<html><body>
<p>
 Displaying results for: &#39;laser&#39; (max 50 per page, page 1) (took 9.607s)
</p>
<div class="searchresult">
 <a href="/HyperNews/CMS/get//ecal-calibration/127"
 >EE scale, asymmetry and Zee resolution with V5/V6 laser tags.</a>
 2011-05-06 09:07:08
 - by <a href="/HyperNews/CMS/view-member.pl?responses=ric">ric</a> <br />
 <p>
 ... with <mark>laser</mark> V6 no VPT we recover 2010 resolution.
 </p>
</div>
<div class="searchresult">
 <a href="/HyperNews/CMS/get//mip-timing-detector/276/1"
 >Re: MTD ETL conference opportunities - deadline Mon Aug 17</a>
 2020-08-16 20:35:07 - by
 <a href="/HyperNews/CMS/view-member.pl?responses=karlis.dreimanis@cern.ch"
 >karlis.dreimanis@cern.ch</a> <br />
 <p>
 ... a new, dedicated MIP <mark>timing</mark> detector.
 </p>
</div>
</body></html>
"""

SEARCH_EMPTY_PAGE = """<html><body>
<p>
 Displaying results for: &#39;zzzz-no-such-thing&#39; (max 50 per page, page 1) (took 0.912s)
</p>
</body></html>
"""

FORUM_PAGE = """<html><head><title>HyperNews Test Forum</title></head><body>
<ul class="breadcrumb">
    <li><a href="/HyperNews/CMS/get/hnTest">hnTest</a></li>
</ul>
<br />
<div>
 Forum body text.
</div>
<hr size="5">
<div class="listing">
    <ol>
        <li value="789">
            <img src="/HyperNews/CMS/Icons/hnsmall.gif" alt="None:" width="15"
 height="15" align="texttop">
            <a href="/HyperNews/CMS/get//hnTest/789">
                Test message from lxplus
            </a>
            <em>(Artur Lobanov - Jun 13, 2022)</em></li>
        <li value="788">
            <img src="/HyperNews/CMS/Icons/hnsmall.gif" alt="None:" width="15"
 height="15" align="texttop">
            <a href="/HyperNews/CMS/get//hnTest/788">
                Test message 2021-12-08 07:08:52.430661
            </a>
            <em>(Tier0 Replay - Dec 8, 2021)</em><ul>
                <li> 1 reply </li>
            </ul></li>
        <li value="787">
            <img src="/HyperNews/CMS/Icons/hnsmall.gif" alt="None:" width="15"
 height="15" align="texttop">
            <a href="/HyperNews/CMS/get//hnTest/787">
                Test
            </a>
            <em>(Saskia Falke - Nov 3, 2021)</em></li>
    </ol>
</div>
</body></html>
"""

MESSAGE_PAGE = """<html><head><title>test message</title></head><body>
<ul class="breadcrumb">
    <li><a href="/HyperNews/CMS/get/hnTest">hnTest</a></li>
    <li><a href="/HyperNews/CMS/get/hnTest/1">1</a></li>
</ul>
<table><tbody><tr><td><nobr>
 <a href="http://hypernews.cern.ch/HyperNews/CMS/get/hnTest/1.html?dir=next-in-thread"
 class="commandlink">&nbsp;Next-in-Thread</a>
</nobr></td><td><nobr>
 <a href="http://hypernews.cern.ch/HyperNews/CMS/get/hnTest/1.html?dir=nextResponse"
 class="commandlink">&nbsp;Next-in-Forum</a>
</nobr></td></tr></tbody></table>
<!-- start header -->
<h2><img src="/HyperNews/CMS/Icons/hnsmall.gif" alt="None" width="15"
 height="15" align="texttop" border="0">test message</h2>
<dl>
    <dd>
        <nobr>
            <font size="+1">
                <i>Forum:</i>
                <a href="/HyperNews/CMS/get//hnTest">
                    HyperNews Test Forum</a>
                <br>
            </font>
            <i>Date:</i> 05 Dec, 2005 <br>
            <i>From:</i>
            <a href="/view-member.pl?elmer"><i></i>Peter Elmer</i>
                &lt;elmer&gt;</a><br>
        </nobr>
    </dd>
</dl>
<!--  start message body  -->
<table border="" cellpadding="5" cellspacing="0" width="100%" bgcolor="#FFFFFF">
    <tbody>
        <tr>
            <td>
                test with -slac-28
<P>
                see <a href="get/AUX/2005/12/05/01:55:48-94204-notes.pdf">the attachment</a>
<PRE>
                Attachment:
                   <A HREF="https://hypernews.cern.ch/HyperNews/CMS/get/AUX/2005/12/05/report.ps"
                   >https://hypernews.cern.ch/HyperNews/CMS/get/AUX/2005/12/05/report.ps</A>
</PRE>
            </td>
        </tr>
    </tbody>
</table>
<!--  end message body -->
<div class="listing">
    <ol>
        <li value="1">
            <img src="/HyperNews/CMS/Icons/hnsmall.gif" alt="None:" width="15"
 height="15" align="texttop">
            <a href="/HyperNews/CMS/get//hnTest/1/1">
                Re: test message
            </a>
            <em>(Robert Clare - Dec 9, 2005)</em></li>
    </ol>
</div>
<details>
    <summary>Click to see raw info</summary>
    <pre>URCMessage(...)</pre>
</details>
</body></html>
"""

MESSAGE_HTML_BODY_PAGE = """<html><head><title>html message</title></head><body>
<!-- start header -->
<h2><img src="/HyperNews/CMS/Icons/hnsmall.gif" alt="None" width="15"
 height="15" align="texttop" border="0">html message</h2>
<dl>
    <dd><nobr>
        <font size="+1"><i>Forum:</i>
 <a href="/HyperNews/CMS/get//hnTest">HyperNews Test Forum</a><br></font>
        <i>Date:</i> 10 Dec, 2005 <br>
        <i>From:</i> <a href="/view-member.pl?elmer">Peter Elmer&lt;elmer&gt;</a><br>
    </nobr></dd>
</dl>
<div>
    <b>Rich HTML body</b> with a link to
 <a href="https://hypernews.cern.ch/HyperNews/CMS/get/AUX/2005/12/10/plot.png"
 >a plot</a>.
</div>
<details><summary>Click to see raw info</summary><pre>URCMessage(...)</pre></details>
</body></html>
"""

MESSAGE_MISSING_PAGE = "Unable to find message: hnTest/99999 at /data/hnfiles"

FORUM_MISSING_PAGE = """<!doctype html>
<html lang=en>
<title>500 Internal Server Error</title>
<h1>Internal Server Error</h1>
"""

MEMBER_PAGE = """<html><head><title>Member: Riccardo Paramatti </title></head><body>
user_id: ric<br>
name: Riccardo Paramatti<br>
user_url: http://cern.ch/ric.html<br>
email: riccardo.paramatti@cern.ch<br>
email2: <br>
content: Everything<br>
status: registered<br>
</body></html>
"""

MEMBER_MISSING_PAGE = """<html><head><title>Member: </title></head><body>
</body></html>
"""


class Recorder:
    """Mock-transport handler that records every request."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def make_client(sandbox: Path, recorder: Recorder) -> SafeHttpClient:
    settings = Settings(state_dir=sandbox, require_sso=False, browser_executable=None)
    auth = AuthService(settings)
    transport = httpx.MockTransport(recorder)
    return SafeHttpClient(
        settings, auth_service=auth, client=httpx.AsyncClient(transport=transport)
    )


def _query(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(urlsplit(str(request.url)).query, keep_blank_values=True)


_STATIC_PAGES: tuple[tuple[str, int, str], ...] = (
    ("/CMS/index", 200, INDEX_PAGE),
    ("/CMS/cindex", 200, CINDEX_PAGE),
    (f"{GET}/hnTest/1", 200, MESSAGE_PAGE),
    (f"{GET}/hnTest/2", 200, MESSAGE_HTML_BODY_PAGE),
    (f"{GET}/hnTest/99999", 200, MESSAGE_MISSING_PAGE),
    (f"{GET}/hnTest", 200, FORUM_PAGE),
    (f"{GET}/no-such-forum", 500, FORUM_MISSING_PAGE),
)


def _search_page(request: httpx.Request) -> httpx.Response:
    q = _query(request).get("query", [""])[0]
    page = SEARCH_EMPTY_PAGE if "no-such-thing" in q else SEARCH_PAGE
    return httpx.Response(200, content=page.encode())


def _member_page(request: httpx.Request) -> httpx.Response:
    page = MEMBER_PAGE if "ric" in _query(request) else MEMBER_MISSING_PAGE
    return httpx.Response(200, content=page.encode())


def page_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "/CMS/search" in url:
        return _search_page(request)
    if "view-member.pl" in url:
        return _member_page(request)
    if "/get/AUX/" in url:
        return httpx.Response(200, content=b"%PDF-1.7 attachment bytes")
    for suffix, status, page in _STATIC_PAGES:
        if url.endswith(suffix):
            return httpx.Response(status, content=page.encode())
    return httpx.Response(404, content=b"nope")


# --- search ---


async def test_search_parses_results(tmp_path: Path) -> None:
    recorder = Recorder(page_handler)
    client = make_client(tmp_path, recorder)
    result = await hypernews.search_messages(client, query="laser")
    assert recorder.requests[0].url.path == "/HyperNews/CMS/search"
    query = _query(recorder.requests[0])
    assert query["query"] == ["laser"]
    assert query["page"] == ["1"]
    assert "start" not in query and "stop" not in query

    assert result.total_count is None
    assert len(result.items) == 2
    first = result.items[0]
    assert first["forum"] == "ecal-calibration"
    assert first["message"] == "127"
    assert first["title"].startswith("EE scale, asymmetry")
    assert first["posted"] == "2011-05-06 09:07:08"
    assert first["author_username"] == "ric"
    assert "laser V6" in first["snippet"]
    assert first["url"] == f"{GET}/ecal-calibration/127.html"
    second = result.items[1]
    assert second["forum"] == "mip-timing-detector"
    assert second["message"] == "276/1"
    assert second["author_username"] == "karlis.dreimanis@cern.ch"
    assert "discontinued" in (result.note or "")


async def test_search_date_range_params(tmp_path: Path) -> None:
    recorder = Recorder(page_handler)
    client = make_client(tmp_path, recorder)
    await hypernews.search_messages(
        client, query="laser", start="2019-01-01", stop="2020-12-31", page=3
    )
    query = _query(recorder.requests[0])
    assert query["start"] == ["2019-01-01"]
    assert query["stop"] == ["2020-12-31"]
    assert query["page"] == ["3"]


async def test_search_empty_result_page(tmp_path: Path) -> None:
    client = make_client(tmp_path, Recorder(page_handler))
    result = await hypernews.search_messages(client, query="zzzz-no-such-thing")
    assert result.items == []
    assert result.has_more is False


@pytest.mark.parametrize("bad", ["2019-13-01", "01 Jan 2019", "20190101"])
async def test_search_rejects_bad_dates(tmp_path: Path, bad: str) -> None:
    client = make_client(tmp_path, Recorder(page_handler))
    with pytest.raises(InvalidInputError):
        await hypernews.search_messages(client, query="laser", start=bad)


async def test_search_rejects_empty_query(tmp_path: Path) -> None:
    client = make_client(tmp_path, Recorder(page_handler))
    with pytest.raises(InvalidInputError):
        await hypernews.search_messages(client, query="   ")


# --- forum listing ---


async def test_list_forums_parses_index(tmp_path: Path) -> None:
    recorder = Recorder(page_handler)
    client = make_client(tmp_path, recorder)
    result = await hypernews.list_forums(client)
    assert recorder.requests[0].url.path == "/HyperNews/CMS/index"
    names = [item["forum"] for item in result.items]
    assert names == ["osg-tier3", "calibrations", "luminosity", "hnTest"]
    assert result.items[0]["title"] == "OSG Tier3 Discussions"
    assert result.items[0]["last_activity"] == "2023-07-04"
    assert result.items[3]["url"] == f"{GET}/hnTest.html"
    assert result.total_count == 4
    assert result.has_more is False


async def test_list_forums_category_filter_uses_cindex(tmp_path: Path) -> None:
    recorder = Recorder(page_handler)
    client = make_client(tmp_path, recorder)
    result = await hypernews.list_forums(client, category="bril")
    assert {r.url.path for r in recorder.requests} == {
        "/HyperNews/CMS/index",
        "/HyperNews/CMS/cindex",
    }
    assert [item["forum"] for item in result.items] == ["luminosity"]
    assert result.items[0]["category"] == "BRIL"


async def test_list_forums_title_filter_and_limit(tmp_path: Path) -> None:
    recorder = Recorder(page_handler)
    client = make_client(tmp_path, recorder)
    result = await hypernews.list_forums(client, title_contains="DISCUSSIONS", limit=2)
    assert [item["forum"] for item in result.items] == ["osg-tier3", "calibrations"]
    assert result.total_count == 3
    assert result.has_more is True


async def test_list_forums_rejects_bad_limit(tmp_path: Path) -> None:
    client = make_client(tmp_path, Recorder(page_handler))
    with pytest.raises(InvalidInputError):
        await hypernews.list_forums(client, limit=0)


# --- forum threads ---


async def test_get_forum_lists_threads(tmp_path: Path) -> None:
    recorder = Recorder(page_handler)
    client = make_client(tmp_path, recorder)
    result = await hypernews.get_forum(client, forum="hnTest")
    assert recorder.requests[0].url.path == "/HyperNews/CMS/get/hnTest"
    assert result["title"] == "HyperNews Test Forum"
    assert result["thread_count"] == 3
    assert result["truncated"] is False
    first = result["threads"][0]
    assert first["num"] == 789
    assert first["message"] == "789"
    assert first["title"] == "Test message from lxplus"
    assert first["author"] == "Artur Lobanov"
    assert first["date"] == "Jun 13, 2022"
    assert "replies" not in first
    second = result["threads"][1]
    assert second["replies"] == 1
    assert second["url"] == f"{GET}/hnTest/788.html"


async def test_get_forum_limit_truncates_but_counts_all(tmp_path: Path) -> None:
    client = make_client(tmp_path, Recorder(page_handler))
    result = await hypernews.get_forum(client, forum="hnTest", limit=2)
    assert [t["num"] for t in result["threads"]] == [789, 788]
    assert result["thread_count"] == 3
    assert result["truncated"] is True


async def test_get_forum_rejects_bad_limit(tmp_path: Path) -> None:
    client = make_client(tmp_path, Recorder(page_handler))
    with pytest.raises(InvalidInputError):
        await hypernews.get_forum(client, forum="hnTest", limit=0)


async def test_get_forum_missing_forum_is_upstream_error(tmp_path: Path) -> None:
    client = make_client(tmp_path, Recorder(page_handler))
    with pytest.raises(UpstreamError) as excinfo:
        await hypernews.get_forum(client, forum="no-such-forum")
    assert excinfo.value.status_code == 500


async def test_get_forum_rejects_bad_name(tmp_path: Path) -> None:
    client = make_client(tmp_path, Recorder(page_handler))
    with pytest.raises(InvalidInputError):
        await hypernews.get_forum(client, forum="../etc")


# --- message detail ---


async def test_get_message_parses_header_body_replies(tmp_path: Path) -> None:
    recorder = Recorder(page_handler)
    client = make_client(tmp_path, recorder)
    result = await hypernews.get_message(client, forum="hnTest", path="1")
    assert recorder.requests[0].url.path == "/HyperNews/CMS/get/hnTest/1"
    assert result["title"] == "test message"
    assert result["forum_title"] == "HyperNews Test Forum"
    assert result["date"] == "05 Dec, 2005"
    assert result["author_name"] == "Peter Elmer"
    assert result["author_username"] == "elmer"
    assert "test with -slac-28" in result["text"]
    assert result["attachments"] == [
        f"{GET}/AUX/2005/12/05/01:55:48-94204-notes.pdf",
        f"{GET}/AUX/2005/12/05/report.ps",  # uppercase HREF in the archived body
    ]
    assert result["replies"][0]["message"] == "1/1"
    assert result["replies"][0]["title"] == "Re: test message"
    assert result["replies"][0]["author"] == "Robert Clare"
    assert result["has_next_in_thread"] is True
    assert result["has_next_in_forum"] is True
    assert result["url"] == f"{GET}/hnTest/1.html"


async def test_get_message_html_body_without_markers(tmp_path: Path) -> None:
    client = make_client(tmp_path, Recorder(page_handler))
    result = await hypernews.get_message(client, forum="hnTest", path="2")
    assert result["title"] == "html message"
    assert "Rich HTML body" in result["text"]
    assert result["attachments"] == [f"{GET}/AUX/2005/12/10/plot.png"]
    assert result["replies"] == []
    assert result["has_next_in_thread"] is False


async def test_get_message_missing_raises_404(tmp_path: Path) -> None:
    client = make_client(tmp_path, Recorder(page_handler))
    with pytest.raises(UpstreamError) as excinfo:
        await hypernews.get_message(client, forum="hnTest", path="99999")
    assert excinfo.value.status_code == 404
    assert excinfo.value.retryable is False


async def test_get_message_rejects_bad_path(tmp_path: Path) -> None:
    client = make_client(tmp_path, Recorder(page_handler))
    with pytest.raises(InvalidInputError):
        await hypernews.get_message(client, forum="hnTest", path="1;drop")


# --- member profile ---


async def test_get_member_parses_profile(tmp_path: Path) -> None:
    recorder = Recorder(page_handler)
    client = make_client(tmp_path, recorder)
    result = await hypernews.get_member(client, username="ric")
    # The route reads the first query *key*: the username is the parameter name.
    assert _query(recorder.requests[0]) == {"ric": [""]}
    assert result["username"] == "ric"
    assert result["name"] == "Riccardo Paramatti"
    assert result["email"] == "riccardo.paramatti@cern.ch"
    assert result["user_url"] == "http://cern.ch/ric.html"
    assert result["status"] == "registered"
    assert result["fields"]["content"] == "Everything"


async def test_get_member_missing_raises(tmp_path: Path) -> None:
    client = make_client(tmp_path, Recorder(page_handler))
    with pytest.raises(UpstreamError) as excinfo:
        await hypernews.get_member(client, username="ghost")
    assert excinfo.value.status_code == 404


async def test_get_member_rejects_bad_username(tmp_path: Path) -> None:
    client = make_client(tmp_path, Recorder(page_handler))
    with pytest.raises(InvalidInputError):
        await hypernews.get_member(client, username="a b?")


# --- attachment download ---


async def test_download_file_fetches_aux(tmp_path: Path) -> None:
    recorder = Recorder(page_handler)
    client = make_client(tmp_path, recorder)
    result = await hypernews.download_file(
        client,
        download_url="https://hypernews.cern.ch/HyperNews/CMS/get/AUX/2005/12/05/notes.pdf",
    )
    assert recorder.requests[0].url.path.endswith("/get/AUX/2005/12/05/notes.pdf")
    assert result["size_bytes"] == len(b"%PDF-1.7 attachment bytes")
    assert Path(result["path"]).is_file()


@pytest.mark.parametrize(
    "url",
    [
        "https://hypernews.cern.ch/HyperNews/CMS/get/hnTest/1",
        "https://other.example/get/AUX/x.pdf",
        "not-a-url",
    ],
)
async def test_download_file_rejects_non_aux_urls(tmp_path: Path, url: str) -> None:
    client = make_client(tmp_path, Recorder(page_handler))
    with pytest.raises(InvalidInputError):
        await hypernews.download_file(client, download_url=url)
