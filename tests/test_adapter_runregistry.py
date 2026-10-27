"""Fixture-driven contract tests for the CMS Run Registry adapter."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from cmcps.adapters import runregistry
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

RUNS_PAYLOAD = {
    "runs": [
        {
            "run_number": 379800,
            "oms_attributes": {"run_number": 379800, "fill_number": 9501, "energy": 6800},
            "rr_attributes": {"class": "Collisions2024", "significant": True, "state": "SIGNOFF"},
            "triplet_summary": {"tracker-strip": {"GOOD": 100, "BAD": 2}},
        }
    ],
    "count": 200,
    "pages": 4,
}

DATASETS_PAYLOAD = {
    "datasets": [
        {
            "run_number": 379800,
            "name": "/PromptReco/Collisions2024A/DQM",
            "dataset_attributes": {"global_state": "SIGNOFF", "tracker_state": "COMPLETED"},
            "triplet_summary": {"tracker-strip": {"GOOD": 95, "STANDBY": 7}},
        }
    ],
    "count": 1,
    "pages": 1,
}

DATASET_NAMES_PAYLOAD = [
    "online",
    "/Express/Collisions2024A/DQM",
    "/PromptReco/Collisions2024A/DQM",
]

LUMI_RANGES_PAYLOAD = [
    {"start": 1, "end": 40, "tracker-strip": {"status": "GOOD", "comment": "", "cause": ""}},
    {"start": 41, "end": 55, "tracker-strip": {"status": "STANDBY", "comment": "", "cause": ""}},
]

LUMI_RANGES_BY_COMPONENT_PAYLOAD = {
    "tracker-strip": [
        {"start": 1, "end": 40, "status": "GOOD", "comment": "", "cause": ""},
        {"start": 41, "end": 55, "status": "STANDBY", "comment": "", "cause": ""},
    ]
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


def posted_filter(recorder: Recorder) -> dict[str, Any]:
    return json.loads(recorder.requests[-1].content.decode())["filter"]


def posted_body(recorder: Recorder) -> dict[str, Any]:
    return json.loads(recorder.requests[-1].content.decode())


async def test_search_runs_builds_typed_filters(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=RUNS_PAYLOAD))
    client = make_client(tmp_path, recorder)
    result = await runregistry.search_runs(
        client,
        run_from=379000,
        run_to=380000,
        fill_number=9501,
        significant=True,
        class_name="Collisions2024",
        hlt_key_contains="commissioning",
        component_statuses=["tracker-strip:good", "dt-dt:GOOD"],
        page=2,
        page_size=50,
    )
    body = posted_body(recorder)
    assert body["page"] == 2
    assert body["page_size"] == 50
    filters = body["filter"]
    assert filters["run_number"] == {"and": [{">=": 379000}, {"<=": 380000}]}
    assert filters["oms_attributes.fill_number"] == {"=": 9501}
    assert filters["rr_attributes.significant"] == {"=": True}
    assert filters["rr_attributes.class"] == {"=": "Collisions2024"}
    assert filters["oms_attributes.hlt_key"] == {"like": "%commissioning%"}
    assert filters["triplet_summary.tracker-strip.GOOD"] == {">": 0}
    assert filters["triplet_summary.dt-dt.GOOD"] == {">": 0}
    assert result.items[0]["run_number"] == 379800
    assert result.total_count == 200
    assert result.page == 3
    assert result.has_more is True
    assert recorder.requests[-1].url.host == "cmsrunregistry.web.cern.ch"
    assert recorder.requests[-1].url.path == "/api/runs_filtered_ordered"


async def test_search_runs_rejects_number_plus_range(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=RUNS_PAYLOAD))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await runregistry.search_runs(client, run_number=379800, run_from=379000)


async def test_search_runs_rejects_bad_component_status(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=RUNS_PAYLOAD))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await runregistry.search_runs(client, component_statuses=["tracker-strip:PERFECT"])
    with pytest.raises(InvalidInputError):
        await runregistry.search_runs(client, component_statuses=["trackerstrip:GOOD"])


async def test_search_runs_invalid_page_args(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=RUNS_PAYLOAD))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await runregistry.search_runs(client, page=-1)
    with pytest.raises(InvalidInputError):
        await runregistry.search_runs(client, page_size=0)


async def test_search_runs_upstream_err_key(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={"err": "unknown filter attribute"}))
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError, match="unknown filter attribute"):
        await runregistry.search_runs(client)


async def test_search_runs_non_json_is_upstream_error(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text="<html>login</html>"))
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await runregistry.search_runs(client)


async def test_get_run_single_or_missing(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        filters = json.loads(request.content.decode())["filter"]
        if filters["run_number"] == {"=": 379800}:
            return httpx.Response(200, json=RUNS_PAYLOAD)
        return httpx.Response(200, json={"runs": [], "count": 0, "pages": 0})

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    found = await runregistry.get_run(client, run_number=379800)
    assert found["run"]["rr_attributes"]["class"] == "Collisions2024"
    assert found["source"] == "run-registry"
    with pytest.raises(UpstreamError, match="no run 123456"):
        await runregistry.get_run(client, run_number=123456)


async def test_list_run_dataset_names(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=DATASET_NAMES_PAYLOAD))
    client = make_client(tmp_path, recorder)
    result = await runregistry.list_run_dataset_names(client, run_number=379800)
    assert recorder.requests[-1].method == "GET"
    assert recorder.requests[-1].url.path == "/api/get_all_dataset_names_of_run/379800"
    assert result["dataset_names"] == DATASET_NAMES_PAYLOAD
    assert result["count"] == 3


async def test_search_datasets_binds_name_and_run_range(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=DATASETS_PAYLOAD))
    client = make_client(tmp_path, recorder)
    result = await runregistry.search_datasets(
        client,
        run_from=379000,
        run_to=380000,
        dataset_name_contains="PromptReco",
        component_statuses=["tracker-strip:GOOD"],
    )
    body = posted_body(recorder)
    filters = body["filter"]
    assert filters["run_number"] == {"and": [{">=": 379000}, {"<=": 380000}]}
    assert filters["name"] == {"like": "%PromptReco%"}
    assert filters["triplet_summary.tracker-strip.GOOD"] == {">": 0}
    assert recorder.requests[-1].url.path == "/api/datasets_filtered_ordered"
    assert result.items[0]["name"] == "/PromptReco/Collisions2024A/DQM"
    assert result.has_more is False


async def test_get_dataset_exact_and_missing(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        filters = json.loads(request.content.decode())["filter"]
        if filters["name"] == {"=": "/PromptReco/Collisions2024A/DQM"}:
            return httpx.Response(200, json=DATASETS_PAYLOAD)
        return httpx.Response(200, json={"datasets": [], "count": 0, "pages": 0})

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    found = await runregistry.get_dataset(
        client, run_number=379800, dataset_name="/PromptReco/Collisions2024A/DQM"
    )
    assert found["dataset"]["dataset_attributes"]["global_state"] == "SIGNOFF"
    with pytest.raises(UpstreamError, match="no dataset"):
        await runregistry.get_dataset(
            client, run_number=379800, dataset_name="/PromptReco/NoSuch/DQM"
        )
    with pytest.raises(InvalidInputError):
        await runregistry.get_dataset(client, run_number=379800, dataset_name="PromptRecoRelative")


async def test_get_lumisection_ranges_endpoint_switch(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        assert body == {"run_number": 379800, "dataset_name": "online"}
        if request.url.path.endswith("by_component"):
            return httpx.Response(200, json=LUMI_RANGES_BY_COMPONENT_PAYLOAD)
        return httpx.Response(200, json=LUMI_RANGES_PAYLOAD)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    plain = await runregistry.get_lumisection_ranges(client, run_number=379800)
    assert plain["range_count"] == 2
    assert plain["by_component"] is False
    assert "by_component" not in recorder.requests[-1].url.path
    grouped = await runregistry.get_lumisection_ranges(client, run_number=379800, by_component=True)
    assert grouped["range_count"] == 2
    assert grouped["by_component"] is True
    assert list(grouped["ranges"].keys()) == ["tracker-strip"]


async def test_get_lumisection_ranges_rejects_bad_shape(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={"unexpected": True}))
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await runregistry.get_lumisection_ranges(client, run_number=379800)


async def test_get_lumisection_ranges_online_default(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        assert body["dataset_name"] == "online"
        return httpx.Response(200, json=LUMI_RANGES_PAYLOAD)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    await runregistry.get_lumisection_ranges(client, run_number=379800, dataset_name="  ")
