"""Contract tests for the dasgoclient-backed DAS adapter."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

import pytest

from cmcps.adapters import das
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.proc import CommandResult

FakeRunner = Callable[[Sequence[str]], Awaitable[CommandResult]]

DATASET = "/DYToLL_M-50_TuneCP5_14TeV-pythia8/Campaign-v1/MINIAODSIM"


def _record(entity: str, attrs: dict) -> dict:
    return {"das": {"instance": "prod/global"}, "qhash": "x", entity: [attrs]}


def make_runner(
    payload: list[dict], *, expect_limit: str | None = None
) -> tuple[FakeRunner, list[list[str]]]:
    """Runner stub returning canned -json stdout; records argv per call."""
    calls: list[list[str]] = []

    async def run(argv: Sequence[str]) -> CommandResult:
        calls.append(list(argv))
        if expect_limit is not None:
            idx = argv.index("-limit") + 1
            assert argv[idx] == expect_limit
        return CommandResult(exit_code=0, stdout=json.dumps(payload).encode(), stderr=b"")

    return run, calls


async def test_search_builds_dasgoclient_argv() -> None:
    runner, calls = make_runner([], expect_limit="4")
    await das.search(runner, query="dataset=/TT*/RunII*/AODSIM", limit=3, offset=50)
    argv = calls[0]
    assert argv[:1] == ["-query"]
    assert argv[1] == "dataset=/TT*/RunII*/AODSIM instance=prod/global"
    assert "-json" in argv
    assert argv[argv.index("-idx") + 1] == "50"


async def test_search_parses_records_and_dedups() -> None:
    payload = [
        _record("dataset", {"name": DATASET, "nevents": 100}),
        _record("dataset", {"name": DATASET, "nevents": 100}),  # replica duplicate
        _record("site", {"name": "T2_CH_CERN"}),
    ]
    runner, _ = make_runner(payload)
    result = await das.search(runner, query="dataset=/X/*/Y", limit=10)
    assert result.size == 2
    assert [item["entity"] for item in result.items] == ["dataset", "site"]
    assert result.items[0]["name"] == DATASET
    assert result.total_count is None
    assert result.has_more is False
    assert result.query == "[prod/global] dataset=/X/*/Y"


async def test_search_has_more_from_limit_plus_one_probe() -> None:
    payload = [_record("dataset", {"name": f"/A{i}/B/C"}) for i in range(4)]
    runner, _ = make_runner(payload, expect_limit="4")
    result = await das.search(runner, query="dataset=/A*/B/C", limit=3)
    assert result.size == 3
    assert result.has_more is True


async def test_search_rejects_bad_args_without_running() -> None:
    runner, calls = make_runner([])
    with pytest.raises(InvalidInputError):
        await das.search(runner, query="")
    with pytest.raises(InvalidInputError):
        await das.search(runner, query="dataset=/X;rm -rf/Y/Z")
    with pytest.raises(InvalidInputError):
        await das.search(runner, query="dataset=/X/Y/Z", limit=0)
    with pytest.raises(InvalidInputError):
        await das.search(runner, query="dataset=/X/Y/Z", limit=51)
    with pytest.raises(InvalidInputError):
        await das.search(runner, query="dataset=/X/Y/Z", offset=-1)
    with pytest.raises(InvalidInputError):
        await das.search(runner, query="dataset=/X/Y/Z", instance="prod/global evil")
    assert calls == []


async def test_search_ql_error_maps_invalid_input() -> None:
    async def run(_argv: Sequence[str]) -> CommandResult:
        stderr = b"ERROR DAS QL ERROR, msg=Not a DAS key"
        return CommandResult(exit_code=17, stdout=b"", stderr=stderr)

    with pytest.raises(InvalidInputError):
        await das.search(run, query="bogus")


async def test_search_validation_error_on_stdout_maps_invalid_input() -> None:
    """dasgoclient emits pattern validation errors on stdout (exit 18), not stderr."""
    async def run(_argv: Sequence[str]) -> CommandResult:
        return CommandResult(
            exit_code=18, stdout=b"Validation error: unmatched dataset pattern", stderr=b""
        )

    with pytest.raises(InvalidInputError):
        await das.search(run, query="dataset=/DYToEE*Phase2*/*")


async def test_search_nonzero_exit_maps_upstream() -> None:
    async def run(_argv: Sequence[str]) -> CommandResult:
        return CommandResult(exit_code=2, stdout=b"", stderr=b"panic: boom")

    with pytest.raises(UpstreamError):
        await das.search(run, query="dataset=/X/Y/Z")


async def test_search_non_json_maps_upstream() -> None:
    async def run(_argv: Sequence[str]) -> CommandResult:
        return CommandResult(exit_code=0, stdout=b"<html>ui</html>", stderr=b"")

    with pytest.raises(UpstreamError):
        await das.search(run, query="dataset=/X/Y/Z")


async def test_get_dataset_info_exact_match() -> None:
    payload = [_record("dataset", {"name": DATASET, "datatype": "mc"})]
    runner, calls = make_runner(payload)
    result = await das.get_dataset_info(runner, dataset=DATASET)
    assert result["datatype"] == "mc"
    assert result["source"] == "das"
    assert result["url"] == f"https://cmsweb.cern.ch/das/request?input=dataset%3D{DATASET}"
    argv_query = calls[0][1]
    assert argv_query == f"dataset={DATASET} instance=prod/global"


async def test_get_dataset_info_rejects_wildcards_without_running() -> None:
    runner, calls = make_runner([])
    with pytest.raises(InvalidInputError):
        await das.get_dataset_info(runner, dataset="/TT*/X/Y")
    with pytest.raises(InvalidInputError):
        await das.get_dataset_info(runner, dataset="/only/two")
    assert calls == []


async def test_get_dataset_info_missing_raises() -> None:
    runner, _ = make_runner([])
    with pytest.raises(UpstreamError):
        await das.get_dataset_info(runner, dataset="/NoSuch/Thing-v1/NONE")


def test_make_runner_requires_binary(tmp_path: Path) -> None:
    settings = Settings(
        state_dir=tmp_path,
        browser_executable=None,
        dasgoclient_path=None,
    )
    with pytest.raises(UpstreamError, match="dasgoclient is not available"):
        das.make_runner(settings)


def test_make_runner_requires_credentials(tmp_path: Path) -> None:
    settings = Settings(
        state_dir=tmp_path,
        browser_executable=None,
        dasgoclient_path="/bin/true",
        grid_cert_file=tmp_path / "missing-cert.pem",
        grid_key_file=tmp_path / "missing-key.pem",
    )
    with pytest.raises(UpstreamError, match="grid credential is not readable"):
        das.make_runner(settings)


def test_make_runner_binds_env(tmp_path: Path) -> None:
    cert, key = tmp_path / "usercert.pem", tmp_path / "userkey.pem"
    cert.write_text("cert")
    key.write_text("key")
    settings = Settings(
        state_dir=tmp_path,
        browser_executable=None,
        dasgoclient_path="/bin/true",
        grid_cert_file=cert,
        grid_key_file=key,
    )
    runner = das.make_runner(settings)
    assert isinstance(runner, das.DasGoRunner)
    assert runner.binary == "/bin/true"
    assert runner.env["X509_USER_CERT"] == str(cert)
    assert runner.env["X509_USER_KEY"] == str(key)
