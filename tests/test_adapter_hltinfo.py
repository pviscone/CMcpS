"""Fixture-driven contract tests for the CMS HLT info adapter."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cmcps.adapters import hltinfo
from cmcps.auth.service import AuthService
from cmcps.auth.tokens import ServiceTokenRecord, ServiceTokenStore
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

TEST_TOKEN = "unit-test-token"  # noqa: S105 - mock credential, not a real secret

PATH_RESULT = {
    "result": {
        "name": "HLT_Ele32_WPTight_Gsf",
        "versions": ["v1.0", "v1.1"],
        "filters": [{"name": "hltEle32Filter", "type": "EDFilter", "runs": [361234, 361400]}],
    }
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
    tmp_path: Path, recorder: Recorder, *, token: str | None = TEST_TOKEN
) -> tuple[Settings, SafeHttpClient]:
    settings = Settings(
        state_dir=tmp_path,
        require_sso=False,
        browser_executable=None,
        hltinfo_token=token,
    )
    auth = AuthService(settings)
    transport = httpx.MockTransport(recorder)
    client = SafeHttpClient(
        settings, auth_service=auth, client=httpx.AsyncClient(transport=transport)
    )
    return settings, client


async def test_list_menus_injects_bearer(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == f"Bearer {TEST_TOKEN}"
        assert request.url.path == "/api/v0/hltmenus"
        payload = {"menus": ["/cdaq/physics/Run2025/v1.0.0", "/cdaq/physics/Run2025/v1.1.0"]}
        return httpx.Response(200, json=payload)

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await hltinfo.list_menus(client, settings)
    assert result["menu_count"] == 2
    assert result["menus"][0].endswith("v1.0.0")


async def test_missing_token_rejected_without_request(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={}))
    settings, client = make_client(tmp_path, recorder, token=None)
    with pytest.raises(InvalidInputError):
        await hltinfo.list_menus(client, settings)
    assert recorder.requests == []


async def test_401_maps_to_helpful_upstream(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(401, json={"status": "Computer says no"}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await hltinfo.list_menus(client, settings)


async def test_get_path_summary(tmp_path: Path) -> None:
    payload = {"result": {"2024": {"HLT_Ele32": ["v1", "v2"]}}}
    recorder = Recorder(lambda _r: httpx.Response(200, json=payload))
    settings, client = make_client(tmp_path, recorder)
    result = await hltinfo.get_path_summary(client, settings)
    assert "result" not in result  # rows live in the file, never inline
    assert result["years_returned"] == ["2024"]
    summary_file = Path(result["file"])
    assert summary_file.exists()
    dumped = json.loads(summary_file.read_text())
    assert "2024" in dumped["result"]


async def test_get_path_summary_year_and_filter(tmp_path: Path) -> None:
    payload = {
        "result": {
            "2025": {"paths": [{"pathname": "HLT_Ele32"}, {"pathname": "HLT_Mu50"}]},
            "2024": {"paths": [{"pathname": "HLT_Ele35"}]},
        }
    }
    recorder = Recorder(lambda _r: httpx.Response(200, json=payload))
    settings, client = make_client(tmp_path, recorder)
    result = await hltinfo.get_path_summary(client, settings, year=2025, pathname_contains="ele")
    assert result["years_returned"] == ["2025"]
    assert result["per_year"]["2025"] == {"paths_total": 1, "paths_returned": 1}
    dumped = json.loads(Path(result["file"]).read_text())
    names = [p["pathname"] for p in dumped["result"]["2025"]["paths"]]
    assert names == ["HLT_Ele32"]


async def test_get_path_summary_rejects_unknown_year(tmp_path: Path) -> None:
    payload = {"result": {"2025": {"paths": []}}}
    recorder = Recorder(lambda _r: httpx.Response(200, json=payload))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await hltinfo.get_path_summary(client, settings, year=1999)


async def test_get_path_names_with_filter(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Verified live: no filter path segment exists; filtering is client-side.
        assert request.url.path == "/api/v0/pathnames"
        return httpx.Response(
            200,
            json={"result": ["HLT_Ele32_WPTight_Gsf", "HLT_Mu50", "HLT_Ele35_WPTight"]},
        )

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await hltinfo.get_path_names(client, settings, name_filter="hlt_ele")
    assert result["name_count"] == 2
    assert "HLT_Mu50" not in result["names"]


async def test_get_path_names_rejects_empty_filter(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await hltinfo.get_path_names(client, settings, name_filter="   ")
    assert recorder.requests == []


async def test_summary_bounding(tmp_path: Path) -> None:
    years = {str(2000 + i): {f"p{j}": None for j in range(2)} for i in range(10)}
    payload = {"result": years}
    recorder = Recorder(lambda _r: httpx.Response(200, json=payload))
    settings, client = make_client(tmp_path, recorder)
    result = await hltinfo.get_path_summary(client, settings)
    assert result["years_returned"][0] == "2009"
    assert len(result["years_returned"]) == 5
    assert result["truncated"] is True


async def test_summary_byte_cap_shrinks_paths_lists(tmp_path: Path) -> None:
    """Real-shape payloads (year → {"paths": [...]}) always fit the byte budget."""
    path_item = "x" * 5_000
    years = {str(2020 + i): {"paths": [path_item] * 200, "year": 2020 + i} for i in range(5)}
    payload = {"result": years}
    recorder = Recorder(lambda _r: httpx.Response(200, json=payload))
    settings, client = make_client(tmp_path, recorder)
    result = await hltinfo.get_path_summary(client, settings)
    dumped = json.loads(Path(result["file"]).read_text())
    assert len(json.dumps(dumped["result"], default=str)) <= hltinfo._SUMMARY_MAX_RESULT_BYTES
    assert result["truncated"] is True
    # Deterministic halving: 200 → 100 → 50 entries per year fits the 2 MiB budget.
    assert len(dumped["result"]["2024"]["paths"]) == 50
    assert result["per_year"]["2024"] == {"paths_total": 200, "paths_returned": 50}


async def test_summary_rejects_non_dict_result(tmp_path: Path) -> None:
    """A non year-keyed payload is an adapter-visible error, not silent output."""
    recorder = Recorder(lambda _r: httpx.Response(200, json={"result": [1, 2, 3]}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await hltinfo.get_path_summary(client, settings)


async def test_get_path(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v0/path/HLT_Ele32_WPTight_Gsf"
        return httpx.Response(200, json=PATH_RESULT)

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await hltinfo.get_path(client, settings, path_name="HLT_Ele32_WPTight_Gsf")
    assert result["result"]["name"] == "HLT_Ele32_WPTight_Gsf"
    assert result["result"]["filters"][0]["runs"] == [361234, 361400]


async def test_get_path_empty_result_raises(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={"result": None}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await hltinfo.get_path(client, settings, path_name="HLT_Missing")


async def test_get_path_rejects_bad_name(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await hltinfo.get_path(client, settings, path_name="HLT/Evil")
    with pytest.raises(InvalidInputError):
        await hltinfo.get_path(client, settings, path_name="   ")
    assert recorder.requests == []


async def test_prescales_builds_run_range(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v0/prescales/path/HLT_Ele32_WPTight_Gsf"
        assert request.url.params["minrun"] == "360000"
        assert request.url.params["maxrun"] == "370000"
        return httpx.Response(200, json={"result": [[361234, 1], [361400, 2]]})

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await hltinfo.get_path_prescales(
        client, settings, path_name="HLT_Ele32_WPTight_Gsf", min_run=360000, max_run=370000
    )
    assert result["min_run"] == 360000
    assert result["result"] == [[361234, 1], [361400, 2]]


async def test_prescales_rejects_bad_range(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await hltinfo.get_path_prescales(
            client, settings, path_name="HLT_X", min_run=100, max_run=10
        )
    with pytest.raises(InvalidInputError):
        await hltinfo.get_path_prescales(client, settings, path_name="HLT_X", min_run=0)
    assert recorder.requests == []


async def test_run_lumis(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/runlumis")
        return httpx.Response(200, json={"result": [[361234, [[1, 50], [80, 120]]]]})

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await hltinfo.get_run_lumis_by_prescale(
        client, settings, path_name="HLT_Ele32_WPTight_Gsf"
    )
    assert result["result"][0][0] == 361234


async def test_captured_bearer_wins_over_env_token(tmp_path: Path) -> None:
    captured_record = ServiceTokenRecord(
        service="hltinfo",
        token_endpoint="https://auth.cern.ch/auth/realms/cern/protocol/openid-connect/token",  # noqa: S106
        client_id="cmshltinfo-frontend",
        access_token="captured-access-value",  # noqa: S106
        refresh_token=None,
        access_expires_at=None,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer captured-access-value"
        return httpx.Response(200, json={"menus": []})

    recorder = Recorder(handler)
    settings = Settings(
        state_dir=tmp_path,
        require_sso=False,
        browser_executable=None,
        hltinfo_token=TEST_TOKEN,
    )
    ServiceTokenStore(settings.service_tokens_path).save(captured_record)
    auth = AuthService(settings)
    client = SafeHttpClient(
        settings,
        auth_service=auth,
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
    )
    result = await hltinfo.list_menus(client, settings)
    assert result["menu_count"] == 0
