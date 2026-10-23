"""Read-only request and credential-safe URL policies."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from cmcps.contracts.errors import PolicyViolationError

SAFE_METHODS = frozenset({"GET", "HEAD"})
SENSITIVE_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})
SENSITIVE_QUERY_PARTS = (
    "access_token",
    "acs",
    "api_key",
    "apikey",
    "assertion",
    "auth",
    "client_secret",
    "code",
    "credential",
    "idp",
    "jwt",
    "key",
    "password",
    "private_key",
    "saml",
    "secret",
    "session",
    "sig",
    "signature",
    "ticket",
    "token",
)


def host_matches(host: str, patterns: Iterable[str]) -> bool:
    """Match exact hosts or exact ``*.example.com`` subdomains."""
    normalized = host.rstrip(".").lower()
    for raw in patterns:
        pattern = raw.rstrip(".").lower()
        if pattern.startswith("*."):
            suffix = pattern[2:]
            if normalized == suffix or normalized.endswith(f".{suffix}"):
                return True
        elif normalized == pattern:
            return True
    return False


def validate_url(url: str, *, allowed_hosts: Iterable[str], require_https: bool = True) -> str:
    """Validate an absolute HTTP(S) URL and return the normalized URL."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise PolicyViolationError("Only absolute HTTP and HTTPS URLs are allowed")
    if require_https and parsed.scheme != "https":
        raise PolicyViolationError("Plain HTTP URLs are not allowed")
    if parsed.username or parsed.password:
        raise PolicyViolationError("Credentials in URLs are not allowed")
    if not parsed.hostname or not host_matches(parsed.hostname, allowed_hosts):
        raise PolicyViolationError(
            f"Host is not in the configured allowlist: {parsed.hostname or 'unknown'}"
        )
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))


def validate_method(method: str, *, body: object | None = None) -> str:
    """Require a read-only method and an empty request body."""
    normalized = method.upper()
    if normalized not in SAFE_METHODS:
        raise PolicyViolationError(
            f"Only read-only methods are allowed: {', '.join(sorted(SAFE_METHODS))}"
        )
    if body is not None:
        raise PolicyViolationError("Request bodies are not allowed")
    return normalized


def validate_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Reject client-supplied credential headers and return a plain header mapping."""
    validated = dict(headers or {})
    provided = {name.lower() for name in validated}
    leaked = sorted(provided & SENSITIVE_HEADERS)
    if leaked:
        raise PolicyViolationError(
            f"Credential headers must be supplied by the auth service, not callers: {leaked}"
        )
    return {name: value for name, value in validated.items() if value is not None}


def redact_url(url: str) -> str:
    """Return a URL with likely secret query values replaced."""
    parsed = urlsplit(url)
    pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        redacted = "<redacted>" if any(part in lowered for part in SENSITIVE_QUERY_PARTS) else value
        pairs.append((key, redacted))
    netloc = parsed.netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", maxsplit=1)[1]
    return urlunsplit((parsed.scheme, netloc, parsed.path, urlencode(pairs), ""))
