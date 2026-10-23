"""Fixture-driven contract tests for the CMS public results adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cmcps.adapters import cmsresults
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

INDEX_HTML = """
<html><body>
<a href="./public-results/publications/HIG/index.html">HIG papers</a>
<a href="./public-results/publications/SMP/index.html">SMP papers</a>
<a href="./public-results/preliminary-results/HIG/index.html">HIG preliminary</a>
<a href="./publications/PR/PR.html">physics results</a>
</body></html>
"""

GROUP_HTML = """
<html><body>
<table>
<!-- BEGIN HIG-12-028  Run1 _H125 _ZZ_ -->
<tr>
  <td class="num"><b>2</b></td>
  <td class="cadi"><a href="../HIG-12-028/index.html"><b>HIG-12-028</b></a></td>
  <td class="title">Observation of a new boson at a mass of 125 GeV</td>
  <td class="status">
    <a href="http://dx.doi.org/10.1016/j.physletb.2012.08.021">PLB 716 (2012) 30-61</a>
  </td>
  <td class="date">2012-08-31</td>
</tr>
<!-- END -->
<!-- BEGIN HIG-11-024 -->
<tr>
  <td class="num"><b>3</b></td>
  <td class="cadi"><a href="../HIG-11-024/index.html"><b>HIG-11-024</b></a></td>
  <td class="title">Search for the standard model Higgs boson decaying to WW</td>
  <td class="status">
    <a href="http://dx.doi.org/10.1016/j.physletb.2012.02.076">PLB 710 (2012) 91-113</a>
  </td>
  <td class="date">2012-02-07</td>
</tr>
<!-- END -->
</table>
</body></html>
"""

PAPER_HTML = """
<html><body>
<div id="publi">
<table>
<tr><td colspan=3 align="right">CMS-HIG-12-028 ; CERN-PH-EP-2012-220</td></tr>
<tr><td colspan=3 class="titleline">
    Observation of a new boson at a mass of 125 GeV
</td></tr>
<tr><td colspan=3 class="dateline">CMS Collaboration</td></tr>
<tr><td colspan=3 class="dateline">31 July 2012</td></tr>
<tr><td colspan=3 class="journalline"><i>
  <a href="http://dx.doi.org/10.1016/j.physletb.2012.08.021">Phys. Lett. B 716 (2012) 30</a>
</i></td></tr>
<tr><td colspan=3 class="textline">
    <b>Abstract: </b>Results are presented from a search for the standard
    model Higgs boson. A new boson is observed.
</td></tr>
<tr><td class="link"><b>Links: </b>
  e-print <a href="http://arxiv.org/abs/1207.7235">arXiv:1207.7235</a>
  (<a href="http://arxiv.org/pdf/1207.7235">PDF</a>) ;
  <a href="https://cds.cern.ch/record/1460438">CDS record</a> ;
  <a href="http://inspirehep.net/record/1124338">inSPIRE record</a> ;
</td></tr>
</table>
<a href="CMS-HIG-12-028_Figure_001-a.png">fig1</a>
<a href="CMS-HIG-12-028_Figure_001-a-thumb.png">fig1-thumb</a>
<a href="CMS-HIG-12-028_Table_001.pdf">tab1</a>
</div>
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


def make_client(tmp_path: Path, recorder: Recorder) -> tuple[Settings, SafeHttpClient]:
    settings = Settings(state_dir=tmp_path, require_sso=False, browser_executable=None)
    auth = AuthService(settings)
    transport = httpx.MockTransport(recorder)
    client = SafeHttpClient(
        settings, auth_service=auth, client=httpx.AsyncClient(transport=transport)
    )
    return settings, client


async def test_list_groups(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=INDEX_HTML))
    _settings, client = make_client(tmp_path, recorder)
    result = await cmsresults.list_groups(client)
    assert result["group_count"] == 2
    codes = {g["group"] for g in result["items"]}
    assert codes == {"HIG", "SMP"}
    hig = next(g for g in result["items"] if g["group"] == "HIG")
    assert hig["preliminary_url"].endswith("/preliminary-results/HIG/index.html")


async def test_list_papers_parses_entries(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == ("/cms-results/public-results/publications/HIG/index.html")
        return httpx.Response(200, text=GROUP_HTML)

    recorder = Recorder(handler)
    _settings, client = make_client(tmp_path, recorder)
    result = await cmsresults.list_papers(client, group="hig")
    assert result.total_count == 2
    assert result.has_more is False
    first = result.items[0]
    assert first["paper_id"] == "HIG-12-028"  # sorted by date desc
    assert first["title"] == "Observation of a new boson at a mass of 125 GeV"
    assert first["doi"] == "10.1016/j.physletb.2012.08.021"
    assert first["journal"] == "PLB 716 (2012) 30-61"
    assert first["url"].endswith("/publications/HIG-12-028/index.html")


async def test_list_papers_filters_year_and_title(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=GROUP_HTML))
    _settings, client = make_client(tmp_path, recorder)
    by_year = await cmsresults.list_papers(client, group="HIG", year=2012)
    assert by_year.total_count == 2  # both entries dated 2012
    by_title = await cmsresults.list_papers(client, group="HIG", title_contains="observation")
    assert by_title.total_count == 1
    assert by_title.items[0]["paper_id"] == "HIG-12-028"


async def test_list_papers_rejects_bad_kind_and_group(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=""))
    _settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await cmsresults.list_papers(client, group="HIG", kind="internal")
    with pytest.raises(InvalidInputError):
        await cmsresults.list_papers(client, group="HIG; rm -rf")
    assert recorder.requests == []


async def test_get_paper_detail(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/preliminary-results/" in str(request.url):
            return httpx.Response(404, text="nf")
        return httpx.Response(200, text=PAPER_HTML)

    recorder = Recorder(handler)
    _settings, client = make_client(tmp_path, recorder)
    result = await cmsresults.get_paper(client, paper_id="hig-12-028")
    assert result["paper_id"] == "HIG-12-028"
    assert result["kind"] == "publications"
    assert result["title"] == "Observation of a new boson at a mass of 125 GeV"
    assert result["doi"] == "10.1016/j.physletb.2012.08.021"
    assert result["abstract"] == (
        "Results are presented from a search for the standard\n"
        "model Higgs boson. A new boson is observed."
    )
    labels = [link["label"] for link in result["links"]]
    assert "arXiv:1207.7235" in labels
    assert "CDS record" in labels
    prefix = "https://cms-results.web.cern.ch/cms-results/public-results/publications/HIG-12-028/"
    assert result["figures_and_tables"] == [
        prefix + "CMS-HIG-12-028_Figure_001-a.png",
        prefix + "CMS-HIG-12-028_Table_001.pdf",
    ]


async def test_get_paper_missing_raises_upstream(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(404, text="nf"))
    _settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await cmsresults.get_paper(client, paper_id="HIG-99-999")


async def test_get_paper_rejects_bad_id(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=""))
    _settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await cmsresults.get_paper(client, paper_id="not-an-id")
    assert recorder.requests == []


async def test_download_rejects_foreign_host(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, content=b"x"))
    _settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await cmsresults.download_file(client, download_url="https://evil.example.com/fig.png")
    assert recorder.requests == []


async def test_download_writes_file(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, content=b"png-bytes"))
    settings, client = make_client(tmp_path, recorder)
    result = await cmsresults.download_file(
        client,
        download_url=(
            "https://cms-results.web.cern.ch/cms-results/public-results/"
            "publications/HIG-12-028/figures/fig1.png"
        ),
    )
    saved = Path(result["path"])
    assert saved.read_bytes() == b"png-bytes"
    assert saved.parent == settings.download_dir
