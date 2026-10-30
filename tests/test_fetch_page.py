"""Tests for the generic headless page fetch behind cern_fetch_page."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError

from cmcps.app import build_server
from cmcps.auth import browser as browser_module
from cmcps.auth.browser import fetch_page_text
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import BrowserUnavailableError


def _settings(tmp_path: Path, **kwargs: Any) -> Settings:
    return Settings(
        state_dir=tmp_path,
        browser_executable=None,
        sso_poll_seconds=0.11,
        **kwargs,
    )


class _FakePage:
    """Minimal stand-in for a Playwright Page with a scripted body text."""

    def __init__(
        self,
        texts: list[str],
        *,
        title: str = "Title",
        redirect: str | None = None,
        fail_goto: bool = False,
        fail_goto_message: str = "navigation failed",
        goto_response: _FakeAPIResponse | None = None,
    ) -> None:
        self._texts = iter(texts)
        self._last = texts[-1] if texts else ""
        self._title = title
        self._redirect = redirect
        self._fail_goto = fail_goto
        self._fail_goto_message = fail_goto_message
        self._goto_response = goto_response
        self.url = "https://app.cern.ch/page"

    def is_closed(self) -> bool:
        return False

    async def goto(self, url: str, wait_until: str, timeout: int) -> _FakeAPIResponse | None:
        if self._fail_goto:
            raise PlaywrightError(self._fail_goto_message)
        self.last_goto = (url, wait_until, timeout)
        self.url = self._redirect or url
        return self._goto_response

    async def inner_text(self, selector: str) -> str:
        self.last_selector = selector
        try:
            return next(self._texts)
        except StopIteration:
            return self._last

    async def title(self) -> str:
        return self._title


class _FakeAPIResponse:
    """Stand-in for the Playwright APIRequestContext response."""

    def __init__(self, url: str, content_type: str) -> None:
        self.url = url
        self.headers = {"content-type": content_type}


class _FakeRequest:
    def __init__(self, response: _FakeAPIResponse | None) -> None:
        self._response = response

    async def get(self, _url: str) -> _FakeAPIResponse:
        if self._response is None:
            raise PlaywrightError("api request failed")
        return self._response


class _FakeContext:
    def __init__(
        self,
        page: _FakePage,
        state: dict[str, Any],
        probe_response: _FakeAPIResponse | None = None,
        fail_storage_state: bool = False,
    ) -> None:
        self._page = page
        self._state = state
        self._fail_storage_state = fail_storage_state
        self.request = _FakeRequest(probe_response)

    async def new_page(self) -> _FakePage:
        return self._page

    async def storage_state(self) -> dict[str, Any]:
        if self._fail_storage_state:
            raise PlaywrightError("Target page, context or browser has been closed")
        return copy.deepcopy(self._state)

    async def cookies(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return list(self._state.get("cookies", []))

    async def close(self) -> None:
        return None


class _FakeBrowser:
    def __init__(self, context: _FakeContext) -> None:
        self._context = context

    async def new_context(self, **_: Any) -> _FakeContext:
        return self._context

    async def close(self) -> None:
        return None


def _patch_playwright(
    monkeypatch: Any,
    page: _FakePage,
    state: dict[str, Any],
    probe_response: _FakeAPIResponse | None = None,
    fail_storage_state: bool = False,
) -> None:
    fake_browser = _FakeBrowser(_FakeContext(page, state, probe_response, fail_storage_state))

    class _PlaywrightCM:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_: Any) -> bool:
            return False

    monkeypatch.setattr(browser_module, "async_playwright", _PlaywrightCM)

    async def _launch(*_: Any, **__: Any) -> _FakeBrowser:
        return fake_browser

    monkeypatch.setattr(browser_module, "_launch_browser", _launch)


async def test_fetch_page_text_waits_for_marker(monkeypatch, tmp_path: Path) -> None:
    refreshed = {"cookies": [{"name": "PORTAL", "value": "1"}], "origins": []}
    _patch_playwright(
        monkeypatch, _FakePage(["loading…", "Welcome TSO table"], title="Roster"), refreshed
    )
    result = await fetch_page_text(
        _settings(tmp_path),
        {"cookies": [], "origins": []},
        "https://app.cern.ch/page",
        "tso TABLE",
        30,
    )
    assert result is not None
    payload, state = result
    assert payload["marker_found"] is True
    assert payload["text"] == "Welcome TSO table"
    assert payload["title"] == "Roster"
    assert payload["url"] == "https://app.cern.ch/page"
    assert payload["final_url"] == "https://app.cern.ch/page"
    assert payload["auth_divert"] is False
    assert state == refreshed


async def test_fetch_page_text_settles_without_marker(monkeypatch, tmp_path: Path) -> None:
    _patch_playwright(monkeypatch, _FakePage(["Hello", "Hello"]), {"cookies": [], "origins": []})
    result = await fetch_page_text(
        _settings(tmp_path), {"cookies": [], "origins": []}, "https://app.cern.ch/page", None, 30
    )
    assert result is not None
    payload, _ = result
    assert payload["marker_found"] is None
    assert payload["text"] == "Hello"


async def test_fetch_page_text_marker_missing_reports_not_found(
    monkeypatch, tmp_path: Path
) -> None:
    _patch_playwright(monkeypatch, _FakePage(["empty shell"]), {"cookies": [], "origins": []})
    result = await fetch_page_text(
        _settings(tmp_path), {"cookies": [], "origins": []}, "https://app.cern.ch/page", "zzz", 1
    )
    assert result is not None
    payload, _ = result
    assert payload["marker_found"] is False
    assert payload["text"] == "empty shell"


async def test_fetch_page_text_flags_auth_divert(monkeypatch, tmp_path: Path) -> None:
    _patch_playwright(
        monkeypatch,
        _FakePage(
            ["Sign in"],
            redirect="https://authflow.cern.ch/realms/cern/protocol/openid-connect/auth?x=1",
        ),
        {"cookies": [], "origins": []},
    )
    result = await fetch_page_text(
        _settings(tmp_path), {"cookies": [], "origins": []}, "https://app.cern.ch/page", None, 5
    )
    assert result is not None
    payload, _ = result
    assert payload["auth_divert"] is True


async def test_fetch_page_text_goto_failure_returns_none(monkeypatch, tmp_path: Path) -> None:
    _patch_playwright(monkeypatch, _FakePage(["x"], fail_goto=True), {"cookies": [], "origins": []})
    result = await fetch_page_text(
        _settings(tmp_path), {"cookies": [], "origins": []}, "https://app.cern.ch/page", None, 5
    )
    assert result is None


async def test_fetch_page_text_pdf_abort_reports_binary(monkeypatch, tmp_path: Path) -> None:
    """A navigation aborted by a binary download yields a binary payload, not None."""
    probe = _FakeAPIResponse("https://cms-alcm.web.cern.ch/notes/x/v4.pdf", "application/pdf")
    _patch_playwright(
        monkeypatch,
        _FakePage(
            ["x"], fail_goto=True, fail_goto_message="Page.goto: net::ERR_ABORTED at https://x"
        ),
        {"cookies": [], "origins": []},
        probe,
    )
    result = await fetch_page_text(
        _settings(tmp_path),
        {"cookies": [], "origins": []},
        "https://cms-alcm.web.cern.ch/notes/x/v4.pdf",
        None,
        5,
    )
    assert result is not None
    payload, _ = result
    assert payload["content_kind"] == "binary"
    assert payload["content_type"] == "application/pdf"
    assert payload["text"] == ""
    assert payload["auth_divert"] is False


async def test_fetch_page_text_abort_landing_on_login_returns_none(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """ERR_ABORTED from an interrupted SSO redirect keeps the stale-session report."""
    probe = _FakeAPIResponse("https://auth.cern.ch/auth/realms/cern/login", "text/html")
    _patch_playwright(
        monkeypatch,
        _FakePage(["x"], fail_goto=True, fail_goto_message="Page.goto: net::ERR_ABORTED"),
        {"cookies": [], "origins": []},
        probe,
    )
    result = await fetch_page_text(
        _settings(tmp_path), {"cookies": [], "origins": []}, "https://app.cern.ch/page", None, 5
    )
    assert result is None


async def test_fetch_page_text_binary_goto_headers_obscura_style(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Engines that answer a binary goto instead of aborting (obscura) are caught
    by the navigation response headers; a dead target afterwards is tolerated."""
    goto_response = _FakeAPIResponse(
        "https://cms-alcm.web.cern.ch/notes/x/v4.pdf", "application/pdf"
    )
    _patch_playwright(
        monkeypatch,
        _FakePage(["x"], goto_response=goto_response),
        {"cookies": [], "origins": []},
        probe_response=None,  # the APIRequestContext probe must not be needed
        fail_storage_state=True,
    )
    result = await fetch_page_text(
        _settings(tmp_path),
        {"cookies": [], "origins": []},
        "https://cms-alcm.web.cern.ch/notes/x/v4.pdf",
        None,
        5,
    )
    assert result is not None
    payload, state = result
    assert payload["content_kind"] == "binary"
    assert payload["content_type"] == "application/pdf"
    assert state == {"cookies": [], "origins": []}  # input state kept as fallback


def _fair_fetcher(captured: dict[str, Any], text: str, refreshed: dict[str, Any]) -> Any:
    async def fake_fetcher(
        _settings: Settings, _state: object, url: str, wait_for: str | None, timeout: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        captured.update(url=url, wait_for=wait_for, timeout=timeout)
        return (
            {
                "url": url,
                "final_url": url,
                "title": "T",
                "text": text,
                "marker_found": True,
                "auth_divert": False,
            },
            refreshed,
        )

    return fake_fetcher


async def test_service_fetch_page_truncates_and_persists(
    settings: Settings, valid_storage_state: dict[str, Any], future_epoch: float
) -> None:
    refreshed = copy.deepcopy(valid_storage_state)
    refreshed["cookies"].append(
        {
            "name": "PORTAL",
            "value": "refreshed",
            "domain": ".service.cern.ch",
            "path": "/",
            "expires": future_epoch,
            "secure": True,
            "httpOnly": True,
            "sameSite": "Lax",
        }
    )
    captured: dict[str, Any] = {}
    auth = AuthService(settings, page_fetcher=_fair_fetcher(captured, "x" * 4_000, refreshed))
    auth.store.save(valid_storage_state)
    result = await auth.fetch_page(
        "https://service.cern.ch/page",
        wait_for="ready",
        timeout_seconds=20,
        max_chars=1_000,
    )
    assert result is not None
    assert result["truncated"] is True
    assert len(result["text"]) == 1_000
    assert result["marker_found"] is True
    assert captured == {"url": "https://service.cern.ch/page", "wait_for": "ready", "timeout": 20}
    record = auth.store.load()
    assert record is not None
    assert any(cookie["name"] == "PORTAL" for cookie in record.cookies)


async def test_service_fetch_page_full_text_without_max_chars(
    settings: Settings, valid_storage_state: dict[str, Any]
) -> None:
    auth = AuthService(
        settings,
        page_fetcher=_fair_fetcher({}, "y" * 3_000, valid_storage_state),
    )
    auth.store.save(valid_storage_state)
    result = await auth.fetch_page("https://service.cern.ch/page")
    assert result is not None
    assert result["truncated"] is False
    assert len(result["text"]) == 3_000


async def test_service_fetch_page_requires_session(settings: Settings) -> None:
    auth = AuthService(settings, page_fetcher=_fair_fetcher({}, "x", {}))
    assert await auth.fetch_page("https://service.cern.ch/page") is None


async def test_service_fetch_page_bypass_returns_none(tmp_path: Path) -> None:
    settings = Settings(state_dir=tmp_path, require_sso=False, browser_executable=None)
    auth = AuthService(settings, page_fetcher=_fair_fetcher({}, "x", {}))
    assert await auth.fetch_page("https://service.cern.ch/page") is None


async def test_service_fetch_page_browser_unavailable_returns_none(
    settings: Settings, valid_storage_state: dict[str, Any]
) -> None:
    async def broken(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]] | None:
        raise BrowserUnavailableError("no browser")

    auth = AuthService(settings, page_fetcher=broken)
    auth.store.save(valid_storage_state)
    assert await auth.fetch_page("https://service.cern.ch/page") is None


async def test_cern_fetch_page_requires_sso(settings: Settings) -> None:
    server = build_server(settings)
    result = await server.call_tool("cern_fetch_page", {"url": "https://service.cern.ch/page"})
    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "AUTH_REQUIRED"


async def test_cern_fetch_page_success(
    settings: Settings, valid_storage_state: dict[str, Any]
) -> None:
    auth = AuthService(
        settings,
        page_fetcher=_fair_fetcher({}, "TSO of B15", copy.deepcopy(valid_storage_state)),
    )
    auth.store.save(valid_storage_state)
    server = build_server(settings, auth_service=auth)
    result = await server.call_tool(
        "cern_fetch_page", {"url": "https://service.cern.ch/page", "wait_for": "TSO"}
    )
    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "ok"
    data = payload["data"]
    assert data["text"] == "TSO of B15"
    assert data["marker"] == "TSO"
    assert data["marker_found"] is True
    assert data["auth_divert"] is False
    assert data["truncated"] is False


async def test_cern_fetch_page_binary_points_to_download_tool(
    settings: Settings, valid_storage_state: dict[str, Any]
) -> None:
    """Binary URLs surface a targeted error, not the stale-session hint."""

    async def binary_fetcher(
        _settings: Settings, _state: object, url: str, _wait_for: str | None, _timeout: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        state = {"cookies": [], "origins": []}
        return (
            {
                "url": url,
                "final_url": url,
                "title": None,
                "text": "",
                "marker_found": None,
                "auth_divert": False,
                "content_kind": "binary",
                "content_type": "application/pdf",
            },
            state,
        )

    auth = AuthService(settings, page_fetcher=binary_fetcher)
    auth.store.save(valid_storage_state)
    server = build_server(settings, auth_service=auth)
    result = await server.call_tool(
        "cern_fetch_page", {"url": "https://cms-alcm.web.cern.ch/notes/x/v4.pdf"}
    )
    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "UPSTREAM_ERROR"
    assert "application/pdf" in payload["error"]["message"]
    assert payload["error"]["instructions"].find("cern_download_file") >= 0
    assert "SSO session may be stale" not in payload["error"]["instructions"]


async def test_cern_download_file_requires_sso(settings: Settings) -> None:
    server = build_server(settings)
    result = await server.call_tool(
        "cern_download_file", {"url": "https://service.cern.ch/file.pdf"}
    )
    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "AUTH_REQUIRED"


async def test_cern_download_file_rejects_path_filename(settings: Settings) -> None:
    server = build_server(settings)
    result = await server.call_tool(
        "cern_download_file",
        {"url": "https://service.cern.ch/file.pdf", "filename": "../evil.pdf"},
    )
    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "INVALID_INPUT"
