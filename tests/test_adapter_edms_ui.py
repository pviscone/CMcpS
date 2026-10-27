"""Tests for the EDMS UI full-text-search scrape adapter.

The parser fixture is a verbatim excerpt of a real rendered results page
(captured 2026-10-27 from the SSO-session scrape of query 'ECAL
milestones'; images stripped). It is kept as a data file because the GXT
markup is one minified 25 KB line that cannot round-trip through an editor.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from cmcps.adapters import edms_ui
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    """Mock-transport handler that records every request."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


FIXTURE = (Path(__file__).parent / "fixtures" / "edms_ui_search.html").read_text()


def test_parse_items_dedup_and_fields() -> None:
    result = edms_ui.parse_results(FIXTURE)
    items = result["items"]
    assert len(items) == 2  # three grid rows dedup into two documents

    doc = items[0]
    assert doc["document_number"] == 350221
    assert doc["code"] == "CMS-EC-SG-0004"
    assert doc["version"] == "6"
    assert doc["title"] == "CMS-ECAL Milestones"
    assert doc["status"] == "In Work"
    assert doc["authors"] == "Dawn HUDSON, Gerard MILLERET"
    assert doc["last_modified"] == "2007-11-16"
    assert doc["context"] == "CMSE-GENERAL"
    assert doc["keywords"] == "Milestones, V36.5, baseline, last estimate"
    assert doc["type"] == "Schedule (S_)"
    assert doc["url"] == "https://edms.cern.ch/document/350221/6"
    assert doc["snippet"] and "ECAL" in doc["snippet"]
    assert {f["name"] for f in doc["files"]} == {
        "v36-5_Oct07_ECAL_GM3.pdf",
        "v36-5_Oct07_ECAL_GM3.xls",
    }
    assert doc["files"][0] == {
        "document_number": 350221,
        "version": "6",
        "name": "v36-5_Oct07_ECAL_GM3.pdf",
        "url": "https://edms.cern.ch/file/350221/6/v36-5_Oct07_ECAL_GM3.pdf",
    }


def test_parse_numeric_code_row() -> None:
    item = edms_ui.parse_results(FIXTURE)["items"][1]
    assert item["document_number"] == 3481902
    assert item["code"] == "3481902"
    assert item["version"] == "0.1"
    assert item["title"] == "Milestones"
    assert item["url"] == "https://edms.cern.ch/document/3481902/0.1"
    assert item["files"] == []


def test_parse_totals_more_and_logged_in() -> None:
    result = edms_ui.parse_results(FIXTURE)
    assert result["totals"] == {"Docs & Files": "100+", "Projects": "100+"}
    assert result["has_more"] is True
    assert result["ui_logged_in"] is True


def test_parse_anonymous_page_detected() -> None:
    result = edms_ui.parse_results(FIXTURE + '<div><a href="/login">Sign in</a></div>')
    assert result["ui_logged_in"] is False


def test_parse_empty_page() -> None:
    result = edms_ui.parse_results("<html><body><p>No objects found</p></body></html>")
    assert result["items"] == []
    assert result["totals"] == {}
    assert result["has_more"] is False


def test_search_url_builds_deep_link() -> None:
    url = edms_ui.search_url("ECAL milestones", latest_version=True, hide_obsolete=False)
    assert url == (
        "https://edms.cern.ch/ui/#!master/search/view?objecttype=document"
        "&query=ECAL%20milestones&latestversion=true&hideobsolete=false"
        "&dadvanced=false&global=true"
    )
    assert search_url_defaults() is False


def search_url_defaults() -> bool:
    return "latestversion=true" in edms_ui.search_url("x")


class _FakeService:
    def __init__(self, payload: dict | list[dict | None] | None) -> None:
        # list form: successive fetches pop successive payloads
        self.payloads = payload if isinstance(payload, list) else [payload]
        self.urls: list[str] = []
        self.interact_seen = None
        self.forced_flags: list[bool] = []

    async def fetch_page_html(
        self,
        url,
        *,
        wait_for=None,
        interact=None,
        settle_seconds=0.0,
        timeout_seconds=None,
        **extra,
    ):
        self.urls.append(url)
        self.interact_seen = interact
        self.wait_for = wait_for
        self.settle = settle_seconds
        self.timeout = timeout_seconds
        self.forced_flags.append(bool(extra.get("interact_even_if_found")))
        if len(self.payloads) > 1:
            return self.payloads.pop(0)
        return self.payloads[0]


async def test_search_text_renders_and_parses() -> None:
    service = _FakeService({"html": FIXTURE, "auth_divert": False})
    result = await edms_ui.search_text(service, query="ECAL milestones")

    assert result.source == "edms-ui"
    assert result.query == "ECAL milestones"
    assert result.size == 2
    assert result.has_more is True
    assert result.total_count is None
    assert result.items[0]["document_number"] == 350221
    assert "Docs & Files: 100+" in (result.note or "")
    assert "DOM-coupled" in (result.note or "")
    assert service.urls == [edms_ui.search_url("ECAL milestones")]
    # the results-grid marker and the sign-in interact must be wired
    assert service.wait_for == edms_ui.RESULTS_MARKER
    assert service.interact_seen is edms_ui._sign_in_interact


async def test_search_text_refetches_when_anonymous() -> None:
    anonymous = FIXTURE + '<div><a href="/login">Sign in</a></div>'  # ui_logged_in=False
    service = _FakeService(
        [{"html": anonymous, "auth_divert": False}, {"html": FIXTURE, "auth_divert": False}]
    )
    result = await edms_ui.search_text(service, query="ECAL milestones")

    assert result.items[0]["document_number"] == 350221
    assert len(service.urls) == 2
    assert service.urls[0] == service.urls[1]
    assert service.forced_flags == [False, True]  # second pass forces the Sign-in interact
    assert "logged-in search" in (result.note or "")


async def test_search_text_run_without_browser_result() -> None:
    service = _FakeService(None)
    with pytest.raises(UpstreamError, match="could not be rendered"):
        await edms_ui.search_text(service, query="x")


async def test_search_text_reports_session_expiry() -> None:
    service = _FakeService({"html": "", "auth_divert": True})
    with pytest.raises(UpstreamError, match="SSO session expired"):
        await edms_ui.search_text(service, query="x")


async def test_search_text_rejects_empty_query() -> None:
    service = _FakeService({"html": FIXTURE, "auth_divert": False})
    with pytest.raises(UpstreamError, match="must not be empty"):
        await edms_ui.search_text(service, query="  ")
    assert service.urls == []


def _http_with_access(tmp_path: Path, readable: set[int]) -> tuple[SafeHttpClient, Recorder]:
    """Mock client: readable document ids answer 200, the rest masked 400."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = urlsplit(str(request.url)).path
        number = int(path.split("/ws/api/documents/")[-1].split("/")[0])
        if number in readable:
            return httpx.Response(200, json={"edmsId": str(number), "version": "1"})
        return httpx.Response(400, json={"exitInfo": [{"exitCode": 1}]})

    settings = Settings(state_dir=tmp_path, require_sso=False, browser_executable=None)
    auth = AuthService(settings)
    recorder = Recorder(handler)
    transport = httpx.MockTransport(recorder)
    client = SafeHttpClient(
        settings, auth_service=auth, client=httpx.AsyncClient(transport=transport)
    )
    return client, recorder


CODE_ONLY_ROW = (
    '<div class="x-grid3-row">'
    '<a href="javascript:;">HL-LHC assembly traveller</a>'
    '<a href="javascript:;">LHC-LMQXFA-FR-0087 v.1</a>'
    "</div>"
)


DENIED_ROW = (
    '<div class="x-grid3-row">'
    '<a class="gwt-Anchor AnchorNoAccess FontSize12px x-component" href="javascript:;">'
    "Secret Building Doc</a>"
    '<div class="SearchColumnRendererHelper-title-accessDenied x-component">'
    '<img title="Access denied"/>'
    "</div>"
    '<a href="https://edms.cern.ch/file/777777/2/secret.pdf">secret.pdf</a>'
    '<a href="javascript:;">777777 v.2</a>'
    "</div>"
)


def with_extra_row(html: str, row: str = CODE_ONLY_ROW) -> str:
    """Append one synthetic grid row inside the fixture's <body>.

    The captured fixture tail is truncated mid-tag; drop any dangling
    unterminated tag before appending so the row is seen as markup.
    """
    body = html.removesuffix("</body></html>")
    body = re.sub(r"<[a-zA-Z][^<>]*$", "", body)
    return body + row + "</body></html>"


async def test_check_access_probes_numbers(tmp_path: Path) -> None:
    http, _ = _http_with_access(tmp_path, readable={350221})
    service = _FakeService({"html": FIXTURE, "auth_divert": False})
    result = await edms_ui.search_text(service, http, query="ECAL milestones", check_access=True)

    assert result.items[0]["document_number"] == 350221
    assert result.items[0]["accessible"] is True
    assert result.items[1]["document_number"] == 3481902
    assert result.items[1]["accessible"] is False
    assert "1 accessible, 1 restricted" in (result.note or "")


async def test_access_filter_restricted(tmp_path: Path) -> None:
    http, _ = _http_with_access(tmp_path, readable={350221})
    service = _FakeService({"html": FIXTURE, "auth_divert": False})
    result = await edms_ui.search_text(service, http, query="x", access="restricted")

    assert [item["document_number"] for item in result.items] == [3481902]


async def test_access_filter_accessible_drops_code_only(tmp_path: Path) -> None:
    html = with_extra_row(FIXTURE)
    http, _ = _http_with_access(tmp_path, readable={350221, 3481902})
    service = _FakeService({"html": html, "auth_divert": False})
    result = await edms_ui.search_text(service, http, query="x", access="accessible")

    numbers = sorted(item["document_number"] for item in result.items)
    assert numbers == [350221, 3481902]
    assert "1 without numeric id" in (result.note or "")


async def test_access_probe_keeps_null_for_code_only(tmp_path: Path) -> None:
    html = with_extra_row("<html><body></body></html>")
    http, _ = _http_with_access(tmp_path, readable=set())
    service = _FakeService({"html": html, "auth_divert": False})
    result = await edms_ui.search_text(service, http, query="x", check_access=True)

    assert result.items[0]["code"] == "LHC-LMQXFA-FR-0087"
    assert result.items[0]["document_number"] is None
    assert result.items[0]["accessible"] is None


async def test_denied_marker_flags_row_without_probe() -> None:
    html = with_extra_row(FIXTURE, row=DENIED_ROW)
    service = _FakeService({"html": html, "auth_divert": False})
    result = await edms_ui.search_text(service, query="x")

    denied = next(item for item in result.items if item["document_number"] == 777777)
    assert denied["accessible"] is False  # UI marker, no REST probe needed
    others = (item for item in result.items if item["document_number"] != 777777)
    assert all(item["accessible"] is None for item in others)


async def test_probe_skips_marker_flagged_rows(tmp_path: Path) -> None:
    html = with_extra_row(FIXTURE, row=DENIED_ROW)
    http, recorder = _http_with_access(tmp_path, readable={350221, 3481902})
    service = _FakeService({"html": html, "auth_divert": False})
    result = await edms_ui.search_text(service, http, query="x", check_access=True)

    assert {item["document_number"]: item["accessible"] for item in result.items}[777777] is False
    probed = {urlsplit(str(request.url)).path for request in recorder.requests}
    assert not any("777777" in path for path in probed)


LETTER_VERSION_ROW = (
    '<div class="x-grid3-row">'
    '<a href="javascript:;">Drawing doc</a>'
    '<a href="https://edms.cern.ch/file/1123356/AI/drawing.pdf">drawing.pdf</a>'
    '<a href="javascript:;">1123356 v.AI</a>'
    "</div>"
)


async def test_access_probe_handles_letter_versions(tmp_path: Path) -> None:
    html = with_extra_row(FIXTURE, row=LETTER_VERSION_ROW)
    http, recorder = _http_with_access(tmp_path, readable={1123356})
    service = _FakeService({"html": html, "auth_divert": False})
    result = await edms_ui.search_text(service, http, query="x", check_access=True)

    item = next(item for item in result.items if item["document_number"] == 1123356)
    assert item["version"] == "AI"
    assert item["accessible"] is True
    assert any(
        urlsplit(str(request.url)).path == "/ws/api/documents/1123356/AI"
        for request in recorder.requests
    )


async def test_restricted_filter_includes_marked_rows(tmp_path: Path) -> None:
    html = with_extra_row(FIXTURE, row=DENIED_ROW)
    http, _ = _http_with_access(tmp_path, readable={350221})
    service = _FakeService({"html": html, "auth_divert": False})
    result = await edms_ui.search_text(service, http, query="x", access="restricted")

    numbers = {item["document_number"] for item in result.items}
    assert numbers == {3481902, 777777}  # REST-denied + UI-marked


async def test_access_rejects_bad_mode(tmp_path: Path) -> None:
    http, _ = _http_with_access(tmp_path, readable=set())
    service = _FakeService({"html": FIXTURE, "auth_divert": False})
    with pytest.raises(InvalidInputError, match="access must be"):
        await edms_ui.search_text(service, http, query="x", access="maybe")
    assert service.urls == []


async def test_check_access_requires_http() -> None:
    service = _FakeService({"html": FIXTURE, "auth_divert": False})
    with pytest.raises(InvalidInputError, match="shared HTTP client"):
        await edms_ui.search_text(service, query="x", check_access=True)
    assert service.urls == []
