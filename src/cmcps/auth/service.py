"""Authentication service and server-side auth guard."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cmcps.auth.browser import (
    PortalInteract,
    collect_sso_state,
    extract_local_storage_value,
    render_page_html,
    warm_cookie_sites,
)
from cmcps.auth.store import SessionRecord, SessionStore, minimize_storage_state
from cmcps.auth.tokens import (
    BEARER_SERVICE_EXCHANGE_AUDIENCES,
    BEARER_SERVICES,
    ServiceTokenStore,
    exchange_for_audience,
    parse_token_exchange,
    refresh_record,
)
from cmcps.config.settings import (
    MAX_SSO_TIMEOUT_SECONDS,
    MIN_SSO_TIMEOUT_SECONDS,
    Settings,
)
from cmcps.contracts.errors import (
    AuthRequiredError,
    BrowserUnavailableError,
    InvalidInputError,
    InvalidSessionError,
    PolicyViolationError,
    SessionStoreError,
)
from cmcps.contracts.results import SessionMetadata, SignInResult
from cmcps.net.safety import host_matches, validate_url

logger = logging.getLogger(__name__)

StorageStateCollector = Callable[[Settings, int | None], Awaitable[dict[str, Any]]]
PageRenderer = Callable[
    [Settings, dict[str, Any], str, "PortalInteract | None"],
    Awaitable["tuple[str, dict[str, Any]] | None"],
]
SiteWarmer = Callable[
    [Settings, dict[str, Any], Sequence[str]],
    Awaitable["dict[str, Any] | None"],
]
LocalStorageExtractor = Callable[
    [Settings, dict[str, Any], str, str],
    Awaitable["tuple[str, dict[str, Any]] | None"],
]

SIGN_IN_FIRST = "Call cern_auth_sign_in and complete the CERN browser login."


@dataclass(slots=True)
class AuthenticatedSession:
    """Valid auth context passed only inside trusted adapter code."""

    record: SessionRecord | None
    require_sso: bool
    identity_names: tuple[str, ...]
    identity_host: str
    max_session_age_seconds: int
    bypass: bool = False

    @property
    def authenticated(self) -> bool:
        return self.bypass or (
            self.record is not None
            and self.record.is_authenticated(
                self.identity_names,
                identity_host=self.identity_host,
                max_session_age_seconds=self.max_session_age_seconds,
            )
        )

    def cookie_header_for(self, url: str) -> str | None:
        """Return destination-scoped cookies without exposing them to adapters' outputs."""
        if self.record is None:
            return None
        return self.record.cookie_header_for(url)


class AuthService:
    """Client-side session lifecycle; never returns secret cookie values."""

    def __init__(
        self,
        settings: Settings,
        *,
        store: SessionStore | None = None,
        collector: StorageStateCollector = collect_sso_state,
        page_renderer: PageRenderer = render_page_html,
        site_warmer: SiteWarmer = warm_cookie_sites,
        storage_extractor: LocalStorageExtractor = extract_local_storage_value,
    ) -> None:
        self.settings = settings
        self.store = store or SessionStore(settings.session_path)
        self.collector = collector
        self.page_renderer = page_renderer
        self.site_warmer = site_warmer
        self.storage_extractor = storage_extractor

    @property
    def identity_host(self) -> str:
        """Hostname whose identity cookie proves the configured SSO flow completed."""
        return self.settings.sso_identity_host

    def _validated_timeout(self, timeout_seconds: int | None) -> int:
        if timeout_seconds is None:
            return self.settings.sso_timeout_seconds
        if not MAX_SSO_TIMEOUT_SECONDS >= timeout_seconds >= MIN_SSO_TIMEOUT_SECONDS:
            raise InvalidInputError(
                "CERN SSO timeout must be between 30 and 900 seconds",
                instructions="Set timeout_seconds within 30 and 900, or omit it for the default.",
            )
        return timeout_seconds

    def _identity_cookie(self, record: SessionRecord) -> dict[str, Any] | None:
        return record.identity_cookie(
            self.settings.identity_cookie_names,
            identity_host=self.identity_host,
            max_session_age_seconds=self.settings.session_max_age_seconds,
        )

    def _identity_expiry(self, record: SessionRecord) -> datetime | None:
        return record.expiry(
            self.settings.identity_cookie_names,
            identity_host=self.identity_host,
            max_session_age_seconds=self.settings.session_max_age_seconds,
        )

    def _metadata(self, record: SessionRecord | None, *, auth_bypass: bool) -> SessionMetadata:
        identity_cookie = self._identity_cookie(record) if record is not None else None
        authenticated = auth_bypass or identity_cookie is not None
        if auth_bypass:
            instructions = "SSO bypass is enabled for offline/public-endpoint development only."
        elif not authenticated:
            instructions = SIGN_IN_FIRST
        else:
            instructions = "A valid CERN SSO session is available."
        return SessionMetadata(
            authenticated=authenticated,
            require_sso=not auth_bypass,
            auth_bypass=auth_bypass,
            state_file=self.settings.session_path.name,
            session_exists=record is not None,
            created_at=record.created_at if record else None,
            expires_at=self._identity_expiry(record) if record else None,
            identity_cookie_name=str(identity_cookie["name"]) if identity_cookie else None,
            cookie_count=len(record.cookies) if record else 0,
            instructions=instructions,
        )

    def status(self) -> SessionMetadata:
        """Return credential metadata without any cookie/token material."""
        auth_bypass = not self.settings.require_sso
        try:
            record = self.store.load()
        except SessionStoreError:
            if not auth_bypass:
                raise
            record = None
        return self._metadata(record, auth_bypass=auth_bypass)

    async def ensure_authenticated(self) -> AuthenticatedSession:
        """Guard every protected tool; callers receive an internal session context."""
        if not self.settings.require_sso:
            return AuthenticatedSession(
                record=None,
                require_sso=False,
                identity_names=self.settings.identity_cookie_names,
                identity_host=self.identity_host,
                max_session_age_seconds=self.settings.session_max_age_seconds,
                bypass=True,
            )
        record = self.store.load()
        if record is None or not record.is_authenticated(
            self.settings.identity_cookie_names,
            identity_host=self.identity_host,
            max_session_age_seconds=self.settings.session_max_age_seconds,
        ):
            raise AuthRequiredError()
        return AuthenticatedSession(
            record=record,
            require_sso=True,
            identity_names=self.settings.identity_cookie_names,
            identity_host=self.identity_host,
            max_session_age_seconds=self.settings.session_max_age_seconds,
        )

    async def sign_in(self, timeout_seconds: int | None = None) -> SignInResult:
        """Run the visible-browser login and persist its minimized cookie state."""
        timeout = self._validated_timeout(timeout_seconds)
        if not host_matches(self.identity_host, self.settings.allowed_hosts):
            raise InvalidInputError(
                "The configured SSO host is not in the request allowlist",
                instructions=(
                    f"Add {self.identity_host} to CMCPS_ALLOWED_HOSTS before calling "
                    "cern_auth_sign_in."
                ),
            )
        existing = self.store.load()
        if existing is not None and existing.is_authenticated(
            self.settings.identity_cookie_names,
            identity_host=self.identity_host,
            max_session_age_seconds=self.settings.session_max_age_seconds,
        ):
            existing_identity = self._identity_cookie(existing)
            return SignInResult(
                authenticated=True,
                state_file=self.settings.session_path.name,
                identity_cookie_name=str(existing_identity["name"]) if existing_identity else None,
                expires_at=self._identity_expiry(existing),
                cookie_count=len(existing.cookies),
                message="A valid CERN SSO session already exists; no browser login was needed.",
            )

        storage_state = await self.collector(self.settings, timeout)
        if not storage_state:
            raise InvalidSessionError("The browser did not produce a storage state")
        service_exchanges = storage_state.pop("service_tokens", {}) or {}
        minimized_state = minimize_storage_state(storage_state, self.settings.allowed_hosts)
        record = self.store.save(minimized_state)
        identity = self._identity_cookie(record)
        if identity is None:
            raise InvalidSessionError(
                "The browser finished without the configured CERN identity cookie",
                instructions="Complete the CERN SSO login and retry cern_auth_sign_in.",
            )
        captured_services = self._persist_service_tokens(service_exchanges)
        if captured_services:
            note = f" Captured Bearer tokens for: {', '.join(captured_services)}."
        else:
            note = ""
        return SignInResult(
            authenticated=True,
            state_file=self.settings.session_path.name,
            identity_cookie_name=str(identity["name"]),
            expires_at=self._identity_expiry(record),
            cookie_count=len(record.cookies),
            message="CERN SSO completed and the local session was stored." + note,
        )

    async def reauthenticate(self, timeout_seconds: int | None = None) -> SignInResult:
        """Discard the stored SSO session and captured Bearer tokens, then sign in fresh."""
        self.store.clear()
        self.service_token_store().clear_all()
        result = await self.sign_in(timeout_seconds)
        result.message += " Stored cookies and service tokens were discarded first."
        return result

    async def warm_sites(self, urls: Sequence[str]) -> bool:
        """Re-establish cookie-only site sessions and persist the refreshed state.

        Site sessions (e.g. Jira's JSESSIONID) can expire before the Keycloak
        identity does; the stored identity makes each site's silent SSO
        handshake succeed again. Best effort: returns ``False`` when SSO is
        bypassed, the session is unusable, no URL matches a configured site,
        or the warm-up failed.
        """
        if not self.settings.require_sso:
            return False
        for url in urls:
            validate_url(url, allowed_hosts=self.settings.allowed_hosts)
        record = self._authenticated_record(urls[0]) if urls else None
        if record is None:
            return False
        refreshed = await self.site_warmer(self.settings, record.storage_state, urls)
        if not refreshed:
            return False
        minimized = minimize_storage_state(refreshed, self.settings.allowed_hosts)
        self.store.save(minimized)
        return True

    async def render_page(self, url: str, *, interact: PortalInteract | None = None) -> str | None:
        """Render one SAML-protected portal page via a headless browser.

        Re-drives ``url`` against the stored Keycloak identity (the SAML form
        round-trip and ADF loopback are silent in a real browser), persists
        the refreshed cookie jar, and returns the ready page HTML. Returns
        ``None`` — leaving the store untouched — when SSO is bypassed, the
        session is missing or expired, or the page never reached content.
        """
        record = self._authenticated_record(url)
        if record is None:
            return None
        try:
            rendered = await self.page_renderer(self.settings, record.storage_state, url, interact)
        except BrowserUnavailableError:
            logger.debug("Portal render skipped; no browser available for %s", url)
            return None
        if not rendered:
            return None
        html, refreshed_state = rendered
        minimized = minimize_storage_state(refreshed_state, self.settings.allowed_hosts)
        self.store.save(minimized)
        return html

    async def extract_local_storage(self, url: str, storage_key: str) -> str | None:
        """Read one ``window.localStorage`` key after driving ``url`` through SSO.

        For SPAs whose API token is minted into browser storage by the app
        backend (not capturable as a Keycloak token exchange, e.g.
        webservices-portal). The returned value is secret and stays inside
        trusted adapter code. ``None`` when SSO is bypassed, the session is
        missing/expired, or the key never appeared.
        """
        record = self._authenticated_record(url)
        if record is None:
            return None
        try:
            extracted = await self.storage_extractor(
                self.settings, record.storage_state, url, storage_key
            )
        except BrowserUnavailableError:
            logger.debug("Local-storage extraction skipped; no browser for %s", url)
            return None
        if not extracted:
            return None
        value, refreshed_state = extracted
        minimized = minimize_storage_state(refreshed_state, self.settings.allowed_hosts)
        self.store.save(minimized)
        return value

    def _authenticated_record(self, url: str) -> SessionRecord | None:
        """Return the stored session when SSO is active, the URL allowlisted, and identity fresh."""
        if not self.settings.require_sso:
            return None
        try:
            validate_url(url, allowed_hosts=self.settings.allowed_hosts)
            record = self.store.load()
        except (PolicyViolationError, SessionStoreError):
            logger.debug("Portal render rejected for %s", url, exc_info=True)
            return None
        if record is None or not record.is_authenticated(
            self.settings.identity_cookie_names,
            identity_host=self.identity_host,
            max_session_age_seconds=self.settings.session_max_age_seconds,
        ):
            return None
        return record

    def _persist_service_tokens(self, exchanges: dict[str, Any]) -> list[str]:
        """Parse intercepted token responses and store them; best effort."""
        captured: list[str] = []
        for service, exchange in exchanges.items():
            if service not in BEARER_SERVICES or not isinstance(exchange, dict):
                continue
            try:
                record = parse_token_exchange(
                    endpoint_url=str(exchange.get("endpoint_url")),
                    post_data=str(exchange.get("post_data")),
                    response_json=exchange.get("response_json"),
                    service=service,
                )
            except (KeyError, TypeError, ValueError):
                logger.debug(
                    "Bearer-token parse failed for %s", service, extra={"service": service}
                )
                continue
            validate_url(record.token_endpoint, allowed_hosts=self.settings.allowed_hosts)
            self.service_token_store().save(record)
            captured.append(service)
        return captured

    def service_token_store(self) -> ServiceTokenStore:
        return ServiceTokenStore(self.settings.service_tokens_path)

    async def service_bearer(self, service: str) -> str | None:
        """Return a valid Bearer access token for an allowlisted service.

        Loads the captured token set, refreshes it through the stored
        Keycloak token endpoint when expired, and persists refreshes. Returns
        ``None`` when no token was captured for the service.
        """
        if service not in BEARER_SERVICES:
            raise InvalidInputError(
                f"unknown Bearer service {service!r}",
                instructions=f"Known services: {', '.join(BEARER_SERVICES)}.",
            )
        record = self.service_token_store().load(service)
        if record is None:
            return None
        if record.access_valid():
            return record.access_token
        refreshed = await refresh_record(self.settings, record)
        exchange_audience = BEARER_SERVICE_EXCHANGE_AUDIENCES.get(service)
        if exchange_audience is not None:
            refreshed = await exchange_for_audience(self.settings, refreshed, exchange_audience)
        self.service_token_store().save(refreshed)
        return refreshed.access_token

    def service_token_status(self) -> dict[str, dict[str, Any]]:
        """Secret-free metadata about captured service tokens."""
        out: dict[str, dict[str, Any]] = {}
        now = time.time()
        try:
            records = self.service_token_store().load_all()
        except SessionStoreError:
            records = {}
        for service in BEARER_SERVICES:
            record = records.get(service)
            if record is None:
                out[service] = {"captured": False}
                continue
            out[service] = {
                "captured": True,
                "access_valid": record.access_valid(now),
                "refresh_available": record.refresh_valid(now),
                "access_expires_at": record.access_expires_at,
            }
        return out
