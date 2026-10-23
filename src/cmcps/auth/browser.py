"""Visible-browser CERN SSO collection using Playwright."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError

from cmcps.auth.store import SessionRecord
from cmcps.auth.tokens import BEARER_SERVICES
from cmcps.config.settings import Settings
from cmcps.contracts.errors import AuthCancelledError, AuthTimeoutError, BrowserUnavailableError

logger = logging.getLogger(__name__)


_CAPTURE_TIMEOUT_SECONDS = 60


async def _launch_browser(settings: Settings, playwright: Playwright, *, headless: bool) -> Browser:
    launcher = playwright.chromium if settings.browser_type == "chromium" else playwright.firefox
    kind = "headless" if headless else "visible"
    try:
        return await launcher.launch(
            headless=headless,
            executable_path=settings.browser_executable,
        )
    except PlaywrightError as exc:
        raise BrowserUnavailableError(
            f"A {kind} browser could not be started",
            instructions=(
                "Install Chromium/Firefox or run `uv run playwright install chromium`; "
                "set CMCPS_BROWSER_EXECUTABLE when the binary is not on PATH."
            ),
        ) from exc


async def _wait_for_authenticated_state(
    settings: Settings,
    context: BrowserContext,
    page: Page,
    timeout: int,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if page.is_closed():
            raise AuthCancelledError(
                "The browser was closed before CERN SSO completed",
                instructions="Call cern_auth_sign_in again and complete the login in the browser.",
            )
        cookies = await context.cookies()
        probe = SessionRecord(storage_state={"cookies": cookies, "origins": []})
        if probe.is_authenticated(
            settings.identity_cookie_names,
            identity_host=settings.sso_identity_host,
            max_session_age_seconds=settings.session_max_age_seconds,
        ):
            await asyncio.sleep(0.5)
            return await context.storage_state()
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AuthTimeoutError(
                f"CERN SSO did not complete within {timeout} seconds",
                instructions="Call cern_auth_sign_in again or increase CMCPS_SSO_TIMEOUT_SECONDS.",
            )
        await asyncio.sleep(min(settings.sso_poll_seconds, remaining))


async def _close_resource(resource: Browser | BrowserContext | None, label: str) -> None:
    if resource is None:
        return
    try:
        await resource.close()
    except PlaywrightError:
        logger.debug("Failed to close Playwright %s", label, exc_info=True)


_CAPTURE_FIELD = "response_json"


async def _capture_one_service(context: BrowserContext, app_url: str) -> dict[str, Any] | None:
    """Open one service app and intercept its Keycloak token exchange."""
    exchange: dict[str, Any] = {}
    page: Page | None = None

    async def on_response(response: Any) -> None:
        request = response.request
        if request.method != "POST" or "protocol/openid-connect/token" not in request.url:
            return
        if exchange.get(_CAPTURE_FIELD) is not None:
            return
        try:
            body = await response.json()
        except PlaywrightError:
            return
        exchange["endpoint_url"] = request.url
        exchange["post_data"] = request.post_data or ""
        exchange[_CAPTURE_FIELD] = body

    try:
        page = await context.new_page()
        page.on("response", on_response)
        deadline = asyncio.get_running_loop().time() + _CAPTURE_TIMEOUT_SECONDS
        await page.goto(
            app_url,
            wait_until="domcontentloaded",
            timeout=min(30_000, _CAPTURE_TIMEOUT_SECONDS * 1000),
        )
        while exchange.get(_CAPTURE_FIELD) is None:
            if page.is_closed() or asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.5)
    except PlaywrightError:
        logger.debug("Bearer-token capture failed for %s", app_url, exc_info=True)
        return None
    finally:
        if page is not None:
            await _close_resource(page, "bearer-capture page")
    return exchange if exchange.get(_CAPTURE_FIELD) is not None else None


async def _capture_bearer_services(
    settings: Settings,
    context: BrowserContext,
) -> dict[str, dict[str, Any]]:
    """Intercept each configured Bearer service's Keycloak token exchange.

    Runs inside the already-authenticated browser context, so each service
    SPA performs a silent OIDC flow visible to the page-level response
    listener. Failures are non-fatal — sign-in succeeds without a token.
    """
    captured: dict[str, dict[str, Any]] = {}
    for service, url_attr in BEARER_SERVICES.items():
        app_url = getattr(settings, url_attr, None)
        if not app_url:
            continue
        exchange = await _capture_one_service(context, str(app_url))
        if exchange is not None:
            captured[service] = exchange
    return captured


# SSO-protected sites that only need session cookies (no Bearer tokens).
# Both CMS Discourse forums alias the full site behind /auth/oauth2_basic, so
# they return 403 for every anonymous request; visiting them inside the
# authenticated context completes the silent OAuth2 handshake and drops
# `_forum_session`/`_t` cookies into the context. Keep in sync with the
# login-required sites in `cmcps.adapters.discourse.SITES`.
# Jira (its.cern.ch) anonymous pages render publicly; the login page
# (/jira/login.jsp) carries the auto-advance to the auth.cern.ch OIDC flow
# (client jira-central-prod) in an inline script, and the handshake drops
# JSESSIONID and a logged-in atlassian.xsrf.token (value ends with "_lin";
# "_lout" when logged out). Keep in sync with the warm-up URLs in
# `cmcps.adapters.jira._SESSION_WARM_URLS`.
JIRA_SITE_URL = "https://its.cern.ch/jira/login.jsp?os_destination=%2Fsecure%2FDashboard.jspa"
_JIRA_COOKIE_URL = "https://its.cern.ch/jira/"

SiteReadiness = Callable[[BrowserContext], Awaitable[bool]]
SiteAdvance = Callable[[Page], Awaitable[None]]


async def _jira_session_ready(context: BrowserContext) -> bool:
    """Whether the context holds a logged-in Jira session on its.cern.ch."""
    try:
        cookies = await context.cookies(_JIRA_COOKIE_URL)
    except PlaywrightError:
        return False
    for cookie in cookies:
        if cookie.get("name") == "atlassian.xsrf.token":
            return str(cookie.get("value", "")).endswith("_lin")
    return False


async def _advance_jira_sso(page: Page) -> None:
    """Move Jira's login page into its auth.cern.ch OIDC handshake.

    login.jsp carries the OIDC authorize URL in an inline script; following it
    explicitly is deterministic across headed/headless runs. The
    page-planted JSESSIONID ties the handshake to this session.
    """
    try:
        if urlsplit(page.url).hostname != "its.cern.ch":
            # The login page's own script already started the handshake.
            return
        href: str | None = await page.evaluate(
            """() => {
                const scripts = [...document.scripts].map(s => s.textContent).join(' ');
                const match = scripts.match(/https:\\/\\/auth\\.cern\\.ch[^"'\\s]+/);
                return match ? match[0] : null;
            }"""
        )
        if href is None:
            # Already advanced (or the dashboard rendered as authenticated).
            return
        await page.goto(href, wait_until="domcontentloaded", timeout=_COOKIE_CAPTURE_TIMEOUT_MS)
    except PlaywrightError:
        logger.debug("Jira SSO advance failed", exc_info=True)


async def _twiki_session_ready(context: BrowserContext) -> bool:
    """Whether the context holds any twiki.cern.ch cookie (session established).

    TWiki forces auth on the ``viewauth`` script; inside an SSO context the
    auth.cern.ch handshake completes silently and drops a session cookie
    (e.g. mod_auth_openidc/TWIKISID) needed for ``raw=on`` page source.
    """
    try:
        cookies = await context.cookies("https://twiki.cern.ch")
    except PlaywrightError:
        return False
    return bool(cookies)


async def _cds_session_ready(context: BrowserContext) -> bool:
    """Whether the context holds a logged-in CDS session on cds.cern.ch.

    cds.cern.ch gates restricted records and attachments (e.g. CMS CWR paper
    drafts) behind CERN SSO; inside an authenticated context the login page
    completes the silent OIDC handshake and drops ``mod_auth_openidc_session``
    plus Invenio session cookies.
    """
    try:
        cookies = await context.cookies("https://cds.cern.ch")
    except PlaywrightError:
        return False
    return any(cookie.get("name") == "mod_auth_openidc_session" for cookie in cookies)


async def _cms_alcm_session_ready(context: BrowserContext) -> bool:
    """Whether the context holds a session on cms-alcm.web.cern.ch.

    cms-alcm.web.cern.ch serves ALL GLANCE/ALCM document files (papers, PAS,
    analysis notes) behind CERN WebFrameworks OIDC; the silent handshake drops
    ``mod_auth_openidc_session``, which the download URLs require.
    """
    try:
        cookies = await context.cookies("https://cms-alcm.web.cern.ch")
    except PlaywrightError:
        return False
    return any(cookie.get("name") == "mod_auth_openidc_session" for cookie in cookies)


# The TWiki entry visits the viewauth script so the SSO handshake drops the
# twiki.cern.ch session cookie used by scripts that gate on login (protected
# attachment downloads); page content itself is served anonymously.
# The CDS entry is needed for restricted CDS records/files (SSO-gated); the
# cms-alcm.web.cern.ch entry for every GLANCE document file (papers, PAS,
# notes) downloaded via the ALCM "cernbox" link pattern.
_COOKIE_ONLY_SITES: tuple[tuple[str, SiteAdvance | None, SiteReadiness | None], ...] = (
    ("https://cms-talk.web.cern.ch", None, None),
    ("https://cms-pub-talk.web.cern.ch", None, None),
    (JIRA_SITE_URL, _advance_jira_sso, _jira_session_ready),
    ("https://twiki.cern.ch/twiki/bin/viewauth/CMS/WebHome", None, _twiki_session_ready),
    ("https://cds.cern.ch/youraccount/login?ln=en", None, _cds_session_ready),
    ("https://cms-alcm.web.cern.ch/", None, _cms_alcm_session_ready),
)

_COOKIE_CAPTURE_TIMEOUT_MS = 30_000
_COOKIE_READY_TIMEOUT_SECONDS = 30.0


async def _visit_cookie_site(
    context: BrowserContext,
    site_url: str,
    advance: SiteAdvance | None,
    ready: SiteReadiness | None,
) -> None:
    """Visit one cookie-only site and, when given, wait for its session to materialize."""
    page: Page | None = None
    try:
        page = await context.new_page()
        await page.goto(
            site_url,
            wait_until="load",
            timeout=_COOKIE_CAPTURE_TIMEOUT_MS,
        )
        if advance is not None:
            await advance(page)
        if ready is None:
            await asyncio.sleep(1.0)  # let the last redirects' Set-Cookie land
            return
        deadline = asyncio.get_running_loop().time() + _COOKIE_READY_TIMEOUT_SECONDS
        while not page.is_closed() and not await ready(context):
            if asyncio.get_running_loop().time() >= deadline:
                logger.debug("Cookie-readiness timed out for %s", site_url)
                break
            await asyncio.sleep(0.5)
    except PlaywrightError:
        logger.debug("Cookie capture failed for %s", site_url, exc_info=True)
    finally:
        if page is not None:
            await _close_resource(page, "cookie-capture page")


async def _capture_cookie_only_sites(context: BrowserContext) -> None:
    """Visit each cookie-only SSO site so its session cookies land in the context.

    Failures are non-fatal — sign-in succeeds without the site session and the
    affected adapter reports AUTH_REQUIRED on 401/403.
    """
    for site_url, advance, ready in _COOKIE_ONLY_SITES:
        await _visit_cookie_site(context, site_url, advance, ready)


async def warm_cookie_sites(
    settings: Settings,
    storage_state: dict[str, Any],
    urls: Sequence[str],
) -> dict[str, Any] | None:
    """Re-drive cookie-only SSO sites headlessly and return refreshed storage state.

    Used by adapters when a cookie-only site's session expired before the
    Keycloak identity did: the stored identity makes each site's silent
    handshake succeed again. Only configured ``_COOKIE_ONLY_SITES`` matching
    ``urls`` are warmed. Returns the refreshed storage state (caller
    persists), or ``None`` when there is nothing to do or the browser failed.
    """
    targets = [(url, advance, ready) for url, advance, ready in _COOKIE_ONLY_SITES if url in urls]
    if not targets:
        return None
    browser: Browser | None = None
    context: BrowserContext | None = None
    try:
        async with async_playwright() as playwright:
            browser = await _launch_browser(settings, playwright, headless=True)
            context = await browser.new_context(storage_state=storage_state, accept_downloads=False)
            for site_url, advance, ready in targets:
                await _visit_cookie_site(context, site_url, advance, ready)
            return await context.storage_state()
    except (PlaywrightError, BrowserUnavailableError):
        logger.debug("Cookie-site warm-up failed for %s", list(urls), exc_info=True)
        return None
    finally:
        await _close_resource(context, "warm-up context")
        await _close_resource(browser, "warm-up browser")


async def collect_sso_state(
    settings: Settings,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Open a browser and wait until a configured CERN identity cookie exists.

    The returned dictionary is Playwright storage state. It contains credentials and must
    only be passed to :class:`cmcps.auth.store.SessionStore`.
    """
    timeout = timeout_seconds if timeout_seconds is not None else settings.sso_timeout_seconds
    browser: Browser | None = None
    context: BrowserContext | None = None

    try:
        async with async_playwright() as playwright:
            browser = await _launch_browser(settings, playwright, headless=False)
            context = await browser.new_context(accept_downloads=False)
            page = await context.new_page()
            await page.goto(
                str(settings.sso_start_url),
                wait_until="domcontentloaded",
                timeout=min(30_000, timeout * 1000),
            )
            await _wait_for_authenticated_state(settings, context, page, timeout)
            capture = await _capture_bearer_services(settings, context)
            await _capture_cookie_only_sites(context)
            # Re-capture after the service visits so Bearer-app and cookie-only
            # session cookies are persisted too.
            storage_state = await context.storage_state()
            storage_state["service_tokens"] = capture
            return storage_state
    finally:
        await _close_resource(context, "context")
        await _close_resource(browser, "browser")


_REFRESH_TIMEOUT_SECONDS = 60
_SAML_FORM_MARKER = "SAMLRequest"
_ADF_LOOPBACK_MARKER = "runLoopback"

PortalInteract = Callable[[Any], Awaitable[bool]]


def _portal_ready(content: str) -> bool:
    """The portal serves content only after SAML and the ADF loopback settle."""
    return _SAML_FORM_MARKER not in content and _ADF_LOOPBACK_MARKER not in content


async def _page_ready(page: Page, target_host: str | None) -> bool:
    if urlsplit(page.url).hostname != target_host:
        return False
    try:
        await page.wait_for_load_state("load", timeout=5_000)
        content = await page.content()
    except PlaywrightError:
        # Transient failure while the framework is mid-navigation.
        return False
    return _portal_ready(content)


async def _wait_page_ready(
    settings: Settings, page: Page, target_host: str | None, deadline: float
) -> bool:
    while True:
        if page.is_closed():
            return False
        if await _page_ready(page, target_host):
            return True
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(settings.sso_poll_seconds, remaining))


async def render_page_html(
    settings: Settings,
    storage_state: dict[str, Any],
    url: str,
    interact: PortalInteract | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Render one SAML-protected portal page headlessly and return its HTML.

    The stored Keycloak identity makes the SAML form round-trip silent; the
    loop then waits through the ADF loopback shell until real content arrives.
    ``interact`` is an optional async hook that drives a read-only portlet
    interaction (such as a date selection) on the live page and returns
    whether it applied; the final readiness poll runs after it. Returns
    ``(html, refreshed_storage_state)`` or ``None`` when the page never
    reached ready content.
    """
    target_host = urlsplit(url).hostname
    deadline = asyncio.get_running_loop().time() + _REFRESH_TIMEOUT_SECONDS
    browser: Browser | None = None
    context: BrowserContext | None = None
    try:
        async with async_playwright() as playwright:
            browser = await _launch_browser(settings, playwright, headless=True)
            context = await browser.new_context(storage_state=storage_state, accept_downloads=False)
            page = await context.new_page()
            try:
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=min(30_000, _REFRESH_TIMEOUT_SECONDS * 1000),
                )
            except PlaywrightError:
                logger.debug("Portal page load failed during render for %s", url)
                return None
            if not await _wait_page_ready(settings, page, target_host, deadline):
                logger.debug("Portal render timed out before ready for %s", url)
                return None
            if interact is not None:
                try:
                    applied = await interact(page)
                except PlaywrightError:
                    logger.debug("Portal interaction failed for %s", url)
                    return None
                if not applied or not await _wait_page_ready(settings, page, target_host, deadline):
                    return None
            content = await page.content()
            if not _portal_ready(content):
                return None
            return content, await context.storage_state()
    finally:
        await _close_resource(context, "context")
        await _close_resource(browser, "browser")
