"""lxplus adapter and tools with SSH stubbed out."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cmsmcp.adapters import lxplus
from cmsmcp.app import build_server
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.errors import InvalidInputError, PolicyViolationError
from cmsmcp.net.proc import CommandResult
from cmsmcp.policy import ApprovedAction, PendingApprovals


@pytest.fixture
def ssh(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    async def fake_run(argv: list[str], **_kw: Any) -> CommandResult:
        calls.append(list(argv))
        return CommandResult(exit_code=0, stdout=b"remote output\n", stderr=b"")

    async def ticket() -> None:
        return None

    monkeypatch.setattr(lxplus, "run_command", fake_run)
    monkeypatch.setattr(lxplus, "_ensure_ticket", ticket)
    return calls


def _settings(tmp_path: Path, **kw: Any) -> Settings:
    return Settings(state_dir=tmp_path, lxplus_user="jdoe", ssh_executable="/usr/bin/ssh", **kw)


def _approval(tool: str, hosts: tuple[str, ...] = ("lxplus",)) -> ApprovedAction:
    return ApprovedAction(tool=tool, summary="s", hosts=hosts, digest="d", _nonce="n")


async def test_eos_ls_quotes_the_remote_command(tmp_path: Path, ssh: list[list[str]]) -> None:
    out = await lxplus.eos_ls(_settings(tmp_path), path="/eos/user/j/jdoe/my dir")
    argv = ssh[0]
    assert argv[0] == "/usr/bin/ssh"
    assert "GSSAPIAuthentication=yes" in argv and "BatchMode=yes" in argv
    assert argv[-2] == "jdoe@lxplus.cern.ch"
    assert argv[-1] == "eos ls -l '/eos/user/j/jdoe/my dir'"
    assert out["output"] == "remote output\n"


@pytest.mark.parametrize(
    "bad", ["/etc/passwd", "eos/user/x", "/eos/user/../../etc", "/eos/x;rm -rf ~", "/eos/$(id)"]
)
def test_paths_are_validated(bad: str) -> None:
    with pytest.raises(InvalidInputError):
        lxplus.check_path(bad)


@pytest.mark.parametrize(
    "cmd", ["cat /tmp/krb5cc_1000", "cp ~/.ssh/id_rsa /eos/x", "cat ~/.globus/userkey.pem", "kinit"]
)
def test_generic_commands_touching_credentials_are_refused(cmd: str) -> None:
    with pytest.raises(PolicyViolationError):
        lxplus.check_generic_command(cmd)


async def test_actions_need_matching_approval(tmp_path: Path, ssh: list[list[str]]) -> None:
    settings = _settings(tmp_path)
    with pytest.raises(PolicyViolationError):
        await lxplus.condor_remove(settings, job_id="12", approval=_approval("lxplus_run"))
    await lxplus.condor_remove(settings, job_id="12", approval=_approval("lxplus_condor_remove"))
    assert ssh[-1][-1] == "condor_rm 12"
    sha = "a" * 64
    await lxplus.condor_submit(
        settings,
        submit_file="/afs/cern.ch/user/j/jdoe/-verbose",
        sha256=sha,
        approval=_approval("lxplus_condor_submit"),
    )
    # The hash the user confirmed is re-checked, and ./ keeps a dash-name from
    # becoming a condor_submit option.
    assert ssh[-1][-1] == (
        f"cd /afs/cern.ch/user/j/jdoe && echo '{sha}  ./-verbose' | sha256sum -c --quiet"
        " && condor_submit ./-verbose"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/afs/cern.ch/user/j/jdoe/.ssh/id_ed25519",
        "/afs/cern.ch/user/j/jdoe/private/jdoe.keytab",
        "/afs/cern.ch/user/j/jdoe/.globus/userkey.pem",
        "/eos/user/j/jdoe/private/notes.txt",
    ],
)
async def test_reads_never_touch_credentials(
    tmp_path: Path, ssh: list[list[str]], path: str
) -> None:
    with pytest.raises(PolicyViolationError):
        await lxplus.read_file(_settings(tmp_path), path=path)
    with pytest.raises(PolicyViolationError):
        await lxplus.download_file(_settings(tmp_path), path=path)
    assert ssh == []


async def test_crab_and_submit_only_from_own_areas(tmp_path: Path, ssh: list[list[str]]) -> None:
    settings = _settings(tmp_path)
    foreign = "/afs/cern.ch/user/a/attacker/public/CMSSW_13_0_0/src"
    with pytest.raises(PolicyViolationError):
        await lxplus.crab_status(settings, cmssw_src=foreign, task_dir="crab_x")
    with pytest.raises(PolicyViolationError):
        await lxplus.crab_status(
            settings,
            cmssw_src="/afs/cern.ch/user/j/jdoe/CMSSW_13_0_0/src",
            task_dir="/eos/user/a/attacker/crab_x",
        )
    with pytest.raises(PolicyViolationError):
        await lxplus.inspect_submit_file(settings, submit_file="/eos/user/a/attacker/job.sub")
    assert ssh == []
    await lxplus.crab_status(
        settings, cmssw_src="/afs/cern.ch/work/j/jdoe/CMSSW_13_0_0/src", task_dir="crab_ok"
    )
    assert ssh[-1][-1].endswith("crab status -d crab_ok")


async def test_unconfigured_user_points_to_setup(tmp_path: Path) -> None:
    with pytest.raises(InvalidInputError) as caught:
        await lxplus.eos_ls(Settings(state_dir=tmp_path), path="/eos/user/j")
    assert "cmsmcp setup lxplus" in (caught.value.instructions or "")


async def test_download_writes_private_file(tmp_path: Path, ssh: list[list[str]]) -> None:
    del ssh
    out = await lxplus.download_file(_settings(tmp_path), path="/eos/user/j/jdoe/plot.png")
    path = Path(out["path"])
    assert path.read_bytes() == b"remote output\n"
    assert path.stat().st_mode & 0o777 == 0o600


async def test_action_tools_only_exist_when_enabled(tmp_path: Path) -> None:
    names = {t.name for t in await build_server(_settings(tmp_path)).list_tools()}
    assert "lxplus_eos_ls" in names and "lxplus_run" not in names
    enabled = build_server(_settings(tmp_path, actions_enabled=True))
    assert "lxplus_run" in {t.name for t in await enabled.list_tools()}


async def test_lxplus_run_parks_then_runs_after_out_of_band_approval(
    tmp_path: Path, ssh: list[list[str]]
) -> None:
    server = build_server(_settings(tmp_path, actions_enabled=True, confirm_mode="approve"))
    args = {"command": "ls /eos/user/j/jdoe"}
    first = json.loads((await server.call_tool("lxplus_run", args)).content[0].text)
    assert first["status"] == "error" and "cmsmcp approve" in first["error"]["instructions"]
    assert ssh == []
    queue = PendingApprovals(tmp_path / "pending-actions.json")
    key = next(iter(queue.load()))
    queue.set_approved(key, True)
    second = json.loads(
        (await server.call_tool("lxplus_run", {**args, "approval_id": key})).content[0].text
    )
    assert second["status"] == "ok"
    assert ssh[-1][-1] == "ls /eos/user/j/jdoe"
