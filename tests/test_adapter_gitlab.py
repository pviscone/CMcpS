"""Fixture-driven contract tests for the GitLab adapter."""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from cmsmcp.adapters import gitlab
from cmsmcp.auth.service import AuthService
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.errors import InvalidInputError, UpstreamError
from cmsmcp.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

PROJECT = {
    "id": 42,
    "name": "Example",
    "path_with_namespace": "group/example",
    "description": "Example project",
    "web_url": "https://gitlab.cern.ch/group/example",
    "default_branch": "master",
    "topics": ["analysis"],
    "star_count": 3,
    "forks_count": 1,
    "last_activity_at": "2026-10-20T10:00:00Z",
    "created_at": "2025-01-01T00:00:00Z",
    "visibility": "public",
    "statistics": {"commit_count": 120, "repository_size": 4096, "storage_size": 8192},
    "license": {"name": "MIT"},
    "namespace": {"name": "Group", "path": "group"},
}

FILE_PAYLOAD: dict[str, Any] = {
    "file_name": "notes.md",
    "file_path": "docs/notes.md",
    "size": 5,
    "ref": "master",
    "last_commit_id": "deadbeef",
    "content_sha256": "abc123",
    "content": base64.b64encode(b"hello").decode(),
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
        gitlab_token=token,
    )
    auth = AuthService(settings)
    transport = httpx.MockTransport(recorder)
    client = SafeHttpClient(
        settings, auth_service=auth, client=httpx.AsyncClient(transport=transport)
    )
    return settings, client


async def test_search_projects_builds_url_and_envelope(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v4/projects"
        params = request.url.params
        assert "visibility" not in params
        assert params["search"] == "daq"
        assert params["with_programming_language"] == "Python"
        assert params["topic"] == "analysis"
        assert params["order_by"] == "star_count"
        assert params["sort"] == "asc"
        assert params["per_page"] == "5"
        assert params["page"] == "2"
        return httpx.Response(200, json=[PROJECT])

    settings, client = make_client(tmp_path, Recorder(handler))
    result = await gitlab.search_projects(
        client,
        settings,
        query="daq",
        language="Python",
        topic="analysis",
        sort_by="star_count",
        order="asc",
        per_page=5,
        page=2,
    )
    assert result.source == "gitlab"
    assert result.items[0]["path_with_namespace"] == "group/example"
    assert result.has_more is False
    assert result.query == "daq"


async def test_search_projects_forwards_visibility_when_given(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["visibility"] == "private"
        return httpx.Response(200, json=[PROJECT])

    settings, client = make_client(tmp_path, Recorder(handler))
    result = await gitlab.search_projects(client, settings, visibility="private")
    assert result.items[0]["path_with_namespace"] == "group/example"


async def test_list_merge_requests_builds_url_and_shape(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v4/projects/group/example/merge_requests"
        params = request.url.params
        assert params["state"] == "opened"
        assert params["order_by"] == "updated_at"
        assert params["sort"] == "asc"
        assert params["search"] == "trigger"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 9001,
                    "iid": 12,
                    "title": "Add trigger",
                    "description": "y" * 300,
                    "state": "opened",
                    "draft": True,
                    "source_branch": "feature/trigger",
                    "target_branch": "master",
                    "web_url": "https://gitlab.cern.ch/group/example/-/merge_requests/12",
                    "author": {"username": "bob"},
                    "created_at": "2026-10-20T09:00:00Z",
                    "updated_at": "2026-10-22T11:00:00Z",
                    "merge_status": "can_be_merged",
                }
            ],
        )

    settings, client = make_client(tmp_path, Recorder(handler))
    result = await gitlab.list_merge_requests(
        client,
        settings,
        project="group/example",
        order_by="updated_at",
        sort="asc",
        search="trigger",
    )
    item = result.items[0]
    assert item["iid"] == 12
    assert item["draft"] is True
    assert item["source_branch"] == "feature/trigger"
    assert item["author"] == "bob"
    assert len(item["description_snippet"]) == 200
    assert result.note == "state=opened project=group/example"


async def test_get_project_merges_statistics_and_languages(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/languages"):
            return httpx.Response(200, json={"Python": 90.0, "C++": 10.0})
        assert request.url.params["statistics"] == "true"
        return httpx.Response(200, json=PROJECT)

    settings, client = make_client(tmp_path, Recorder(handler))
    result = await gitlab.get_project(client, settings, project="42")
    assert result["statistics"]["commit_count"] == 120
    assert result["languages"] == {"Python": 90.0, "C++": 10.0}
    assert result["license"] == "MIT"
    assert result["namespace"] == {"name": "Group", "path": "group"}


async def test_project_path_is_url_encoded(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert b"group%2Fexample" in request.url.raw_path
        return httpx.Response(200, json={**FILE_PAYLOAD, "ref": "master"})

    settings, client = make_client(tmp_path, Recorder(handler))
    await gitlab.get_file(
        client, settings, project="group/example", file_path="docs/notes.md", ref="master"
    )


async def test_get_file_decodes_base64_text_and_is_get_only(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(200, json={**FILE_PAYLOAD, "ref": "master"}))
    settings, client = make_client(tmp_path, recorder)
    result = await gitlab.get_file(
        client, settings, project="42", file_path="docs/notes.md", ref="master"
    )
    assert result["content"] == "hello"
    assert result["is_binary"] is False
    assert result["content_sha256"] == "abc123"
    assert b"docs%2Fnotes.md" in recorder.requests[0].url.raw_path
    assert all(request.method == "GET" for request in recorder.requests)


async def test_get_file_detects_binary_by_nul_byte(tmp_path: Path) -> None:
    payload = {
        **FILE_PAYLOAD,
        "file_name": "data.bin",
        "file_path": "data.bin",
        "content": base64.b64encode(b"\x00\x01\x02binary").decode(),
        "size": 9,
    }
    recorder = Recorder(lambda _: httpx.Response(200, json=payload))
    settings, client = make_client(tmp_path, recorder)
    result = await gitlab.get_file(
        client, settings, project="42", file_path="data.bin", ref="master"
    )
    assert result["is_binary"] is True
    assert result["content"] == "[Binary file, 9 bytes]"


async def test_get_file_detects_binary_by_extension(tmp_path: Path) -> None:
    payload = {
        **FILE_PAYLOAD,
        "file_name": "ntuple.root",
        "file_path": "ntuple.root",
        "content": base64.b64encode(b"ROOT-bytes").decode(),
        "size": 10,
    }
    recorder = Recorder(lambda _: httpx.Response(200, json=payload))
    settings, client = make_client(tmp_path, recorder)
    result = await gitlab.get_file(
        client, settings, project="42", file_path="ntuple.root", ref="master"
    )
    assert result["is_binary"] is True
    assert result["content"] == "[Binary file, 10 bytes]"


async def test_get_file_resolves_default_branch_when_ref_empty(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/repository/files/docs%2Fnotes.md"):
            assert request.url.params["ref"] == "master"
            return httpx.Response(200, json=FILE_PAYLOAD)
        return httpx.Response(200, json=PROJECT)

    settings, client = make_client(tmp_path, Recorder(handler))
    result = await gitlab.get_file(client, settings, project="42", file_path="docs/notes.md")
    assert result["ref"] == "master"


async def test_get_readme_iterates_candidates_past_404(tmp_path: Path) -> None:
    attempted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempted.append(request.url.path)
        if request.url.path.endswith("README.rst"):
            payload = {
                **FILE_PAYLOAD,
                "file_name": "README.rst",
                "file_path": "README.rst",
                "content": base64.b64encode(b"title\n=====").decode(),
                "size": 11,
            }
            return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"message": "404 File Not Found"})

    settings, client = make_client(tmp_path, Recorder(handler))
    result = await gitlab.get_readme(client, settings, project="42", ref="master")
    assert result["found"] is True
    assert result["tried"] == ["README.md", "README.rst"]
    assert result["format"] == "restructuredtext"
    assert result["content"] == "title\n====="


async def test_get_readme_not_found_envelope(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(404, json={"message": "404 File Not Found"}))
    settings, client = make_client(tmp_path, recorder)
    result = await gitlab.get_readme(client, settings, project="42", ref="master")
    assert result["found"] is False
    assert result["tried"] == list(gitlab.README_CANDIDATES)
    assert result["content"] is None


async def test_search_code_project_scoped(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v4/projects/42/search"
        params = request.url.params
        assert params["search"] == "analyze"
        assert params["scope"] == "blobs"
        return httpx.Response(
            200,
            json=[
                {
                    "file_name": "run.py",
                    "path": "src/run.py",
                    "project_id": 42,
                    "data": "def analyze():\n",
                    "ref": "master",
                    "startline": 3,
                }
            ],
        )

    settings, client = make_client(tmp_path, Recorder(handler))
    result = await gitlab.search_code(client, settings, search_term="analyze", project="42")
    assert result.items[0]["data"].startswith("def analyze")
    assert result.items[0]["startline"] == 3


async def test_search_issues_validates_shape(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v4/issues"
        assert request.url.params["state"] == "opened"
        assert request.url.params["scope"] == "all"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 7,
                    "iid": 2,
                    "title": "Bug",
                    "description": "x" * 300,
                    "state": "opened",
                    "web_url": "https://gitlab.cern.ch/group/example/-/issues/2",
                    "author": {"username": "alice"},
                    "created_at": "2026-10-01T00:00:00Z",
                }
            ],
        )

    settings, client = make_client(tmp_path, Recorder(handler))
    result = await gitlab.search_issues(client, settings, search_term="bug", state="opened")
    item = result.items[0]
    assert item["author"] == "alice"
    assert len(item["description_snippet"]) == 200


async def test_invalid_scope_state_sort_raise_without_requests(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(200, json=[]))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await gitlab.search_code(client, settings, search_term="x", scope="commits")
    with pytest.raises(InvalidInputError):
        await gitlab.search_issues(client, settings, search_term="x", state="maybe")
    with pytest.raises(InvalidInputError):
        await gitlab.list_tags(client, settings, project="42", sort="sideways")
    with pytest.raises(InvalidInputError):
        await gitlab.search_projects(client, settings, visibility="secret")
    with pytest.raises(InvalidInputError):
        await gitlab.list_merge_requests(client, settings, project="42", state="wip")
    with pytest.raises(InvalidInputError):
        await gitlab.list_merge_requests(client, settings, project="42", order_by="votes")
    with pytest.raises(InvalidInputError):
        await gitlab.list_merge_requests(client, settings, project="42", sort="sideways")
    assert recorder.requests == []


async def test_private_token_header_only_when_configured(tmp_path: Path) -> None:
    without_token: list[httpx.Request] = []
    settings, client = make_client(
        tmp_path,
        Recorder(lambda r: _record_and_respond(without_token, r)),
    )
    await gitlab.search_projects(client, settings)

    with_token: list[httpx.Request] = []
    settings, client = make_client(
        tmp_path,
        Recorder(lambda r: _record_and_respond(with_token, r)),
        token="glpat-test",  # noqa: S106
    )
    await gitlab.search_projects(client, settings)

    assert "private-token" not in without_token[0].headers
    assert with_token[0].headers["PRIVATE-TOKEN"] == "glpat-test"


def _record_and_respond(target: list[httpx.Request], request: httpx.Request) -> httpx.Response:
    target.append(request)
    return httpx.Response(200, json=[])


async def test_not_found_raises_non_retryable_upstream(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(404, json={"message": "not found"}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError) as caught:
        await gitlab.get_project(client, settings, project="999999")
    assert caught.value.retryable is False
    assert caught.value.status_code == 404


async def test_server_error_raises_retryable_upstream(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(503, json={"error": "busy"}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError) as caught:
        await gitlab.search_projects(client, settings)
    assert caught.value.retryable is True


async def test_non_json_body_raises_upstream(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(200, content=b"<html>login</html>"))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await gitlab.search_projects(client, settings)


PIPELINE = {
    "id": 7,
    "iid": 3,
    "status": "failed",
    "ref": "master",
    "sha": "cafebabe",
    "source": "push",
    "created_at": "2026-10-30T10:00:00Z",
    "updated_at": "2026-10-30T10:05:00Z",
    "web_url": "https://gitlab.cern.ch/group/example/-/pipelines/7",
}

PIPELINE_DETAIL = {
    **PIPELINE,
    "before_sha": "00000000",
    "started_at": "2026-10-30T10:00:10Z",
    "finished_at": "2026-10-30T10:05:00Z",
    "committed_at": "2026-10-30T09:59:00Z",
    "duration": 290,
    "queued_duration": 10,
    "coverage": "94.2",
    "user": {"username": "elmer"},
}

JOB = {
    "id": 99,
    "name": "pytest",
    "stage": "test",
    "status": "failed",
    "ref": "master",
    "tag": False,
    "allow_failure": False,
    "created_at": "2026-10-30T10:00:10Z",
    "started_at": "2026-10-30T10:00:20Z",
    "finished_at": "2026-10-30T10:02:00Z",
    "duration": 100.5,
    "queued_duration": 10.0,
    "web_url": "https://gitlab.cern.ch/group/example/-/jobs/99",
    "user": {"username": "elmer"},
    "runner": {"description": "kubernetes-runner"},
    "pipeline": {
        "id": 7,
        "status": "failed",
        "ref": "master",
        "sha": "cafebabe",
        "web_url": "https://gitlab.cern.ch/group/example/-/pipelines/7",
    },
    "commit": {"id": "cafebabe", "short_id": "cafe", "title": "Fix things"},
    "artifacts": [{"file_type": "archive", "filename": "dist.zip", "size": 2048}],
}


async def test_list_pipelines_builds_url_and_envelope(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v4/projects/42/pipelines"
        params = request.url.params
        assert params["status"] == "failed"
        assert params["ref"] == "master"
        assert params["source"] == "push"
        assert params["per_page"] == "5"
        assert params["page"] == "2"
        return httpx.Response(200, json=[PIPELINE])

    settings, client = make_client(tmp_path, Recorder(handler))
    result = await gitlab.list_pipelines(
        client,
        settings,
        project="42",
        status="failed",
        ref="master",
        source="push",
        per_page=5,
        page=2,
    )
    assert result.source == "gitlab"
    assert result.items[0]["id"] == 7
    assert result.items[0]["status"] == "failed"
    assert result.items[0]["web_url"].endswith("/pipelines/7")
    assert result.has_more is False
    assert result.page == 2


async def test_list_pipelines_omits_empty_filters(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        assert "status" not in params
        assert "ref" not in params
        assert "source" not in params
        return httpx.Response(200, json=[PIPELINE])

    settings, client = make_client(tmp_path, Recorder(handler))
    result = await gitlab.list_pipelines(client, settings, project="42")
    assert result.items[0]["ref"] == "master"


async def test_list_pipelines_rejects_unknown_status(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(200, json=[]))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await gitlab.list_pipelines(client, settings, project="42", status="exploding")
    assert recorder.requests == []


async def test_get_pipeline_returns_detail(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v4/projects/42/pipelines/7"
        return httpx.Response(200, json=PIPELINE_DETAIL)

    settings, client = make_client(tmp_path, Recorder(handler))
    detail = await gitlab.get_pipeline(client, settings, project="42", pipeline_id=7)
    assert detail["id"] == 7
    assert detail["duration"] == 290
    assert detail["coverage"] == "94.2"
    assert detail["user"] == "elmer"
    assert detail["status"] == "failed"


async def test_list_pipeline_jobs_builds_url_and_items(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v4/projects/42/pipelines/7/jobs"
        assert request.url.params["scope"] == "failed"
        return httpx.Response(200, json=[JOB])

    settings, client = make_client(tmp_path, Recorder(handler))
    result = await gitlab.list_pipeline_jobs(
        client, settings, project="42", pipeline_id=7, scope="failed"
    )
    job = result.items[0]
    assert job["id"] == 99
    assert job["name"] == "pytest"
    assert job["stage"] == "test"
    assert job["user"] == "elmer"
    assert job["runner"] == "kubernetes-runner"
    assert result.note == "project=42 pipeline=7"


async def test_list_pipeline_jobs_rejects_unknown_scope(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(200, json=[]))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await gitlab.list_pipeline_jobs(client, settings, project="42", pipeline_id=7, scope="lost")
    assert recorder.requests == []


async def test_get_job_returns_detail_with_pipeline_and_artifacts(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v4/projects/42/jobs/99"
        return httpx.Response(200, json=JOB)

    settings, client = make_client(tmp_path, Recorder(handler))
    detail = await gitlab.get_job(client, settings, project="42", job_id=99)
    assert detail["id"] == 99
    assert detail["pipeline"]["id"] == 7
    assert detail["commit"]["short_id"] == "cafe"
    assert detail["artifacts"] == [{"file_type": "archive", "filename": "dist.zip", "size": 2048}]


async def test_get_job_trace_returns_tail(tmp_path: Path) -> None:
    log = "\n".join(f"line {i}" for i in range(1000))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v4/projects/42/jobs/99/trace"
        return httpx.Response(200, content=log.encode(), headers={"content-type": "text/plain"})

    settings, client = make_client(tmp_path, Recorder(handler))
    untruncated = await gitlab.get_job_trace(client, settings, project="42", job_id=99)
    assert untruncated["truncated"] is False
    assert untruncated["trace"] == log
    assert untruncated["total_chars"] == len(log)

    tailed = await gitlab.get_job_trace(client, settings, project="42", job_id=99, tail_chars=100)
    assert tailed["truncated"] is True
    assert tailed["total_chars"] == len(log)
    assert len(tailed["trace"]) == 100
    assert tailed["trace"] == log[-100:]
