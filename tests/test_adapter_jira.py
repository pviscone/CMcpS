"""Fixture-driven contract tests for the CERN Jira adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cmcps.adapters import jira
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

ISSUE_FIELDS = {
    "summary": "Fake calibration spike",
    "status": {"name": "Open"},
    "issuetype": {"name": "Bug"},
    "priority": {"name": "Major"},
    "assignee": {"displayName": "Jane Doe"},
    "reporter": {"displayName": "John Smith"},
    "created": "2025-01-02T10:00:00.000+0000",
    "updated": "2025-01-03T11:00:00.000+0000",
    "labels": ["tracker", "daq"],
    "components": [{"name": "DAQ"}, {"name": "Calibration"}],
    "project": {"key": "CMSTRACK", "name": "CMS Tracker"},
    "description": "Spike seen in the fake detector during run 1.",
    "attachment": [
        {
            "filename": "plot.png",
            "size": 1234,
            "mimeType": "image/png",
            "content": "https://its.cern.ch/jira/secure/attachment/1/plot.png",
        }
    ],
    "comment": {
        "total": 1,
        "comments": [
            {
                "author": {"displayName": "Jane Doe"},
                "created": "2025-01-03T09:00:00.000+0000",
                "body": "Reproduced on run 2.",
            }
        ],
    },
}

SEARCH_PAYLOAD = {
    "expand": "names,schema",
    "startAt": 0,
    "maxResults": 50,
    "total": 137,
    "issues": [{"id": "1", "key": "CMSTRACK-42", "fields": ISSUE_FIELDS}],
}

ISSUE_PAYLOAD = {"id": "1", "key": "CMSTRACK-42", "fields": ISSUE_FIELDS}

PROJECT_PAYLOAD = {
    "key": "CMSTRACK",
    "name": "CMS Tracker",
    "description": "Tracker project",
    "lead": {"displayName": "Lead Person"},
    "components": [{"name": "DAQ"}],
    "issueTypes": [{"name": "Bug"}, {"name": "Task"}],
}


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


async def test_search_builds_jql_envelope(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/jira/rest/api/2/search"
        params = request.url.params
        assert params["jql"] == "project = CMSTRACK"
        assert params["startAt"] == "50"
        assert "summary" in params["fields"]
        return httpx.Response(200, json=SEARCH_PAYLOAD)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await jira.search_issues(client, jql="project = CMSTRACK", start_at=50)
    assert result.total_count == 137
    assert result.has_more is True
    item = result.items[0]
    assert item["key"] == "CMSTRACK-42"
    assert item["url"] == "https://its.cern.ch/jira/browse/CMSTRACK-42"
    assert item["status"] == "Open"
    assert item["assignee"] == "Jane Doe"
    assert item["components"] == ["DAQ", "Calibration"]
    assert "description" not in item  # list view excludes heavy fields


async def test_search_rejects_bad_args(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={}))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await jira.search_issues(client, jql="   ")
    with pytest.raises(InvalidInputError):
        await jira.search_issues(client, jql="x", max_results=0)
    with pytest.raises(InvalidInputError):
        await jira.search_issues(client, jql="x", start_at=-1)
    assert recorder.requests == []


async def test_get_issue_detail(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/jira/rest/api/2/issue/CMSTRACK-42"
        return httpx.Response(200, json=ISSUE_PAYLOAD)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await jira.get_issue(client, issue_key="cmstrack-42")
    assert result["key"] == "CMSTRACK-42"
    assert result["description"].startswith("Spike seen")
    att = result["attachments"][0]
    assert att["filename"] == "plot.png"
    assert att["url"].startswith("https://its.cern.ch/jira/")
    assert result["comments"][0]["body"] == "Reproduced on run 2."
    assert result["comment_count"] == 1


async def test_get_issue_validates_key(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={}))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await jira.get_issue(client, issue_key="lowercase")
    with pytest.raises(InvalidInputError):
        await jira.get_issue(client, issue_key="no-number-")
    assert recorder.requests == []


async def test_get_issue_404_maps_upstream(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(404, json={"errorMessages": ["nf"]}))
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await jira.get_issue(client, issue_key="CMSTRACK-99999")


async def test_get_project(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=PROJECT_PAYLOAD))
    client = make_client(tmp_path, recorder)
    result = await jira.get_project(client, project_key="cmstrack")
    assert result["key"] == "CMSTRACK"
    assert result["lead"] == "Lead Person"
    assert result["issue_types"] == ["Bug", "Task"]
