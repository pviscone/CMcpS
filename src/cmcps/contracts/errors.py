"""Domain exceptions with safe tool-facing payloads."""

from __future__ import annotations

from cmcps.contracts.results import ErrorCode, ErrorPayload


class CMCPSError(Exception):
    """Base class for expected CMcpS failures."""

    code = ErrorCode.INTERNAL_ERROR
    retryable = False

    def __init__(self, message: str, *, instructions: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.instructions = instructions

    def to_payload(self) -> ErrorPayload:
        """Return an error with cookie, token, and response bodies already excluded."""
        return ErrorPayload(
            code=self.code,
            message=self.message,
            retryable=self.retryable,
            instructions=self.instructions,
        )


class AuthRequiredError(CMCPSError):
    """A protected action attempted to run before CERN SSO completed."""

    code = ErrorCode.AUTH_REQUIRED

    def __init__(self, message: str = "A valid CERN SSO session is required") -> None:
        super().__init__(
            message,
            instructions=(
                "Call cern_auth_sign_in and complete the browser login, then retry the tool."
            ),
        )


class AuthTimeoutError(CMCPSError):
    """The browser login did not finish before its deadline."""

    code = ErrorCode.AUTH_TIMEOUT
    retryable = True


class AuthCancelledError(CMCPSError):
    """The user closed the browser before authentication completed."""

    code = ErrorCode.AUTH_CANCELLED
    retryable = True


class BrowserUnavailableError(CMCPSError):
    """Playwright could not find or start a visible browser."""

    code = ErrorCode.BROWSER_UNAVAILABLE


class InvalidInputError(CMCPSError):
    """A tool argument or runtime configuration value is outside its policy."""

    code = ErrorCode.INVALID_INPUT


class InvalidSessionError(CMCPSError):
    """The collected browser state does not contain a valid login."""

    code = ErrorCode.INVALID_SESSION
    retryable = True


class SessionStoreError(CMCPSError):
    """Local session storage could not be read or written."""

    code = ErrorCode.SESSION_STORE_ERROR


class PolicyViolationError(CMCPSError):
    """A request violated the read-only or host policy."""

    code = ErrorCode.POLICY_VIOLATION


class RequestTooLargeError(CMCPSError):
    """A response or download exceeded its configured size limit."""

    code = ErrorCode.REQUEST_TOO_LARGE


class UpstreamError(CMCPSError):
    """A backend returned an error status or an undecodable body."""

    code = ErrorCode.UPSTREAM_ERROR
    TOO_MANY_REQUESTS = 429
    SERVER_ERROR_MIN = 500

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        instructions: str | None = None,
    ) -> None:
        super().__init__(message, instructions=instructions)
        self.status_code = status_code
        self.retryable = status_code is not None and (
            status_code == self.TOO_MANY_REQUESTS or status_code >= self.SERVER_ERROR_MIN
        )
