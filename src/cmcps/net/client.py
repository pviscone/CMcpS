"""Async HTTP client constrained to read-only, allowlisted requests."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import PolicyViolationError, RequestTooLargeError
from cmcps.net.safety import redact_url, validate_headers, validate_method, validate_url

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_CHUNK_SIZE = 64 * 1024
_DOWNLOAD_MIN_ERROR = 300


@dataclass(frozen=True, slots=True)
class SafeResponse:
    """Bounded response data returned to adapters."""

    status_code: int
    url: str
    headers: dict[str, str]
    content: bytes


class SafeHttpClient:
    """GET/HEAD-only async client with SSO injection and size/redirect limits."""

    def __init__(
        self,
        settings: Settings,
        auth_service: AuthService | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.auth_service = auth_service
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(follow_redirects=False, trust_env=False)

    async def __aenter__(self) -> SafeHttpClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    async def _cookie_for(self, url: str, *, authenticated: bool) -> str | None:
        if not authenticated:
            return None
        if self.auth_service is None:
            raise PolicyViolationError("An auth service is required for authenticated requests")
        session = await self.auth_service.ensure_authenticated()
        return session.cookie_header_for(url)

    async def bearer_for(self, service: str) -> str | None:
        """Captured Keycloak Bearer token for a Bearer-only service, if any."""
        if self.auth_service is None:
            return None
        return await self.auth_service.service_bearer(service)

    async def render_page(self, url: str, *, interact: Any = None) -> str | None:
        """Render a SAML-protected portal page through the headless auth layer.

        Trusted adapters call this when the portal answers with the SAML login
        form or its ADF loopback shell instead of content; the returned HTML
        is the fully rendered page (``interact`` may first drive read-only
        portlet interactions such as a date selection). ``None`` on failure.
        """
        if self.auth_service is None:
            return None
        return await self.auth_service.render_page(url, interact=interact)

    def _next_redirect(
        self,
        response: httpx.Response,
        redirects_seen: int,
    ) -> str | None:
        if response.status_code not in _REDIRECT_STATUSES or "location" not in response.headers:
            return None
        if redirects_seen >= self.settings.max_redirects:
            raise PolicyViolationError(
                "Too many redirects",
                instructions="Check whether the endpoint requires an adapter-specific flow.",
            )
        target = urljoin(str(response.url), response.headers["location"])
        return validate_url(target, allowed_hosts=self.settings.allowed_hosts)

    async def _read_content(self, response: httpx.Response, *, method: str) -> bytes:
        content_length = response.headers.get("content-length")
        if method != "HEAD" and content_length is not None:
            try:
                length = int(content_length)
            except ValueError as exc:
                raise PolicyViolationError(
                    f"Invalid response size header from {redact_url(str(response.url))}"
                ) from exc
            if length > self.settings.max_response_bytes:
                raise RequestTooLargeError(
                    f"Response exceeds the {self.settings.max_response_bytes} byte limit"
                )
        content = bytearray()
        async for chunk in response.aiter_bytes(_CHUNK_SIZE):
            content.extend(chunk)
            if len(content) > self.settings.max_response_bytes:
                raise RequestTooLargeError(
                    f"Response exceeds the {self.settings.max_response_bytes} byte limit"
                )
        return bytes(content)

    @staticmethod
    def _safe_headers(response: httpx.Response) -> dict[str, str]:
        headers = dict(response.headers)
        headers.pop("set-cookie", None)
        if "location" in headers:
            headers["location"] = redact_url(headers["location"])
        return headers

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | int | float | bool] | None = None,
        headers: dict[str, str] | None = None,
        authenticated: bool = True,
        service_headers: dict[str, str] | None = None,
    ) -> SafeResponse:
        """Fetch a bounded response through either GET or HEAD.

        ``service_headers`` carries operator-configured credentials (for example
        an Indico ``Authorization: Bearer`` or GitLab ``PRIVATE-TOKEN`` header)
        injected by trusted adapter code from ``Settings``. Tool arguments are
        never allowed to supply them; ``headers`` still rejects credential
        names through ``validate_headers``.
        """
        safe_method = validate_method(method)
        safe_headers = validate_headers(headers)
        target = validate_url(url, allowed_hosts=self.settings.allowed_hosts)

        for redirects_seen in range(self.settings.max_redirects + 1):
            request_headers = dict(safe_headers)
            if service_headers:
                request_headers.update(service_headers)
            if cookie := await self._cookie_for(target, authenticated=authenticated):
                request_headers["Cookie"] = cookie
            try:
                async with self._client.stream(
                    safe_method,
                    target,
                    params=params if redirects_seen == 0 else None,
                    headers=request_headers,
                    timeout=self.settings.request_timeout_seconds,
                    follow_redirects=False,
                ) as response:
                    redirected = self._next_redirect(response, redirects_seen)
                    if redirected is not None:
                        target = redirected
                        continue
                    content = await self._read_content(response, method=safe_method)
                    return SafeResponse(
                        status_code=response.status_code,
                        url=redact_url(str(response.url)),
                        headers=self._safe_headers(response),
                        content=content,
                    )
            except httpx.HTTPError as exc:
                raise PolicyViolationError(
                    f"Request failed for {redact_url(target)}",
                    instructions="Check network connectivity and adapter endpoint discovery.",
                ) from exc
        raise PolicyViolationError("Redirect handling exceeded its policy bound")

    def _managed_download_path(self, destination: Path | None) -> Path:
        root = self.settings.download_dir.resolve()
        candidate = (
            root / f"download-{time.time_ns()}"
            if destination is None
            else destination
            if destination.is_absolute()
            else root / destination
        )
        final = candidate.resolve()
        if final == root or not final.is_relative_to(root):
            raise PolicyViolationError(
                "Downloads must stay inside the CMcpS download directory",
                instructions="Use a filename inside the CMcpS downloads directory.",
            )
        return final

    async def request_form_post(
        self,
        url: str,
        *,
        data: Mapping[str, str | int | float | bool],
        allowlist: tuple[str, ...] = ("cmsonline.cern.ch",),
        headers: dict[str, str] | None = None,
        authenticated: bool = True,
    ) -> SafeResponse:
        """POST an HTML form through the allowlisted portal portlet dispatch.

        CERN's CMS Online portal only honors portlet actions as HTML-form
        POSTs (read-only semantics: searches and portlet navigation never
        mutate state, like the OIDC token-refresh POST in the auth layer).
        Restrictions: HTTPS + explicit portal allowlist + form-encoded data
        only (no JSON, no file upload, no raw bodies). Everything else the
        safety layer guarantees for GET applies: URL validation, cookie
        re-scoping on redirects, size limits.
        """
        target = validate_url(url, allowed_hosts=self.settings.allowed_hosts)
        host = urlsplit(target).hostname
        if host is None or not any(
            host == allowed or host.endswith(f".{allowed}") for allowed in allowlist
        ):
            raise PolicyViolationError(f"Form POST is restricted to {list(allowlist)}, got {host}")
        for redirects_seen in range(self.settings.max_redirects + 1):
            request_headers: dict[str, str] = dict(validate_headers(headers))
            if cookie := await self._cookie_for(target, authenticated=authenticated):
                request_headers["Cookie"] = cookie
            try:
                async with self._client.stream(
                    "POST",
                    target,
                    data=data if redirects_seen == 0 else None,
                    headers=request_headers or None,
                    timeout=self.settings.request_timeout_seconds,
                    follow_redirects=False,
                ) as response:
                    content = await self._read_content(response, method="POST")
                    if redirect := self._next_redirect(response, redirects_seen):
                        target = redirect
                        continue
                    return SafeResponse(
                        status_code=response.status_code,
                        url=redact_url(str(response.url)),
                        headers=self._safe_headers(response),
                        content=content,
                    )
            except httpx.HTTPError as exc:
                if redirects_seen >= self.settings.max_redirects:
                    raise PolicyViolationError(f"Request failed for {redact_url(url)}") from exc
                raise PolicyViolationError(f"Request failed for {redact_url(url)}") from exc
        raise PolicyViolationError(f"Request failed for {redact_url(url)}")

    async def download(
        self,
        url: str,
        destination: Path | None = None,
        *,
        authenticated: bool = True,
        service_headers: dict[str, str] | None = None,
    ) -> Path:
        """Download GET content into CMcpS state storage with a strict size limit.

        Redirects are followed with per-hop cookie re-scoping, same policy as
        ``request``. ``service_headers`` carries operator-configured credentials
        (never tool arguments).
        """
        validate_method("GET")
        self.settings.ensure_directories()
        target = validate_url(url, allowed_hosts=self.settings.allowed_hosts)
        final = self._managed_download_path(destination)
        temporary = final.with_name(f".{final.name}.{time.time_ns()}.part")
        if self.auth_service is None and authenticated:
            raise PolicyViolationError("An auth service is required for authenticated downloads")
        session = await self.auth_service.ensure_authenticated() if authenticated else None
        try:
            try:
                for redirects_seen in range(self.settings.max_redirects + 1):
                    headers: dict[str, str] = dict(service_headers or {})
                    if session and (header := session.cookie_header_for(target)):
                        headers["Cookie"] = header
                    async with self._client.stream(
                        "GET",
                        target,
                        headers=headers or None,
                        timeout=self.settings.request_timeout_seconds,
                        follow_redirects=False,
                    ) as response:
                        redirected = self._next_redirect(response, redirects_seen)
                        if redirected is not None:
                            target = redirected
                            continue
                        if response.status_code >= _DOWNLOAD_MIN_ERROR:
                            raise PolicyViolationError(
                                f"Download failed with HTTP {response.status_code}"
                            )
                        descriptor = os.open(
                            temporary,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                        )
                        written = 0
                        with os.fdopen(descriptor, "wb") as handle:
                            async for chunk in response.aiter_bytes(_CHUNK_SIZE):
                                written += len(chunk)
                                if written > self.settings.max_download_bytes:
                                    raise RequestTooLargeError(
                                        "Download exceeds the "
                                        f"{self.settings.max_download_bytes} byte limit"
                                    )
                                handle.write(chunk)
                        temporary.replace(final)
                        return final
                raise PolicyViolationError("Redirect handling exceeded its policy bound")
            finally:
                if temporary.exists():
                    temporary.unlink()
        except httpx.HTTPError as exc:
            raise PolicyViolationError(f"Download failed for {redact_url(target)}") from exc
