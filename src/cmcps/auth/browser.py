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


async def capture_bearer_service(
    settings: Settings,
    storage_state: dict[str, Any],
    service: str,
) -> dict[str, Any] | None:
    """Mint one Bearer service's token set headlessly from the stored identity.

    Lazy counterpart of the old eager capture: the first time an adapter
    needs a service, its app root is re-opened headlessly, performs its
    silent OIDC/PKCE flow against the stored Keycloak identity, and the
    token-endpoint exchange is intercepted. Returns the raw captured
    exchange, or ``None`` when the service is unknown/unconfigured or no
    exchange was intercepted. ``BrowserUnavailableError`` propagates so the
    caller can distinguish "no browser" from "no exchange".
    """
    url_attr = BEARER_SERVICES.get(service)
    if url_attr is None:
        return None
    app_url = getattr(settings, url_attr, None)
    if not app_url:
        return None
    browser: Browser | None = None
    context: BrowserContext | None = None
    try:
        async with async_playwright() as playwright:
            browser = await _launch_browser(settings, playwright, headless=True)
            context = await browser.new_context(storage_state=storage_state, accept_downloads=False)
            return await _capture_one_service(context, str(app_url))
    except PlaywrightError:
        logger.debug("Lazy Bearer capture failed for %s", service, exc_info=True)
        return None
    finally:
        await _close_resource(context, f"lazy-capture context {service}")
        await _close_resource(browser, f"lazy-capture browser {service}")


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

# cern.service-now.com: a protected classic page (kb_search.do) redirects
# through saml_redirector.do into the silent CERN SAML handshake, dropping the
# ServiceNow session cookies (JSESSIONID, glide_session_store) that back the
# Service-Portal search API and the legacy JSONv2 read endpoints. Keep in sync
# with `cmcps.adapters.servicenow.SESSION_WARM_URLS`.
SERVICENOW_SITE_URL = "https://cern.service-now.com/service-portal/kb_search.do"
_SERVICENOW_COOKIE_URL = "https://cern.service-now.com"

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


async def _servicenow_session_ready(context: BrowserContext) -> bool:
    """Whether the context holds a cern.service-now.com session.

    Verified cookie names from the live SAML handshake: ``JSESSIONID`` plus
    the ``glide_session_store`` family.
    """
    try:
        cookies = await context.cookies(_SERVICENOW_COOKIE_URL)
    except PlaywrightError:
        return False
    names = {cookie.get("name") for cookie in cookies}
    return bool(names & {"JSESSIONID", "glide_session_store"})


async def _cmsoms_session_ready(context: BrowserContext) -> bool:
    """Whether the context holds a session on cmsoms.cern.ch.

    The CMS OMS API (``/agg/api/...``) gates on WebFrameworks OIDC and drops
    ``mod_auth_openidc_session`` during the silent handshake at the UI root
    (it lands on ``/cms/run_3/index``). Verified 2026-10: the API answers 401
    until this cookie exists.
    """
    try:
        cookies = await context.cookies("https://cmsoms.cern.ch")
    except PlaywrightError:
        return False
    return any(cookie.get("name") == "mod_auth_openidc_session" for cookie in cookies)


async def _mattermost_session_ready(context: BrowserContext) -> bool:
    """Whether the context holds a logged-in Mattermost session.

    CERN Mattermost (mattermost.web.cern.ch) logs in through its
    ``/oauth/gitlab/login`` route, which chains mm-auth.web.cern.ch →
    auth.cern.ch; inside an SSO context that handshake is silent and drops
    the ``MMAUTHTOKEN`` session cookie the API v4 accepts.
    """
    try:
        cookies = await context.cookies("https://mattermost.web.cern.ch")
    except PlaywrightError:
        return False
    return any(cookie.get("name") == "MMAUTHTOKEN" for cookie in cookies)


# The TWiki entry visits the viewauth script so the SSO handshake drops the
# twiki.cern.ch session cookie used by scripts that gate on login (protected
# attachment downloads); page content itself is served anonymously.
# The CDS entry is needed for restricted CDS records/files (SSO-gated); the
# cms-alcm.web.cern.ch entry for every GLANCE document file (papers, PAS,
# notes) downloaded via the ALCM "cernbox" link pattern.
# The CMS OMS entry is needed for the cmsonline_* run/fill/lumi tools: the
# OMS API rejects requests without its own mod_auth_openidc session.
async def _runregistry_session_ready(context: BrowserContext) -> bool:
    """Whether the context holds a session on cmsrunregistry.web.cern.ch.

    The CMS Run Registry (UI and ``/api/...``) sits behind an oauth2-proxy
    gate (``client_id=cmsrunregistry-sso-proxy``, ``/oauth2/callback``);
    inside an SSO context the handshake is silent and drops the chunked
    ``_oauth2_proxy_<n>`` session cookies the API accepts. Verified 2026-10:
    a stored set answers the runregistry_* tools' GET/POST APIs directly.
    """
    try:
        cookies = await context.cookies("https://cmsrunregistry.web.cern.ch")
    except PlaywrightError:
        return False
    return any(cookie.get("name", "").startswith("_oauth2_proxy") for cookie in cookies)


# The Mattermost entry starts at its oauth/gitlab/login route (the web-app
# login button target): unauthenticated visitors of / only get an SPA login
# page with a click-through button, while the OAuth route walks straight
# through mm-auth.web.cern.ch → auth.cern.ch and drops MMAUTHTOKEN.
# The Run Registry entry plants the chunked _oauth2_proxy_<n> cookies so the
# runregistry_* tools' JSON-POST queries reach the API without re-walking
# the oauth2-proxy redirect handshake per call.
_COOKIE_ONLY_SITES: tuple[tuple[str, SiteAdvance | None, SiteReadiness | None], ...] = (
    ("https://cms-talk.web.cern.ch", None, None),
    ("https://cms-pub-talk.web.cern.ch", None, None),
    (JIRA_SITE_URL, _advance_jira_sso, _jira_session_ready),
    ("https://twiki.cern.ch/twiki/bin/viewauth/CMS/WebHome", None, _twiki_session_ready),
    ("https://cds.cern.ch/youraccount/login?ln=en", None, _cds_session_ready),
    ("https://cms-alcm.web.cern.ch/", None, _cms_alcm_session_ready),
    (SERVICENOW_SITE_URL, None, _servicenow_session_ready),
    ("https://cmsoms.cern.ch/", None, _cmsoms_session_ready),
    ("https://cmsrunregistry.web.cern.ch/", None, _runregistry_session_ready),
    (
        "https://mattermost.web.cern.ch/oauth/gitlab/login",
        None,
        _mattermost_session_ready,
    ),
)


def cookie_site_url_for_host(hostname: str) -> str | None:
    """Return the configured cookie-site URL that warms ``hostname``, if any.

    Reverse lookup used by the shared HTTP client: a 401/403 from one of
    these hosts means the site's session expired; warm it headlessly (lazy
    cookie mint) and retry once.
    """
    for site_url, _, _ in _COOKIE_ONLY_SITES:
        if urlsplit(site_url).hostname == hostname:
            return site_url
    return None


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


async def collect_sso_identity(
    settings: Settings,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Open a visible browser and wait until a configured CERN identity cookie exists.

    This one visible login is all the user interaction ever needed: every
    follow-up (Bearer-token capture, cookie-only site sessions, page renders)
    runs headlessly against the saved state. The returned dictionary is
    Playwright storage state. It contains credentials and must only be passed
    to :class:`cmcps.auth.store.SessionStore`.
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
            return await context.storage_state()
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


_EXTRACT_TIMEOUT_SECONDS = 60


async def extract_local_storage_value(
    settings: Settings,
    storage_state: dict[str, Any],
    url: str,
    storage_key: str,
) -> tuple[str, dict[str, Any]] | None:
    """Open ``url`` headlessly through the stored SSO session and read one
    ``window.localStorage`` key once the page's own auth flow settles.

    Used for SPAs whose API token is minted by the app backend into browser
    storage (not visible as a Keycloak token exchange, so the BEARER_SERVICES
    interception cannot capture it). Returns ``(value, refreshed_storage_state)``
    or ``None`` when the key never materializes. The value is a secret and must
    stay inside trusted adapter code.
    """
    deadline = asyncio.get_running_loop().time() + _EXTRACT_TIMEOUT_SECONDS
    browser: Browser | None = None
    context: BrowserContext | None = None
    probe = "(key) => window.localStorage.getItem(key)"
    try:
        async with async_playwright() as playwright:
            browser = await _launch_browser(settings, playwright, headless=True)
            context = await browser.new_context(storage_state=storage_state, accept_downloads=False)
            page = await context.new_page()
            try:
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=min(30_000, _EXTRACT_TIMEOUT_SECONDS * 1000),
                )
            except PlaywrightError:
                logger.debug("Local-storage extraction page load failed for %s", url)
                return None
            value: str | None = None
            while not page.is_closed():
                try:
                    value = await page.evaluate(probe, storage_key)
                except PlaywrightError:
                    value = None
                if isinstance(value, str) and value:
                    return value, await context.storage_state()
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    logger.debug("Local-storage key %r never materialized for %s", storage_key, url)
                    return None
                await asyncio.sleep(min(settings.sso_poll_seconds, remaining))
            return None
    finally:
        await _close_resource(context, "local-storage context")
        await _close_resource(browser, "local-storage browser")


FETCH_DEFAULT_TIMEOUT_SECONDS = 45
FETCH_MAX_TIMEOUT_SECONDS = 120
FETCH_DEFAULT_MAX_CHARS = 20_000
FETCH_MAX_CHARS_LIMIT = 200_000
_FETCH_STABLE_POLLS = 2

# Hosts the page ends on when the stored SSO session is dead: the new authflow
# front and the legacy Keycloak realm (settings.sso_identity_host).
_AUTHFLOW_HOST = "authflow.cern.ch"


async def _poll_body_text(
    page: Page,
    *,
    marker: str | None,
    poll_seconds: float,
    deadline: float,
) -> tuple[str, bool]:
    """Poll ``body`` innerText until the marker appears or the text stabilizes.

    With a ``marker`` the returned flag means "marker found"; without one it
    means "text stopped changing". Transient innerText failures (page
    mid-navigation) keep the current poll cycle.
    """
    text = ""
    last: str | None = None
    stable = 0
    while not page.is_closed():
        try:
            current = await page.inner_text("body")
        except PlaywrightError:
            current = None
        if current is not None:
            text = current
            if marker is not None:
                if marker in current.casefold():
                    return text, True
            elif not current.strip():
                stable = 0
            elif current == last:
                stable += 1
                if stable >= _FETCH_STABLE_POLLS:
                    return text, True
            else:
                stable = 0
            last = current
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return text, False
        await asyncio.sleep(min(poll_seconds, remaining))
    return text, False


async def fetch_page_text(
    settings: Settings,
    storage_state: dict[str, Any],
    url: str,
    wait_for: str | None = None,
    timeout_seconds: int = FETCH_DEFAULT_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Render one SSO-protected page headlessly and return its visible text.

    Generic counterpart to :func:`render_page_html` (which targets the ADF
    portal): works for cookie-backed pages and for Keycloak-PKCE SPAs whose
    token exchange completes silently against the stored identity. Polls the
    ``body`` innerText until ``wait_for`` (case-insensitive substring)
    appears, or — without a marker — until the text stops changing. Returns
    ``(payload, refreshed_storage_state)`` or ``None`` when the page failed
    to load. The payload carries ``auth_divert=True`` when the final URL
    landed on an authentication host, signalling a stale session.
    """
    marker = wait_for.casefold() if wait_for else None
    deadline = asyncio.get_running_loop().time() + timeout_seconds
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
                    timeout=min(30_000, timeout_seconds * 1000),
                )
            except PlaywrightError:
                logger.debug("Fetch-page load failed for %s", url)
                return None
            text, found = await _poll_body_text(
                page,
                marker=marker,
                poll_seconds=settings.sso_poll_seconds,
                deadline=deadline,
            )
            try:
                title = await page.title() or None
            except PlaywrightError:
                title = None
            final_host = urlsplit(page.url).hostname or ""
            payload = {
                "url": url,
                "final_url": page.url,
                "title": title,
                "text": text,
                "marker_found": found if marker is not None else None,
                "auth_divert": final_host in {settings.sso_identity_host, _AUTHFLOW_HOST},
            }
            return payload, await context.storage_state()
    finally:
        await _close_resource(context, "fetch-page context")
        await _close_resource(browser, "fetch-page browser")


async def _capture_html(
    page: Page,
    settings: Settings,
    url: str,
    text: str,
    *,
    found: bool,
    marker: str | None,
    settle_seconds: float,
    deadline: float,
) -> dict[str, Any] | None:
    """Snapshot the rendered page into the fetcher payload (HTML + text).

    Lazy apps (GWT/GXT) paint their shell — including the marker — before
    async data hydrates the grid; when the marker matched, settle up to the
    deadline first so the captured HTML carries rows, not the empty shell.
    """
    if found and settle_seconds > 0:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining > 0:
            await asyncio.sleep(min(settle_seconds, remaining))
        try:
            text = await page.inner_text("body")
        except PlaywrightError:
            pass
    try:
        html = await page.content()
        title = await page.title() or None
    except PlaywrightError:
        return None
    final_host = urlsplit(page.url).hostname or ""
    return {
        "url": url,
        "final_url": page.url,
        "title": title,
        "text": text,
        "html": html,
        "marker_found": found if marker is not None else None,
        "auth_divert": final_host in {settings.sso_identity_host, _AUTHFLOW_HOST},
    }


async def fetch_page_html_text(
    settings: Settings,
    storage_state: dict[str, Any],
    url: str,
    wait_for: str | None = None,
    *,
    interact: PortalInteract | None = None,
    interact_even_if_found: bool = False,
    settle_seconds: float = 0.0,
    timeout_seconds: int = FETCH_DEFAULT_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Render one SSO-protected page headlessly and return HTML plus visible text.

    Companion to :func:`fetch_page_text` for DOM-scraping adapters that need
    anchors and grid markup, not only ``innerText``. When ``interact`` is
    provided and the ``wait_for`` marker does not appear on the first load,
    the hook runs once (e.g. an SP-as sign-in click that completes silently
    against the stored identity), the page is navigated back to ``url`` and
    the marker poll resumes for the remaining budget. By default the hook
    only runs when the marker never appears; ``interact_even_if_found``
    forces it anyway (for SPAs whose anonymous page already shows the
    marker). Payload carries
    ``html``, ``text``, ``final_url``, ``title``, ``marker_found`` and
    ``auth_divert``.
    """
    marker = wait_for.casefold() if wait_for else None
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    browser: Browser | None = None
    context: BrowserContext | None = None

    async def _load_and_poll() -> tuple[str, bool]:
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=min(30_000, timeout_seconds * 1000),
        )
        return await _poll_body_text(
            page,
            marker=marker,
            poll_seconds=settings.sso_poll_seconds,
            deadline=deadline
            if interact is None
            else (deadline + asyncio.get_running_loop().time()) / 2,
        )

    try:
        async with async_playwright() as playwright:
            browser = await _launch_browser(settings, playwright, headless=True)
            context = await browser.new_context(storage_state=storage_state, accept_downloads=False)
            page = await context.new_page()
            try:
                text, found = await _load_and_poll()
            except PlaywrightError:
                logger.debug("Fetch-page-html load failed for %s", url)
                return None
            if interact is not None and (not found or interact_even_if_found):
                try:
                    acted = await interact(page)
                except PlaywrightError:
                    logger.debug("Fetch-page-html interaction failed for %s", url)
                    return None
                if acted:
                    try:
                        text, found = await _load_and_poll()
                    except PlaywrightError:
                        logger.debug("Fetch-page-html reload failed for %s", url)
                        return None
            payload = await _capture_html(
                page,
                settings,
                url,
                text,
                found=found,
                marker=marker,
                settle_seconds=settle_seconds,
                deadline=deadline,
            )
            if payload is None:
                return None
            return payload, await context.storage_state()
    finally:
        await _close_resource(context, "fetch-page-html context")
        await _close_resource(browser, "fetch-page-html browser")
