"""Fixture-driven contract tests for the CMS DocDB adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from cmcps.adapters import docdb
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

DOCDB = "https://cms-docdb.cern.ch/cgi-bin/DocDB"

# Faithful excerpts of real CMS DocDB pages (attribute and row layout kept),
# wrapped at attribute boundaries — the parsers tolerate the extra whitespace.
SHOW_PAGE = f"""<html><head>
<title>CMS-doc-14372-v7: Limited Authorship Guidelines Group (LAGG) Document</title>
</head><body>
<div id="RevisionInfo">
<div id="Header3Col"><div id="DocTitle">
<h1>Limited Authorship Guidelines Group &#x28;LAGG) Document</h1>
</div></div>
<div id="LeftColumn3Col"><div id="BasicDocInfo"><dl>
<dt>Document #:</dt><dd>CMS-doc-14372-v7</dd>
<dt>Document type:</dt><dd><a href="{DOCDB}/ListBy?typeid=3">Note</a></dd>
<dt>Submitted by:</dt>
<dd><a href="{DOCDB}/ListBy?authorid=226"
title="European Organization for Nuclear Research">David Barney</a></dd>
<dt>Updated by:</dt>
<dd><a href="{DOCDB}/ListBy?authorid=1634"
title="Florida State Univ.">Harrison Prosper</a></dd>
<dt>Document Created:</dt> <dd>12 Jan 2022, 21:14</dd>
<dt>Contents Revised:</dt> <dd>06 May 2022, 16:08</dd>
<dt>Metadata Revised:</dt> <dd>06 May 2022, 16:13</dd>
</dl></div></div>
<div id="RightColumn3Col">
<div id="Viewable"><b>Viewable by:</b><br/><ul>
<li><a href="{DOCDB}/ListBy?groupid=6" title="CMS Collaboration">CMS</a></li>
<li><a href="{DOCDB}/ListBy?groupid=11" title="CMS Physics">Physics</a></li>
<li><a href="{DOCDB}/ListBy?groupid=17" title="CMS Executive Management">CMS Exec Mgmt</a></li>
</ul></div>
<div id="Modifiable"><b>Modifiable by:</b><br/><ul>
<li><a href="{DOCDB}/ListBy?groupid=6" title="CMS Collaboration">CMS</a></li>
</ul></div>
<div id="OtherVersions"><p><b>Quick Links:</b> <br/>
<a href="{DOCDB}/ShowDocument?docid=14372"
title="CMS-doc-14372-v7">Latest Version</a></p>
<b>Other Versions:</b>
<table id="OtherVersionTable" class="Alternating LowPaddedTable">
<tr class="Odd"><td><a href="{DOCDB}/ShowDocument?docid=14372&amp;version=6"
title="CMS-doc-14372-v6">CMS-doc-14372-v6</a> <br/>23 Mar 2022, 17:22 </td></tr>
<tr class="Even"><td><a href="{DOCDB}/ShowDocument?docid=14372&amp;version=1"
title="CMS-doc-14372-v1">CMS-doc-14372-v1</a> <br/>12 Jan 2022, 21:14 </td></tr>
</table> </div> </div>
<div id="Abstract">
<dl>
<dt class="InfoHeader"><span class="InfoHeader">Abstract:</span></dt>
<dd>The document by the Limited Authorship Guidelines Group &#x28;LAGG) contains the rules and
guidelines for authoring papers that do not contain the full CMS author list.</dd>
</dl>
</div>
<div id="Files">
<dl>
<dt class="InfoHeader"><span class="InfoHeader">Files in Document:</span></dt>
<dd class="FileList">
<ul>
<li><a href="{DOCDB}/RetrieveFile?docid=14372&amp;filename=rules_v1.pdf&amp;version=7"
title="rules_v1.pdf">Limited Authorship Rules and Guidelines</a>
(rules_v1.pdf, 308.4 kB)</li>
<li><a href="{DOCDB}/RetrieveFile?docid=14372&amp;filename=rules_v1.docx&amp;version=7"
title="rules_v1.docx">Limited Authorship Rules and Guidelines / Source</a>
(rules_v1.docx, 833.1 kB)</li>
</ul>
</dd>
</dl>
</div>
<div id="Topics">
<dl>
<dt class="InfoHeader"><span class="InfoHeader">Topics:</span></dt>
</dl>
<ul>
<li><a href="{DOCDB}/ListBy?topicid=190"
title="CMS Top-Level Management">CMS Management</a>:<a href="{DOCDB}/ListBy?topicid=191"
title="CMS Collaboration Board">Collaboration Board</a>:<a href="{DOCDB}/ListBy?topicid=195"
title="CMS Authorship Board">Authorship Board</a></li>
</ul>
</div>
<div id="Authors"><dl><dt class="InfoHeader"><span class="InfoHeader">Authors:</span></dt></dl>
<ul><li><a href="{DOCDB}/ListBy?authorid=226"
title="European Organization for Nuclear Research">David Barney</a></li><li><a
href="{DOCDB}/ListBy?authorid=1212"
title="INFN Firenze">Elisabetta Gallo</a></li><li><a href="{DOCDB}/ListBy?authorid=393"
title="LLR Ecole Polytechnique">Alexandre Zabi</a></li></ul></div>
<div id="Keywords">
<dl>
<dt class="InfoHeader"><span class="InfoHeader">Keywords:</span></dt>
<dd>
<a href="{DOCDB}/Search?keywordsearch=LAGG">LAGG</a>
<a href="{DOCDB}/Search?keywordsearch=Limited">Limited</a>
<a href="{DOCDB}/Search?keywordsearch=Authorship">Authorship</a>
</dd>
</dl>
</div>
</body></html>
"""

NOT_AUTHORIZED_PAGE = """<html><head>
<title>CMS-doc-999999-v: Not authorized</title>
</head><body></body></html>
"""

SEARCH_PAGE = f"""<html><body>
<table id="DocumentTable" class="Alternating DocumentList tablesorter">
<thead><tr>
<th class="Docid">CMS-doc-#</th>
<th class="Title">Title</th>
<th class="Author">Author(s)</th>
<th class="Topics">Topic(s)</th>
<th class="Updated">Last Updated</th>
</tr></thead><tbody>
<tr>
<td class="Docid"><a href="{DOCDB}/ShowDocument?docid=14372"
title="CMS-doc-14372-v7">14372-v7</a></td>
<td class="Title"><a href="{DOCDB}/ShowDocument?docid=14372"
title="CMS-doc-14372-v7">Limited Authorship Guidelines &#x28;LAGG)</a></td>
<td class="Author"><a href="{DOCDB}/ListBy?authorid=226"
title="European Organization for Nuclear Research">David Barney</a> <i>et al.</i></td>
<td class="Topics"><a href="{DOCDB}/ListBy?topicid=195"
title="CMS Authorship Board">Authorship Board</a></td>
<td class="Updated">06 May 2022</td>
</tr>
<tr>
<td class="Docid"><a href="{DOCDB}/ShowDocument?docid=1234"
title="CMS-doc-1234-v2">1234-v2</a></td>
<td class="Title"><a href="{DOCDB}/ShowDocument?docid=1234"
title="CMS-doc-1234-v2">Tracker alignment workshop</a></td>
<td class="Author"><a href="{DOCDB}/ListBy?authorid=10" title="DESY">Ann Author</a></td>
<td class="Topics"><a href="{DOCDB}/ListBy?topicid=1" title="Trk">Tracker</a>
<a href="{DOCDB}/ListBy?topicid=2" title="Al">Alignment</a></td>
<td class="Updated">16 Oct 2026</td>
</tr>
<tr>
<td class="Docid"><a href="{DOCDB}/ShowDocument?docid=5678"
title="CMS-doc-5678-v1">5678-v1</a></td>
<td class="Title"><a href="{DOCDB}/ShowDocument?docid=5678"
title="CMS-doc-5678-v1">Millepede II manual</a></td>
<td class="Author"><a href="{DOCDB}/ListBy?authorid=20" title="DESY">Blobel</a></td>
<td class="Topics"></td>
<td class="Updated">01 Jan 2021</td>
</tr>
</tbody></table>
<p>Number of documents found: 3</p>
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


def _params(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(urlsplit(str(request.url)).query)


def show_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "ShowDocument" in url and "docid=999999" in url:
        return httpx.Response(200, content=NOT_AUTHORIZED_PAGE.encode("latin-1"))
    if "ShowDocument" in url:
        return httpx.Response(200, content=SHOW_PAGE.encode("latin-1"))
    if "Search" in url or "ListBy" in url:
        return httpx.Response(200, content=SEARCH_PAGE.encode("latin-1"))
    if "RetrieveFile" in url:
        return httpx.Response(200, content=b"%PDF-1.7 fake bytes")
    return httpx.Response(404, content=b"nope")


@pytest.mark.asyncio
async def test_get_document_parses_all_fields(tmp_path: Path) -> None:
    recorder = Recorder(show_handler)
    client = make_client(tmp_path, recorder)
    doc = await docdb.get_document(client, docid=14372)
    assert doc["canonical"] == "CMS-doc-14372-v7"
    assert doc["title"] == "Limited Authorship Guidelines Group (LAGG) Document"
    assert doc["doc_type"] == "Note"
    assert doc["submitted_by"] == "David Barney"
    assert doc["updated_by"] == "Harrison Prosper"
    assert doc["created"] == "12 Jan 2022, 21:14"
    assert doc["contents_revised"] == "06 May 2022, 16:08"
    assert doc["metadata_revised"] == "06 May 2022, 16:13"
    assert doc["viewable_by"] == ["CMS", "Physics", "CMS Exec Mgmt"]
    assert doc["modifiable_by"] == ["CMS"]
    assert doc["abstract"] == (
        "The document by the Limited Authorship Guidelines Group (LAGG) contains the rules "
        "and guidelines for authoring papers that do not contain the full CMS author list."
    )
    assert doc["topics"] == ["CMS Management : Collaboration Board : Authorship Board"]
    assert [a["name"] for a in doc["authors"]] == [
        "David Barney",
        "Elisabetta Gallo",
        "Alexandre Zabi",
    ]
    assert doc["authors"][0]["institute"] == "European Organization for Nuclear Research"
    assert doc["keywords"] == ["LAGG", "Limited", "Authorship"]
    assert [f["filename"] for f in doc["files"]] == ["rules_v1.pdf", "rules_v1.docx"]
    pdf = doc["files"][0]
    assert pdf["description"] == "Limited Authorship Rules and Guidelines"
    assert pdf["size"] == "308.4 kB"
    assert "filename=rules_v1.pdf&version=7" in pdf["download_url"]
    assert [v["version"] for v in doc["versions"]] == [6, 1]
    assert doc["versions"][0]["date"] == "23 Mar 2022, 17:22"
    assert doc["url"] == f"{DOCDB}/ShowDocument?docid=14372"


@pytest.mark.asyncio
async def test_get_document_version_param(tmp_path: Path) -> None:
    recorder = Recorder(show_handler)
    client = make_client(tmp_path, recorder)
    await docdb.get_document(client, docid=14372, version=6)
    query = _params(recorder.requests[0])
    assert query["version"] == ["6"]


@pytest.mark.asyncio
async def test_get_document_not_found(tmp_path: Path) -> None:
    client = make_client(tmp_path, Recorder(show_handler))
    with pytest.raises(UpstreamError) as err:
        await docdb.get_document(client, docid=999999)
    assert err.value.retryable is False
    assert "not found or not visible" in str(err.value)


@pytest.mark.asyncio
async def test_get_document_bad_ids(tmp_path: Path) -> None:
    client = make_client(tmp_path, Recorder(show_handler))
    with pytest.raises(InvalidInputError):
        await docdb.get_document(client, docid=0)
    with pytest.raises(InvalidInputError):
        await docdb.get_document(client, docid=1, version=0)


@pytest.mark.asyncio
async def test_search_rows_and_pagination(tmp_path: Path) -> None:
    recorder = Recorder(show_handler)
    client = make_client(tmp_path, recorder)
    result = await docdb.search_documents(client, text="authorship", limit=2)
    assert result.total_count == 3
    assert len(result.items) == 2
    assert result.has_more is True
    first = result.items[0]
    assert first["docid"] == 14372
    assert first["canonical"] == "14372-v7"
    assert first["title"] == "Limited Authorship Guidelines (LAGG)"
    assert first["first_author"] == "David Barney"
    assert first["topics"] == ["Authorship Board"]
    assert first["updated"] == "06 May 2022"
    page2 = await docdb.search_documents(client, text="authorship", limit=2, offset=2)
    assert len(page2.items) == 1
    assert page2.has_more is False
    assert page2.items[0]["docid"] == 5678
    query = _params(recorder.requests[0])
    assert query["simpletext"] == ["authorship"]
    assert query["simple"] == ["1"]


@pytest.mark.asyncio
async def test_search_title_with_dates(tmp_path: Path) -> None:
    recorder = Recorder(show_handler)
    client = make_client(tmp_path, recorder)
    await docdb.search_documents(
        client,
        title="alignment",
        updated_from="2020-01-05",
        updated_to="2024-12-24",
    )
    query = _params(recorder.requests[0])
    assert query["titlesearch"] == ["alignment"]
    assert query["titlesearchmode"] == ["allword"]
    assert query["mode"] == ["title"]
    assert query["afteryear"] == ["2020"]
    assert query["aftermonth"] == ["Jan"]
    assert query["afterday"] == ["5"]
    assert query["beforeyear"] == ["2024"]
    assert query["beforemonth"] == ["Dec"]
    assert query["beforeday"] == ["24"]


@pytest.mark.asyncio
async def test_search_requires_query(tmp_path: Path) -> None:
    client = make_client(tmp_path, Recorder(show_handler))
    with pytest.raises(InvalidInputError):
        await docdb.search_documents(client)
    with pytest.raises(InvalidInputError):
        await docdb.search_documents(client, text="x", updated_from="not-a-date")
    with pytest.raises(InvalidInputError):
        await docdb.search_documents(client, text="x", updated_from="2020-02-30")


@pytest.mark.asyncio
async def test_search_upstream_error(tmp_path: Path) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        assert "Search" in str(request.url)
        return httpx.Response(500, content=b"borked")

    client = make_client(tmp_path, Recorder(boom))
    with pytest.raises(UpstreamError) as err:
        await docdb.search_documents(client, text="x")
    assert err.value.retryable is True


@pytest.mark.asyncio
async def test_list_recent(tmp_path: Path) -> None:
    recorder = Recorder(show_handler)
    client = make_client(tmp_path, recorder)
    result = await docdb.list_recent(client, days=45, limit=10)
    assert result.total_count == 3
    assert len(result.items) == 3
    assert result.items[1]["topics"] == ["Tracker", "Alignment"]
    query = _params(recorder.requests[0])
    assert query["days"] == ["45"]
    with pytest.raises(InvalidInputError):
        await docdb.list_recent(client, days=0)


@pytest.mark.asyncio
async def test_download_file(tmp_path: Path) -> None:
    recorder = Recorder(show_handler)
    client = make_client(tmp_path, recorder)
    result = await docdb.download_file(client, docid=14372, filename="rules.pdf", version=7)
    saved = (tmp_path / "downloads" / "rules.pdf").read_bytes()
    assert saved.startswith(b"%PDF")
    assert result["docid"] == 14372
    assert result["filename"] == "rules.pdf"
    assert result["size_bytes"] > 0
    assert result["path"].endswith("rules.pdf")
    query = _params(recorder.requests[0])
    assert query["docid"] == ["14372"]
    assert query["filename"] == ["rules.pdf"]
    assert query["version"] == ["7"]


@pytest.mark.asyncio
async def test_download_file_rejects_paths(tmp_path: Path) -> None:
    client = make_client(tmp_path, Recorder(show_handler))
    for bad in ("../evil.pdf", "a/b.pdf", "a\\b.pdf", ".hidden", ".."):
        with pytest.raises(InvalidInputError):
            await docdb.download_file(client, docid=1, filename=bad)
    with pytest.raises(InvalidInputError):
        await docdb.download_file(client, docid=0, filename="ok.pdf")
