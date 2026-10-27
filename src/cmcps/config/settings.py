"""Typed runtime configuration for CMcpS."""

from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_IDENTITY_COOKIES = ("KEYCLOAK_IDENTITY",)
DEFAULT_ALLOWED_HOSTS = (
    "cern.ch",
    "*.cern.ch",
    "inspirehep.net",
    "*.inspirehep.net",
    # Public certificate-transparency database queried by the websites adapter
    # (GET-only, no credentials sent).
    "crt.sh",
    # CERN IT Service-Now portal (service-portal search API + KB JSONv2 reads).
    "cern.service-now.com",
)
DEFAULT_SSO_START_URL = "https://indico.cern.ch/user/dashboard/"
DEFAULT_SSO_IDENTITY_HOST = "auth.cern.ch"
MIN_SSO_TIMEOUT_SECONDS = 30
MAX_SSO_TIMEOUT_SECONDS = 900


def _default_state_dir() -> Path:
    xdg_state = os.environ.get("XDG_STATE_HOME")
    root = Path(xdg_state) if xdg_state else Path.home() / ".local" / "state"
    return root / "cmcps"


def _default_browser_executable() -> str | None:
    for name in ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _default_dasgoclient() -> str | None:
    return shutil.which("dasgoclient")


class Settings(BaseSettings):
    """Environment-backed settings.

    Lists in environment variables use JSON syntax, for example
    ``CMCPS_IDENTITY_COOKIE_NAMES='["KEYCLOAK_IDENTITY"]'``.
    """

    model_config = SettingsConfigDict(
        env_prefix="CMCPS_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    state_dir: Path = Field(default_factory=_default_state_dir)
    sso_start_url: HttpUrl = HttpUrl(DEFAULT_SSO_START_URL)
    sso_identity_host: str = DEFAULT_SSO_IDENTITY_HOST
    sso_timeout_seconds: int = Field(
        default=180,
        ge=MIN_SSO_TIMEOUT_SECONDS,
        le=MAX_SSO_TIMEOUT_SECONDS,
    )
    session_max_age_seconds: int = Field(
        default=24 * 60 * 60,
        ge=60 * 60,
        le=7 * 24 * 60 * 60,
    )
    identity_cookie_names: tuple[str, ...] = DEFAULT_IDENTITY_COOKIES
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS
    browser_type: Literal["chromium", "firefox"] = "chromium"
    browser_executable: str | None = Field(default_factory=_default_browser_executable)
    require_sso: bool = True
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    max_response_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    max_download_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    max_redirects: int = Field(default=5, ge=0, le=10)
    sso_poll_seconds: float = Field(default=1.0, gt=0.1, le=10)
    server_name: str = "cmcps"
    server_version: str = "0.1.0"

    # M2 adapter endpoints and service-specific tokens. Tokens come only from
    # environment configuration and are injected by the trusted HTTP layer;
    # they are never accepted from tool arguments or logged.
    indico_base_url: str = "https://indico.cern.ch"
    indico_api_token: str | None = None
    inspire_base_url: str = "https://inspirehep.net/api"
    gitlab_base_url: str = "https://gitlab.cern.ch"
    gitlab_token: str | None = None
    cds_base_url: str = "https://cds.cern.ch"
    # M3: HLT info service (Keycloak Bearer token, operator-configured only).
    hltinfo_base_url: str = "https://cmshltinfo-dev.app.cern.ch"
    hltinfo_token: str | None = None
    # M3: GLANCE/CMSFence (cmsfence.cern.ch); the ALCM and Membership SPAs each
    # use their own Keycloak Bearer token (audiences cms-alcm-api-prod and
    # cms-membership-api-prod, captured from the app roots during sign-in).
    glance_base_url: str = "https://cmsfence.cern.ch"
    glance_alcm_app_url: str = "https://cmsfence.cern.ch/alcm"
    glance_membership_app_url: str = "https://cmsfence.cern.ch/membership"
    glance_alcm_token: str | None = None
    glance_membership_token: str | None = None
    # M4: CERN website inventory (webservices-portal.web.cern.ch). The portal's
    # SPA authenticates through a backend /authenticate route (no Keycloak
    # token POST in the browser), so the Bearer token is minted on demand by a
    # headless SSO visit that reads window.localStorage["token"]
    # (settings.websites_portal_app_url); CMCPS_WEBSITES_TOKEN overrides. The
    # second source, crt.sh certificate-transparency logs, is public.
    websites_portal_base_url: str = "https://webservices-portal.web.cern.ch"
    websites_portal_app_url: str = "https://webservices-portal.web.cern.ch/login"
    websites_token: str | None = None
    websites_ctlog_url: str = "https://crt.sh/"
    websites_cache_max_age_days: int = Field(default=30, ge=1, le=365)
    # Mattermost (mattermost.web.cern.ch, stock API v4): authenticates with the
    # MMAUTHTOKEN session cookie minted by the oauth/gitlab/login SSO handshake
    # (mm-auth.web.cern.ch brokers to auth.cern.ch; warmed lazily by the shared
    # client). Optional personal access token fallback (Authorization: Bearer).
    mattermost_base_url: str = "https://mattermost.web.cern.ch"
    mattermost_token: str | None = None
    # DAS: serviced by the dasgoclient CLI (GSI client-certificate auth), not
    # by the SSO-cookie HTTP client. cert/key default to ~/.globus.
    dasgoclient_path: str | None = Field(default_factory=_default_dasgoclient)
    grid_cert_file: Path = Field(default_factory=lambda: Path.home() / ".globus" / "usercert.pem")
    grid_key_file: Path = Field(default_factory=lambda: Path.home() / ".globus" / "userkey.pem")
    # Hosts the CMS web front-end authenticates with an X509 grid client
    # certificate over TLS (no SSO-cookie flow exists there): requests to one
    # of these hosts go through a lazy SSL context that loads the cert/key
    # above instead of carrying SSO cookies.
    grid_cert_hosts: tuple[str, ...] = ("cmsweb.cern.ch",)

    @property
    def session_path(self) -> Path:
        """Path to the browser storage-state credential file."""
        return self.state_dir / "cern-sso-storage-state.json"

    @property
    def service_tokens_path(self) -> Path:
        """Path to the captured Keycloak Bearer-token store."""
        return self.state_dir / "cern-service-tokens.json"

    @property
    def websites_cache_path(self) -> Path:
        """SQLite cache of the website inventory (FTS index + metadata)."""
        return self.state_dir / "websites-inventory.db"

    @property
    def download_dir(self) -> Path:
        """Directory used by future download adapters."""
        return self.state_dir / "downloads"

    @field_validator("identity_cookie_names", "allowed_hosts", "grid_cert_hosts", mode="before")
    @classmethod
    def _split_comma_tuple(cls, value: object) -> object:
        if isinstance(value, str) and not value.lstrip().startswith("["):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("identity_cookie_names")
    @classmethod
    def _nonempty_cookie_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("at least one identity cookie name is required")
        return value

    def ensure_directories(self) -> None:
        """Create state directories with permissions appropriate for credentials."""
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.download_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
        self.download_dir.chmod(0o700)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return process-wide settings."""
    return Settings()
