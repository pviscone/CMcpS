"""User-confirmed web actions: the write path and the action tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from cmsmcp.adapters import actions, mattermost
from cmsmcp.app import build_server
from cmsmcp.auth.service import AuthService
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.errors import InvalidInputError, PolicyViolationError
from cmsmcp.net.client import SafeHttpClient
from cmsmcp.policy import ApprovedAction, PendingApprovals

CHANNEL = "c" * 26


def _approval(tool: str, host: str) -> ApprovedAction:
    return ApprovedAction(tool=tool, summary="s", hosts=(host,), digest="d", _nonce="n")


def _client(tmp_path: Path, handler: Any, **kw: Any) -> tuple[SafeHttpClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    settings = Settings(state_dir=tmp_path, require_sso=False, **kw)
    client = SafeHttpClient(
        settings,
        AuthService(settings),
        client=httpx.AsyncClient(transport=httpx.MockTransport(record)),
    )
    return client, seen


async def test_write_path_requires_a_scoped_approval(tmp_path: Path) -> None:
    client, seen = _client(tmp_path, lambda _r: httpx.Response(201, json={}))
    url = "https://mattermost.web.cern.ch/api/v4/posts"
    with pytest.raises(PolicyViolationError):
        await client.request_action("not-an-approval", "POST", url, json_body={})  # type: ignore[arg-type]
    with pytest.raises(PolicyViolationError):
        await client.request_action(_approval("t", "gitlab.cern.ch"), "POST", url, json_body={})
    with pytest.raises(PolicyViolationError):
        await client.request_action(
            _approval("t", "mattermost.web.cern.ch"), "DELETE", url, json_body={}
        )
    assert seen == []


async def test_mattermost_post_shape(tmp_path: Path) -> None:
    client, seen = _client(tmp_path, lambda _r: httpx.Response(201, json={"id": "p" * 26}))
    out = await actions.mattermost_post(
        client,
        client.settings,
        _approval("mattermost_post", actions.MATTERMOST_HOST),
        channel_id=CHANNEL,
        message="hello",
        root_id="r" * 26,
    )
    request = seen[0]
    assert request.method == "POST" and request.url.path == "/api/v4/posts"
    assert json.loads(request.content) == {
        "channel_id": CHANNEL,
        "message": "hello",
        "root_id": "r" * 26,
    }
    assert request.headers["X-Requested-With"] == "XMLHttpRequest"
    assert out["url"].endswith("/_redirect/pl/" + "p" * 26)


async def test_gitlab_comment_needs_token_and_sends_only_the_token(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, lambda _r: httpx.Response(201, json={"id": 7}))
    with pytest.raises(InvalidInputError) as caught:
        await actions.gitlab_comment(
            client,
            client.settings,
            _approval("gitlab_comment", actions.GITLAB_HOST),
            project="g/p",
            kind="issue",
            iid=3,
            body="x",
        )
    assert "cmsmcp setup gitlab" in (caught.value.instructions or "")
    client, seen = _client(
        tmp_path, lambda _r: httpx.Response(201, json={"id": 7}), gitlab_token="glpat-x"
    )
    out = await actions.gitlab_comment(
        client,
        client.settings,
        _approval("gitlab_comment", actions.GITLAB_HOST),
        project="g/p",
        kind="merge_request",
        iid=3,
        body="LGTM",
    )
    request = seen[0]
    assert request.url.raw_path == b"/api/v4/projects/g%2Fp/merge_requests/3/notes"
    assert request.headers["PRIVATE-TOKEN"] == "glpat-x"
    assert "cookie" not in request.headers
    assert out == {"commented": True, "note_id": 7, "project": "g/p", "iid": 3}


async def test_action_tools_absent_unless_enabled(tmp_path: Path) -> None:
    names = {t.name for t in await build_server(Settings(state_dir=tmp_path)).list_tools()}
    assert not names & {"mattermost_post", "gitlab_comment", "jira_comment"}
    enabled = build_server(Settings(state_dir=tmp_path, actions_enabled=True))
    names = {t.name for t in await enabled.list_tools()}
    assert {"mattermost_post", "gitlab_comment", "gitlab_create_issue", "jira_comment"} <= names
    tool = next(t for t in await enabled.list_tools() if t.name == "mattermost_post")
    assert tool.annotations is not None and tool.annotations.destructive_hint is True


async def test_mattermost_post_tool_parks_then_posts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted: list[dict[str, Any]] = []

    async def channel(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"display_name": "Town Square", "type": "O"}

    async def post(_c: Any, _s: Any, approval: ApprovedAction, **kw: Any) -> dict[str, Any]:
        assert approval.tool == "mattermost_post"
        posted.append(kw)
        return {"posted": True}

    monkeypatch.setattr(mattermost, "get_channel", channel)
    monkeypatch.setattr(actions, "mattermost_post", post)
    server = build_server(
        Settings(
            state_dir=tmp_path, require_sso=False, actions_enabled=True, confirm_mode="approve"
        )
    )
    args = {"channel_id": CHANNEL, "message": "meeting moved to 15:00"}
    first = json.loads((await server.call_tool("mattermost_post", args)).content[0].text)
    assert first["status"] == "error" and posted == []
    queue = PendingApprovals(tmp_path / "pending-actions.json")
    key, entry = next(iter(queue.load().items()))
    assert entry["details"]["channel"] == "Town Square"
    assert entry["details"]["message"] == "meeting moved to 15:00"
    queue.set_approved(key, True)
    done = json.loads(
        (await server.call_tool("mattermost_post", {**args, "approval_id": key})).content[0].text
    )
    assert done["status"] == "ok"
    assert posted == [{"channel_id": CHANNEL, "message": "meeting moved to 15:00", "root_id": None}]


def test_gitlab_quick_actions_are_refused() -> None:
    with pytest.raises(InvalidInputError):
        actions.check_no_quick_actions("looks good\n/approve\n", "comment")
    assert actions.check_no_quick_actions("see path a/b and 1/2", "comment")


async def test_lazy_mode_keeps_act_tools_listed_and_out_of_cmsmcp_call(tmp_path: Path) -> None:
    server = build_server(
        Settings(state_dir=tmp_path, actions_enabled=True, tool_mode="lazy", output_format="text")
    )
    tools = {t.name: t for t in await server.list_tools()}
    assert tools["mattermost_post"].annotations.destructive_hint is True
    assert "mattermost_search_posts" not in tools
    via_call = await server.call_tool(
        "cmsmcp_call", {"name": "mattermost_post", "arguments": {"channel_id": "x"}}
    )
    assert via_call.content[0].text.startswith("ERROR INVALID_INPUT")


async def test_generic_fetch_and_download_refuse_mattermost_without_opt_in(tmp_path: Path) -> None:
    server = build_server(Settings(state_dir=tmp_path, require_sso=False))
    for tool, args in (
        ("cern_fetch_page", {"url": "https://mattermost.web.cern.ch/api/v4/channels/x/posts"}),
        ("cern_download_file", {"url": "https://mattermost.web.cern.ch/api/v4/files/x"}),
    ):
        out = json.loads((await server.call_tool(tool, args)).content[0].text)
        assert out["status"] == "error" and "mattermost_" in out["error"]["instructions"]
