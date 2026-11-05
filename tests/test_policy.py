"""Policy engine: confirmation flows, approvals, personal opt-in, audit."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cmsmcp.config.settings import Settings
from cmsmcp.policy import (
    ActionDeclinedError,
    ApprovedAction,
    ConfirmationRequiredError,
    Effect,
    PersonalSourceDisabledError,
    Policy,
)


class _Accepted:
    action = "accept"

    def __init__(self, approve: bool) -> None:
        self.data = type("D", (), {"approve": approve})()


class _Ctx:
    def __init__(self, result: Any = None, *, fail: bool = False) -> None:
        self.result = result
        self.fail = fail
        self.messages: list[str] = []

    async def elicit(self, message: str, schema: type) -> Any:  # noqa: ARG002
        self.messages.append(message)
        if self.fail:
            raise RuntimeError("client does not support elicitation")
        return self.result


def _policy(tmp_path: Path, **kw: Any) -> Policy:
    return Policy(Settings(state_dir=tmp_path, actions_enabled=True, **kw))


CALL = {
    "tool": "mattermost_post",
    "summary": "post to town-square",
    "details": {"channel": "town-square", "message": "hi"},
    "hosts": ("mattermost.web.cern.ch",),
}


async def test_actions_disabled_refuses(tmp_path: Path) -> None:
    policy = Policy(Settings(state_dir=tmp_path))
    with pytest.raises(ActionDeclinedError):
        await policy.confirm(_Ctx(_Accepted(True)), **CALL)


async def test_elicitation_accept_returns_scoped_approval(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    policy.note_read("twiki")
    ctx = _Ctx(_Accepted(True))
    approval = await policy.confirm(ctx, **CALL)
    assert isinstance(approval, ApprovedAction)
    assert approval.allows_host("mattermost.web.cern.ch")
    assert not approval.allows_host("evil.example.org")
    # The prompt shows the exact payload and the session's provenance.
    assert '"message": "hi"' in ctx.messages[0]
    assert "twiki" in ctx.messages[0]


async def test_elicitation_decline_refuses(tmp_path: Path) -> None:
    with pytest.raises(ActionDeclinedError):
        await _policy(tmp_path).confirm(_Ctx(_Accepted(False)), **CALL)
    declined = type("R", (), {"action": "decline", "data": None})()
    with pytest.raises(ActionDeclinedError):
        await _policy(tmp_path).confirm(_Ctx(declined), **CALL)


async def test_no_elicitation_parks_action_for_out_of_band_approval(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    with pytest.raises(ConfirmationRequiredError) as parked:
        await policy.confirm(_Ctx(fail=True), **CALL)
    key = next(iter(policy.pending.load()))
    assert key in parked.value.message
    # Not yet approved: the retry is refused.
    with pytest.raises(ActionDeclinedError):
        await policy.confirm(None, approval_id=key, **CALL)
    policy.pending.set_approved(key, True)
    # Different parameters cannot ride on the approval.
    other = {**CALL, "details": {"channel": "town-square", "message": "something else"}}
    with pytest.raises(ActionDeclinedError):
        await policy.confirm(None, approval_id=key, **other)
    approval = await policy.confirm(None, approval_id=key, **CALL)
    assert approval.tool == "mattermost_post"
    # Single use.
    with pytest.raises(ActionDeclinedError):
        await policy.confirm(None, approval_id=key, **CALL)


async def test_approve_mode_never_elicits_and_deny_mode_refuses(tmp_path: Path) -> None:
    ctx = _Ctx(_Accepted(True))
    with pytest.raises(ConfirmationRequiredError):
        await _policy(tmp_path, confirm_mode="approve").confirm(ctx, **CALL)
    assert ctx.messages == []
    with pytest.raises(ActionDeclinedError):
        await _policy(tmp_path / "d", confirm_mode="deny").confirm(ctx, **CALL)


def test_personal_sources_opt_in(tmp_path: Path) -> None:
    policy = Policy(Settings(state_dir=tmp_path))
    with pytest.raises(PersonalSourceDisabledError):
        policy.require_personal("mattermost_dm", "a direct message")
    Policy(Settings(state_dir=tmp_path, personal_sources=("mattermost_dm",))).require_personal(
        "mattermost_dm", "a direct message"
    )
    assert Policy(Settings(state_dir=tmp_path, personal_sources="all")).personal_allowed("x")


def test_audit_log_is_private_and_clips_values(tmp_path: Path) -> None:
    policy = Policy(Settings(state_dir=tmp_path))
    policy.audit("t", Effect.READ, {"q": "x" * 500}, status="ok", elapsed=0.01)
    path = tmp_path / "audit.jsonl"
    assert path.stat().st_mode & 0o777 == 0o600
    record = json.loads(path.read_text().splitlines()[0])
    assert record["tool"] == "t" and record["status"] == "ok"
    assert len(record["args"]["q"]) < 260
