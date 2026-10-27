"""Fixture-driven contract tests for the CMS DQM GUI adapter.

Fixtures are shaped after responses captured live from
cmsweb.cern.ch/dqm/offline on 2026-10-27 (grid-cert authenticated).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cmcps.adapters import dqm
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

DATASET = "/ZeroBias/Run2026A-PromptReco-v1/DQMIO"
RUN = 401605

SAMPLES = {
    "samples": [
        {
            "type": "offline_data",
            "items": [
                {
                    "type": "offline_data",
                    "run": str(RUN),
                    "dataset": DATASET,
                    "version": "",
                    "importversion": 1,
                },
                # Same pair re-imported: the GUI repeats samples; dedupe.
                {
                    "type": "offline_data",
                    "run": str(RUN),
                    "dataset": DATASET,
                    "version": "",
                    "importversion": 2,
                },
                {
                    "type": "offline_data",
                    "run": f"{RUN}:15",
                    "dataset": DATASET,
                    "version": "",
                    "importversion": 1,
                },
            ],
        }
    ]
}


def _me_payload() -> dict:
    return {
        "hist": {
            "type": "TH1",
            "title": "lumisec",
            "bins": {"integral": 36957, "content": [0, 132, 157]},
        }
    }


ARCHIVE_ROOT = {
    "contents": [
        {"streamerinfo": ""},
        {"subdir": "AlCaReco"},
        {"subdir": "JetMET", "me_count": 42},
    ]
}

ME_ENTRY = {
    "obj": "lumisec",
    "path": "JetMET/lumisec",
    "properties": {
        "kind": "ROOT",
        "type": "TH1F",
        "lumisect": "0",
        "report": {"alarm": 0, "error": 0, "warn": 0, "other": 0},
        "hasref": 0,
        "isEff": 0,
        "tagged": 0,
    },
    "tag": 0,
    "qresults": [],
    "nentries": 36957,
    "stats": {
        "x": {"nbins": 2501, "mean": 120.798, "rms": 69.5656, "min": -1, "max": 2500},
        "y": {"nbins": 1, "mean": 0, "rms": 0, "min": 0, "max": 1},
        "z": {"nbins": 1, "mean": 0, "rms": 0, "min": 0, "max": 1},
    },
}

ARCHIVE_JETMET = {"contents": [{"streamerinfo": ""}, ME_ENTRY]}


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
    return SafeHttpClient(settings, auth, client=httpx.AsyncClient(transport=transport))


def standard_handler(request: httpx.Request) -> httpx.Response:
    """Route the samples/archive/jsonfairy/plotfairy lifecycle."""
    path = request.url.path
    if path.endswith("/data/json/samples"):
        response = httpx.Response(200, json=SAMPLES)
    elif "/data/json/archive/" in path:
        if path.rstrip("/").endswith("JetMET"):
            response = httpx.Response(200, json=ARCHIVE_JETMET)
        elif path.rstrip("/").endswith("DQMIO"):
            response = httpx.Response(200, json=ARCHIVE_ROOT)
        else:
            # Unknown folder: the GUI returns only the streamerinfo pseudo-entry.
            response = httpx.Response(200, json={"contents": [{"streamerinfo": ""}]})
    elif "/jsonfairy/" in path:
        response = httpx.Response(200, json=_me_payload())
    elif "/plotfairy/" in path:
        response = httpx.Response(200, content=b"\x89PNG\r\n\x1a\nfake")
    else:
        response = httpx.Response(404, text="unknown")
    return response


async def test_list_samples_parses_and_dedupes(tmp_path: Path) -> None:
    recorder = Recorder(standard_handler)
    client = make_client(tmp_path, recorder)

    result = await dqm.list_samples(client, match=re.escape(DATASET), run=RUN)

    assert result.items == [
        {"run": RUN, "lumi": None, "dataset": DATASET},
        {"run": RUN, "lumi": 15, "dataset": DATASET},
    ]
    assert result.source == "dqm:offline"
    params = dict(recorder.requests[0].url.params)
    assert params["match"] == re.escape(DATASET)
    assert params["run"] == str(RUN)


async def test_list_samples_requires_selector(tmp_path: Path) -> None:
    recorder = Recorder(standard_handler)
    client = make_client(tmp_path, recorder)

    with pytest.raises(InvalidInputError):
        await dqm.list_samples(client)
    assert recorder.requests == []


async def test_list_samples_rejects_bad_regex(tmp_path: Path) -> None:
    recorder = Recorder(standard_handler)
    client = make_client(tmp_path, recorder)

    with pytest.raises(InvalidInputError):
        await dqm.list_samples(client, match="/X/[bad")
    assert recorder.requests == []


async def test_list_samples_truncates(tmp_path: Path) -> None:
    big = {
        "samples": [
            {
                "type": "offline_data",
                "items": [
                    {"run": str(400000 + i), "dataset": DATASET, "importversion": 1}
                    for i in range(dqm._MAX_ITEMS + 5)
                ],
            }
        ]
    }
    recorder = Recorder(lambda _r: httpx.Response(200, json=big))
    client = make_client(tmp_path, recorder)

    result = await dqm.list_samples(client, match=DATASET)

    assert result.size == dqm._MAX_ITEMS
    assert result.note is not None and "truncated" in result.note


async def test_get_workspace_lists_content(tmp_path: Path) -> None:
    recorder = Recorder(standard_handler)
    client = make_client(tmp_path, recorder)

    result = await dqm.get_workspace(client, run=RUN, dataset=DATASET)

    assert result["subdirs"] == [{"name": "AlCaReco"}, {"name": "JetMET", "me_count": 42}]
    me = result["monitoring_elements"]
    assert me == []
    # First request is the exact sample-existence check.
    sample_params = dict(recorder.requests[0].url.params)
    assert sample_params["match"] == re.escape(DATASET)
    # The archive URL carries the dataset without its leading slash.
    archive_url = str(recorder.requests[1].url)
    assert f"/data/json/archive/{RUN}/ZeroBias/Run2026A-PromptReco-v1/DQMIO" in archive_url


async def test_get_workspace_folder_mes(tmp_path: Path) -> None:
    recorder = Recorder(standard_handler)
    client = make_client(tmp_path, recorder)

    result = await dqm.get_workspace(client, run=RUN, dataset=DATASET, folder="JetMET")

    assert result["folder"] == "JetMET"
    (me,) = result["monitoring_elements"]
    assert me["path"] == "JetMET/lumisec"
    assert me["root_type"] == "TH1F"
    assert me["lumi_plot"] is False
    assert me["nentries"] == 36957
    assert me["stats"]["x"]["nbins"] == 2501


async def test_get_workspace_missing_sample_fails(tmp_path: Path) -> None:
    recorder = Recorder(
        lambda _r: httpx.Response(200, json={"samples": [{"type": "offline_data", "items": []}]})
    )
    client = make_client(tmp_path, recorder)

    with pytest.raises(UpstreamError) as excinfo:
        await dqm.get_workspace(client, run=999999, dataset=DATASET)
    assert excinfo.value.status_code == 404
    # The archive endpoint must never be hit for an unknown sample.
    assert all("/data/json/archive/" not in str(r.url) for r in recorder.requests)


async def test_get_workspace_missing_folder_fails(tmp_path: Path) -> None:
    recorder = Recorder(standard_handler)
    client = make_client(tmp_path, recorder)

    with pytest.raises(UpstreamError) as excinfo:
        await dqm.get_workspace(client, run=RUN, dataset=DATASET, folder="JetMETX")
    assert excinfo.value.status_code == 404


async def test_get_monitoring_element_th1(tmp_path: Path) -> None:
    recorder = Recorder(standard_handler)
    client = make_client(tmp_path, recorder)

    result = await dqm.get_monitoring_element(
        client, run=RUN, dataset=DATASET, me_path="JetMET/lumisec"
    )

    assert result["me"]["path"] == "JetMET/lumisec"
    assert result["hist"]["type"] == "TH1"
    assert result["hist"]["bins"]["content"] == [0, 132, 157]
    assert "note" not in result


async def test_get_monitoring_element_unsupported_type(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/jsonfairy/" in request.url.path:
            return httpx.Response(200, json={"hist": "unsupported type"})
        return standard_handler(request)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)

    result = await dqm.get_monitoring_element(
        client, run=RUN, dataset=DATASET, me_path="JetMET/lumisec"
    )

    assert result["hist"] is None
    assert "not serialize" in result["note"]


async def test_get_monitoring_element_missing_me_fails(tmp_path: Path) -> None:
    recorder = Recorder(standard_handler)
    client = make_client(tmp_path, recorder)

    with pytest.raises(UpstreamError) as excinfo:
        await dqm.get_monitoring_element(client, run=RUN, dataset=DATASET, me_path="JetMET/nope")
    assert excinfo.value.status_code == 404
    # jsonfairy is never consulted for a missing ME.
    assert all("/jsonfairy/" not in str(r.url) for r in recorder.requests)


async def test_download_plot_writes_png(tmp_path: Path) -> None:
    recorder = Recorder(standard_handler)
    client = make_client(tmp_path, recorder)

    result = await dqm.download_plot(
        client, run=RUN, dataset=DATASET, me_path="JetMET/lumisec", width=800, height=500
    )

    path = Path(result["path"])
    assert path.read_bytes() == b"\x89PNG\r\n\x1a\nfake"
    assert path.name == f"dqm-offline-{RUN}-JetMET_lumisec.png"
    assert result["size_bytes"] == len(b"\x89PNG\r\n\x1a\nfake")
    params = dict(recorder.requests[-1].url.params)
    assert params["w"] == "800" and params["h"] == "500"
    assert result["width"] == 800 and result["height"] == 500


async def test_download_plot_never_fetches_placeholder(tmp_path: Path) -> None:
    recorder = Recorder(standard_handler)
    client = make_client(tmp_path, recorder)

    with pytest.raises(UpstreamError):
        await dqm.download_plot(client, run=RUN, dataset=DATASET, me_path="JetMET/nope")
    # samples check + parent listing; plotfairy must never be called.
    assert len(recorder.requests) == 2
    assert all("/plotfairy/" not in str(r.url) for r in recorder.requests)


async def test_requests_never_carry_sso_cookies(tmp_path: Path) -> None:
    recorder = Recorder(standard_handler)
    client = make_client(tmp_path, recorder)

    await dqm.list_samples(client, match=DATASET)
    await dqm.get_monitoring_element(client, run=RUN, dataset=DATASET, me_path="JetMET/lumisec")

    assert all("Cookie" not in r.headers for r in recorder.requests)


async def test_401_maps_to_grid_credential_error(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(401, content=b""))
    client = make_client(tmp_path, recorder)

    with pytest.raises(UpstreamError) as excinfo:
        await dqm.list_samples(client, match=DATASET)
    assert excinfo.value.status_code == 401
    assert "grid" in (excinfo.value.instructions or "")


async def test_invalid_arguments_make_no_requests(tmp_path: Path) -> None:
    recorder = Recorder(standard_handler)
    client = make_client(tmp_path, recorder)

    for call in (
        lambda: dqm.get_workspace(client, run=RUN, dataset=DATASET, instance="newdqmgui"),
        lambda: dqm.get_workspace(client, run=RUN, dataset="ZeroBias/Run2026A/DQMIO"),
        lambda: dqm.get_workspace(client, run=0, dataset=DATASET),
        lambda: dqm.get_monitoring_element(
            client, run=RUN, dataset=DATASET, me_path="JetMET/../lumisec"
        ),
        lambda: dqm.download_plot(
            client, run=RUN, dataset=DATASET, me_path="JetMET/lumisec", width=8
        ),
    ):
        with pytest.raises(InvalidInputError):
            await call()
    assert recorder.requests == []


async def test_instance_selects_gui_url(tmp_path: Path) -> None:
    recorder = Recorder(standard_handler)
    client = make_client(tmp_path, recorder)

    await dqm.get_workspace(client, run=RUN, dataset=DATASET, instance="relval")

    assert str(recorder.requests[0].url).startswith("https://cmsweb.cern.ch/dqm/relval/")
