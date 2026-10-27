"""Stable schemas returned by CMcpS tools."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

# Hard budget for one tool result as JSON. JSON-RPC frames die at 16 MiB; an
# oversize adapter result must fail cleanly, not kill the stdio transport.
MAX_ENVELOPE_JSON_BYTES = 12 * 1024 * 1024


class ErrorCode(StrEnum):
    """Machine-readable error categories."""

    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_TIMEOUT = "AUTH_TIMEOUT"
    AUTH_CANCELLED = "AUTH_CANCELLED"
    BROWSER_UNAVAILABLE = "BROWSER_UNAVAILABLE"
    INVALID_INPUT = "INVALID_INPUT"
    INVALID_SESSION = "INVALID_SESSION"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    REQUEST_TOO_LARGE = "REQUEST_TOO_LARGE"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    SESSION_STORE_ERROR = "SESSION_STORE_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ErrorPayload(BaseModel):
    """Structured, secret-free tool error."""

    code: ErrorCode
    message: str
    retryable: bool = False
    instructions: str | None = None


class ToolEnvelope(BaseModel):
    """Uniform tool response for bootstrap and later adapter calls."""

    status: Literal["ok", "error"]
    data: dict[str, Any] | None = None
    error: ErrorPayload | None = None

    @classmethod
    def success(cls, data: BaseModel | dict[str, Any]) -> ToolEnvelope:
        payload = data.model_dump(mode="json") if isinstance(data, BaseModel) else dict(data)
        if len(json.dumps(payload, default=str)) > MAX_ENVELOPE_JSON_BYTES:
            return cls.failure(
                ErrorPayload(
                    code=ErrorCode.REQUEST_TOO_LARGE,
                    message=("the tool result exceeded the transport budget and was not returned"),
                    retryable=False,
                    instructions=(
                        "Narrow the request (filters, smaller limits); the result "
                        "did not fit one MCP frame."
                    ),
                )
            )
        return cls(status="ok", data=payload)

    @classmethod
    def failure(cls, error: ErrorPayload) -> ToolEnvelope:
        return cls(status="error", error=error)


class SearchResult(BaseModel):
    """Normalized search envelope shared by every adapter.

    ``items`` keeps the backend-native item shape; the envelope adds common
    pagination and provenance fields.
    """

    source: str
    items: list[dict[str, Any]]
    total_count: int | None = None
    page: int = 1
    size: int
    has_more: bool | None = None
    query: str | None = None
    sort: str | None = None
    note: str | None = None


class SessionMetadata(BaseModel):
    """Credential metadata that is safe to expose to clients."""

    authenticated: bool
    require_sso: bool
    auth_bypass: bool = False
    state_file: str
    session_exists: bool = False
    created_at: datetime | None = None
    expires_at: datetime | None = None
    identity_cookie_name: str | None = None
    cookie_count: int = 0
    instructions: str


class SignInResult(BaseModel):
    """Result of a browser-based CERN sign-in."""

    authenticated: bool
    state_file: str
    identity_cookie_name: str | None = None
    expires_at: datetime | None = None
    cookie_count: int
    message: str


class HealthResult(BaseModel):
    """Protected health check data."""

    server: str
    version: str
    authenticated: bool
    auth_bypass: bool
    read_only_methods: tuple[Literal["GET", "HEAD"], ...] = ("GET", "HEAD")
    max_response_bytes: int
    max_download_bytes: int
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PageFetchResult(BaseModel):
    """Visible-text render of one SSO-protected page via the headless browser."""

    url: str
    final_url: str
    title: str | None = None
    text: str
    truncated: bool = False
    marker: str | None = None
    marker_found: bool | None = None
    auth_divert: bool = False
