"""Typed runtime configuration for cmsmcp."""

from __future__ import annotations

import json
import os
import shutil
import stat
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, HttpUrl, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

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
    # GitHub REST API for cms-sw repositories (token sent only to these hosts).
    "api.github.com",
    "github.com",
    "raw.githubusercontent.com",
)
DEFAULT_SSO_START_URL = "https://indico.cern.ch/user/dashboard/"
DEFAULT_SSO_IDENTITY_HOST = "auth.cern.ch"
MIN_SSO_TIMEOUT_SECONDS = 30
MAX_SSO_TIMEOUT_SECONDS = 900


def default_config_dir() -> Path:
    """``$CMSMCP_CONFIG_DIR`` or ``$XDG_CONFIG_HOME/cmsmcp`` (``~/.config/cmsmcp``)."""
    explicit = os.environ.get("CMSMCP_CONFIG_DIR")
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "cmsmcp"


# Settings fields that hold credentials. ``cmsmcp setup`` writes each one to
# ``<config_dir>/secrets/<field>`` (mode 0600); the environment still wins.
SECRET_FIELDS = (
    "indico_api_token",
    "gitlab_token",
    "github_token",
    "jira_token",
    "mattermost_token",
    "hltinfo_token",
    "glance_alcm_token",
    "glance_membership_token",
    "phonebook_token",
    "websites_token",
)


def secrets_dir() -> Path:
    return default_config_dir() / "secrets"


def config_file() -> Path:
    """Non-secret options written by ``cmsmcp setup`` (``CMSMCP_*=value`` lines)."""
    return default_config_dir() / "cmsmcp.env"


class SecretFilesSource(PydanticBaseSettingsSource):
    """Read credential fields from private files written by ``cmsmcp setup``.

    Files readable by group/other are ignored (and reported by ``cmsmcp doctor``)
    rather than trusted.
    """

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:  # noqa: ARG002
        if field_name not in SECRET_FIELDS:
            return None, field_name, False
        path = secrets_dir() / field_name
        try:
            if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                return None, field_name, False
            value = path.read_text().strip()
        except OSError:
            return None, field_name, False
        return (value or None), field_name, False

    def __call__(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name, info in self.settings_cls.model_fields.items():
            value, key, _ = self.get_field_value(info, name)
            if value is not None:
                values[key] = value
        return values


def _default_state_dir() -> Path:
    xdg_state = os.environ.get("XDG_STATE_HOME")
    root = Path(xdg_state) if xdg_state else Path.home() / ".local" / "state"
    return root / "cmsmcp"


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
    ``CMSMCP_IDENTITY_COOKIE_NAMES='["KEYCLOAK_IDENTITY"]'``.
    """

    model_config = SettingsConfigDict(
        env_prefix="CMSMCP_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003 - see below
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Precedence: explicit arguments > environment > the fixed config file
        # written by `cmsmcp setup` > private secret files. Nothing is read from
        # the MCP client's working directory.
        return (
            init_settings,
            env_settings,
            DotEnvSettingsSource(settings_cls, env_file=config_file()),
            SecretFilesSource(settings_cls),
        )

    state_dir: Path = Field(default_factory=_default_state_dir)
    config_dir: Path = Field(default_factory=default_config_dir)

    # --- Token efficiency -------------------------------------------------
    # text: compact line-oriented results (default); json: one compact JSON line.
    output_format: Literal["text", "json"] = "text"
    # Per-result character budget; longer results are cut at a line boundary
    # with a note telling the agent how to narrow the request.
    max_result_chars: int = Field(default=16_000, ge=2_000, le=1_000_000)
    # Tool groups to expose ("all", profile names, or group prefixes such as
    # "indico,gitlab"); see cmsmcp.registry.PROFILES. Core tools are always on.
    profiles: Annotated[tuple[str, ...], NoDecode] = ("all",)
    # direct: every tool listed to the client. lazy: only cern_search plus
    # cmsmcp_find_tools / cmsmcp_describe_tool / cmsmcp_call (for clients that
    # load every tool schema up front).
    tool_mode: Literal["direct", "lazy"] = "direct"

    # --- Policy -----------------------------------------------------------
    # Personal content sources the user opted into, e.g. ["mattermost_dm"].
    personal_sources: Annotated[tuple[str, ...], NoDecode] = ()
    # Write/act tools are only registered when this is true.
    actions_enabled: bool = False
    # elicit: confirm through the MCP client (falls back to approve when the
    # client cannot elicit); approve: always out-of-band `cmsmcp approve`;
    # deny: refuse every action.
    confirm_mode: Literal["elicit", "approve", "deny"] = "elicit"
    audit_enabled: bool = True

    # --- lxplus / Kerberos -------------------------------------------------
    lxplus_host: str = "lxplus.cern.ch"
    lxplus_user: str | None = None
    ssh_executable: str | None = Field(default_factory=lambda: shutil.which("ssh"))
    ssh_timeout_seconds: float = Field(default=60.0, gt=0, le=900)
    # How long the `cmsmcp connect` SSH master stays open (OpenSSH time format).
    lxplus_persist: str = Field(default="12h", pattern=r"^\d+[smhdw]?$")

    # --- Grid / Rucio --------------------------------------------------------
    rucio_base_url: str = "https://cms-rucio.cern.ch"
    rucio_auth_url: str = "https://cms-rucio-auth.cern.ch"
    rucio_account: str | None = None

    # --- Extra personal tokens (setup wizard) --------------------------------
    github_token: str | None = None
    jira_token: str | None = None
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
    identity_cookie_names: Annotated[tuple[str, ...], NoDecode] = DEFAULT_IDENTITY_COOKIES
    allowed_hosts: Annotated[tuple[str, ...], NoDecode] = DEFAULT_ALLOWED_HOSTS
    browser_type: Literal["chromium", "firefox"] = "chromium"
    browser_executable: str | None = Field(default_factory=_default_browser_executable)
    require_sso: bool = True
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    max_response_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    max_download_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    max_redirects: int = Field(default=5, ge=0, le=10)
    sso_poll_seconds: float = Field(default=1.0, gt=0.1, le=10)
    server_name: str = "cmsmcp"
    server_version: str = "0.3.0"

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
    # directory.web.cern.ch fronts the CERN Phonebook SPA (phonebook.cern.ch);
    # its /api/* routes accept only the Keycloak public-client Bearer token of
    # client "phonebook", minted headlessly on first use from the stored SSO
    # session (see BEARER_SERVICES). CMSMCP_PHONEBOOK_TOKEN overrides.
    phonebook_app_url: str = "https://phonebook.cern.ch/"
    phonebook_token: str | None = None
    # M4: CERN website inventory (webservices-portal.web.cern.ch). The portal's
    # SPA authenticates through a backend /authenticate route (no Keycloak
    # token POST in the browser), so the Bearer token is minted on demand by a
    # headless SSO visit that reads window.localStorage["token"]
    # (settings.websites_portal_app_url); CMSMCP_WEBSITES_TOKEN overrides. The
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
    grid_cert_hosts: Annotated[tuple[str, ...], NoDecode] = (
        "cmsweb.cern.ch",
        "cms-rucio-auth.cern.ch",
    )

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

    @field_validator(
        "identity_cookie_names",
        "allowed_hosts",
        "grid_cert_hosts",
        "profiles",
        "personal_sources",
        mode="before",
    )
    @classmethod
    def _split_comma_tuple(cls, value: object) -> object:
        if isinstance(value, str):
            if value.lstrip().startswith("["):
                return tuple(json.loads(value))
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
