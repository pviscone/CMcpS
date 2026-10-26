"""Fixture-driven contract tests for the CERN website inventory adapter."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from cmcps.adapters import websites
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

TEST_TOKEN = "unit-test-token"  # noqa: S105 - mock credential, not a real secret

WSP_PAYLOAD = [
    {
        "url": "atlas.web.cern.ch",
        "projectName": "atlas",
        "owner": "atsys",
        "category": "Official",
        "environment": "webeos",
        "displayType": "WebeosSite",
        "description": "ATLAS experiment home page",
        "creationTimestamp": "2020-01-01T00:00:00Z",
    },
    {
        "url": "hlt.docs.cern.ch",
        "projectName": "hlt-docs",
        "owner": "cmsops",
        "category": "Official",
        "environment": "paas",
        "displayType": "GitlabPagesSite",
        "description": "HLT trigger documentation",
        "creationTimestamp": "2021-06-01T00:00:00Z",
    },
    {
        "url": "jq.web.cern.ch",
        "projectName": "jq",
        "owner": "jdoe",
        "category": "Personal",
        "environment": "webeos",
        "displayType": "WebeosSite",
        "description": None,
        "creationTimestamp": None,
    },
    {
        "url": "https://Upper.web.cern.ch:443/path",
        "projectName": "upper",
        "owner": "jdoe",
        "category": "Test",
        "environment": "paas",
        "displayType": "PaasProject",
        "description": "normalization probe",
        "creationTimestamp": None,
    },
    {"url": "", "projectName": "no-hostname"},
    {"projectName": "no-url-field"},
    "not-a-record",
]

CT_PAYLOAD = [
    {"name_value": "atlas.web.cern.ch\nextra-node.cern.ch\n*.wild.cern.ch\noutside.example.org"}
]


@pytest.fixture(autouse=True)
def clean_refresh_state():
    """Isolate module-level refresh-task state between tests."""
    websites._REFRESH_STATE.update(
        task=None,
        started_at=None,
        last_finished_at=None,
        last_error=None,
        last_result=None,
    )
    yield
    task = websites._REFRESH_STATE.get("task")
    if task is not None and not task.done():
        task.cancel()


def make_client(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    token: str | None = TEST_TOKEN,
    max_age_days: int = 30,
) -> tuple[Settings, SafeHttpClient]:
    settings = Settings(
        state_dir=tmp_path,
        require_sso=False,
        browser_executable=None,
        websites_token=token,
        websites_cache_max_age_days=max_age_days,
    )
    auth = AuthService(settings)
    transport = httpx.MockTransport(handler)
    client = SafeHttpClient(
        settings, auth_service=auth, client=httpx.AsyncClient(transport=transport)
    )
    return settings, client


def ok_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "webservices-portal.web.cern.ch":
        assert request.headers.get("authorization") == f"Bearer {TEST_TOKEN}"
        assert request.url.path == "/api/search/hostnames"
        return httpx.Response(200, json=WSP_PAYLOAD)
    if request.url.host == "crt.sh":
        assert request.url.params.get("output") == "json"
        return httpx.Response(200, json=CT_PAYLOAD)
    return httpx.Response(404, json={"error": "unexpected host"})


async def test_refresh_builds_cache_and_reports_counts(tmp_path: Path) -> None:
    settings, client = make_client(tmp_path, ok_handler)
    result = await websites.refresh_sync(client, settings)
    assert result["refreshed"] is True
    # 4 valid wsp rows (empty-url / missing-url / non-dict dropped)
    assert result["wsp_count"] == 4
    # atlas.web.cern.ch deduplicated against wsp; outside.example.org dropped
    assert result["ctlog_count"] == 2
    assert result["site_count"] == 6
    assert result["environment_counts"] == {"paas": 2, "webeos": 2}
    assert settings.websites_cache_path.exists()
    assert not settings.websites_cache_path.with_suffix(".tmp").exists()

    st = await websites.status(settings)
    assert st["cache_exists"] is True
    assert st["stale"] is False
    assert st["site_count"] == 6
    assert st["source_counts"] == {"wsp": 4, "ctlog": 2}
    assert st["fts5"] is True


async def test_search_prefix_filters_paging_and_meta(tmp_path: Path) -> None:
    settings, client = make_client(tmp_path, ok_handler)
    await websites.refresh_sync(client, settings)

    result = await websites.search(client, settings, query="atla")
    assert result["total_count"] == 1
    assert result["items"][0]["hostname"] == "atlas.web.cern.ch"
    assert result["items"][0]["owner"] == "atsys"
    assert result["cache"]["fetched_at"]
    assert result["cache"]["stale_served"] is False

    by_owner = await websites.search(client, settings, owner="jdoe")
    assert {i["hostname"] for i in by_owner["items"]} == {"jq.web.cern.ch", "upper.web.cern.ch"}

    by_env = await websites.search(
        client, settings, environment="paas", category="official"
    )
    assert [i["hostname"] for i in by_env["items"]] == ["hlt.docs.cern.ch"]

    window = await websites.search(client, settings, limit=1, offset=1)
    assert window["has_more"] is True
    assert window["limit"] == 1 and window["offset"] == 1
    assert window["total_count"] == 6

    with pytest.raises(InvalidInputError):
        await websites.search(client, settings, query="!!!")


async def test_search_description_and_substring_fallback(tmp_path: Path) -> None:
    settings, client = make_client(tmp_path, ok_handler)
    await websites.refresh_sync(client, settings)

    desc = await websites.search(client, settings, query="trigger doc")
    assert [i["hostname"] for i in desc["items"]] == ["hlt.docs.cern.ch"]

    con = sqlite3.connect(settings.websites_cache_path)
    con.execute("UPDATE meta SET value = '0' WHERE key = 'fts5'")
    con.commit()
    con.close()
    fallback = await websites.search(client, settings, query="trigger doc")
    assert [i["hostname"] for i in fallback["items"]] == ["hlt.docs.cern.ch"]
    assert "substring" in fallback["note"]


async def test_lookup_normalizes_and_reports_missing(tmp_path: Path) -> None:
    settings, client = make_client(tmp_path, ok_handler)
    await websites.refresh_sync(client, settings)

    found = await websites.lookup(client, settings, hostname="HTTPS://Upper.web.cern.ch/x?q=1")
    assert found["found"] is True
    assert found["hostname"] == "upper.web.cern.ch"
    assert found["items"][0]["display_type"] == "PaasProject"

    missing = await websites.lookup(client, settings, hostname="nope.web.cern.ch")
    assert missing["found"] is False and missing["items"] == []

    with pytest.raises(InvalidInputError):
        await websites.lookup(client, settings, hostname="not a host")


def set_cache_age(db_path: Path, *, age_days: float) -> None:
    con = sqlite3.connect(db_path)
    con.execute(
        "UPDATE meta SET value = ? WHERE key = 'fetched_at_epoch'",
        (str(time.time() - age_days * 86_400),),
    )
    con.commit()
    con.close()


FetchMock = Callable[[SafeHttpClient, Settings], Awaitable[list[dict[str, Any]]]]


async def test_ttl_background_refresh_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = asyncio.Event()
    calls = {"n": 0}

    async def gated_wsp(_client: SafeHttpClient, _settings: Settings) -> list[dict[str, Any]]:
        calls["n"] += 1
        await gate.wait()
        return list(WSP_PAYLOAD)

    monkeypatch.setattr(websites, "_fetch_wsp_sites", gated_wsp)
    monkeypatch.setattr(
        websites, "_fetch_ctlog_hostnames", lambda _c, _s: _payload_async(CT_PAYLOAD)
    )
    settings = Settings(
        state_dir=tmp_path,
        require_sso=False,
        browser_executable=None,
        websites_token=None,
        websites_cache_max_age_days=30,
    )
    client = SafeHttpClient(settings, auth_service=None)  # never hit: fetchers mocked

    # Cold cache: search cannot serve anything -> retryable 503; refresh started.
    with pytest.raises(UpstreamError) as exc_info:
        await websites.search(client, settings, query="atla")
    assert exc_info.value.retryable is True
    assert websites._refresh_running() is True

    # Starter is idempotent while the first refresh runs.
    second = await websites.refresh(client, settings)
    assert second["refresh_started"] is False and second["refresh_running"] is True

    gate.set()
    await websites._REFRESH_STATE["task"]
    await asyncio.sleep(0.01)  # let the done-callback record the outcome
    assert calls["n"] == 1
    st = await websites.status(settings)
    assert st["refresh_running"] is False
    assert st["refresh_error"] is None
    assert st["last_refresh"]["site_count"] == 6

    fresh = await websites.search(client, settings, query="atla")
    assert fresh["total_count"] == 1 and fresh["cache"]["stale_served"] is False

    # Stale cache: search serves stale data immediately, refresh starts in background.
    gate.clear()
    set_cache_age(settings.websites_cache_path, age_days=45)
    stale = await websites.search(client, settings, query="atla")
    assert stale["cache"]["stale_served"] is True
    assert "refresh running in the background" in stale["note"]
    assert [i["hostname"] for i in stale["items"]] == ["atlas.web.cern.ch"]
    assert calls["n"] == 2

    gate.set()
    await websites._REFRESH_STATE["task"]
    await asyncio.sleep(0.01)
    refreshed = await websites.search(client, settings, query="atla")
    assert refreshed["cache"]["stale_served"] is False


async def test_background_refresh_failure_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_wsp(_client: SafeHttpClient, _settings: Settings) -> list[dict[str, Any]]:
        raise UpstreamError("portal exploded", instructions="test")

    monkeypatch.setattr(websites, "_fetch_wsp_sites", failing_wsp)
    monkeypatch.setattr(
        websites, "_fetch_ctlog_hostnames", lambda _c, _s: _payload_async(CT_PAYLOAD)
    )
    settings = Settings(
        state_dir=tmp_path, require_sso=False, browser_executable=None, websites_token=None
    )
    client = SafeHttpClient(settings, auth_service=None)

    started = await websites.refresh(client, settings)
    assert started["refresh_started"] is True
    with pytest.raises(UpstreamError, match="portal exploded"):
        await websites._REFRESH_STATE["task"]
    await asyncio.sleep(0.01)  # let the done-callback record the outcome
    st = await websites.status(settings)
    assert st["refresh_running"] is False
    assert st["refresh_error"] == "portal exploded"
    assert not settings.websites_cache_path.exists()  # failed refresh writes nothing

    # Cold cache with a failed previous refresh: next attempt restarts and reports retryable.
    with pytest.raises(UpstreamError) as exc_info:
        await websites.search(client, settings, query="atla")
    assert exc_info.value.retryable is True
    with pytest.raises(UpstreamError, match="portal exploded"):
        await websites._REFRESH_STATE["task"]
    assert not settings.websites_cache_path.exists()


async def _payload_async(payload: Any) -> list[dict[str, Any]]:
    return list(payload)


async def test_portal_401_raises_and_leaves_no_cache(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    settings, client = make_client(tmp_path, handler)
    with pytest.raises(UpstreamError):
        await websites.refresh_sync(client, settings)
    assert not settings.websites_cache_path.exists()


async def test_ctlog_failure_is_partial_success(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "crt.sh":
            return httpx.Response(500, text="crt.sh is down")
        return ok_handler(request)

    settings, client = make_client(tmp_path, handler)
    result = await websites.refresh_sync(client, settings)
    assert result["refreshed"] is True
    assert result["ctlog_count"] == 0
    assert result["wsp_count"] == 4
    assert "crt.sh" in result["note"]
    st = await websites.status(settings)
    assert st["source_counts"] == {"wsp": 4}


async def test_refresh_without_token_or_browser_fails_closed(tmp_path: Path) -> None:
    settings, client = make_client(tmp_path, ok_handler, token=None)
    with pytest.raises(InvalidInputError):
        await websites.refresh_sync(client, settings)
    assert not settings.websites_cache_path.exists()


def test_host_normalization_edges() -> None:
    assert websites._normalize_ctlog_hostname("*.WWW.CERN.CH") == "www.cern.ch"
    assert websites._normalize_ctlog_hostname("no-dot-name") is None
    assert websites._normalize_ctlog_hostname("evil.example.com") is None
    row = websites._normalize_wsp_record({"url": "HTTP://A.web.cern.ch:8443/x?y"})
    assert row is not None and row["hostname"] == "a.web.cern.ch"
    epoch_note = websites._age_days(
        {"fetched_at_epoch": str(time.time() - 2 * 86_400)}, now=time.time()
    )
    assert epoch_note is not None and 1.9 < epoch_note < 2.1
    iso = datetime.now(UTC).isoformat()
    built = websites._iso_from_meta({"fetched_at": iso})
    assert built == iso
