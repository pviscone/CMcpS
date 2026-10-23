"""Visible-browser CERN SSO collection using Playwright."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

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


async def _launch_browser(settings: Settings, playwright: Playwright) -> Browser:
    launcher = playwright.chromium if settings.browser_type == "chromium" else playwright.firefox
    try:
        return await launcher.launch(
            headless=False,
            executable_path=settings.browser_executable,
        )
    except PlaywrightError as exc:
        raise BrowserUnavailableError(
            "A visible browser could not be started",
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
            browser = await _launch_browser(settings, playwright)
            context = await browser.new_context(accept_downloads=False)
            page = await context.new_page()
            await page.goto(
                str(settings.sso_start_url),
                wait_until="domcontentloaded",
                timeout=min(30_000, timeout * 1000),
            )
            storage_state = await _wait_for_authenticated_state(settings, context, page, timeout)
            capture = await _capture_bearer_services(settings, context)
            storage_state["service_tokens"] = capture
            return storage_state
    finally:
        await _close_resource(context, "context")
        await _close_resource(browser, "browser")
