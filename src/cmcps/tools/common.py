"""Shared helpers for adapter tool registration modules."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from cmcps.auth.service import AuthService
from cmcps.contracts.errors import CMCPSError
from cmcps.contracts.results import ErrorCode, ErrorPayload, ToolEnvelope


def failure(exc: Exception) -> ToolEnvelope:
    """Map an exception to a secret-free tool failure envelope."""
    if isinstance(exc, CMCPSError):
        return ToolEnvelope.failure(exc.to_payload())
    return ToolEnvelope.failure(
        ErrorPayload(
            code=ErrorCode.INTERNAL_ERROR,
            message="The operation failed without exposing internal details",
            retryable=False,
        )
    )


async def guarded(
    auth: AuthService,
    call: Callable[[], Awaitable[BaseModel | dict[str, Any]]],
) -> ToolEnvelope:
    """Run an adapter call behind the SSO guard with a uniform envelope."""
    try:
        await auth.ensure_authenticated()
        return ToolEnvelope.success(await call())
    except Exception as exc:
        return failure(exc)
