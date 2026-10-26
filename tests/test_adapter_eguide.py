"""Fixture-driven contract tests for the Admin e-guide adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cmcps.adapters import eguide
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import AuthRequiredError, InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

SEARCH_HTML = """
<h2>Search results</h2><ol><li>
<h3 lang="fr">
  <a href="https://admin-eguide.web.cern.ch/procedure/visa-entree">Visas d&#039;entrée</a>
</h3>
  <p lang="fr">… Qu&#039;est-ce qu&#039;un <strong>visa</strong> ? …</p>
  <p></p>
</li><li>
<h3>
  <a href="https://admin-eguide.web.cern.ch/en/procedure/visas-entry">Entry visas</a>
</h3>
  <p>… What is a <strong>visa</strong>? …</p>
  <p></p>
</li><li>
<h3>
  <a href="https://admin-eguide.web.cern.ch/en/faq/visa-letter">3. What information?</a>
</h3>
  <p>… For letters to be used for a <strong>visa</strong> …</p>
  <p></p>
</li></ol>
<nav class="pager"><a href="?keys=visa&amp;page=1">Next</a></nav>
"""

INDEX_HTML = """
<div class="view-content clearfix">
<div class="views-row">
<a href="/en/procedure/life-insurance-helvetia" hreflang="en"> Life Insurance</a>
<span class="field-content"></span>
</div>
<h3>A</h3>
<div class="views-row">
<a href="/en/procedure/access-club" hreflang="en">Access to CERN for clubs</a>
<span class="field-content"></span>
</div>
<h3>B</h3>
<div class="views-row">
<a href="/en/procedure/budget-control" hreflang="en">Budget control</a>
<span class="field-content"><a href="/en/faq/budget-control">(FAQ)</a></span>
</div>
</div>
"""

_PROCEDURE_HEAD = """
<html><head><title>CERN access card | Admin e-guide</title></head><body>
<h1 class="page-header">CERN access card</h1>
<article role="article" about="/en/procedure/cern-access-card"
 class="procedure full clearfix">
<div class="views-field views-field-changed">
<span class="views-label views-label-changed">Last update: </span>
<span class="field-content">18.02.2026</span></div>
<div class="views-field views-field-field-date-creation">
<span class="views-label views-label-field-date-creation">Creation date: </span>
<span class="field-content">23.03.2010</span></div>
<div class="views-field views-field-field-processus-approbation">
<span class="views-label views-label-field-processus-approbation">Approval process: </span>
<span class="field-content"><a href="/node/1372#approb">B</a></span></div>
<div class="views-field views-field-field-access">
<span class="views-label views-label-field-access">Access: </span>
<span class="field-content">public</span></div>
"""

_PROCEDURE_SECTIONS = """
<div class="views-field views-field-body">
<h2 class="views-label views-label-body procedure-label">General</h2>
<div class="field-content"><p>A valid CERN access card is needed for the site.</p>
</div></div><div class="views-field views-field-field-procedure">
<h2 class="views-label views-label-field-procedure procedure-label">Procedure</h2>
<div class="field-content"><h3>1. How to obtain a CERN card</h3>
<p>The CERN card is issued to members of the personnel.</p>
</div></div><div class="views-field views-field-field-revisions">
<h2 class="views-label views-label-field-revisions revision-label">Revisions </h2>
<div class="field-content revision-table"><table border="0" cellpadding="1">
<thead><tr>
<th scope="col">No revision</th>
<th scope="col">Changes</th>
<th scope="col">Approved on</th>
</tr></thead><tbody>
<tr>
<td>1</td>
<td>First approval</td>
<td>CERN/DG-RH/17306 - 27 April 2014</td>
</tr>
<tr>
<td>2</td>
<td>Second approval</td>
<td>18 February 2026</td>
</tr>
</tbody></table>
"""

_PROCEDURE_TAIL = """
<div class="views-field views-field-field-ref-contacts contacts-wrapper-html-element">
<div class="field-content contacts-html-element">
<article data-history-node-id="2949" role="article"
 about="/en/contact/cern-community-support-centres-ccsc"
 class="contact is-promoted teaser clearfix">
      <h2>
      <a href="/en/contact/cern-community-support-centres-ccsc" rel="bookmark">
<span>CERN Community Support Centres (CCSC)</span>
</a>
    </h2>
</article>
</div></div>
</article>
<h1 class="mb-3 mt-3 text-center">Reference documents</h1>
<a href="https://cds.cern.ch/record/2109697/files/OC2_Rev3.pdf" target="_blank">OC 2 Rev. 3</a>
<a href="/sites/default/files/2026-02/card-request-form.pdf">Card request form</a>
<h1 class="mb-3 mt-3 text-center">Other procedures</h1>
<a href="/en/procedure/swiss-cards" hreflang="en">Swiss cards</a>
</body></html>
"""

PROCEDURE_HTML = _PROCEDURE_HEAD + _PROCEDURE_SECTIONS + _PROCEDURE_TAIL

LOGIN_HTML = "<html><head><title>Sign in to CERN</title></head><body>SSO</body></html>"

EMPTY_HTML = "<html><body><h1 class='page-header'>Nothing</h1></body></html>"


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


async def test_search_parses_mixed_language_results(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/en/search/node"
        assert request.url.params["keys"] == "visa"
        assert request.url.params["page"] == "0"
        return httpx.Response(200, text=SEARCH_HTML)

    _s, client = make_client(tmp_path, Recorder(handler))
    result = await eguide.search(client, query="visa")
    assert result.size == 3
    assert result.has_more is True
    fr, en, faq = result.items
    assert fr["lang"] == "fr"
    assert fr["kind"] == "procedure"
    assert fr["slug"] == "visa-entree"
    assert en["kind"] == "procedure" and en["lang"] == "en"
    assert faq["kind"] == "faq"
    assert faq["snippet"] and "visa" in faq["snippet"]


async def test_search_without_results_or_pager(tmp_path: Path) -> None:
    _s, client = make_client(tmp_path, Recorder(lambda _r: httpx.Response(200, text=EMPTY_HTML)))
    result = await eguide.search(client, query="zzz-no-such-thing")
    assert result.items == []
    assert result.has_more is False


async def test_search_rejects_empty_query(tmp_path: Path) -> None:
    _s, client = make_client(tmp_path, Recorder(lambda _r: httpx.Response(200, text=EMPTY_HTML)))
    with pytest.raises(InvalidInputError):
        await eguide.search(client, query="   ")


async def test_search_login_page_raises_auth_required(tmp_path: Path) -> None:
    _s, client = make_client(tmp_path, Recorder(lambda _r: httpx.Response(200, text=LOGIN_HTML)))
    with pytest.raises(AuthRequiredError):
        await eguide.search(client, query="visa")


async def test_list_procedures_parses_letters_and_faq(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/en/index-procedures"
        return httpx.Response(200, text=INDEX_HTML)

    _s, client = make_client(tmp_path, Recorder(handler))
    result = await eguide.list_procedures(client)
    assert result.size == 3
    assert result.has_more is False
    life, access, budget = result.items
    assert life["letter"] is None
    assert access["letter"] == "A"
    assert access["faq_path"] is None
    assert budget["letter"] == "B"
    assert budget["faq_path"] == "/en/faq/budget-control"


async def test_list_procedures_title_filter(tmp_path: Path) -> None:
    _s, client = make_client(tmp_path, Recorder(lambda _r: httpx.Response(200, text=INDEX_HTML)))
    result = await eguide.list_procedures(client, title_contains="access")
    assert [item["slug"] for item in result.items] == ["access-club"]


async def test_get_procedure_parses_all_fields(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/en/procedure/cern-access-card"
        return httpx.Response(200, text=PROCEDURE_HTML)

    _s, client = make_client(tmp_path, Recorder(handler))
    result = await eguide.get_procedure(client, slug="cern-access-card")
    assert result["title"] == "CERN access card"
    assert result["access"] == "public"
    assert result["created"] == "23.03.2010"
    assert result["updated"] == "18.02.2026"
    assert result["approval_process"] == "B"
    assert result["general"] is not None and "access card" in result["general"]
    assert result["procedure"] is not None and "How to obtain" in result["procedure"]
    assert result["revisions"] == [
        {"no": "1", "changes": "First approval", "approved": "CERN/DG-RH/17306 - 27 April 2014"},
        {"no": "2", "changes": "Second approval", "approved": "18 February 2026"},
    ]
    assert result["contacts"] == [
        {
            "name": "CERN Community Support Centres (CCSC)",
            "path": "/en/contact/cern-community-support-centres-ccsc",
        }
    ]
    sections = {link["section"] for link in result["related_links"]}
    assert sections == {"Reference documents", "Other procedures"}
    assert any(
        link["url"] == "/sites/default/files/2026-02/card-request-form.pdf"
        for link in result["related_links"]
    )


async def test_get_procedure_accepts_prefixed_slug(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=PROCEDURE_HTML))
    _s, client = make_client(tmp_path, recorder)
    result = await eguide.get_procedure(client, slug="/en/procedure/cern-access-card")
    assert result["slug"] == "cern-access-card"


async def test_get_procedure_rejects_bad_slug(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=PROCEDURE_HTML))
    _s, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await eguide.get_procedure(client, slug="../index-procedures")


async def test_get_procedure_unrenderable_page_raises(tmp_path: Path) -> None:
    _s, client = make_client(tmp_path, Recorder(lambda _r: httpx.Response(200, text=EMPTY_HTML)))
    with pytest.raises(UpstreamError):
        await eguide.get_procedure(client, slug="no-such-procedure")


async def test_get_procedure_server_error_raises_upstream(tmp_path: Path) -> None:
    _s, client = make_client(tmp_path, Recorder(lambda _r: httpx.Response(503, text="oops")))
    with pytest.raises(UpstreamError):
        await eguide.get_procedure(client, slug="cern-access-card")


async def test_download_file_rejects_foreign_host(tmp_path: Path) -> None:
    _s, client = make_client(tmp_path, Recorder(lambda _r: httpx.Response(200, text="x")))
    with pytest.raises(InvalidInputError):
        await eguide.download_file(client, download_url="https://cds.cern.ch/record/1/files/x.pdf")


async def test_download_file_stores_pdf(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/sites/default/files/2026-02/card-request-form.pdf"
        return httpx.Response(200, content=b"%PDF-1.4 fake")

    _s, client = make_client(tmp_path, Recorder(handler))
    result = await eguide.download_file(
        client,
        download_url="https://admin-eguide.web.cern.ch/sites/default/files/2026-02/card-request-form.pdf",
    )
    assert result["size_bytes"] == len(b"%PDF-1.4 fake")
    assert Path(result["path"]).read_bytes() == b"%PDF-1.4 fake"


async def test_french_urls_use_fr_prefix(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=INDEX_HTML))
    _s, client = make_client(tmp_path, recorder)
    await eguide.list_procedures(client, lang="fr")
    assert recorder.requests[0].url.path == "/fr/index-procedures"
