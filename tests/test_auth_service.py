"""Authentication lifecycle and guard tests."""
# ruff: noqa: S106 - fixture credential placeholders, not real secrets

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cmcps.auth.service import AuthService
from cmcps.auth.tokens import ServiceTokenRecord
from cmcps.config.settings import Settings
from cmcps.contracts.errors import AuthRequiredError, InvalidInputError


async def test_sign_in_collects_and_stores_state(
    settings: Settings,
    valid_storage_state: dict[str, Any],
) -> None:
    calls: list[int | None] = []

    async def collector(_: Settings, timeout: int | None) -> dict[str, Any]:
        calls.append(timeout)
        return valid_storage_state

    auth = AuthService(settings, collector=collector)
    result = await auth.sign_in(timeout_seconds=60)

    assert calls == [60]
    assert result.authenticated is True
    assert result.identity_cookie_name == "KEYCLOAK_IDENTITY"
    assert "secret" not in result.model_dump_json()


async def test_auth_guard_rejects_missing_and_expired_sessions(
    settings: Settings,
    expired_storage_state: dict[str, Any],
) -> None:
    auth = AuthService(settings)
    with pytest.raises(AuthRequiredError):
        await auth.ensure_authenticated()

    auth.store.save(expired_storage_state)
    with pytest.raises(AuthRequiredError):
        await auth.ensure_authenticated()


async def test_auth_guard_accepts_valid_state(
    settings: Settings,
    valid_storage_state: dict[str, Any],
) -> None:
    auth = AuthService(settings)
    auth.store.save(valid_storage_state)
    session = await auth.ensure_authenticated()
    assert session.authenticated
    assert session.cookie_header_for("https://auth.cern.ch/account") == (
        "KEYCLOAK_IDENTITY=sso-secret-value"
    )


async def test_explicit_bypass_allows_offline_public_development(tmp_path) -> None:
    settings = Settings(state_dir=tmp_path, require_sso=False)
    auth = AuthService(settings)
    session = await auth.ensure_authenticated()
    status = auth.status()
    assert session.authenticated
    assert session.bypass
    assert status.authenticated
    assert status.auth_bypass
    assert status.session_exists is False


async def test_bypass_status_preserves_existing_session_metadata(
    tmp_path,
    valid_storage_state: dict[str, Any],
) -> None:
    settings = Settings(state_dir=tmp_path, require_sso=False)
    auth = AuthService(settings)
    auth.store.save(valid_storage_state)
    status = auth.status()
    assert status.authenticated
    assert status.auth_bypass
    assert status.session_exists is True
    assert status.cookie_count == 2


async def test_corrupt_stale_store_does_not_block_explicit_bypass(tmp_path) -> None:
    settings = Settings(state_dir=tmp_path, require_sso=False)
    auth = AuthService(settings)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.session_path.write_text("not json", encoding="utf-8")
    status = auth.status()
    session = await auth.ensure_authenticated()
    assert status.authenticated
    assert status.auth_bypass
    assert status.session_exists is False
    assert session.authenticated


async def test_status_exposes_metadata_but_not_credential_values(
    settings: Settings,
    valid_storage_state: dict[str, Any],
) -> None:
    auth = AuthService(settings)
    auth.store.save(valid_storage_state)
    payload = json.dumps(auth.status().model_dump(mode="json"))
    assert "sso-secret-value" not in payload
    assert "service-secret-value" not in payload
    assert "KEYCLOAK_IDENTITY" in payload
    assert settings.session_path.name in payload
    assert str(settings.state_dir) not in payload


async def test_sign_in_rejects_timeout_outside_server_policy(
    settings: Settings,
    valid_storage_state: dict[str, Any],
) -> None:
    calls: list[int | None] = []

    async def collector(_: Settings, timeout: int | None) -> dict[str, Any]:
        calls.append(timeout)
        return valid_storage_state

    auth = AuthService(settings, collector=collector)
    for invalid in (10, 901):
        with pytest.raises(InvalidInputError):
            await auth.sign_in(timeout_seconds=invalid)
    assert calls == []


async def test_sign_in_reports_sso_host_missing_from_allowlist(tmp_path: Path) -> None:
    settings = Settings(
        state_dir=tmp_path,
        allowed_hosts=("gitlab.cern.ch",),
    )

    async def collector(_: Settings, _timeout: int | None) -> dict[str, Any]:
        raise AssertionError("collector must not run before configuration validation")

    auth = AuthService(settings, collector=collector)
    with pytest.raises(InvalidInputError) as exc_info:
        await auth.sign_in()
    assert "CMCPS_ALLOWED_HOSTS" in (exc_info.value.instructions or "")


async def test_sign_in_persists_only_allowlisted_cookie_state(
    settings: Settings,
    valid_storage_state: dict[str, Any],
) -> None:
    state = dict(valid_storage_state)
    state["cookies"] = [
        *valid_storage_state["cookies"],
        {
            "name": "FOREIGN",
            "value": "foreign-secret",
            "domain": ".example.net",
            "path": "/",
            "expires": 2000000000,
            "secure": True,
            "httpOnly": True,
        },
    ]
    state["origins"] = [{"origin": "https://auth.cern.ch", "localStorage": [{"n": "s"}]}]

    async def collector(_: Settings, timeout: int | None) -> dict[str, Any]:
        assert timeout == settings.sso_timeout_seconds
        return state

    auth = AuthService(settings, collector=collector)
    await auth.sign_in()
    stored = auth.store.load()
    assert stored is not None
    serialized = stored.model_dump_json()
    assert len(stored.cookies) == 2
    assert stored.storage_state["origins"] == []
    assert "foreign-secret" not in serialized
    assert "localStorage" not in serialized


async def test_sign_in_mints_nothing_eagerly(
    settings: Settings,
    valid_storage_state: dict[str, Any],
) -> None:
    """Sign-in stores only the identity; Bearer capture waits for first use."""

    async def collector(_: Settings, _timeout: int | None) -> dict[str, Any]:
        return valid_storage_state

    async def failing_captor(*_args: Any, **_kwargs: Any) -> dict[str, Any] | None:
        raise AssertionError("no Bearer capture may run during sign-in")

    auth = AuthService(settings, collector=collector, bearer_captor=failing_captor)
    result = await auth.sign_in()
    assert result.authenticated is True
    assert "Captured" not in result.message
    assert auth.service_token_store().load_all() == {}
    session = await auth.ensure_authenticated()
    assert session.authenticated


async def test_reauthenticate_discards_session_and_service_tokens(
    settings: Settings,
    valid_storage_state: dict[str, Any],
) -> None:
    calls: list[int | None] = []

    async def collector(_: Settings, timeout: int | None) -> dict[str, Any]:
        calls.append(timeout)
        return valid_storage_state

    auth = AuthService(settings, collector=collector)
    await auth.sign_in(timeout_seconds=60)
    token_store = auth.service_token_store()
    token_store.save_all(
        {
            "glance_alcm": ServiceTokenRecord(
                service="glance_alcm",
                token_endpoint="https://auth.cern.ch/auth/realms/cern/protocol/openid-connect/token",
                client_id="cmsfence-cms-alcm",
                access_token="stale-access",
                refresh_token="stale-refresh",
                access_expires_at=1.0,
                refresh_expires_at=1.0,
            )
        }
    )
    real_collector = auth.collector

    async def checking_collector(inner_settings: Settings, timeout: int | None):
        assert auth.store.load() is None
        assert token_store.load_all() == {}
        return await real_collector(inner_settings, timeout)

    auth.collector = checking_collector
    result = await auth.reauthenticate(timeout_seconds=120)

    assert calls == [60, 120]
    assert result.authenticated is True
    assert "discarded" in result.message
    session = await auth.ensure_authenticated()
    assert session.authenticated


PORTAL_URL = "https://cmsonline.cern.ch/webcenter/portal/cmsonline/pages_common/shiftlist"


def _portal_cookie(expires: float) -> dict[str, Any]:
    return {
        "name": "PORTALSESSION",
        "value": "portal-secret-value",
        "domain": ".cmsonline.cern.ch",
        "path": "/",
        "expires": expires,
        "secure": True,
        "httpOnly": True,
        "sameSite": "Lax",
    }


async def test_render_page_returns_html_and_persists_cookies(
    settings: Settings,
    valid_storage_state: dict[str, Any],
    future_epoch: float,
) -> None:
    calls: list[tuple[str, Any]] = []

    async def renderer(
        _: Settings, state: dict[str, Any], url: str, interact: Any
    ) -> tuple[str, dict[str, Any]]:
        calls.append((url, interact))
        return (
            '<table bgcolor="Lavender"></table>',
            {
                "cookies": [*state["cookies"], _portal_cookie(future_epoch)],
                "origins": [],
            },
        )

    async def interact(_page: Any) -> bool:
        return True

    auth = AuthService(settings, page_renderer=renderer)
    auth.store.save(valid_storage_state)

    html = await auth.render_page(PORTAL_URL, interact=interact)
    assert html == '<table bgcolor="Lavender"></table>'
    assert calls == [(PORTAL_URL, interact)]

    record = auth.store.load()
    assert record is not None
    assert record.cookie_header_for(PORTAL_URL) == "PORTALSESSION=portal-secret-value"
    assert (
        record.cookie_header_for("https://auth.cern.ch/account")
        == "KEYCLOAK_IDENTITY=sso-secret-value"
    )


async def test_render_page_keeps_store_when_render_fails(
    settings: Settings,
    valid_storage_state: dict[str, Any],
) -> None:
    async def renderer(_: Settings, _state: dict[str, Any], _url: str, _interact: Any) -> None:
        return None

    auth = AuthService(settings, page_renderer=renderer)
    before = auth.store.save(valid_storage_state)

    assert await auth.render_page(PORTAL_URL) is None
    after = auth.store.load()
    assert after is not None
    assert after.created_at == before.created_at
    assert after.cookie_header_for(PORTAL_URL) is None


async def test_render_page_requires_usable_identity(
    settings: Settings,
    expired_storage_state: dict[str, Any],
) -> None:
    called = False

    async def renderer(
        _: Settings, _state: dict[str, Any], _url: str, _interact: Any
    ) -> tuple[str, dict[str, Any]]:
        nonlocal called
        called = True
        return ("", {})

    auth = AuthService(settings, page_renderer=renderer)

    assert await auth.render_page(PORTAL_URL) is None
    auth.store.save(expired_storage_state)
    assert await auth.render_page(PORTAL_URL) is None
    assert called is False


async def test_render_page_rejects_non_allowlisted_url(
    settings: Settings,
    valid_storage_state: dict[str, Any],
) -> None:
    auth = AuthService(settings)
    auth.store.save(valid_storage_state)
    assert await auth.render_page("https://attacker.example/portal") is None


async def test_render_page_disabled_in_bypass(tmp_path: Path) -> None:
    settings = Settings(state_dir=tmp_path, require_sso=False)
    auth = AuthService(settings)
    assert await auth.render_page(PORTAL_URL) is None


JIRA_SITE = "https://its.cern.ch/jira/secure/Dashboard.jspa"


async def test_warm_sites_persists_refreshed_state(
    settings: Settings,
    valid_storage_state: dict[str, Any],
) -> None:
    jira_cookie = {
        "name": "JSESSIONID",
        "value": "fresh-jira-session",
        "domain": "its.cern.ch",
        "path": "/jira",
        "expires": -1,
        "secure": True,
        "httpOnly": True,
    }
    calls: list[tuple[dict[str, Any], Any]] = []

    async def warmer(_: Settings, state: dict[str, Any], urls: Any) -> dict[str, Any]:
        calls.append((state, urls))
        refreshed = dict(state)
        refreshed["cookies"] = [*state["cookies"], jira_cookie]
        return refreshed

    auth = AuthService(settings, site_warmer=warmer)
    auth.store.save(valid_storage_state)
    assert await auth.warm_sites((JIRA_SITE,)) is True
    assert len(calls) == 1
    assert calls[0][0]["cookies"][0]["value"] == "sso-secret-value"
    stored = auth.store.load()
    assert stored is not None
    assert any(c["name"] == "JSESSIONID" for c in stored.cookies)


async def test_warm_sites_does_not_persist_unrelated_state(
    settings: Settings,
    valid_storage_state: dict[str, Any],
) -> None:
    calls = 0

    async def warmer(_: Settings, _state: dict[str, Any], _urls: Any) -> None:
        nonlocal calls
        calls += 1

    auth = AuthService(settings, site_warmer=warmer)
    auth.store.save(valid_storage_state)
    before = auth.store.load()
    assert await auth.warm_sites((JIRA_SITE,)) is False
    assert calls == 1
    assert auth.store.load() == before


async def test_warm_sites_rejected_without_usable_session(
    settings: Settings,
    expired_storage_state: dict[str, Any],
) -> None:
    async def warmer(*_args: Any) -> dict[str, Any]:
        raise AssertionError("warmer must not run without a live identity")

    auth = AuthService(settings, site_warmer=warmer)
    assert await auth.warm_sites((JIRA_SITE,)) is False
    auth.store.save(expired_storage_state)
    assert await auth.warm_sites((JIRA_SITE,)) is False


async def test_warm_sites_disabled_in_bypass(tmp_path: Path) -> None:
    async def warmer(*_args: Any) -> dict[str, Any]:
        raise AssertionError("warmer must not run in bypass mode")

    settings = Settings(state_dir=tmp_path, require_sso=False)
    auth = AuthService(settings, site_warmer=warmer)
    assert await auth.warm_sites((JIRA_SITE,)) is False
