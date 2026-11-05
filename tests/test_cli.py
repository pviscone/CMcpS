"""CLI: guided setup, config/secret storage, status, logout, approve."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx

from cmsmcp import cli
from cmsmcp.cli import (
    IO,
    Setup,
    cmd_approve,
    cmd_logout,
    live_token_check,
    set_option,
    write_secret,
)
from cmsmcp.config.settings import Settings, config_file, secrets_dir
from cmsmcp.credentials import FAIL, OK, TOKENS_BY_NAME, Check, format_status, gather_status
from cmsmcp.policy import PendingApprovals


def _io(answers: list[str], secrets: list[str] | None = None) -> tuple[IO, list[str]]:
    said: list[str] = []
    replies: Iterator[str] = iter(answers)
    hidden: Iterator[str] = iter(secrets or [])
    return (
        IO(
            ask=lambda _p: next(replies, ""),
            secret=lambda _p: next(hidden, ""),
            say=said.append,
            open_url=lambda _u: None,
            run=lambda _argv: 0,
        ),
        said,
    )


def test_options_and_secrets_feed_settings(tmp_path: Path) -> None:
    set_option("lxplus_user", "jdoe")
    set_option("lxplus_user", "jdoe2")
    set_option("personal_sources", "mattermost_dm")
    assert config_file().read_text().count("CMSMCP_LXPLUS_USER") == 1
    path = write_secret("gitlab_token", "glpat-abc")
    assert path.stat().st_mode & 0o777 == 0o600
    settings = Settings(state_dir=tmp_path)
    assert settings.lxplus_user == "jdoe2"
    assert settings.personal_sources == ("mattermost_dm",)
    assert settings.gitlab_token == "glpat-abc"
    set_option("lxplus_user", None)
    assert Settings(state_dir=tmp_path).lxplus_user is None


def test_token_step_stores_and_verifies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def accepted(_settings: Settings, _spec: object) -> Check:
        return Check(OK, "token accepted")

    monkeypatch.setattr(cli, "live_token_check", accepted)
    io, said = _io(["y", "n"], ["indp_secret"])
    Setup(io, lambda: Settings(state_dir=tmp_path)).step_token(TOKENS_BY_NAME["indico"])
    assert (secrets_dir() / "indico_api_token").read_text().strip() == "indp_secret"
    assert any("token accepted" in line for line in said)
    assert not any("indp_secret" in line for line in said)


def test_rejected_token_is_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def rejected(_settings: Settings, _spec: object) -> Check:
        return Check(FAIL, "rejected")

    monkeypatch.setattr(cli, "live_token_check", rejected)
    io, _ = _io(["y", "n", "y"], ["glpat-bad"])
    Setup(io, lambda: Settings(state_dir=tmp_path)).step_token(TOKENS_BY_NAME["gitlab"])
    assert not (secrets_dir() / "gitlab_token").exists()


def test_policy_step_writes_choices(tmp_path: Path) -> None:
    io, _ = _io(["y", "y", "a"])
    Setup(io, lambda: Settings(state_dir=tmp_path)).step_policy()
    settings = Settings(state_dir=tmp_path)
    assert settings.personal_sources == ("mattermost_dm",)
    assert settings.actions_enabled is True
    assert settings.confirm_mode == "approve"
    io, _ = _io(["n", "n"])
    Setup(io, lambda: Settings(state_dir=tmp_path)).step_policy()
    settings = Settings(state_dir=tmp_path)
    assert settings.personal_sources == ()
    assert settings.actions_enabled is False


def test_unknown_setup_step(tmp_path: Path) -> None:
    io, said = _io([])
    assert Setup(io, lambda: Settings(state_dir=tmp_path)).run(["nope"]) == 2
    assert "Unknown setup step" in said[0]


@respx.mock
def test_gitlab_write_scope_rejected_when_actions_off(tmp_path: Path) -> None:
    respx.get("https://gitlab.cern.ch/api/v4/personal_access_tokens/self").mock(
        return_value=httpx.Response(200, json={"scopes": ["api"], "expires_at": "2027-01-01"})
    )
    spec = TOKENS_BY_NAME["gitlab"]
    off = Settings(state_dir=tmp_path, gitlab_token="glpat-x")
    assert asyncio.run(live_token_check(off, spec)).state == FAIL
    on = Settings(state_dir=tmp_path, gitlab_token="glpat-x", actions_enabled=True)
    verdict = asyncio.run(live_token_check(on, spec))
    assert verdict.state == OK and "api" in verdict.detail


def test_status_rows_cover_every_credential(tmp_path: Path) -> None:
    rows = asyncio.run(gather_status(Settings(state_dir=tmp_path), live=False))
    names = [name for name, _ in rows]
    assert names[:4] == ["sso", "kerberos", "grid", "lxplus"]
    assert {"gitlab", "indico", "jira", "mattermost", "github", "actions"} <= set(names)
    assert "sso" in format_status(rows)


def test_logout_removes_named_tokens(tmp_path: Path) -> None:
    write_secret("gitlab_token", "glpat-x")
    write_secret("indico_api_token", "indp_x")
    assert cmd_logout(Settings(state_dir=tmp_path), ["gitlab"], everything=False, purge=False) == 0
    assert not (secrets_dir() / "gitlab_token").exists()
    assert (secrets_dir() / "indico_api_token").exists()


def test_approve_marks_pending_action(tmp_path: Path) -> None:
    settings = Settings(state_dir=tmp_path)
    queue = PendingApprovals(tmp_path / "pending-actions.json")
    key = queue.add("mattermost_post", "post hi", {"message": "hi"}, "d1")
    io, said = _io(["y"])
    assert cmd_approve(settings, None, deny=False, io=io) == 0
    assert queue.load()[key]["approved"] is True
    assert any(f"approval_id={key}" in line for line in said)


def test_default_setup_runs_only_core_and_lists_extras(tmp_path: Path) -> None:
    io, said = _io(["y"])  # keep the (absent) SSO state as is -> skip
    steps: list[str] = []
    setup = Setup(io, lambda: Settings(state_dir=tmp_path))
    setup.step_sso = lambda: steps.append("sso")  # type: ignore[method-assign]
    setup.step_grid = lambda: steps.append("grid")  # type: ignore[method-assign]
    assert setup.run([]) == 0
    assert steps == ["sso"]
    text = "\n".join(said)
    assert "cmsmcp setup grid" in text and "--all" in text
    steps.clear()
    setup.step_token = lambda _spec: steps.append("token")  # type: ignore[method-assign]
    for name in ("kerberos", "lxplus", "rucio", "policy"):
        setattr(setup, f"step_{name}", lambda n=name: steps.append(n))
    setup.run([], tier="tokens")
    assert steps == ["sso"] + ["token"] * 5
    steps.clear()
    setup.run([], tier="all")
    assert steps[0] == "sso" and {"kerberos", "grid", "policy"} <= set(steps)
