"""Fixture-driven contract tests for the CMS Online (OMS) adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cmcps.adapters import cmsonline
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

RUN_PAYLOAD = {
    "meta": {"totalResourceCount": 1},
    "links": {"self": "https://cmsoms.cern.ch/agg/api/v1/runs"},
    "data": [
        {
            "id": "379611",
            "type": "runs",
            "attributes": {
                "run_number": 379611,
                "start_time": "2024-05-01T10:00:00Z",
                "fill_number": 9500,
            },
        }
    ],
}

VERSION_PAYLOAD = {"data": {"version": "1.2.3"}}


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


async def test_get_version(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=VERSION_PAYLOAD))
    client = make_client(tmp_path, recorder)
    result = await cmsonline.get_version(client)
    assert result["version"] == "1.2.3"


async def test_list_runs_builds_filters(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        assert params["filter[run_number][GEQ]"] == "379250"
        assert params["filter[run_number][LEQ]"] == "379750"
        assert params["filter[fill_number][EQ]"] == "9500"
        assert params["page[offset]"] == "20"
        return httpx.Response(200, json=RUN_PAYLOAD)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await cmsonline.list_runs(
        client, run_number=379500, fill_number=9500, limit=10, offset=20
    )
    assert result.total_count == 1
    assert result.items[0]["run_number"] == 379611


async def test_list_runs_rejects_bad_resource_args(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={}))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await cmsonline.list_runs(client, limit=999)
    with pytest.raises(InvalidInputError):
        await cmsonline.list_runs(client, run_number=0)
    assert recorder.requests == []


async def test_get_run_and_not_found(tmp_path: Path) -> None:
    recorder = Recorder(
        lambda _r: httpx.Response(200, json={"data": [], "meta": {"totalResourceCount": 0}})
    )
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await cmsonline.get_run(client, run_number=1)


async def test_get_run_returns_attributes(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=RUN_PAYLOAD))
    client = make_client(tmp_path, recorder)
    result = await cmsonline.get_run(client, run_number=379611)
    assert result["run_number"] == 379611
    assert result["fill_number"] == 9500


async def test_list_lumisections(tmp_path: Path) -> None:
    payload = {
        "meta": {"totalResourceCount": 120},
        "data": [
            {
                "id": "379611-1",
                "type": "lumisections",
                "attributes": {"run_number": 379611, "lumisection_number": 1},
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["filter[run_number][EQ]"] == "379611"
        return httpx.Response(200, json=payload)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await cmsonline.list_lumisections(client, run_number=379611, limit=50)
    assert result.total_count == 120
    assert result.has_more is True
    assert result.items[0]["lumisection_number"] == 1


async def test_401_maps_to_upstream(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(401, text="Unauthorized"))
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await cmsonline.get_version(client)


SHIFT_HTML = """<html><body>
<input name="javax.faces.ViewState" type="hidden" value="xy">
<table bgcolor="Lavender" border="0" cellpadding="1"><tr>
<th style="vertical-align:bottom;" rowspan="2">Time</th>
<th class="Central" colspan="1">Central</th><th class="DAQ" colspan="1">DAQ</th>
<th class="HGCAL" colspan="1">HGCAL</th> <th class="TC" colspan="1">TC</th></tr>
<tr><th class="Central">LS3 Technical Shifter</th><th class="DAQ">sysadmin oncall</th>
<th class="HGCAL">CR2</th><th class="TC">Safety tour</th></tr>
<tr><th> <font size="1">0:00</font></th><td class="Central"></td>
<td bgcolor="ddddbb" class="DAQ" rowspan="12" align="center">
<a href="javascript:void(0)" onclick="showShifterInfo('887540'); return false;">
<div class="shifterInfo">Benjamin<br>Calvet</div></a></td>
<td class="HGCAL"></td><td class="TC"></td></tr>
<tr><th> <font size="1">1:00</font></th><td class="Central"></td>
<td class="HGCAL"></td><td class="TC"></td></tr>
<tr><th> <font size="1">2:00</font></th><td class="Central"></td>
<td class="HGCAL"></td><td class="TC"></td></tr>
<tr><th> <font size="1">3:00</font></th><td class="Central"></td>
<td class="HGCAL"></td><td class="TC"></td></tr>
<tr><th> <font size="1">4:00</font></th><td class="Central"></td>
<td class="HGCAL"></td><td class="TC"></td></tr>
</table>
</body></html>"""


async def test_shift_schedule_parses_grid(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=SHIFT_HTML))
    client = make_client(tmp_path, recorder)
    result = await cmsonline.get_shift_schedule(client)
    assert result["systems"] == ["Central", "DAQ", "HGCAL", "TC"]
    assert result["roles"] == [
        "LS3 Technical Shifter",
        "sysadmin oncall",
        "CR2",
        "Safety tour",
    ]
    assert result["block_count"] == 1
    block = result["items"][0]
    assert block["name"] == "Benjamin Calvet"
    assert block["shifter_id"] == "887540"
    assert block["system"] == "DAQ"
    assert block["role"] == "sysadmin oncall"
    assert block["hours"] == 12
    assert block["start_label"] == "0:00"


async def test_shift_schedule_date_posts_adf_fields(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = request.content.decode()
            assert cmsonline._SHIFT_FIELD_PREFIX + "year=2026" in body
            assert cmsonline._SHIFT_FIELD_PREFIX + "month=10" in body
            assert cmsonline._SHIFT_FIELD_PREFIX + "day=27" in body
            assert "event=" in body
            assert "Adf-Page-Id=194" in body
        return httpx.Response(200, text=SHIFT_HTML)

    recorder = Recorder(handler)
    client = make_client(tmp_path, recorder)
    result = await cmsonline.get_shift_schedule(client, date="2026-10-27")
    assert result["date"] == "2026-10-27"
    assert result["block_count"] == 1


async def test_shift_schedule_rejects_bad_date(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text=SHIFT_HTML))
    client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await cmsonline.get_shift_schedule(client, date="27-10-2026")


async def test_shift_schedule_saml_maps_upstream(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, text="<body>SAMLRequest x</body>"))
    client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await cmsonline.get_shift_schedule(client)
