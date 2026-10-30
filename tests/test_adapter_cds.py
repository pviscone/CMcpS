"""Fixture-driven contract tests for the CDS adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from cmcps.adapters import cds
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

RECORD: dict[str, Any] = {
    "recid": 12345,
    "title": {"title": "Observation of a new boson"},
    "authors": [
        {"full_name": "Smith, Alice", "affiliation": "CERN"},
        {"full_name": "Doe, Bob", "affiliation": ["MIT", "CERN"]},
    ],
    "abstract": {"summary": "A search for a new boson is presented."},
    "accelerator_experiment": [{"experiment": "CMS"}],
    "collection": [{"primary": "CMS Public Publications"}],
    "creation_date": "2024-03-15",
    "version_id": "2",
    "subject": [{"term": "Higgs"}, {"term": "CMS"}],
    "files": [
        {
            "name": "paper.pdf",
            "full_name": "paper.pdf",
            "size": 2 * 1024 * 1024,
            "magic": ["paper.pdf", "application/pdf", "PDF document"],
        }
    ],
    "doi": "10.1093/ptep/ptaa001",
}

RECORD_STRING_ABSTRACT: dict[str, Any] = {
    **RECORD,
    "abstract": "Plain string summary.",
}


class Recorder:
    """Mock-transport handler that records every request."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def make_client(tmp_path: Path, recorder: Recorder) -> tuple[Settings, SafeHttpClient]:
    settings = Settings(
        state_dir=tmp_path,
        require_sso=False,
        browser_executable=None,
    )
    auth = AuthService(settings)
    transport = httpx.MockTransport(recorder)
    client = SafeHttpClient(
        settings, auth_service=auth, client=httpx.AsyncClient(transport=transport)
    )
    return settings, client


def ok(records: list[dict[str, Any]]) -> Handler:
    return lambda _: httpx.Response(200, json=records)


async def test_search_builds_query_pagination_and_sort(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        params = request.url.params
        assert params["p"] == "Higgs AND experiment:CMS AND collection:ARTICLE AND year:2020-2026"
        assert params["of"] == "recjson"
        assert params["rg"] == "10"
        assert params["jrec"] == "21"
        assert params["sf"] == "d"
        return httpx.Response(200, json=[RECORD])

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await cds.search(
        client,
        settings,
        query="Higgs",
        experiment="CMS",
        doc_type="ARTICLE",
        from_date="2020-01-01",
        until_date="2026-10-22",
        size=10,
        page=3,
        sort="mostrecent",
    )
    assert result.source == "cds"
    assert result.total_count is None
    assert result.page == 3
    assert result.size == 10
    assert result.has_more is False
    assert result.note == "Total hit count is not exposed by the CDS search endpoint"
    item = result.items[0]
    assert item["cds_id"] == 12345
    assert item["mcp_id"] == "cds:12345"
    assert item["title"] == "Observation of a new boson"
    assert item["authors"] == ["Smith, Alice", "Doe, Bob"]
    assert item["abstract"] == "A search for a new boson is presented."
    assert item["experiment"] == "CMS"
    assert item["doc_type"] == "CMS Public Publications"
    assert item["created"] == "2024-03-15"
    assert item["updated"] == "2"
    assert item["url"] == "https://cds.cern.ch/record/12345"
    assert item["file_count"] == 1
    assert item["keywords"] == ["Higgs", "CMS"]
    assert item["subjects"] == ["Higgs", "CMS"]
    assert item["doi"] == "10.1093/ptep/ptaa001"
    assert item["arxiv_id"] is None
    assert all(request.method == "GET" for request in recorder.requests)


async def test_search_sort_mapping(tmp_path: Path) -> None:
    seen: list[str] = []
    for sort, _code in (("bestmatch", "r"), ("mostcited", "c")):
        recorder = Recorder(ok([]))
        settings, client = make_client(tmp_path, recorder)
        await cds.search(client, settings, query="Higgs", sort=sort)
        seen.append(recorder.requests[0].url.params["sf"])
    assert seen == ["r", "c"]


async def test_search_rejects_invalid_sort_page_and_dates(tmp_path: Path) -> None:
    recorder = Recorder(ok([]))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await cds.search(client, settings, query="Higgs", sort="random")
    with pytest.raises(InvalidInputError):
        await cds.search(client, settings, query="Higgs", page=0)
    with pytest.raises(InvalidInputError):
        await cds.search(client, settings, query="Higgs", size=101)
    with pytest.raises(InvalidInputError):
        await cds.search(client, settings, query="Higgs", from_date="15/03/2024")
    with pytest.raises(InvalidInputError):
        await cds.search(client, settings, query="Higgs", until_date="March 2026")
    assert recorder.requests == []


async def test_search_full_page_reports_has_more(tmp_path: Path) -> None:
    recorder = Recorder(ok([RECORD, RECORD]))
    settings, client = make_client(tmp_path, recorder)
    result = await cds.search(client, settings, query="Higgs", size=2)
    assert result.has_more is True


async def test_search_accepts_string_abstract(tmp_path: Path) -> None:
    recorder = Recorder(ok([RECORD_STRING_ABSTRACT]))
    settings, client = make_client(tmp_path, recorder)
    result = await cds.search(client, settings, query="Higgs")
    assert result.items[0]["abstract"] == "Plain string summary."


async def test_get_record_accepts_prefixed_id(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["p"] == "recid:12345"
        assert request.url.params["rg"] == "1"
        return httpx.Response(200, json=[RECORD])

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    for recid in ("12345", "cds:12345"):
        result = await cds.get_record(client, settings, recid=recid)
    assert result["cds_id"] == 12345
    assert result["mcp_id"] == "cds:12345"
    assert result["authors_detailed"] == [
        {"name": "Smith, Alice", "affiliation": ["CERN"], "orcid": None},
        {"name": "Doe, Bob", "affiliation": ["MIT", "CERN"], "orcid": None},
    ]
    assert result["files"] == [
        {
            "name": "paper.pdf",
            "size_bytes": 2 * 1024 * 1024,
            "size_mb": 2.0,
            "mime_type": "application/pdf",
            "url": "https://cds.cern.ch/record/12345/files/paper.pdf",
        }
    ]


async def test_get_record_rejects_bad_id_without_request(tmp_path: Path) -> None:
    recorder = Recorder(ok([]))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await cds.get_record(client, settings, recid="CERN-PH-EP-2024-001")
    with pytest.raises(InvalidInputError):
        await cds.get_record(client, settings, recid="cds:abc")
    assert recorder.requests == []


async def test_get_record_unknown_id_raises(tmp_path: Path) -> None:
    recorder = Recorder(ok([]))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await cds.get_record(client, settings, recid="999999999")
    assert len(recorder.requests) == 1


async def test_get_files_returns_links_only(tmp_path: Path) -> None:
    recorder = Recorder(ok([RECORD]))
    settings, client = make_client(tmp_path, recorder)
    result = await cds.get_files(client, settings, recid="12345")
    assert result["cds_id"] == 12345
    assert result["mcp_id"] == "cds:12345"
    assert result["file_count"] == 1
    assert result["items"][0] == {
        "name": "paper.pdf",
        "size_bytes": 2 * 1024 * 1024,
        "size_mb": 2.0,
        "mime_type": "application/pdf",
        "download_url": "https://cds.cern.ch/record/12345/files/paper.pdf",
    }
    assert all(request.method == "GET" for request in recorder.requests)


async def test_anubis_html_page_raises_upstream_with_instructions(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html><script>anubis challenge</script></html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError) as caught:
        await cds.search(client, settings, query="Higgs")
    assert "Anubis" in (caught.value.instructions or "")
    assert "cern_auth_sign_in" in (caught.value.instructions or "")


async def test_error_status_raises_structured_upstream(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(500, json={"error": "boom"}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError) as caught:
        await cds.search(client, settings, query="Higgs")
    assert caught.value.retryable is True


async def test_non_json_body_raises_upstream(tmp_path: Path) -> None:
    recorder = Recorder(
        lambda _: httpx.Response(
            200, content=b"not json at all", headers={"content-type": "application/json"}
        )
    )
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError) as caught:
        await cds.search(client, settings, query="Higgs")
    # A declared-JSON-but-unparseable body must not be called "non-JSON" or
    # blamed on authentication; the message carries a body snippet instead.
    assert "could not be parsed as JSON" in caught.value.message
    assert "not json at all" in caught.value.message
    assert "cern_auth_sign_in" not in (caught.value.instructions or "")


async def test_html_error_page_keeps_sso_hint(tmp_path: Path) -> None:
    recorder = Recorder(
        lambda _: httpx.Response(
            200, content=b"<!DOCTYPE html><html>login</html>", headers={"content-type": "text/html"}
        )
    )
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError) as caught:
        await cds.search(client, settings, query="Higgs")
    assert "HTML page" in caught.value.message
    assert "cern_auth_sign_in" in (caught.value.instructions or "")


async def test_all_requests_are_get(tmp_path: Path) -> None:
    recorder = Recorder(ok([RECORD]))
    settings, client = make_client(tmp_path, recorder)
    await cds.search(client, settings, query="Higgs", from_date="2024-01-01")
    await cds.get_record(client, settings, recid="cds:12345")
    await cds.get_files(client, settings, recid="12345")
    assert len(recorder.requests) == 3
    assert all(request.method == "GET" for request in recorder.requests)


async def test_download_file_rejects_non_cds_hosts(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(200, content=b"data"))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await cds.download_file(client, settings, download_url="https://evil.example.org/file.pdf")
    assert recorder.requests == []


async def test_download_file_streams_pdf_to_state(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(200, content=b"%PDF-1.7 body"))
    settings, client = make_client(tmp_path, recorder)
    result = await cds.download_file(
        client, settings, download_url="https://cds.cern.ch/record/12345/files/paper.pdf"
    )
    path = Path(result["path"])
    assert path.parent == settings.download_dir
    assert path.read_bytes() == b"%PDF-1.7 body"
    assert result["size_bytes"] == len(b"%PDF-1.7 body")
    assert recorder.requests[0].method == "GET"


async def test_download_file_rejects_icms_stub_and_removes_file(tmp_path: Path) -> None:
    stub = b'<!--jsp:forward page="iCMS_login.jsp" /-->\n<script>alert("login")</script>'

    recorder = Recorder(lambda _: httpx.Response(200, content=stub))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError) as caught:
        await cds.download_file(
            client,
            settings,
            download_url="https://cds.cern.ch/record/2875713/files/IN2023_001.pdf",
        )
    assert "iCMS" in caught.value.message
    assert "cern_auth_sign_in" in (caught.value.instructions or "")
    assert not list(settings.download_dir.glob("IN2023_001.pdf*"))


async def test_download_file_rejects_anubis_challenge(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(200, content=b"<html>anubis challenge</html>"))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await cds.download_file(
            client, settings, download_url="https://cds.cern.ch/record/1/files/a.pdf"
        )
    assert not list(settings.download_dir.glob("a.pdf*"))
