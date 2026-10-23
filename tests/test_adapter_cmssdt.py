"""Fixture-driven contract tests for the CMS SDT adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cmcps.adapters import cmssdt
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

RELEASES_XML = """<projects>
  <architecture name="slc7_amd64_gcc700">
    <project label="CMSSW_10_6_20" type="Production" state="Announced"/>
    <project label="SAMPLE_CMSSW_14_0_0" type="IB" state="Announced"/>
  </architecture>
  <architecture name="el9_amd64_gcc12">
    <project label="CMSSW_14_0_0" type="Production" state="NoIB"/>
    <project label="CMSSW_15_0_0_pre1" type="Production" state="NoIB"/>
  </architecture>
</projects>
"""


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


async def test_list_releases_parses_xml(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=RELEASES_XML))
    client = make_client(tmp_path, recorder)
    result = await cmssdt.list_releases(client)
    assert result.total_count == 4
    assert not result.has_more
    labels = {i["label"] for i in result.items}
    assert "CMSSW_15_0_0_pre1" in labels
    el9 = [i for i in result.items if i["architecture"] == "el9_amd64_gcc12"]
    assert len(el9) == 2


async def test_list_releases_filters(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=RELEASES_XML))
    client = make_client(tmp_path, recorder)
    by_arch = await cmssdt.list_releases(client, arch_contains="el9")
    assert by_arch.total_count == 2
    by_label = await cmssdt.list_releases(client, label_contains="14_0")
    assert by_label.total_count == 2
    by_state = await cmssdt.list_releases(client, state="Announced")
    assert by_state.total_count == 2


async def test_list_releases_pagination(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=RELEASES_XML))
    client = make_client(tmp_path, recorder)
    page1 = await cmssdt.list_releases(client, limit=2, offset=0)
    page2 = await cmssdt.list_releases(client, limit=2, offset=2)
    assert page1.total_count == 4
    assert page1.has_more is True
    assert page2.has_more is False
    assert page1.items[0]["label"] != page2.items[0]["label"]


async def test_list_releases_rejects_bad_args(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=RELEASES_XML))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await cmssdt.list_releases(client, limit=0)
    with pytest.raises(InvalidInputError):
        await cmssdt.list_releases(client, offset=-1)
    assert recorder.requests == []


async def test_list_releases_bad_xml_raises(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text="<html>broken"))
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await cmssdt.list_releases(client)


STRUCTURE_JSON = {
    "CMSSW_20_0_X": ["CMSSW_20_0_X"],
    "CMSSW_20_1_X": ["CMSSW_20_1_X", "CMSSW_20_1_CPP23_X"],
    "all_prefixes": ["CMSSW_20_0_X", "CMSSW_20_1_X"],
    "all_release_queues": ["CMSSW_20_1_X"],
    "default_release": "CMSSW_20_1_X",
}

LATEST_IBS_JSON = {
    "CMSSW_20_1_X": {
        "el9_amd64_gcc13": {"latest_IB": "CMSSW_20_1_X_2026-10-22-1100", "status": "passed"},
        "el8_amd64_gcc12": {"latest_IB": "CMSSW_20_1_X_2026-10-22-1100", "status": "failed"},
    },
    "CMSSW_20_1_CPP23_X": {
        "el9_amd64_gcc13": {
            "latest_IB": "CMSSW_20_1_CPP23_X_2026-10-22-1100",
            "status": "passed",
        }
    },
}

IB_DETAILS_JSON = {
    "base_branch": "master",
    "release_name": "CMSSW_20_1_X",
    "comparisons": [
        {
            "ib_date": "2026-10-22-1100",
            "isIB": True,
            "inProgress": False,
            "next_ib": "CMSSW_20_1_X_2026-10-22-2300",
            "compared_tags": "COMPARED",
            "cmsdist_compared_tags": "COMPARED",
            "comp_baseline": "CMSSW_20_1_X_2026-10-21-2300",
            "comp_baseline_state": "ready",
            "builds": [{"arch": "el9_amd64_gcc13", "passed": True}],
            "relvals": [{"arch": "el9_amd64_gcc13", "passed": False}],
            "utests": [],
            "check-headers": "passed",
            "hlt_tests": "passed",
            "static_checks": "notrun",
            "merged_prs": [
                {
                    "number": 12345,
                    "title": "Fix something",
                    "author_login": "dev",
                    "url": "https://github.com/cms-sw/cmssw/pull/12345",
                    "hash": "abc123",
                }
            ],
            "cmsdist_merged_prs": {"el9_amd64_gcc13": [{"number": 10920, "title": "Bump llvm"}]},
        },
        {
            "ib_date": "2026-10-21-2300",
            "isIB": True,
            "inProgress": False,
            "builds": [],
            "merged_prs": [],
            "cmsdist_merged_prs": {},
        },
    ],
}

LABELS_JSON = {
    "alca": "fbca04",
    "analysis": "1d76db",
    "analysis-approved": "009800",
    "bug": "e11d21",
}

PRS_JSON = {
    "cmsdist": {
        "10920": {
            "number": 10920,
            "title": "Update llvm23",
            "author": "akritkbehera",
            "state": "closed",
            "labels": ["externals-approved", "orp-approved"],
            "branch": "IB/CMSSW_20_1_X/master",
            "created_at": "1791890823",
            "merged_at": "1792475027",
        },
        "10921": {
            "number": 10921,
            "title": "Bump something",
            "author": "dev",
            "state": "open",
            "labels": ["externals-pending"],
            "branch": "IB/CMSSW_20_1_X/master",
            "created_at": "1792000000",
            "merged_at": None,
        },
    },
    "cmssw": {
        "49001": {
            "number": 49001,
            "title": "Fix reco crash",
            "author": "physicist",
            "state": "open",
            "labels": ["reconstruction", "bug"],
            "branch": "master",
            "created_at": "1792100000",
            "merged_at": None,
        },
        "49000": {
            "number": 49000,
            "title": "Old closed PR",
            "author": "physicist",
            "state": "closed",
            "labels": ["dqm"],
            "branch": "master",
            "created_at": "1790500000",
            "merged_at": "1790600000",
        },
    },
}

RELVALS_JSON = {
    "CMSSW_20_1_X_2026-10-21-2300": "el9_amd64_gcc13",
    "CMSSW_20_1_X_2026-10-22-1100": "el9_amd64_gcc13,el8_amd64_gcc12",
}

_ROUTES: dict[str, object] = {
    "/SDT/html/data/structure.json": STRUCTURE_JSON,
    "/SDT/html/data/LatestIBsSummary.json": LATEST_IBS_JSON,
    "/SDT/html/data/CMSSW_20_1_X.json": IB_DETAILS_JSON,
    "/SDT/public/cms-sw.github.io/data/cmssw_labels.json": LABELS_JSON,
    "/SDT/public/cms-sw.github.io/data/prs/CMSSW_20_1.json": PRS_JSON,
    "/SDT/public/cms-sw.github.io/data/RelvalsAvailableResults.json": RELVALS_JSON,
}


def router(request: httpx.Request) -> httpx.Response:
    payload = _ROUTES.get(request.url.path)
    if payload is None:
        return httpx.Response(404, text="not found")
    return httpx.Response(200, json=payload)


async def test_list_ib_queues(tmp_path: Path) -> None:
    client = make_client(tmp_path, Recorder(router))
    result = await cmssdt.list_ib_queues(client)
    assert result.total_count == 3
    assert "CMSSW_20_1_X" in (result.note or "")
    queues = {i["queue"] for i in result.items}
    assert queues == {"CMSSW_20_0_X", "CMSSW_20_1_X", "CMSSW_20_1_CPP23_X"}
    assert all(i["family"].endswith("_X") for i in result.items)
    filtered = await cmssdt.list_ib_queues(client, family="20_0")
    assert [i["queue"] for i in filtered.items] == ["CMSSW_20_0_X"]


async def test_list_ib_queues_pagination(tmp_path: Path) -> None:
    client = make_client(tmp_path, Recorder(router))
    page1 = await cmssdt.list_ib_queues(client, limit=2)
    page2 = await cmssdt.list_ib_queues(client, limit=2, offset=2)
    assert page1.has_more is True
    assert page2.has_more is False
    with pytest.raises(InvalidInputError):
        await cmssdt.list_ib_queues(client, limit=0)


async def test_ib_summary_filters(tmp_path: Path) -> None:
    client = make_client(tmp_path, Recorder(router))
    result = await cmssdt.ib_summary(client)
    assert result.total_count == 3
    assert {i["status"] for i in result.items} == {"passed", "failed"}
    by_queue = await cmssdt.ib_summary(client, queue="cpp23")
    assert by_queue.total_count == 1
    assert by_queue.items[0]["queue"] == "CMSSW_20_1_CPP23_X"
    by_arch = await cmssdt.ib_summary(client, arch="el8")
    assert by_arch.total_count == 1
    by_status = await cmssdt.ib_summary(client, status="failed")
    assert by_status.total_count == 1
    assert by_status.items[0]["arch"] == "el8_amd64_gcc12"
    with pytest.raises(InvalidInputError):
        await cmssdt.ib_summary(client, offset=-1)


async def test_ib_summary_oidc_html_raises(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text="<html>login</html>"))
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await cmssdt.ib_summary(client)


async def test_get_ib_details_by_index_and_date(tmp_path: Path) -> None:
    client = make_client(tmp_path, Recorder(router))
    newest = await cmssdt.get_ib_details(client, queue="CMSSW_20_1_X")
    assert newest["ib_date"] == "2026-10-22-1100"
    assert newest["base_branch"] == "master"
    assert newest["release_name"] == "CMSSW_20_1_X"
    assert newest["status"] == {
        "check-headers": "passed",
        "hlt_tests": "passed",
        "static_checks": "notrun",
    }
    assert newest["counts"] == {"builds": 1, "relvals": 1, "utests": 0}
    assert newest["merged_prs"][0]["number"] == 12345
    assert 10920 == newest["cmsdist_merged_prs"]["el9_amd64_gcc13"][0]["number"]
    older = await cmssdt.get_ib_details(client, queue="CMSSW_20_1_X", ib_date="2026-10-21-2300")
    assert older["isIB"] is True


async def test_get_ib_details_bad_selectors(tmp_path: Path) -> None:
    client = make_client(tmp_path, Recorder(router))
    with pytest.raises(InvalidInputError):
        await cmssdt.get_ib_details(client, queue="CMSSW_20_1_X", comparison_index=-1)
    with pytest.raises(InvalidInputError):
        await cmssdt.get_ib_details(client, queue="CMSSW_20_1_X", comparison_index=5)
    with pytest.raises(UpstreamError):
        await cmssdt.get_ib_details(client, queue="CMSSW_20_1_X", ib_date="1999-01-01-0000")
    with pytest.raises(UpstreamError):
        await cmssdt.get_ib_details(client, queue="CMSSW_99_9_X")


async def test_list_cmssw_labels(tmp_path: Path) -> None:
    client = make_client(tmp_path, Recorder(router))
    result = await cmssdt.list_cmssw_labels(client)
    assert result.total_count == 4
    assert result.items[0] == {"label": "alca", "color": "fbca04"}
    filtered = await cmssdt.list_cmssw_labels(client, prefix="ANAL")
    assert [i["label"] for i in filtered.items] == ["analysis", "analysis-approved"]


async def test_list_open_prs_filters_and_sort(tmp_path: Path) -> None:
    client = make_client(tmp_path, Recorder(router))
    opened = await cmssdt.list_open_prs(client, family="CMSSW_20_1")
    assert [i["number"] for i in opened.items] == [49001, 10921]  # newest first
    every = await cmssdt.list_open_prs(client, family="CMSSW_20_1", state=None)
    assert every.total_count == 4
    by_repo = await cmssdt.list_open_prs(client, family="CMSSW_20_1", repo="cmssw")
    assert all(i["repo"] == "cmssw" for i in by_repo.items)
    by_label = await cmssdt.list_open_prs(client, family="CMSSW_20_1", label="bug")
    assert [i["number"] for i in by_label.items] == [49001]
    by_state = await cmssdt.list_open_prs(client, family="CMSSW_20_1", state="closed")
    assert by_state.total_count == 2
    with pytest.raises(InvalidInputError):
        await cmssdt.list_open_prs(client, family="CMSSW_20_1", repo="cmsswdata")


async def test_relval_archs(tmp_path: Path) -> None:
    client = make_client(tmp_path, Recorder(router))
    result = await cmssdt.relval_archs(client)
    assert result.total_count == 2
    newest = next(i for i in result.items if i["ib_name"].endswith("22-1100"))
    assert newest["archs"] == ["el9_amd64_gcc13", "el8_amd64_gcc12"]
    filtered = await cmssdt.relval_archs(client, ib_name_contains="21-2300")
    assert filtered.total_count == 1


def test_build_log_url_plain() -> None:
    out = cmssdt.build_log_url("CMSSW_20_1_X_2026-10-22-1100", arch="el9_amd64_gcc13")
    assert out["url"] == (
        "https://cmssdt.cern.ch/SDT/cgi-bin/showBuildLogs.py/"
        "el9_amd64_gcc13/www/thu/20.1-thu-11/CMSSW_20_1_X_2026-10-22-1100"
    )
    assert out["ib_name"] == "CMSSW_20_1_X_2026-10-22-1100"


def test_build_log_url_variant_and_fwlite() -> None:
    out = cmssdt.build_log_url(
        "CMSSW_20_1_CPP23_X_2026-10-22-1100", arch="el9_amd64_gcc15", fwlite=True
    )
    assert out["url"] == (
        "https://cmssdt.cern.ch/SDT/cgi-bin/showBuildLogs.py/fwlite/"
        "el9_amd64_gcc15/www/thu/20.1.CPP23-thu-11/CMSSW_20_1_CPP23_X_2026-10-22-1100"
    )


def test_build_log_url_rejects_bad_names() -> None:
    for bad in (
        "CMSSW_20_1_X",  # missing date
        "CMSSW_20_1_X_2026-10-22",  # missing hour
        "CMSSW_20_1_0_2026-10-22-1100",  # production release, not an IB queue
        "garbage",
        "CMSSW_20_1_X_2026-13-22-1100",  # invalid month
    ):
        with pytest.raises(InvalidInputError):
            cmssdt.build_log_url(bad, arch="el9_amd64_gcc13")
