"""Async HTTP client constrained to read-only, allowlisted requests."""

from __future__ import annotations

import hashlib
import os
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from cmcps.auth.browser import cookie_site_url_for_host
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import PolicyViolationError, RequestTooLargeError
from cmcps.net.safety import redact_url, validate_headers, validate_method, validate_url

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_CHUNK_SIZE = 64 * 1024
_DOWNLOAD_MIN_ERROR = 300
_WARM_RETRY_STATUSES = {401, 403}
_DUPLICATE_SLOT_LIMIT = 100


def _sha256(path: Path) -> str:
    """Streaming SHA-256 of one file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
        # System CA store (not certifi): some CERN hosts, e.g. cms-docdb.cern.ch,
        # serve host certificates issued by the CERN Grid Certification
        # Authority, which is absent from the bundled certifi roots.
        self._client = client or httpx.AsyncClient(
            follow_redirects=False, trust_env=False, verify=ssl.create_default_context()
        )
        self._grid_client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> SafeHttpClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()
        if self._grid_client is not None:
            await self._grid_client.aclose()

    def _client_for(self, url: str) -> httpx.AsyncClient:
        """Pick the transport for ``url``: the grid-cert client for its hosts, else default.

        The CMS web front-end (cmsweb.cern.ch) has no SSO-cookie login flow:
        it authenticates X509 grid client certificates over TLS. Requests to a
        host in ``settings.grid_cert_hosts`` use a lazily built SSL context
        that loads the configured cert/key. A test-injected client always
        wins, so no real credential ever touches a mock transport.
        """
        if not self._owned_client:
            return self._client
        host = urlsplit(url).hostname
        if host is None or not any(
            host == allowed or host.endswith(f".{allowed}")
            for allowed in self.settings.grid_cert_hosts
        ):
            return self._client
        if self._grid_client is None:
            cert = self.settings.grid_cert_file
            key = self.settings.grid_key_file
            if not cert.is_file() or not key.is_file():
                raise PolicyViolationError(
                    f"Grid client certificate files are missing ({cert}, {key})",
                    instructions=(
                        "Point CMCPS_GRID_CERT_FILE/CMCPS_GRID_KEY_FILE at a valid "
                        "grid credential (e.g. ~/.globus/usercert.pem/userkey.pem or a "
                        "voms-proxy-init proxy)."
                    ),
                )
            context = ssl.create_default_context()
            try:
                context.load_cert_chain(str(cert), str(key))
            except (ssl.SSLError, OSError, ValueError) as exc:
                raise PolicyViolationError(
                    f"Grid client certificate could not be loaded ({cert}, {key})",
                    instructions=(
                        "The key may be passphrase-protected: provide an unencrypted "
                        "key or a voms-proxy-init proxy via "
                        "CMCPS_GRID_CERT_FILE/CMCPS_GRID_KEY_FILE."
                    ),
                ) from exc
            self._grid_client = httpx.AsyncClient(
                follow_redirects=False, trust_env=False, verify=context
            )
        return self._grid_client

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

    async def warm_sites(self, urls: list[str] | tuple[str, ...]) -> bool:
        """Re-establish cookie-only SSO sessions for the given sites, if possible."""
        if self.auth_service is None:
            return False
        return await self.auth_service.warm_sites(list(urls))

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

    async def browser_local_storage(self, url: str, storage_key: str) -> str | None:
        """Read one localStorage key from a page driven through SSO headlessly.

        For app-backend-minted tokens (e.g. the websites portal SPA). Returns
        ``None`` when no auth service, no session, or the key never appeared.
        """
        if self.auth_service is None:
            return None
        return await self.auth_service.extract_local_storage(url, storage_key)

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
        """Fetch a bounded response, warming the site's SSO session on 401/403.

        Lazy cookie mint: when a mapped cookie-only site answers 401/403, its
        session is re-established headlessly from the stored identity and the
        request is retried once — adapters do not warm sites themselves.
        """
        response = await self._request(
            method,
            url,
            params=params,
            headers=headers,
            authenticated=authenticated,
            service_headers=service_headers,
        )
        if response.status_code in _WARM_RETRY_STATUSES and await self._warm_for_host(
            url, authenticated=authenticated
        ):
            response = await self._request(
                method,
                url,
                params=params,
                headers=headers,
                authenticated=authenticated,
                service_headers=service_headers,
            )
        return response

    async def _warm_for_host(self, url: str, *, authenticated: bool) -> bool:
        """Headlessly re-establish the cookie session for ``url``'s host, if mapped."""
        if not authenticated or self.auth_service is None:
            return False
        hostname = urlsplit(url).hostname
        if hostname is None:
            return False
        site_url = cookie_site_url_for_host(hostname)
        if site_url is None:
            return False
        return await self.auth_service.warm_sites([site_url])

    async def _request(
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
                async with self._client_for(target).stream(
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

    @staticmethod
    def _deduped_download_path(final: Path, temporary: Path) -> Path:
        """Resolve name collisions by content: reuse identical files, suffix distinct ones.

        ``final`` holds the server's name for the payload now sitting in
        ``temporary``. If a file already occupies that name, compare SHA-256:
        identical content means the download is a no-op (the existing path is
        returned); different content lands at ``name-1.ext``/``name-2.ext``/…,
        reusing any suffix slot that already stores identical content. This
        way nothing is ever silently overwritten.
        """
        if not final.exists():
            return final
        downloaded = _sha256(temporary)
        for index in range(_DUPLICATE_SLOT_LIMIT):
            candidate = (
                final if index == 0 else final.with_name(f"{final.stem}-{index}{final.suffix}")
            )
            if not candidate.exists():
                return candidate
            if _sha256(candidate) == downloaded:
                return candidate
        raise PolicyViolationError(
            f"Too many distinct downloads named {final.name}",
            instructions="Clean up the CMcpS downloads directory.",
        )

    def _place_download(self, final: Path, temporary: Path) -> Path:
        """Install one completed temp download with content-based dedup."""
        resolved = self._deduped_download_path(final, temporary)
        if resolved.exists():
            # Identical content already stored there: absorb.
            temporary.unlink()
        else:
            temporary.replace(resolved)
        return resolved

    async def _post(
        self,
        url: str,
        *,
        form: Mapping[str, str | int | float | bool] | None,
        json_body: Mapping[str, Any] | None,
        allowlist: tuple[str, ...],
        headers: dict[str, str] | None,
        authenticated: bool,
        service_headers: dict[str, str] | None = None,
    ) -> SafeResponse:
        """Shared POST core behind the explicit read-only allowlist gates."""
        target = validate_url(url, allowed_hosts=self.settings.allowed_hosts)
        host = urlsplit(target).hostname
        if host is None or not any(
            host == allowed or host.endswith(f".{allowed}") for allowed in allowlist
        ):
            raise PolicyViolationError(f"POST is restricted to {list(allowlist)}, got {host}")
        for redirects_seen in range(self.settings.max_redirects + 1):
            request_headers: dict[str, str] = dict(validate_headers(headers))
            if service_headers:
                request_headers.update(service_headers)
            if cookie := await self._cookie_for(target, authenticated=authenticated):
                request_headers["Cookie"] = cookie
            try:
                async with self._client.stream(
                    "POST",
                    target,
                    data=form if redirects_seen == 0 else None,
                    json=json_body if redirects_seen == 0 else None,
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
                raise PolicyViolationError(f"Request failed for {redact_url(url)}") from exc
        raise PolicyViolationError(f"Request failed for {redact_url(url)}")

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
        return await self._post(
            url,
            form=data,
            json_body=None,
            allowlist=allowlist,
            headers=headers,
            authenticated=authenticated,
        )

    async def request_json_post(
        self,
        url: str,
        *,
        json_body: Mapping[str, Any],
        allowlist: tuple[str, ...],
        headers: dict[str, str] | None = None,
        authenticated: bool = True,
        service_headers: dict[str, str] | None = None,
    ) -> SafeResponse:
        """POST one JSON body to an allowlisted read-only query endpoint.

        Some CERN services only expose their search backend as a JSON POST
        (the CERN Service-Now Service-Portal ``/api/now/sp/search`` has no GET
        equivalent, verified: GET answers 405; the Mattermost API v4 post
        search likewise). Semantics stay read-only: the call issues a query
        and never mutates state, like the CMS Online form POST and the
        auth-layer OIDC token-refresh POST. Restrictions: HTTPS + explicit
        endpoint allowlist + one small JSON object body (no form data, no
        file upload, no streaming). Everything else the safety layer
        guarantees for GET applies: URL validation, cookie re-scoping on
        redirects, size limits, and one headless warm-retry when a mapped
        cookie-only site answers 401/403.
        """
        response = await self._post(
            url,
            form=None,
            json_body=json_body,
            allowlist=allowlist,
            headers=headers,
            authenticated=authenticated,
            service_headers=service_headers,
        )
        if response.status_code in _WARM_RETRY_STATUSES and await self._warm_for_host(
            url, authenticated=authenticated
        ):
            response = await self._post(
                url,
                form=None,
                json_body=json_body,
                allowlist=allowlist,
                headers=headers,
                authenticated=authenticated,
                service_headers=service_headers,
            )
        return response

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
        (never tool arguments). Name collisions are resolved by content: an
        existing file with identical SHA-256 makes the download a no-op
        (its path is returned); a genuinely different payload with the same
        server-supplied name lands at ``name-1.ext``/``name-2.ext``/… —
        bytes already stored are never silently overwritten.
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
                    async with self._client_for(target).stream(
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
                        return self._place_download(final, temporary)
                raise PolicyViolationError("Redirect handling exceeded its policy bound")
            finally:
                if temporary.exists():
                    temporary.unlink()
        except httpx.HTTPError as exc:
            raise PolicyViolationError(f"Download failed for {redact_url(target)}") from exc
