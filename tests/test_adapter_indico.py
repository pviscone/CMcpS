"""Fixture-driven contract tests for the Indico adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from cmcps.adapters import indico
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

EVENT = {
    "id": "123456",
    "title": "CMS Weekly Meeting",
    "description": "Detector operations",
    "startDate": {"date": "2026-10-26 14:00:00", "tz": "Europe/Zurich"},
    "endDate": {"date": "2026-10-26 15:00:00", "tz": "Europe/Zurich"},
    "location": "CERN",
    "room": "40-S2-A01",
    "type": "meeting",
    "category": "CMS",
    "categoryId": "68",
    "url": "https://indico.cern.ch/event/123456/",
}

EVENT_WITH_FILES: dict[str, Any] = {
    **EVENT,
    "folders": [
        {
            "id": "f1",
            "title": "Slides",
            "materials": [
                {
                    "id": "10",
                    "title": "Slides",
                    "resources": [{"id": "r1", "name": "slides.pdf"}],
                }
            ],
        }
    ],
    "contributions": [
        {
            "id": "c1",
            "title": "Status report",
            "duration": "20 minutes",
            "startDate": {"date": "2026-10-26 14:00:00"},
            "location": "CERN",
            "room": "40-S2-A01",
            "speakers": [{"fullName": "Alice Example", "affiliation": "CERN"}],
            "folders": [
                {
                    "id": "f2",
                    "title": "Talk files",
                    "materials": [
                        {
                            "id": "11",
                            "title": "Talk files",
                            "resources": [
                                {
                                    "id": "r2",
                                    "name": "talk.pdf",
                                    "url": "https://indico.cern.ch/event/123456/contributions/c1/attachments/2/3/talk.pdf",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    ],
}


class Recorder:
    """Mock-transport handler that records every request."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def make_client(
    tmp_path: Path, recorder: Recorder, *, token: str | None = None
) -> tuple[Settings, SafeHttpClient]:
    settings = Settings(
        state_dir=tmp_path,
        require_sso=False,
        browser_executable=None,
        indico_api_token=token,
    )
    auth = AuthService(settings)
    transport = httpx.MockTransport(recorder)
    client = SafeHttpClient(
        settings, auth_service=auth, client=httpx.AsyncClient(transport=transport)
    )
    return settings, client


async def test_search_events_builds_url_and_envelope(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/export/categ/68.json"
        params = request.url.params
        assert params["pretty"] == "yes"
        assert params["limit"] == "10"
        assert params["offset"] == "20"
        assert params["from"] == "2026-10-01"
        assert params["to"] == "2026-10-31"
        assert params["onlypublic"] == "yes"
        return httpx.Response(200, json={"count": 30, "results": [EVENT]})

    settings, client = make_client(tmp_path, Recorder(handler))
    result = await indico.search_events(
        client,
        settings,
        category_id="68",
        from_date="2026-10-01",
        to_date="2026-10-31",
        limit=10,
        offset=20,
        only_public=True,
    )
    assert result.source == "indico"
    assert result.total_count == 30
    assert result.page == 3
    assert result.size == 10
    assert result.has_more is True
    event = result.items[0]
    assert event["id"] == "123456"
    assert event["url"] == "https://indico.cern.ch/event/123456/"
    assert event["timezone"] == "Europe/Zurich"


async def test_search_events_accepts_singleton_results_dict(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(200, json={"count": 1, "results": EVENT}))
    settings, client = make_client(tmp_path, recorder)
    result = await indico.search_events(client, settings, category_id="0")
    assert len(result.items) == 1
    assert result.has_more is False


async def test_search_events_rejects_bad_dates(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(200, json={"count": 0, "results": []}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await indico.search_events(client, settings, from_date="26-10-2026")
    with pytest.raises(InvalidInputError):
        await indico.search_events(client, settings, to_date="October")
    assert recorder.requests == []


async def test_full_text_search_groups_by_type_and_is_get_only(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search/api/search"
        assert request.url.params["q"] == "tracker"
        return httpx.Response(200, json={"results": [{"title": "hit"}]})

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await indico.search(client, settings, query="tracker", limit=5)
    assert set(result["results"]) == {
        "category",
        "event",
        "contribution",
        "subcontribution",
        "attachment",
        "event_note",
    }
    assert result["total"] == 6
    assert len(recorder.requests) == 6
    assert all(request.method == "GET" for request in recorder.requests)


async def test_get_event_merges_contributions_and_sessions(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        detail = request.url.params.get("detail")
        payload = dict(EVENT)
        if detail == "contributions":
            payload["contributions"] = EVENT_WITH_FILES["contributions"]
        else:
            payload["sessions"] = [{"id": "s1", "title": "Session"}]
        return httpx.Response(200, json={"count": 1, "results": [payload]})

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await indico.get_event(
        client,
        settings,
        event_id="123456",
        include_contributions=True,
        include_sessions=True,
    )
    assert len(recorder.requests) == 2
    assert result["contribution_count"] == 1
    assert result["contributions"][0]["speakers"] == [
        {"name": "Alice Example", "affiliation": "CERN"}
    ]
    assert result["sessions"] == [{"id": "s1", "title": "Session"}]


async def test_get_event_unknown_id_raises(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(200, json={"count": 0, "results": []}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await indico.get_event(client, settings, event_id="999999")


async def test_contributions_support_subcontributions(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["detail"] == "subcontributions"
        payload = {
            **EVENT,
            "contributions": [
                {
                    "id": "c1",
                    "title": "Talk",
                    "subContributions": [{"id": "sc1", "title": "Part 1"}],
                }
            ],
        }
        return httpx.Response(200, json={"count": 1, "results": [payload]})

    settings, client = make_client(tmp_path, Recorder(handler))
    result = await indico.get_event_contributions(
        client, settings, event_id="123456", include_subcontributions=True
    )
    assert result["total_count"] == 1
    assert result["items"][0]["subcontributions"][0]["title"] == "Part 1"


async def test_event_files_collects_resources_and_builds_urls(tmp_path: Path) -> None:
    recorder = Recorder(
        lambda _: httpx.Response(200, json={"count": 1, "results": [EVENT_WITH_FILES]})
    )
    settings, client = make_client(tmp_path, recorder)
    result = await indico.get_event_files(client, settings, event_id="123456")
    assert result["total_count"] == 2
    by_name = {item["filename"]: item for item in result["items"]}
    assert by_name["slides.pdf"]["download_url"] == (
        "https://indico.cern.ch/export/event/123456/material/10/r1.bin"
    )
    assert by_name["slides.pdf"]["contribution_id"] is None
    assert by_name["talk.pdf"]["contribution_id"] == "c1"
    assert by_name["talk.pdf"]["download_url"] == (
        "https://indico.cern.ch/event/123456/contributions/c1/attachments/2/3/talk.pdf"
    )


async def test_download_file_rejects_non_indico_hosts(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(200, content=b"data"))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await indico.download_file(
            client, settings, download_url="https://evil.example.org/file.pdf"
        )
    assert recorder.requests == []


async def test_download_file_streams_with_bearer_token(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer indp_test"
        return httpx.Response(200, content=b"%PDF-1.7 bytes")

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder, token="indp_test")  # noqa: S106
    result = await indico.download_file(
        client,
        settings,
        download_url="https://indico.cern.ch/event/1/material/10/r1.bin",
    )
    path = Path(result["path"])
    assert path.is_file()
    assert path.parent == settings.download_dir
    assert result["size_bytes"] == len(b"%PDF-1.7 bytes")
    assert path.read_bytes() == b"%PDF-1.7 bytes"


async def test_bearer_header_only_when_token_configured(tmp_path: Path) -> None:
    without_token: list[httpx.Request] = []
    with_token: list[httpx.Request] = []

    def record(target: list[httpx.Request]) -> Recorder:
        return Recorder(lambda request: _record_and_respond(target, request))

    async def _search(tmp: Path, target: list[httpx.Request], token: str | None) -> None:
        settings = Settings(
            state_dir=tmp,
            require_sso=False,
            indico_api_token=token,
        )
        auth = AuthService(settings)
        client = SafeHttpClient(
            settings,
            auth_service=auth,
            client=httpx.AsyncClient(transport=httpx.MockTransport(record(target))),
        )
        await indico.get_user_info(client, settings) if token else await indico.search_events(
            client, settings
        )

    await _search(tmp_path / "a", without_token, None)
    await _search(tmp_path / "b", with_token, "indp_test")
    assert "Authorization" not in without_token[0].headers
    assert with_token[0].headers["Authorization"] == "Bearer indp_test"


def _record_and_respond(target: list[httpx.Request], request: httpx.Request) -> httpx.Response:
    target.append(request)
    if request.url.path == "/api/user/":
        return httpx.Response(200, json={"id": 1, "email": "a@cern.ch"})
    return httpx.Response(200, json={"count": 0, "results": []})


async def test_user_info_requires_token(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(200, json={}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await indico.get_user_info(client, settings)
    assert recorder.requests == []


async def test_error_status_raises_structured_upstream(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(503, json={"error": "busy"}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError) as caught:
        await indico.search_events(client, settings)
    assert caught.value.status_code == 503
    assert caught.value.retryable is True


async def test_non_json_body_raises_upstream(tmp_path: Path) -> None:
    recorder = Recorder(
        lambda _: httpx.Response(200, content=b"<html>Making sure you are not a bot</html>")
    )
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await indico.search_events(client, settings)
