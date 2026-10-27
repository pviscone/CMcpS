"""Fixture-driven contract tests for the CERN EDMS adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from cmcps.adapters import edms
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

SEARCH_PAYLOAD = {
    "exitInfo": [],
    "documents": [
        {
            "edmsId": "319622",
            "version": "1",
            "title": "Manufacturer's Traveller for HCMB__A025-10004120",
            "creationDate": "Wed Jul 18 12:56:53 CEST 2001",
            "creationDateTime": 995453813000,
            "modificationDate": "Wed Jul 18 13:35:18 CEST 2001",
            "modificationDateTime": 995456118000,
            "extRef": "HCMB__A025-10004120",
            "context": "AT-MEL-CFU",
            "type": "Report",
            "typeAttributes": "Test",
            "releaseProc": "DOC-OWNER",
            "status": "In Work",
            "statusColor": "#CC0000",
            "obsolete": False,
            "filesExitInfo": {"exitCode": 20027},
            "actions": {"putFileAllowed": False},
        },
        {
            "edmsId": "1525125",
            "version": "5",
            "title": "Searching in EDMS",
            "creationDate": "2021-06-22",
            "context": "EDMS-SERVICE-PUBLIC",
            "type": "Presentation / Publication",
            "releaseProc": "DOC-AL",
            "status": "Released",
            "obsolete": False,
            "actions": {"putFileAllowed": False},
        },
    ],
}

DETAIL_PAYLOAD = {
    "url": "https://edms.cern.ch/document/1525125/5",
    "edmsId": "1525125",
    "version": "5",
    "title": "Searching in EDMS",
    "authors": "Claudio Scoero, Stefania Schettino",
    "creationDate": "2021-06-22",
    "keywords": "EDMS, FAQ, Frequently Asked Questions",
    "context": "EDMS-SERVICE-PUBLIC",
    "type": "Presentation / Publication",
    "releaseProc": "DOC-AL",
    "status": "Released",
    "statusColor": "#00CC00",
    "visibility": "Public",
    "files": [
        {
            "name": "Searching_EDMS.docx",
            "size": 2108284,
            "lastModifiedDate": "2021-07-05 03:43:28",
            "lastModifiedBy": "CLAUDIO SCOERO",
            "convertedPdfUrl": "https://edms.cern.ch/file/1525125/5/Searching_EDMS_docx_cpdf.pdf",
        },
        {
            "name": "Searching_EDMS.pdf",
            "size": 1664520,
            "lastModifiedDate": "2023-06-29 01:37:30",
            "lastModifiedBy": "Admin EDMS",
        },
    ],
    "actions": {"putFileAllowed": False},
}

TYPES_PAYLOAD = [
    {"typeId": "266", "typeName": "Report", "typeDesc": "An account given of a matter"},
    {"typeId": "256", "typeName": "Illustration / Picture"},
]

USER_PAYLOAD = {
    "uic": 77175,
    "username": "jdoe",
    "email": "jdoe@example.com",
    "edmsDisplayName": "JANE DOE",
}

FILE_BYTES = b"%PDF-1.4 fake edms bytes\n"


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


async def test_search_filters_params_and_envelope(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert urlsplit(str(request.url)).path == "/ws/api/documents"
        params = request.url.params
        assert params["context"] == "EDMS-SERVICE-PUBLIC"
        assert params["status"] == "Released"
        assert params["obsolete"] == "false"
        assert "assetAndJobs" not in params
        return httpx.Response(200, json=SEARCH_PAYLOAD)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await edms.search_documents(
        client, context="EDMS-SERVICE-PUBLIC", status="Released", obsolete=False
    )

    assert result.source == "edms"
    assert result.total_count == 2
    assert result.page == 1
    assert result.size == 20
    assert result.has_more is False
    assert result.note and "parent-structure listing" in result.note
    first = result.items[0]
    assert first["edms_id"] == "319622"
    assert first["version"] == "1"
    assert first["release_proc"] == "DOC-OWNER"
    assert first["obsolete"] is False
    assert first["url"] == "https://edms.cern.ch/document/319622/1"


async def test_search_client_side_pagination(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=SEARCH_PAYLOAD))
    client = make_client(tmp_path, recorder)
    result = await edms.search_documents(client, status="Released", limit=1, offset=1)

    assert result.total_count == 2
    assert result.has_more is False
    assert result.page == 2
    assert len(result.items) == 1
    assert result.items[0]["edms_id"] == "1525125"


async def test_search_pagination_reports_more(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=SEARCH_PAYLOAD))
    client = make_client(tmp_path, recorder)
    result = await edms.search_documents(client, status="Released", limit=1)

    assert result.has_more is True
    assert result.items[0]["edms_id"] == "319622"


async def test_search_parent_scope_params(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        assert params["parentObjectType"] == "A"
        assert params["parentObjectId"] == "HCMB__A025-10004120"
        assert params["assetAndJobs"] == "true"
        return httpx.Response(200, json=SEARCH_PAYLOAD)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await edms.search_documents(
        client,
        parent_object_type="A",
        parent_object_id="HCMB__A025-10004120",
        asset_and_jobs=True,
    )
    assert result.total_count == 2


async def test_search_requires_at_least_one_filter(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=SEARCH_PAYLOAD))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError, match="at least one filter"):
        await edms.search_documents(client)
    assert recorder.requests == []


async def test_search_upstream_error(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(500, json={"error": "boom"}))
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError) as err:
        await edms.search_documents(client, status="Released")
    assert err.value.retryable is True


async def test_get_document_versionless_path(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert urlsplit(str(request.url)).path == "/ws/api/documents/1525125"
        return httpx.Response(200, json=DETAIL_PAYLOAD)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await edms.get_document(client, document_number="1525125")

    assert result["source"] == "edms"
    assert result["edmsId"] == "1525125"
    assert result["version"] == "5"
    assert result["visibility"] == "Public"
    assert result["files"][0]["convertedPdfUrl"].endswith("Searching_EDMS_docx_cpdf.pdf")


async def test_get_document_version_and_last_released(tmp_path: Path) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(urlsplit(str(request.url)).path)
        return httpx.Response(200, json=DETAIL_PAYLOAD)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    await edms.get_document(client, document_number="188033", version="1.2")
    await edms.get_document(client, document_number="188033", version="last_released")
    await edms.get_document(client, document_number="188033", version="AI")

    assert paths == [
        "/ws/api/documents/188033/1.2",
        "/ws/api/documents/188033/LAST_RELEASED",
        "/ws/api/documents/188033/AI",
    ]


@pytest.mark.parametrize("status", [400, 404])
async def test_get_document_not_found(tmp_path: Path, status: int) -> None:
    # Live behavior (2026-10): unknown documents answer HTTP 400 with a JSON
    # {"exitInfo": [...]} error body, never 404.
    recorder = Recorder(lambda _r: httpx.Response(status, json={"exitInfo": [{"exitCode": 1}]}))
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError, match="not found or not visible") as err:
        await edms.get_document(client, document_number="987654")
    assert err.value.retryable is False


async def test_get_document_validates_inputs(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=DETAIL_PAYLOAD))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await edms.get_document(client, document_number="abc")
    with pytest.raises(InvalidInputError):
        await edms.get_document(client, document_number="0")
    with pytest.raises(InvalidInputError):
        await edms.get_document(client, document_number="188033", version="../latest")
    with pytest.raises(InvalidInputError):
        await edms.get_document(client, document_number="188033", version="1.2 beta")
    assert recorder.requests == []


async def test_download_file(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = urlsplit(str(request.url)).path
        assert path == "/ws/api/files/1525125/LAST_RELEASED/Searching_EDMS.pdf"
        return httpx.Response(200, content=FILE_BYTES)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await edms.download_file(
        client, document_number="1525125", filename="Searching_EDMS.pdf"
    )

    assert result["source"] == "edms"
    assert result["version"] == "LAST_RELEASED"
    assert result["size_bytes"] == len(FILE_BYTES)
    assert (tmp_path / "downloads" / "Searching_EDMS.pdf").read_bytes() == FILE_BYTES


async def test_download_accepts_leading_dash_drawings(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = urlsplit(str(request.url)).path
        assert path.endswith("/----atl109460004-v.AI.pdf")
        return httpx.Response(200, content=FILE_BYTES)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await edms.download_file(
        client, document_number="1123356", filename="----atl109460004-v.AI.pdf", version="AI"
    )
    assert result["size_bytes"] == len(FILE_BYTES)


async def test_download_rejects_bad_filenames(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, content=FILE_BYTES))
    client = make_client(tmp_path, recorder)
    for bad in ("../evil.pdf", "a/b.pdf", ".hidden", "a..b.pdf"):
        with pytest.raises(InvalidInputError):
            await edms.download_file(client, document_number="1525125", filename=bad)
    assert recorder.requests == []


async def test_list_document_types(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert urlsplit(str(request.url)).path == "/ws/api/documentTypes"
        assert request.url.params["cadimType"] == "TEXTFILE"
        assert request.url.params["contextId"] == "ORG-003107"
        return httpx.Response(200, json=TYPES_PAYLOAD)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await edms.list_document_types(client, context_id="ORG-003107", cadim_type="textfile")

    assert result.total_count == 2
    assert result.has_more is False
    assert result.items[0]["typeName"] == "Report"


async def test_list_document_types_requires_context_id(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=TYPES_PAYLOAD))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError, match="context_id"):
        await edms.list_document_types(client, context_id="  ")
    assert recorder.requests == []


async def test_list_document_types_rejects_bad_cadim(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=TYPES_PAYLOAD))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError, match="cadim_type"):
        await edms.list_document_types(client, context_id="ORG-003107", cadim_type="MOVIE")
    assert recorder.requests == []


async def test_get_current_user(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert urlsplit(str(request.url)).path == "/ws/api/users/current"
        return httpx.Response(200, json=USER_PAYLOAD)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await edms.get_current_user(client)

    assert result["source"] == "edms"
    assert result["username"] == "jdoe"
    assert result["uic"] == 77175


async def test_get_current_user_auth_status_mapped(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(401, json={"error": "unauthorized"}))
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError) as err:
        await edms.get_current_user(client)
    assert err.value.retryable is False
