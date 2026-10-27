"""Keycloak Bearer-token capture, store, and refresh tests."""
# ruff: noqa: S105, S106 - fixture credential placeholders, not real secrets

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from cmcps.auth import service as service_module
from cmcps.auth.service import AuthService
from cmcps.auth.tokens import (
    ServiceTokenRecord,
    ServiceTokenStore,
    exchange_for_audience,
    parse_token_exchange,
    refresh_record,
)
from cmcps.config.settings import Settings
from cmcps.contracts.errors import (
    BrowserUnavailableError,
    InvalidInputError,
    PolicyViolationError,
)

_ENDPOINT = "https://auth.cern.ch/auth/realms/cern/protocol/openid-connect/token"
_POST = "grant_type=authorization_code&client_id=cmshltinfo-frontend&code=abc"
_RESPONSE = {
    "access_token": "access-secret",
    "refresh_token": "refresh-secret",
    "expires_in": 300,
    "refresh_expires_in": 1800,
    "token_type": "Bearer",
}


def _record(*, access_expired: bool = False) -> ServiceTokenRecord:
    now = time.time()
    return ServiceTokenRecord(
        service="hltinfo",
        token_endpoint=_ENDPOINT,
        client_id="cmshltinfo-frontend",
        access_token="access-secret",
        refresh_token="refresh-secret",
        access_expires_at=(now - 10) if access_expired else (now + 600),
        refresh_expires_at=now + 1800,
    )


def test_parse_token_exchange() -> None:
    record = parse_token_exchange(
        endpoint_url=_ENDPOINT,
        post_data=_POST,
        response_json=_RESPONSE,
        service="hltinfo",
    )
    assert record.service == "hltinfo"
    assert record.client_id == "cmshltinfo-frontend"
    assert record.access_token == "access-secret"
    assert record.refresh_token == "refresh-secret"
    assert record.access_valid()
    assert record.refresh_valid()


def test_parse_token_exchange_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="non-object"):
        parse_token_exchange(
            endpoint_url=_ENDPOINT, post_data=_POST, response_json=["x"], service="hltinfo"
        )
    with pytest.raises(ValueError, match="missing"):
        parse_token_exchange(
            endpoint_url=_ENDPOINT,
            post_data="grant_type=authorization_code",
            response_json=_RESPONSE,
            service="hltinfo",
        )
    with pytest.raises(ValueError, match="missing"):
        parse_token_exchange(
            endpoint_url=_ENDPOINT, post_data=_POST, response_json={}, service="hltinfo"
        )


def test_store_roundtrip_permissions(tmp_path: Path) -> None:
    store = ServiceTokenStore(tmp_path / "state" / "service-tokens.json")
    store.save(_record())
    reloaded = store.load("hltinfo")
    assert reloaded is not None
    assert reloaded.access_token == "access-secret"
    mode = (tmp_path / "state" / "service-tokens.json").stat().st_mode & 0o777
    assert mode == 0o600
    payload = json.loads((tmp_path / "state" / "service-tokens.json").read_text())
    assert payload["tokens"]["hltinfo"]["access_token"] == "access-secret"


def test_store_load_missing(tmp_path: Path) -> None:
    store = ServiceTokenStore(tmp_path / "none.json")
    assert store.load_all() == {}
    assert store.load("hltinfo") is None


async def test_service_bearer_mints_lazily_on_first_use(
    settings: Settings, valid_storage_state: dict[str, Any]
) -> None:
    """First use mints via headless capture; later calls reuse the stored record."""
    calls: list[str] = []

    async def captor(_: Settings, _state: dict[str, Any], service: str) -> dict[str, Any] | None:
        calls.append(service)
        return {
            "endpoint_url": _ENDPOINT,
            "post_data": _POST,
            "response_json": dict(_RESPONSE),
        }

    auth = AuthService(settings, bearer_captor=captor)
    auth.store.save(valid_storage_state)
    token = await auth.service_bearer("hltinfo")
    assert token == "access-secret"
    assert calls == ["hltinfo"]
    again = await auth.service_bearer("hltinfo")
    assert again == "access-secret"
    assert calls == ["hltinfo"]  # second call used the stored record


async def test_service_bearer_lazy_mint_capture_failure(
    settings: Settings, valid_storage_state: dict[str, Any]
) -> None:
    """A failed headless capture surfaces as a missing token, not an exception."""

    async def captor(_: Settings, _state: dict[str, Any], _service: str) -> dict[str, Any] | None:
        return None

    auth = AuthService(settings, bearer_captor=captor)
    auth.store.save(valid_storage_state)
    assert await auth.service_bearer("hltinfo") is None


async def test_service_bearer_lazy_mint_browser_unavailable(
    settings: Settings, valid_storage_state: dict[str, Any]
) -> None:
    async def captor(*_args: Any, **_kwargs: Any) -> dict[str, Any] | None:
        raise BrowserUnavailableError("no browser")

    auth = AuthService(settings, bearer_captor=captor)
    auth.store.save(valid_storage_state)
    assert await auth.service_bearer("hltinfo") is None


async def test_service_bearer_lazy_mint_requires_session(settings: Settings) -> None:
    calls: list[str] = []

    async def captor(_: Settings, _state: dict[str, Any], service: str) -> dict[str, Any] | None:
        calls.append(service)
        return None

    auth = AuthService(settings, bearer_captor=captor)
    assert await auth.service_bearer("hltinfo") is None
    assert calls == []  # no valid session: the browser is never started


async def test_service_bearer_unknown_service(settings: Settings) -> None:
    auth = AuthService(settings)
    with pytest.raises(InvalidInputError):
        await auth.service_bearer("backend-from-nowhere")
    assert await auth.service_bearer("hltinfo") is None


async def test_service_bearer_refresh_path(settings: Settings) -> None:
    refreshed_record = _record(access_expired=False)
    auth = AuthService(settings)
    auth.service_token_store().save(_record(access_expired=True))

    async def fake_refresh(_: Settings, record: ServiceTokenRecord) -> ServiceTokenRecord:
        assert record.refresh_token == "refresh-secret"
        return refreshed_record

    async def fake_exchange(
        _s: Settings, _r: ServiceTokenRecord, _a: str, **_: object
    ) -> ServiceTokenRecord:
        raise AssertionError("hltinfo has no exchange audience; must not be exchanged")

    original = service_module.refresh_record
    original_exchange = service_module.exchange_for_audience
    service_module.refresh_record = fake_refresh  # type: ignore[assignment]
    service_module.exchange_for_audience = fake_exchange  # type: ignore[assignment]
    try:
        token = await auth.service_bearer("hltinfo")
    finally:
        service_module.refresh_record = original  # type: ignore[assignment]
        service_module.exchange_for_audience = original_exchange  # type: ignore[assignment]
    assert token == refreshed_record.access_token
    assert auth.service_token_store().load("hltinfo") is not None


async def test_service_bearer_refresh_exchange_chain(settings: Settings) -> None:
    """glance_* services re-exchange the refreshed client token for the API audience."""
    stored = ServiceTokenRecord(
        service="glance_alcm",
        token_endpoint=_ENDPOINT,
        client_id="cms-alcm-client-prod",
        access_token="stale-api-token",
        refresh_token="refresh-secret",
        access_expires_at=time.time() - 10,
        refresh_expires_at=time.time() + 1800,
    )
    client_record = ServiceTokenRecord(
        service="glance_alcm",
        token_endpoint=_ENDPOINT,
        client_id="cms-alcm-client-prod",
        access_token="client-scoped-token",
        refresh_token="fresh-refresh",
        access_expires_at=time.time() + 600,
    )
    api_record = ServiceTokenRecord(
        service="glance_alcm",
        token_endpoint=_ENDPOINT,
        client_id="cms-alcm-client-prod",
        access_token="api-scoped-token",
        refresh_token="fresh-refresh",
        access_expires_at=time.time() + 600,
    )
    auth = AuthService(settings)
    auth.service_token_store().save(stored)
    seen_audiences: list[str] = []

    async def fake_refresh(_: Settings, record: ServiceTokenRecord) -> ServiceTokenRecord:
        assert record.refresh_token == "refresh-secret"
        return client_record

    async def fake_exchange(
        _s: Settings, record: ServiceTokenRecord, audience: str, **_: object
    ) -> ServiceTokenRecord:
        assert record.access_token == "client-scoped-token"
        seen_audiences.append(audience)
        return api_record

    original = service_module.refresh_record
    original_exchange = service_module.exchange_for_audience
    service_module.refresh_record = fake_refresh  # type: ignore[assignment]
    service_module.exchange_for_audience = fake_exchange  # type: ignore[assignment]
    try:
        token = await auth.service_bearer("glance_alcm")
    finally:
        service_module.refresh_record = original  # type: ignore[assignment]
        service_module.exchange_for_audience = original_exchange  # type: ignore[assignment]
    assert seen_audiences == ["cms-alcm-api-prod"]
    assert token == "api-scoped-token"
    persisted = auth.service_token_store().load("glance_alcm")
    assert persisted is not None
    assert persisted.access_token == "api-scoped-token"
    assert persisted.refresh_token == "fresh-refresh"


async def test_exchange_for_audience_http(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(_ENDPOINT)
        body = request.content.decode()
        assert "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Atoken-exchange" in body
        assert "client_id=cms-alcm-client-prod" in body
        assert "subject_token=client-scoped-token" in body
        assert "audience=cms-alcm-api-prod" in body
        return httpx.Response(200, json={"access_token": "api-scoped-token", "expires_in": 1200})

    record = ServiceTokenRecord(
        service="glance_alcm",
        token_endpoint=_ENDPOINT,
        client_id="cms-alcm-client-prod",
        access_token="client-scoped-token",
        refresh_token="refresh-secret",
        access_expires_at=time.time() + 600,
        refresh_expires_at=time.time() + 1800,
    )
    transport = httpx.MockTransport(handler)
    exchanged = await exchange_for_audience(
        settings, record, "cms-alcm-api-prod", transport=transport
    )
    assert exchanged.access_token == "api-scoped-token"
    assert exchanged.refresh_token == "refresh-secret"
    assert exchanged.access_valid()


async def test_refresh_record_http(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(_ENDPOINT)
        body = request.content.decode()
        assert "grant_type=refresh_token" in body
        assert "refresh-secret" in body
        assert "client_id=cmshltinfo-frontend" in body
        return httpx.Response(
            200, json={"access_token": "fresh-token", "expires_in": 600, "refresh_token": "rt2"}
        )

    record = _record(access_expired=True)
    transport = httpx.MockTransport(handler)
    refreshed = await refresh_record(settings, record, transport=transport)
    assert refreshed.access_token == "fresh-token"
    assert refreshed.refresh_token == "rt2"


async def test_refresh_record_rejects_foreign_endpoint(settings: Settings) -> None:
    record = ServiceTokenRecord(
        service="hltinfo",
        token_endpoint="https://attacker.example.org/token",
        client_id="c",
        access_token="access-secret",
        refresh_token="refresh-secret",
        access_expires_at=time.time() - 10,
    )
    with pytest.raises(PolicyViolationError):
        await refresh_record(settings, record)


async def test_service_token_status_shape(settings: Settings) -> None:
    auth = AuthService(settings)
    assert auth.service_token_status() == {
        "hltinfo": {"captured": False},
        "glance_alcm": {"captured": False},
        "glance_membership": {"captured": False},
    }
    auth.service_token_store().save(_record())
    status = auth.service_token_status()
    assert status["hltinfo"]["captured"] is True
    assert status["hltinfo"]["access_valid"] is True
    assert status["hltinfo"]["refresh_available"] is True
