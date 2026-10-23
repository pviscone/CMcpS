"""Keycloak Bearer-token capture, storage, and refresh for Bearer-only services.

Some CERN services (e.g. the CMS HLT info API) accept only an
``Authorization: Bearer <token>`` header whose token belongs to the
service's own Keycloak public client — browser SSO cookies alone return
``401``. The visible SSO sign-in flow therefore also opens each configured
service app in the same authenticated browser context and intercepts the
SPA's authorization-code exchange with the Keycloak token endpoint
(``/protocol/openid-connect/token``). The captured public-client token
response (access token, refresh token, client id, endpoint URL) is stored
with 0600 permissions and refreshed later with a plain
``grant_type=refresh_token`` POST, so day-to-day use never reopens the
browser. Tokens are only returned inside trusted adapter code and are never
logged or exposed in tool results.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
from pydantic import BaseModel, Field

from cmcps.config.settings import Settings
from cmcps.contracts.errors import SessionStoreError, UpstreamError
from cmcps.net.safety import host_matches, validate_url

TOKEN_STORE_SCHEMA_VERSION = 1
_EXPIRY_SKEW_SECONDS = 30.0
_DEFAULT_ACCESS_LIFETIME_SECONDS = 300.0
_REFRESH_TIMEOUT_SECONDS = 20.0
_HTTP_OK = 200

# Services whose APIs require a Keycloak Bearer token discovered through
# the app's own frontend. Keys are service names used by adapters.
# Each entry maps a service name to the settings attribute carrying the
# service's app root URL (the sign-in flow opens it to trigger the OIDC
# authorization-code exchange).
BEARER_SERVICES: dict[str, str] = {
    "hltinfo": "hltinfo_base_url",
    "glance_alcm": "glance_alcm_app_url",
    "glance_membership": "glance_membership_app_url",
}

# Services whose API tokens are issued through RFC 8693 token exchange: the
# app SPA first obtains a client-scoped token (PKCE login), then exchanges it
# for the API audience. A plain refresh_token grant only yields the
# client-scoped token again (aud = app client id), which the API rejects with
# "Authentication token introspection failed". After each refresh the
# refreshed client token must therefore be re-exchanged for this audience.
BEARER_SERVICE_EXCHANGE_AUDIENCES: dict[str, str] = {
    "glance_alcm": "cms-alcm-api-prod",
    "glance_membership": "cms-membership-api-prod",
}

_TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"  # noqa: S105
_ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"  # noqa: S105


class ServiceTokenRecord(BaseModel):
    """One captured Bearer-token set; fields are credential material."""

    service: str
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    token_endpoint: str
    client_id: str
    access_token: str = Field(repr=False)
    refresh_token: str | None = Field(default=None, repr=False)
    access_expires_at: float | None = None
    refresh_expires_at: float | None = None

    def access_valid(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if self.access_expires_at is None:
            return True  # expiry unknown; try and let the service judge
        return now < self.access_expires_at - _EXPIRY_SKEW_SECONDS

    def refresh_valid(self, now: float | None = None) -> bool:
        if self.refresh_token is None:
            return False
        now = time.time() if now is None else now
        if self.refresh_expires_at is None:
            return True
        return now < self.refresh_expires_at - _EXPIRY_SKEW_SECONDS


def parse_token_exchange(
    *,
    endpoint_url: str,
    post_data: str,
    response_json: Any,
    service: str,
) -> ServiceTokenRecord:
    """Build one record from the intercepted OIDC token exchange."""
    form = dict(parse_qsl(post_data))
    client_id = form.get("client_id")
    if not isinstance(response_json, dict):
        raise ValueError("token endpoint returned a non-object body")
    access_token = response_json.get("access_token")
    if not client_id or not isinstance(access_token, str) or not access_token:
        raise ValueError("token exchange missing client_id or access_token")
    now = time.time()
    try:
        expires_in = float(response_json.get("expires_in", _DEFAULT_ACCESS_LIFETIME_SECONDS))
    except (TypeError, ValueError):
        expires_in = _DEFAULT_ACCESS_LIFETIME_SECONDS
    refresh_expires_in = response_json.get("refresh_expires_in")
    return ServiceTokenRecord(
        service=service,
        token_endpoint=endpoint_url,
        client_id=client_id,
        access_token=access_token,
        refresh_token=response_json.get("refresh_token"),
        access_expires_at=now + expires_in,
        refresh_expires_at=(now + float(refresh_expires_in)) if refresh_expires_in else None,
    )


class ServiceTokenStore:
    """Atomic 0600 store for captured Bearer tokens."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load_all(self) -> dict[str, ServiceTokenRecord]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != TOKEN_STORE_SCHEMA_VERSION:
                raise ValueError("unsupported service-token schema version")
            records = {
                name: ServiceTokenRecord.model_validate(entry)
                for name, entry in (payload.get("tokens") or {}).items()
            }
        except (OSError, ValueError, TypeError) as exc:
            raise SessionStoreError(
                "The service-token store could not be read",
                instructions="Remove the corrupted file and call cern_auth_sign_in again.",
            ) from exc
        return records

    def load(self, service: str) -> ServiceTokenRecord | None:
        return self.load_all().get(service)

    def save_all(self, records: dict[str, ServiceTokenRecord]) -> None:
        payload = {
            "schema_version": TOKEN_STORE_SCHEMA_VERSION,
            "tokens": {name: json.loads(r.model_dump_json()) for name, r in records.items()},
        }
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.path.parent, 0o700)
            fd, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent, text=True
            )
            temporary_path = Path(temporary)
            try:
                os.chmod(temporary, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2)
                    handle.write("\n")
                os.replace(temporary_path, self.path)
                os.chmod(self.path, 0o600)
            finally:
                if temporary_path.exists():
                    temporary_path.unlink()
        except OSError as exc:
            raise SessionStoreError(
                "The service-token store could not be written",
                instructions="Check that CMCPS_STATE_DIR is writable and not world-readable.",
            ) from exc

    def save(self, record: ServiceTokenRecord) -> None:
        records = self.load_all()
        records[record.service] = record
        self.save_all(records)

    def clear(self, service: str) -> None:
        records = self.load_all()
        if service in records:
            del records[service]
            self.save_all(records)


async def refresh_record(
    settings: Settings,
    record: ServiceTokenRecord,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ServiceTokenRecord:
    """Exchange the stored refresh token for a fresh access token.

    Uses an isolated httpx client with the same URL policy as the rest of
    the auth layer; no cookies or other credentials are attached.
    ``transport`` exists only for tests.
    """
    if not record.refresh_valid():
        raise UpstreamError(
            f"the captured {record.service} token and refresh token are expired",
            instructions="Call cern_auth_sign_in again to recapture fresh credentials.",
        )
    endpoint = validate_url(record.token_endpoint, allowed_hosts=settings.allowed_hosts)
    parsed = urlsplit(endpoint)
    if parsed.hostname is None or not host_matches(parsed.hostname, settings.allowed_hosts):
        raise UpstreamError(
            f"refusing to refresh {record.service}: unexpected token endpoint {endpoint}",
            instructions="The stored endpoint is outside CMCPS_ALLOWED_HOSTS; re-sign in.",
        )
    async with httpx.AsyncClient(
        follow_redirects=False, trust_env=False, transport=transport
    ) as client:
        response = await client.post(
            endpoint,
            data={
                "grant_type": "refresh_token",
                "client_id": record.client_id,
                "refresh_token": record.refresh_token or "",
            },
            timeout=_REFRESH_TIMEOUT_SECONDS,
        )
    if response.status_code != _HTTP_OK:
        raise UpstreamError(
            f"{record.service} token refresh failed with status {response.status_code}",
            status_code=response.status_code,
            instructions="The refresh token may be revoked; call cern_auth_sign_in again.",
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise UpstreamError(
            f"{record.service} token refresh returned non-JSON",
            instructions="Call cern_auth_sign_in again to recapture credentials.",
        ) from exc
    now = time.time()
    access_token = body.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise UpstreamError(
            f"{record.service} token refresh returned no access_token",
            instructions="Call cern_auth_sign_in again to recapture credentials.",
        )
    try:
        expires_in = float(body.get("expires_in", _DEFAULT_ACCESS_LIFETIME_SECONDS))
    except (TypeError, ValueError):
        expires_in = _DEFAULT_ACCESS_LIFETIME_SECONDS
    refresh_expires_in = body.get("refresh_expires_in")
    return ServiceTokenRecord(
        service=record.service,
        captured_at=record.captured_at,
        token_endpoint=record.token_endpoint,
        client_id=record.client_id,
        access_token=access_token,
        refresh_token=body.get("refresh_token") or record.refresh_token,
        access_expires_at=now + expires_in,
        refresh_expires_at=(now + float(refresh_expires_in))
        if refresh_expires_in
        else record.refresh_expires_at,
    )


async def exchange_for_audience(
    settings: Settings,
    record: ServiceTokenRecord,
    audience: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ServiceTokenRecord:
    """Re-exchange a refreshed client-scoped token for the API audience.

    Required for services in ``BEARER_SERVICE_EXCHANGE_AUDIENCES``: keeps the
    fresh refresh token (client-level, reusable for later cycles) and replaces
    only the access token with the audience-scoped one the API accepts.
    """
    endpoint = validate_url(record.token_endpoint, allowed_hosts=settings.allowed_hosts)
    parsed = urlsplit(endpoint)
    if parsed.hostname is None or not host_matches(parsed.hostname, settings.allowed_hosts):
        raise UpstreamError(
            f"refusing to exchange {record.service}: unexpected token endpoint {endpoint}",
            instructions="The stored endpoint is outside CMCPS_ALLOWED_HOSTS; re-sign in.",
        )
    async with httpx.AsyncClient(
        follow_redirects=False, trust_env=False, transport=transport
    ) as client:
        response = await client.post(
            endpoint,
            data={
                "grant_type": _TOKEN_EXCHANGE_GRANT,
                "client_id": record.client_id,
                "subject_token": record.access_token,
                "subject_token_type": _ACCESS_TOKEN_TYPE,
                "audience": audience,
            },
            timeout=_REFRESH_TIMEOUT_SECONDS,
        )
    if response.status_code != _HTTP_OK:
        raise UpstreamError(
            f"{record.service} token exchange failed with status {response.status_code}",
            status_code=response.status_code,
            instructions="The captured session may be stale; call cern_auth_sign_in again.",
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise UpstreamError(
            f"{record.service} token exchange returned non-JSON",
            instructions="Call cern_auth_sign_in again to recapture credentials.",
        ) from exc
    now = time.time()
    access_token = body.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise UpstreamError(
            f"{record.service} token exchange returned no access_token",
            instructions="Call cern_auth_sign_in again to recapture credentials.",
        )
    try:
        expires_in = float(body.get("expires_in", _DEFAULT_ACCESS_LIFETIME_SECONDS))
    except (TypeError, ValueError):
        expires_in = _DEFAULT_ACCESS_LIFETIME_SECONDS
    return ServiceTokenRecord(
        service=record.service,
        captured_at=record.captured_at,
        token_endpoint=record.token_endpoint,
        client_id=record.client_id,
        access_token=access_token,
        refresh_token=record.refresh_token,
        access_expires_at=now + expires_in,
        refresh_expires_at=record.refresh_expires_at,
    )
