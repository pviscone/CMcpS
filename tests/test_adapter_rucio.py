"""Rucio adapter against a mock transport (x509 token login + NDJSON lists)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from cmcps.adapters import rucio
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

RULE = {
    "id": "a" * 32,
    "scope": "cms",
    "name": "/A/B/NANOAOD",
    "rse_expression": "T2_CH_CERN",
    "state": "STUCK",
    "copies": 1,
    "locks_ok_cnt": 3,
    "locks_replicating_cnt": 0,
    "locks_stuck_cnt": 2,
    "error": "transfer failed",
}


def _client(tmp_path: Path, *, auth_status: int = 200, **kw: Any) -> tuple[SafeHttpClient, list]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "cms-rucio-auth.cern.ch":
            return httpx.Response(auth_status, headers={"X-Rucio-Auth-Token": "tok-1"})
        if request.url.path == "/rules/":
            lines = "\n".join(json.dumps({**RULE, "name": f"/A/{i}/NANOAOD"}) for i in range(3))
            return httpx.Response(200, content=lines.encode())
        if request.url.path == f"/rules/{'a' * 32}":
            return httpx.Response(200, json=RULE)
        return httpx.Response(404, content=b"not found")

    settings = Settings(state_dir=tmp_path, **kw)
    client = SafeHttpClient(
        settings,
        AuthService(settings),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return client, seen


@pytest.fixture(autouse=True)
def _fresh_tokens() -> None:
    rucio._tokens.clear()


async def test_list_rules_logs_in_once_and_parses_ndjson(tmp_path: Path) -> None:
    client, seen = _client(tmp_path, rucio_account="jdoe")
    result = await rucio.list_rules(client, client.settings, state="STUCK", limit=2)
    await rucio.list_rules(client, client.settings)
    auth = [r for r in seen if r.url.host == "cms-rucio-auth.cern.ch"]
    assert len(auth) == 1 and auth[0].headers["X-Rucio-Account"] == "jdoe"
    data = [r for r in seen if r.url.host == "cms-rucio.cern.ch"]
    assert data[0].headers["X-Rucio-Auth-Token"] == "tok-1"
    assert data[0].url.params["account"] == "jdoe" and data[0].url.params["state"] == "STUCK"
    assert "cookie" not in data[0].headers
    assert result.size == 2 and result.total_count == 3 and result.has_more is True
    assert result.items[0]["locks"] == "ok 3 / replicating 0 / stuck 2"


async def test_get_rule_and_validation(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, rucio_account="jdoe")
    rule = await rucio.get_rule(client, client.settings, rule_id="a" * 32)
    assert rule["state"] == "STUCK" and rule["error"] == "transfer failed"
    with pytest.raises(InvalidInputError):
        await rucio.get_rule(client, client.settings, rule_id="nope")
    with pytest.raises(InvalidInputError):
        await rucio.get_did(client, client.settings, did="no-scope")


async def test_missing_account_and_failed_login_point_to_configuration(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    with pytest.raises(InvalidInputError) as missing:
        await rucio.list_rules(client, client.settings)
    assert "CMCPS_RUCIO_ACCOUNT" in (missing.value.instructions or "")
    client, _ = _client(tmp_path, auth_status=401, rucio_account="jdoe")
    with pytest.raises(UpstreamError) as failed:
        await rucio.list_rules(client, client.settings)
    assert "CMCPS_GRID_CERT_FILE" in (failed.value.instructions or "")
