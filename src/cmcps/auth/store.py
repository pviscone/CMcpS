"""Owner-only local persistence for minimized Playwright cookie state."""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from cmcps.contracts.errors import SessionStoreError
from cmcps.net.safety import host_matches

SESSION_SCHEMA_VERSION = 1
SESSION_COOKIE_EXPIRES = -1.0


def _cookie_domain_matches(cookie_domain: str, hostname: str) -> bool:
    """Match Playwright's host-only/domain-cookie distinction."""
    candidate = cookie_domain.lower()
    host = hostname.lower()
    if candidate.startswith("."):
        suffix = candidate[1:]
        return host == suffix or host.endswith(f".{suffix}")
    return host == candidate


def _identity_domain_matches(cookie_domain: str, identity_host: str) -> bool:
    """Require exact IdP host ownership; parent domains are not identity proof."""
    return cookie_domain.lower().lstrip(".") == identity_host.lower()


def _path_matches(cookie_path: str, request_path: str) -> bool:
    """Apply RFC 6265 path matching, including the segment boundary."""
    cookie = cookie_path or "/"
    request = request_path or "/"
    if request == cookie:
        return True
    if not request.startswith(cookie):
        return False
    return cookie.endswith("/") or request[len(cookie)] == "/"


def _cookie_expires(cookie: dict[str, Any]) -> float:
    value = cookie.get("expires", SESSION_COOKIE_EXPIRES)
    if value is None:
        return SESSION_COOKIE_EXPIRES
    return float(value)


def minimize_storage_state(
    storage_state: dict[str, Any],
    allowed_hosts: tuple[str, ...],
) -> dict[str, Any]:
    """Keep only allowlisted cookies; never persist localStorage/origins."""
    cookies = []
    for cookie in storage_state.get("cookies", []):
        domain = str(cookie.get("domain", "")).lower().lstrip(".")
        if domain and host_matches(domain, allowed_hosts):
            cookies.append(cookie)
    return {"cookies": cookies, "origins": []}


class SessionRecord(BaseModel):
    """Stored cookie state plus safe bookkeeping metadata."""

    schema_version: int = SESSION_SCHEMA_VERSION
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    storage_state: dict[str, Any] = Field(repr=False)

    @property
    def cookies(self) -> list[dict[str, Any]]:
        cookies = self.storage_state.get("cookies", [])
        return cookies if isinstance(cookies, list) else []

    def _session_cookie_too_old(self, now: float, max_age_seconds: int) -> bool:
        return now > self.created_at.timestamp() + max_age_seconds

    def identity_cookie(
        self,
        names: tuple[str, ...],
        *,
        identity_host: str,
        max_session_age_seconds: int,
        now_epoch: float | None = None,
    ) -> dict[str, Any] | None:
        """Return the configured SSO identity cookie that is fresh enough to use."""
        now = time.time() if now_epoch is None else now_epoch
        name_order = {name: index for index, name in enumerate(names)}
        candidates = []
        for cookie in self.cookies:
            name = cookie.get("name")
            if name not in name_order:
                continue
            domain = str(cookie.get("domain", ""))
            if not _identity_domain_matches(domain, identity_host):
                continue
            if cookie.get("secure") is not True or cookie.get("httpOnly") is not True:
                continue
            expires = _cookie_expires(cookie)
            if expires >= 0 and expires <= now + 30:
                continue
            if expires < 0 and self._session_cookie_too_old(now, max_session_age_seconds):
                continue
            candidates.append(cookie)
        return min(
            candidates,
            key=lambda item: name_order[str(item.get("name"))],
            default=None,
        )

    def expiry(
        self,
        names: tuple[str, ...],
        *,
        identity_host: str,
        max_session_age_seconds: int,
    ) -> datetime | None:
        """Return the known expiry of the relevant identity cookie."""
        cookie = self.identity_cookie(
            names,
            identity_host=identity_host,
            max_session_age_seconds=max_session_age_seconds,
        )
        if cookie is None:
            return None
        expires = _cookie_expires(cookie)
        if expires < 0:
            return None
        return datetime.fromtimestamp(expires, UTC)

    def is_authenticated(
        self,
        names: tuple[str, ...],
        *,
        identity_host: str,
        max_session_age_seconds: int,
    ) -> bool:
        """Whether a configured identity cookie exists and remains usable."""
        return (
            self.identity_cookie(
                names,
                identity_host=identity_host,
                max_session_age_seconds=max_session_age_seconds,
            )
            is not None
        )

    def cookie_header_for(self, url: str) -> str | None:
        """Build a destination-scoped cookie header for an allowlisted URL."""
        parsed = urlsplit(url)
        if not parsed.hostname or parsed.scheme not in {"http", "https"}:
            return None
        now = time.time()
        selected = []
        for cookie in self.cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            domain = cookie.get("domain")
            if not name or value is None or not domain:
                continue
            expires = _cookie_expires(cookie)
            if expires >= 0 and expires <= now:
                continue
            if cookie.get("secure") and parsed.scheme != "https":
                continue
            if not _cookie_domain_matches(str(domain), parsed.hostname):
                continue
            if not _path_matches(str(cookie.get("path") or "/"), parsed.path):
                continue
            selected.append((len(str(cookie.get("path") or "/")), str(name), str(value)))
        if not selected:
            return None
        selected.sort(key=lambda item: item[0], reverse=True)
        return "; ".join(f"{name}={value}" for _, name, value in selected)


class SessionStore:
    """Atomic credential store with restrictive filesystem permissions."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> SessionRecord | None:
        """Load a session, returning ``None`` when none has been stored."""
        try:
            if not self.path.exists():
                return None
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            record = SessionRecord.model_validate(payload)
        except (OSError, ValueError, TypeError) as exc:
            raise SessionStoreError(
                "The CERN SSO session store could not be read",
                instructions="Remove the corrupt state file and call cern_auth_sign_in again.",
            ) from exc
        if record.schema_version != SESSION_SCHEMA_VERSION:
            raise SessionStoreError(
                "The CERN SSO session store uses an unsupported schema version",
                instructions="Remove the old state file and call cern_auth_sign_in again.",
            )
        return record

    def save(self, storage_state: dict[str, Any]) -> SessionRecord:
        """Atomically save browser storage state with mode 0600."""
        record = SessionRecord(storage_state=storage_state)
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.path.parent, 0o700)
            fd, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
                text=True,
            )
            temporary_path = Path(temporary)
            try:
                os.chmod(temporary, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(record.model_dump_json(indent=2))
                    handle.write("\n")
                os.replace(temporary_path, self.path)
                os.chmod(self.path, 0o600)
            finally:
                if temporary_path.exists():
                    temporary_path.unlink()
        except OSError as exc:
            raise SessionStoreError(
                "The CERN SSO session store could not be written",
                instructions="Check that CMCPS_STATE_DIR is writable and not world-readable.",
            ) from exc
        return record

    def clear(self) -> None:
        """Remove local credentials without touching other state files."""
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise SessionStoreError("The CERN SSO session store could not be removed") from exc
