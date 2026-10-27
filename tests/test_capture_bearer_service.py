"""Tests for the lazy single-service headless Bearer capture."""
# ruff: noqa: S105 - fixture endpoint/token placeholders, not real secrets

from __future__ import annotations

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
