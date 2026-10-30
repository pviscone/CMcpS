"""Tests for the lazy single-service headless Bearer capture."""
# ruff: noqa: S105 - fixture endpoint/token placeholders, not real secrets

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from cmcps.auth import browser as browser_module
from cmcps.auth.browser import capture_bearer_service
from cmcps.config.settings import Settings

_TOKEN_ENDPOINT = "https://auth.cern.ch/auth/realms/cern/protocol/openid-connect/token"
_POST_DATA = "code=deadbeef&client_id=cmshltinfo-app&grant_type=authorization_code"


class _FakeRequest:
    url = _TOKEN_ENDPOINT
    method = "POST"
    post_data = _POST_DATA


class _FakeResponse:
    def __init__(self) -> None:
        self.request = _FakeRequest()

    async def json(self) -> dict[str, Any]:
        return {"access_token": "AT", "refresh_token": "RT", "expires_in": 600}


class _FakePage:
    """Fires stored response listeners (the SPA's Keycloak exchange) on goto."""

    def __init__(self) -> None:
        self.url = "about:blank"
        self._listeners: list[Any] = []

    def is_closed(self) -> bool:
        return False

    def on(self, event: str, callback: Any) -> None:
        if event == "response":
            self._listeners.append(callback)

    async def goto(self, url: str, wait_until: str, timeout: int) -> None:
        self.url = url
        self.last_goto = (url, wait_until, timeout)
        for callback in self._listeners:
            await callback(_FakeResponse())

    async def close(self) -> None:
        return None


class _FakeContext:
    def __init__(self) -> None:
        self.pages: list[_FakePage] = []

    async def new_page(self) -> _FakePage:
        page = _FakePage()
        self.pages.append(page)
        return page

    async def close(self) -> None:
        return None


class _FakeBrowser:
    def __init__(self, context: _FakeContext) -> None:
        self._context = context

    async def new_context(self, **_: Any) -> _FakeContext:
        return self._context

    async def close(self) -> None:
        return None


def _patch_playwright(monkeypatch: Any) -> _FakeContext:
    context = _FakeContext()
    fake_browser = _FakeBrowser(context)

    class _PlaywrightCM:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_: Any) -> bool:
            return False

    monkeypatch.setattr(browser_module, "async_playwright", _PlaywrightCM)

    async def _launch(*_: Any, **__: Any) -> _FakeBrowser:
        return fake_browser

    monkeypatch.setattr(browser_module, "_launch_browser", _launch)
    return context


def _settings(tmp_path: Path) -> Settings:
    return Settings(state_dir=tmp_path, browser_executable=None, sso_poll_seconds=0.11)


async def test_capture_bearer_service_intercepts_exchange(monkeypatch: Any, tmp_path: Path) -> None:
    """One configured app performs its silent exchange headlessly and yields its token set."""
    _patch_playwright(monkeypatch)
    result = await capture_bearer_service(
        _settings(tmp_path), {"cookies": [], "origins": []}, "hltinfo"
    )
    assert result is not None
    assert result["endpoint_url"] == _TOKEN_ENDPOINT
    assert result["post_data"] == _POST_DATA
    assert result["response_json"]["access_token"] == "AT"


async def test_capture_bearer_service_unknown_service_returns_none(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _patch_playwright(monkeypatch)
    result = await capture_bearer_service(
        _settings(tmp_path), {"cookies": [], "origins": []}, "backend-from-nowhere"
    )
    assert result is None


class _FakeAuthorizeRequest:
    """The PKCE authorize hit: the only client_id source without POST bodies."""

    url = (
        "https://auth.cern.ch/auth/realms/cern/protocol/openid-connect/auth"
        "?client_id=cmshltinfo-app&redirect_uri=https%3A%2F%2Fx.cern.ch%2F&response_type=code"
    )
    method = "GET"
    post_data = None


class _FakeBodylessPostRequest:
    """Obscura-style token POST: response body readable, POST body not."""

    url = _TOKEN_ENDPOINT
    method = "POST"
    post_data = None


class _FakeScriptedPage(_FakePage):
    def __init__(self, requests: list[Any], json_body: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._requests = requests
        self._json_body = json_body or {
            "access_token": "AT",
            "refresh_token": "RT",
            "expires_in": 600,
        }

    async def goto(self, url: str, wait_until: str, timeout: int) -> None:
        self.url = url
        self.last_goto = (url, wait_until, timeout)
        for request in self._requests:

            class _Response:
                def __init__(self, req: Any, body: dict[str, Any]) -> None:
                    self.request = req
                    self._body = body

                async def json(self) -> dict[str, Any]:
                    return self._body

            for callback in self._listeners:
                await callback(_Response(request, self._json_body))


async def test_capture_bearer_service_recovers_client_id_from_authorize_url(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Engines that hide POST bodies (obscura) still yield a parseable record."""
    page = _FakeScriptedPage([_FakeAuthorizeRequest(), _FakeBodylessPostRequest()])
    result = await _run_capture(monkeypatch, page, tmp_path)
    assert result is not None
    assert "client_id=cmshltinfo-app" in result["post_data"]
    assert result["response_json"]["access_token"] == "AT"


def _jwt(**claims: Any) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


async def _run_capture(
    monkeypatch: Any, page: _FakeScriptedPage, tmp_path: Path
) -> dict[str, Any] | None:
    class _Context(_FakeContext):
        async def new_page(self) -> _FakeScriptedPage:
            return page

    fake_browser = _FakeBrowser(_Context())

    class _PlaywrightCM:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_: Any) -> bool:
            return False

    monkeypatch.setattr(browser_module, "async_playwright", _PlaywrightCM)

    async def _launch(*_: Any, **__: Any) -> _FakeBrowser:
        return fake_browser

    monkeypatch.setattr(browser_module, "_launch_browser", _launch)
    return await capture_bearer_service(
        _settings(tmp_path), {"cookies": [], "origins": []}, "hltinfo"
    )


async def test_capture_bearer_service_recovers_client_id_from_jwt(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """No POST body and no authorize event: the JWT azp of the body wins."""
    body = {
        "access_token": _jwt(aud="cmshltinfo-api-prod", azp="cmshltinfo-app"),
        "id_token": _jwt(aud="cmshltinfo-api-prod", azp="cmshltinfo-app", typ="ID"),
        "refresh_token": "RT",
        "expires_in": 600,
    }
    page = _FakeScriptedPage([_FakeBodylessPostRequest()], json_body=body)
    result = await _run_capture(monkeypatch, page, tmp_path)
    assert result is not None
    assert "client_id=cmshltinfo-app" in result["post_data"]
    assert "cmshltinfo-api-prod" not in result["post_data"]


async def test_capture_bearer_service_falls_back_to_system_browser(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A stalled obscura capture retries once through the system browser."""
    engines_seen: list[str] = []
    firing_page = _FakeScriptedPage([_FakeAuthorizeRequest(), _FakeBodylessPostRequest()])

    class _SilentPage(_FakeScriptedPage):
        async def goto(self, url: str, wait_until: str, timeout: int) -> None:
            self.url = url  # fires no token exchange: the flow stalls
            self.last_goto = (url, wait_until, timeout)

    def _context_for(engine: str) -> _FakeContext:
        page = _SilentPage([]) if engine == "obscura" else firing_page

        class _Context(_FakeContext):
            async def new_page(self, _page: _FakeScriptedPage = page) -> Any:
                return _page

        return _Context()

    class _PlaywrightCM:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_: Any) -> bool:
            return False

    monkeypatch.setattr(browser_module, "async_playwright", _PlaywrightCM)
    monkeypatch.setattr(browser_module, "_CAPTURE_TIMEOUT_SECONDS", 0.2)

    async def _launch(settings: Settings, *_args: Any, **_kwargs: Any) -> _FakeBrowser:
        engines_seen.append(settings.headless_browser)
        return _FakeBrowser(_context_for(settings.headless_browser))

    monkeypatch.setattr(browser_module, "_launch_browser", _launch)
    result = await capture_bearer_service(
        _settings(tmp_path), {"cookies": [], "origins": []}, "hltinfo"
    )
    assert engines_seen == ["obscura", "chromium"]
    assert result is not None
    assert result["response_json"]["access_token"] == "AT"
