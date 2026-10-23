"""Owner-only session storage and destination cookie-scoping tests."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from cmcps.auth.store import SessionRecord, SessionStore, minimize_storage_state
from cmcps.config.settings import Settings
from cmcps.contracts.errors import SessionStoreError


def _identity_arguments(settings: Settings) -> dict[str, Any]:
    return {
        "identity_host": "auth.cern.ch",
        "max_session_age_seconds": settings.session_max_age_seconds,
    }


def test_save_load_permissions_and_identity(
    settings: Settings,
    valid_storage_state: dict[str, Any],
) -> None:
    store = SessionStore(settings.session_path)
    record = store.save(valid_storage_state)
    assert (settings.session_path.stat().st_mode & 0o777) == 0o600

    loaded = store.load()
    assert loaded is not None
    assert loaded.created_at >= record.created_at
    assert loaded.is_authenticated(
        settings.identity_cookie_names,
        **_identity_arguments(settings),
    )
    identity = loaded.identity_cookie(
        settings.identity_cookie_names,
        **_identity_arguments(settings),
    )
    assert identity is not None
    assert identity["name"] == "KEYCLOAK_IDENTITY"


def test_cookie_header_is_destination_scoped(
    settings: Settings,
    valid_storage_state: dict[str, Any],
) -> None:
    record = SessionStore(settings.session_path).save(valid_storage_state)
    service = record.cookie_header_for("https://service.cern.ch/path")
    auth = record.cookie_header_for("https://auth.cern.ch/account")
    unrelated = record.cookie_header_for("https://other.cern.ch/path")

    assert service == "SERVICE=service-secret-value"
    assert auth == "KEYCLOAK_IDENTITY=sso-secret-value"
    assert unrelated is None


def test_cookie_path_respects_rfc6265_segment_boundary(settings: Settings) -> None:
    state = {
        "cookies": [
            {
                "name": "API",
                "value": "api-secret",
                "domain": ".service.cern.ch",
                "path": "/api",
                "expires": time.time() + 3600,
                "secure": True,
                "httpOnly": True,
            }
        ],
        "origins": [],
    }
    record = SessionStore(settings.session_path).save(state)
    assert record.cookie_header_for("https://service.cern.ch/api") == "API=api-secret"
    assert record.cookie_header_for("https://service.cern.ch/api/v2") == "API=api-secret"
    assert record.cookie_header_for("https://service.cern.ch/apiv2") is None


def test_host_only_cookie_is_not_sent_to_subdomains(settings: Settings) -> None:
    state = {
        "cookies": [
            {
                "name": "HOSTONLY",
                "value": "host-secret",
                "domain": "service.cern.ch",
                "path": "/",
                "expires": time.time() + 3600,
                "secure": True,
                "httpOnly": True,
            }
        ],
        "origins": [],
    }
    record = SessionStore(settings.session_path).save(state)
    assert record.cookie_header_for("https://service.cern.ch/") == "HOSTONLY=host-secret"
    assert record.cookie_header_for("https://sub.service.cern.ch/") is None


def test_expired_identity_is_not_authenticated(
    settings: Settings,
    expired_storage_state: dict[str, Any],
) -> None:
    record = SessionStore(settings.session_path).save(expired_storage_state)
    assert not record.is_authenticated(
        settings.identity_cookie_names,
        **_identity_arguments(settings),
    )


def test_unix_epoch_identity_is_expired_not_session_scoped(
    settings: Settings,
    valid_storage_state: dict[str, Any],
) -> None:
    state = dict(valid_storage_state)
    state["cookies"] = [
        dict(cookie, expires=0) if cookie["name"] == "KEYCLOAK_IDENTITY" else cookie
        for cookie in valid_storage_state["cookies"]
    ]
    record = SessionStore(settings.session_path).save(state)
    assert not record.is_authenticated(
        settings.identity_cookie_names,
        **_identity_arguments(settings),
    )


def test_parent_domain_identity_cookie_is_not_auth_proof(
    settings: Settings,
    valid_storage_state: dict[str, Any],
) -> None:
    state = dict(valid_storage_state)
    state["cookies"] = [
        dict(cookie, domain=".cern.ch") if cookie["name"] == "KEYCLOAK_IDENTITY" else cookie
        for cookie in valid_storage_state["cookies"]
    ]
    record = SessionStore(settings.session_path).save(state)
    assert not record.is_authenticated(
        settings.identity_cookie_names,
        **_identity_arguments(settings),
    )


def test_insecure_identity_cookie_is_not_auth_proof(
    settings: Settings,
    valid_storage_state: dict[str, Any],
) -> None:
    state = dict(valid_storage_state)
    state["cookies"] = [
        dict(cookie, secure=False, httpOnly=False)
        if cookie["name"] == "KEYCLOAK_IDENTITY"
        else cookie
        for cookie in valid_storage_state["cookies"]
    ]
    record = SessionStore(settings.session_path).save(state)
    assert not record.is_authenticated(
        settings.identity_cookie_names,
        **_identity_arguments(settings),
    )


def test_session_cookie_identity_is_bounded_by_record_age(
    settings: Settings,
    valid_storage_state: dict[str, Any],
) -> None:
    state = dict(valid_storage_state)
    state["cookies"] = [
        dict(cookie, expires=-1) if cookie["name"] == "KEYCLOAK_IDENTITY" else cookie
        for cookie in valid_storage_state["cookies"]
    ]
    old = datetime.now(UTC) - timedelta(seconds=settings.session_max_age_seconds + 60)
    record = SessionRecord(storage_state=state, created_at=old)
    assert not record.is_authenticated(
        settings.identity_cookie_names,
        **_identity_arguments(settings),
    )


def test_identity_cookie_is_scoped_to_configured_sso_host(
    settings: Settings,
    valid_storage_state: dict[str, Any],
) -> None:
    record = SessionStore(settings.session_path).save(valid_storage_state)
    assert not record.is_authenticated(
        settings.identity_cookie_names,
        identity_host="sso-auth.example",
        max_session_age_seconds=settings.session_max_age_seconds,
    )

    state = dict(valid_storage_state)
    state["cookies"] = [
        dict(cookie, domain=".sso-auth.example")
        if cookie["name"] == "KEYCLOAK_IDENTITY"
        else cookie
        for cookie in valid_storage_state["cookies"]
    ]
    moved = SessionStore(settings.session_path).save(state)
    assert moved.is_authenticated(
        settings.identity_cookie_names,
        identity_host="sso-auth.example",
        max_session_age_seconds=settings.session_max_age_seconds,
    )


def test_storage_state_minimization_drops_origins_and_foreign_cookies(
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
            "expires": time.time() + 3600,
            "secure": True,
            "httpOnly": True,
        },
    ]
    state["origins"] = [
        {
            "origin": "https://auth.cern.ch",
            "localStorage": [{"name": "oidc", "value": "local-secret"}],
        }
    ]
    minimized = minimize_storage_state(state, settings.allowed_hosts)
    serialized = json.dumps(minimized)
    assert len(minimized["cookies"]) == 2
    assert minimized["origins"] == []
    assert "foreign-secret" not in serialized
    assert "local-secret" not in serialized


def test_corrupt_session_store_reports_actionable_error(settings: Settings) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.session_path.write_text(json.dumps({"unexpected": True}), encoding="utf-8")
    with pytest.raises(SessionStoreError) as exc_info:
        SessionStore(settings.session_path).load()
    assert exc_info.value.instructions is not None
    assert "cern_auth_sign_in" in exc_info.value.instructions
