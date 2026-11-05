"""Headless CERN SSO login using a Kerberos ticket plus a terminal-entered 2FA code.

The visible-browser login (:func:`cmsmcp.auth.browser.collect_sso_identity`)
remains the default. This flow replaces only the username/password screen:
Keycloak's ``kerberos`` identity-provider link is followed with a SPNEGO token
generated from the user's ``kinit`` ticket cache, and the 2FA one-time code is
typed into the terminal instead of the browser.

Chrome's built-in Negotiate support is not used: on stock Linux Chrome it can
fail to initialise GSSAPI (``ERR_UNSUPPORTED_AUTH_SCHEME``) even with
``--auth-server-allowlist``. Instead the SPNEGO token is generated with
``python-gssapi`` and attached to exactly one request — the Kerberos realm
authorization URL on the configured SSO identity host. No other request ever
carries it.

The Keycloak identity cookie appears *before* the 2FA step completes, so a
cookie check alone would accept a half-finished login. Success here requires
the browser to land back on the start URL's host with a valid identity cookie.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urljoin, urlsplit

from playwright.async_api import BrowserContext, Page, async_playwright
from playwright.async_api import Error as PlaywrightError

from cmsmcp.auth.browser import _close_resource, _launch_browser
from cmsmcp.auth.store import SessionRecord
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.errors import CMSMCPError, InvalidSessionError
from cmsmcp.contracts.results import ErrorCode

logger = logging.getLogger(__name__)

# SPNEGO mechanism OID (RFC 4178).
_SPNEGO_OID = "1.3.6.1.5.5.2"
_KERBEROS_LINK = 'a[href*="/broker/kerberos/login"]'
_KERBEROS_REALM_PATH = "/realms/kerberos/"
_OTP_FIELD = "#otp"
_OTP_SUBMIT = "#kc-login"
_OTP_ERROR = "#input-error-otp-code, #input-error, .pf-c-alert__title, .kc-feedback-text"
_STEP_TIMEOUT_SECONDS = 45
_POLL_SECONDS = 0.5
MAX_OTP_ATTEMPTS = 3

# Receives the previous attempt's error text (None on the first attempt) and
# returns the one-time code to submit.
OtpProvider = Callable[[str | None], Awaitable[str]]


class KerberosLoginError(CMSMCPError):
    """The Kerberos-backed login could not run or was rejected."""

    code = ErrorCode.AUTH_REQUIRED


def _gssapi() -> Any:
    try:
        import gssapi  # noqa: PLC0415 - optional extra, imported only for this flow
    except ImportError as exc:
        raise KerberosLoginError(
            "Kerberos login needs the optional python-gssapi package",
            instructions=(
                "Install it with `uv sync --extra kerberos` (building it needs the "
                "Kerberos development headers, e.g. `apt install libkrb5-dev`)."
            ),
        ) from exc
    return gssapi


def _no_ticket_error() -> KerberosLoginError:
    return KerberosLoginError(
        "No usable Kerberos ticket was found",
        instructions="Run `kinit <username>@CERN.CH` and retry.",
    )


def check_kerberos_ticket() -> None:
    """Fail fast, before any browser starts, when there is no valid TGT."""
    gssapi = _gssapi()
    try:
        credentials = gssapi.Credentials(usage="initiate")
        lifetime = credentials.lifetime
    except gssapi.exceptions.GSSError as exc:
        raise _no_ticket_error() from exc
    if lifetime is not None and lifetime <= 0:
        raise KerberosLoginError(
            "The Kerberos ticket has expired",
            instructions="Run `kinit -R` (or `kinit <username>@CERN.CH`) and retry.",
        )


def negotiate_header(host: str) -> str:
    """Return an ``Authorization: Negotiate`` value for ``HTTP@host``."""
    gssapi = _gssapi()
    try:
        context = gssapi.SecurityContext(
            name=gssapi.Name(f"HTTP@{host}", gssapi.NameType.hostbased_service),
            usage="initiate",
            mech=gssapi.OID.from_int_seq(_SPNEGO_OID),
        )
        token = context.step()
    except gssapi.exceptions.GSSError as exc:
        raise _no_ticket_error() from exc
    return "Negotiate " + base64.b64encode(token).decode("ascii")


def validate_kerberos_redirect(location: str, identity_host: str) -> str:
    """Only ever send the SPNEGO token to the configured SSO host's Kerberos realm."""
    parts = urlsplit(location)
    if (
        parts.scheme != "https"
        or parts.hostname != identity_host
        or _KERBEROS_REALM_PATH not in parts.path
    ):
        raise KerberosLoginError(
            "The CERN login page redirected Kerberos sign-in somewhere unexpected",
            instructions="Use cern_auth_sign_in (visible browser) instead.",
        )
    return location


def _strip_fragment(url: str) -> str:
    return url.split("#", 1)[0]


async def _identity_ready(settings: Settings, context: BrowserContext) -> bool:
    probe = SessionRecord(storage_state={"cookies": await context.cookies(), "origins": []})
    return probe.is_authenticated(
        settings.identity_cookie_names,
        identity_host=settings.sso_identity_host,
        max_session_age_seconds=settings.session_max_age_seconds,
    )


async def _wait_for_outcome(
    settings: Settings, context: BrowserContext, page: Page, start_host: str | None
) -> str:
    """Return ``done``, ``otp`` or ``kerberos_rejected`` once the page settles."""
    deadline = asyncio.get_running_loop().time() + _STEP_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
            parts = urlsplit(page.url)
            if parts.hostname == start_host and await _identity_ready(settings, context):
                return "done"
            if parts.hostname == settings.sso_identity_host:
                if _KERBEROS_REALM_PATH in parts.path:
                    return "kerberos_rejected"
                if await page.locator(_OTP_FIELD).count() > 0:
                    return "otp"
        except PlaywrightError:
            logger.debug("Page mid-navigation while waiting for login outcome", exc_info=True)
        await asyncio.sleep(_POLL_SECONDS)
    raise KerberosLoginError(
        "The CERN login did not reach the 2FA step or finish in time",
        instructions="Retry, or use cern_auth_sign_in (visible browser) instead.",
    )


async def _otp_error_text(page: Page) -> str:
    try:
        locator = page.locator(_OTP_ERROR)
        if await locator.count() > 0:
            text = re.sub(r"\s+", " ", await locator.first.inner_text()).strip()
            if text:
                return text
    except PlaywrightError:
        logger.debug("Could not read the 2FA error text", exc_info=True)
    return "CERN rejected the one-time code"


async def _start_kerberos(settings: Settings, context: BrowserContext, page: Page) -> None:
    """Open the login page and follow its Kerberos link with a SPNEGO token attached."""
    await page.goto(str(settings.sso_start_url), wait_until="domcontentloaded", timeout=30_000)
    link = page.locator(_KERBEROS_LINK)
    await link.first.wait_for(state="attached", timeout=30_000)
    if urlsplit(page.url).hostname != settings.sso_identity_host:
        raise KerberosLoginError(
            "The start URL did not lead to the CERN login page",
            instructions="Check CMSMCP_SSO_START_URL or use cern_auth_sign_in.",
        )
    href = await link.first.get_attribute("href")
    broker_url = urljoin(page.url, href or "")
    validate_kerberos_redirect_host(broker_url, settings.sso_identity_host)

    # The broker step answers 303 → Kerberos realm. Resolve it outside the page
    # so the realm URL is a top-level navigation the route below can match
    # exactly (Playwright does not route redirect hops).
    response = await context.request.get(broker_url, max_redirects=0)
    location = validate_kerberos_redirect(
        urljoin(broker_url, response.headers.get("location", "")),
        settings.sso_identity_host,
    )
    target = _strip_fragment(location)
    header = negotiate_header(settings.sso_identity_host)

    async def attach_token(route: Any) -> None:
        headers = await route.request.all_headers()
        headers["authorization"] = header
        await route.continue_(headers=headers)

    def matches(url: str) -> bool:
        return _strip_fragment(url) == target

    await page.route(matches, attach_token, times=1)
    try:
        await page.goto(location, wait_until="domcontentloaded", timeout=30_000)
    finally:
        await page.unroute(matches)


def validate_kerberos_redirect_host(url: str, identity_host: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != identity_host:
        raise KerberosLoginError(
            "The CERN login page's Kerberos link points somewhere unexpected",
            instructions="Use cern_auth_sign_in (visible browser) instead.",
        )


async def _complete_otp(
    settings: Settings,
    context: BrowserContext,
    page: Page,
    start_host: str | None,
    otp_provider: OtpProvider,
) -> None:
    previous_error: str | None = None
    for _ in range(MAX_OTP_ATTEMPTS):
        code = await otp_provider(previous_error)
        await page.fill(_OTP_FIELD, code)
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=30_000):
            await page.click(_OTP_SUBMIT)
        outcome = await _wait_for_outcome(settings, context, page, start_host)
        if outcome == "done":
            return
        if outcome != "otp":
            break
        previous_error = await _otp_error_text(page)
    raise InvalidSessionError(
        "CERN did not accept the one-time code",
        instructions="Run the login again with a fresh code from your authenticator app.",
    )


def make_kerberos_collector(
    otp_provider: OtpProvider,
) -> Callable[[Settings, int | None], Awaitable[dict[str, Any]]]:
    """Build an :class:`AuthService` storage-state collector for this flow."""

    async def collect(settings: Settings, _timeout_seconds: int | None) -> dict[str, Any]:
        check_kerberos_ticket()
        start_host = urlsplit(str(settings.sso_start_url)).hostname
        async with async_playwright() as playwright:
            browser = await _launch_browser(settings, playwright, headless=True)
            context: BrowserContext | None = None
            try:
                context = await browser.new_context(accept_downloads=False)
                page = await context.new_page()
                await _start_kerberos(settings, context, page)
                outcome = await _wait_for_outcome(settings, context, page, start_host)
                if outcome == "kerberos_rejected":
                    raise KerberosLoginError(
                        "CERN did not accept the Kerberos ticket",
                        instructions=(
                            "Check `klist` shows a valid CERN.CH ticket, run `kinit` "
                            "again if needed, and retry."
                        ),
                    )
                if outcome == "otp":
                    await _complete_otp(settings, context, page, start_host, otp_provider)
                return await context.storage_state()
            finally:
                await _close_resource(context, "kerberos context")
                await _close_resource(browser, "kerberos browser")

    return collect
