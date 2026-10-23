"""Authentication lifecycle and guard tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cmcps.auth.service import AuthService
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
