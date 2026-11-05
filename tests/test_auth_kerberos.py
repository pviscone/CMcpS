"""Kerberos + terminal-2FA login: token scoping, completion checks, CLI behavior."""
# ruff: noqa: S105 - fixture token placeholders, not real secrets

from __future__ import annotations

import sys
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest

from cmsmcp.auth import kerberos
from cmsmcp.auth.kerberos import (
    KerberosLoginError,
    make_kerberos_collector,
    validate_kerberos_redirect,
)
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.errors import InvalidSessionError
from cmsmcp.login import make_otp_provider, normalise_code, run

LOGIN_URL = "https://auth.cern.ch/auth/realms/cern/protocol/openid-connect/auth?client_id=x"
BROKER_HREF = "/auth/realms/cern/broker/kerberos/login?client_id=x&session_code=s"
REALM_URL = "https://auth.cern.ch/auth/realms/kerberos/protocol/openid-connect/auth?state=k"
OTP_URL = "https://auth.cern.ch/auth/realms/cern/login-actions/post-broker-login?x=1"
GOOD_CODE = "123456"
TOKEN = "Negotiate dG9rZW4="


# --- redirect scoping -------------------------------------------------------


def test_redirect_accepts_only_identity_host_kerberos_realm() -> None:
    assert validate_kerberos_redirect(REALM_URL, "auth.cern.ch") == REALM_URL
    for bad in (
        REALM_URL.replace("https://", "http://"),
        REALM_URL.replace("auth.cern.ch", "evil.example.org"),
        "https://auth.cern.ch/auth/realms/cern/protocol/openid-connect/auth",
    ):
        with pytest.raises(KerberosLoginError):
            validate_kerberos_redirect(bad, "auth.cern.ch")


def test_missing_gssapi_explains_the_optional_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "gssapi", None)
    with pytest.raises(KerberosLoginError) as excinfo:
        kerberos.check_kerberos_ticket()
    assert "--extra kerberos" in (excinfo.value.instructions or "")


# --- fake browser -----------------------------------------------------------


class _Locator:
    def __init__(self, page: _Page, selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> _Locator:
        return self

    async def count(self) -> int:
        return 1 if self.page.has(self.selector) else 0

    async def wait_for(self, **_: Any) -> None:
        assert self.page.has(self.selector)

    async def get_attribute(self, name: str) -> str:
        assert name == "href"
        return BROKER_HREF

    async def inner_text(self) -> str:
        return "Invalid authenticator code."


class _Route:
    def __init__(self, page: _Page, url: str) -> None:
        self.page = page
        self.request = self
        self.url = url

    async def all_headers(self) -> dict[str, str]:
        return {"user-agent": "test"}

    async def continue_(self, headers: dict[str, str]) -> None:
        self.page.sent_headers[self.url] = headers


class _Page:
    def __init__(self, context: _Context, *, kerberos_ok: bool) -> None:
        self.context = context
        self.kerberos_ok = kerberos_ok
        self.url = "about:blank"
        self.routes: list[tuple[Callable[[str], bool], Any]] = []
        self.sent_headers: dict[str, dict[str, str]] = {}
        self.filled: list[str] = []
        self.wrong_code = False

    def has(self, selector: str) -> bool:
        if "kerberos/login" in selector:
            return self.url == LOGIN_URL
        if selector == "#otp":
            return self.url == OTP_URL
        return self.wrong_code and "input-error" in selector

    def locator(self, selector: str) -> _Locator:
        return _Locator(self, selector)

    async def goto(self, url: str, **_: Any) -> None:
        for matcher, handler in self.routes:
            if matcher(url):
                await handler(_Route(self, url))
        if url == str(self.context.settings.sso_start_url):
            self.url = LOGIN_URL
        elif url == REALM_URL:
            authorised = self.sent_headers.get(url, {}).get("authorization") == TOKEN
            self.url = OTP_URL if authorised and self.kerberos_ok else REALM_URL
        else:
            # Any other navigation must not carry the SPNEGO token.
            self.url = url

    async def route(self, matcher: Callable[[str], bool], handler: Any, times: int) -> None:
        assert times == 1
        self.routes.append((matcher, handler))

    async def unroute(self, matcher: Callable[[str], bool]) -> None:
        self.routes = [r for r in self.routes if r[0] is not matcher]

    async def wait_for_load_state(self, *_: Any, **__: Any) -> None:
        return None

    async def fill(self, selector: str, value: str) -> None:
        assert selector == "#otp"
        self.filled.append(value)

    async def click(self, selector: str) -> None:
        assert selector == "#kc-login"
        if self.filled[-1] == GOOD_CODE:
            self.wrong_code = False
            self.context.signed_in = True
            self.url = "https://indico.cern.ch/user/dashboard/"
        else:
            self.wrong_code = True

    @asynccontextmanager
    async def expect_navigation(self, **_: Any) -> Any:
        yield


class _Response:
    def __init__(self) -> None:
        self.headers = {"location": REALM_URL}


class _Request:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def get(self, url: str, max_redirects: int) -> _Response:
        self.calls.append((url, max_redirects))
        return _Response()


class _Context:
    def __init__(self, settings: Settings, identity_cookie: dict[str, Any], **page_kw: Any) -> None:
        self.settings = settings
        self.identity_cookie = identity_cookie
        self.signed_in = False
        self.request = _Request()
        self.page = _Page(self, **page_kw)

    async def new_page(self) -> _Page:
        return self.page

    async def cookies(self) -> list[dict[str, Any]]:
        # Keycloak sets the identity cookie before 2FA finishes; the flow must
        # not treat its presence alone as success.
        return [self.identity_cookie]

    async def storage_state(self) -> dict[str, Any]:
        assert self.signed_in, "storage state captured before the login finished"
        return {"cookies": [self.identity_cookie], "origins": []}

    async def close(self) -> None:
        return None


class _Browser:
    def __init__(self, context: _Context) -> None:
        self.context = context

    async def new_context(self, **_: Any) -> _Context:
        return self.context

    async def close(self) -> None:
        return None


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, cookie: dict[str, Any], **page_kw: Any
) -> _Context:
    context = _Context(settings, cookie, **page_kw)

    @asynccontextmanager
    async def fake_playwright() -> Any:
        yield object()

    async def fake_launch(*_: Any, headless: bool) -> _Browser:
        assert headless
        return _Browser(context)

    monkeypatch.setattr(kerberos, "async_playwright", fake_playwright)
    monkeypatch.setattr(kerberos, "_launch_browser", fake_launch)
    monkeypatch.setattr(kerberos, "check_kerberos_ticket", lambda: None)
    monkeypatch.setattr(kerberos, "negotiate_header", lambda _host: TOKEN)
    monkeypatch.setattr(kerberos, "_POLL_SECONDS", 0)
    return context


def _codes(*codes: str) -> tuple[Any, list[str | None]]:
    seen: list[str | None] = []
    queue = list(codes)

    async def provider(previous_error: str | None) -> str:
        seen.append(previous_error)
        return queue.pop(0)

    return provider, seen


# --- collector flow ---------------------------------------------------------


async def test_kerberos_flow_scopes_token_and_waits_for_real_completion(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, valid_storage_state: dict[str, Any]
) -> None:
    cookie = valid_storage_state["cookies"][0]
    context = _install_fakes(monkeypatch, settings, cookie, kerberos_ok=True)
    provider, seen = _codes("000000", GOOD_CODE)

    state = await make_kerberos_collector(provider)(settings, None)

    assert state["cookies"] == [cookie]
    # The broker hop is resolved without following redirects …
    assert context.request.calls == [(f"https://auth.cern.ch{BROKER_HREF}", 0)]
    # … and only the Kerberos realm request carries the SPNEGO token.
    assert list(context.page.sent_headers) == [REALM_URL]
    assert context.page.sent_headers[REALM_URL]["authorization"] == TOKEN
    assert context.page.routes == []
    # A wrong code is reported back and a second one is asked for.
    assert context.page.filled == ["000000", GOOD_CODE]
    assert seen == [None, "Invalid authenticator code."]


async def test_kerberos_rejection_is_reported(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, valid_storage_state: dict[str, Any]
) -> None:
    _install_fakes(monkeypatch, settings, valid_storage_state["cookies"][0], kerberos_ok=False)
    provider, seen = _codes(GOOD_CODE)
    with pytest.raises(KerberosLoginError, match="Kerberos ticket"):
        await make_kerberos_collector(provider)(settings, None)
    assert seen == []


async def test_otp_attempts_are_bounded(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, valid_storage_state: dict[str, Any]
) -> None:
    _install_fakes(monkeypatch, settings, valid_storage_state["cookies"][0], kerberos_ok=True)
    provider, seen = _codes("111111", "222222", "333333", GOOD_CODE)
    with pytest.raises(InvalidSessionError):
        await make_kerberos_collector(provider)(settings, None)
    assert len(seen) == kerberos.MAX_OTP_ATTEMPTS


# --- OTP provider -----------------------------------------------------------


def test_normalise_code() -> None:
    assert normalise_code(" 123 456 ") == "123456"
    for bad in ("12345", "abcdef", "123456789"):
        with pytest.raises(ValueError):
            normalise_code(bad)


async def test_otp_flag_is_used_once() -> None:
    provide = make_otp_provider("123456", interactive=False)
    assert await provide(None) == "123456"
    with pytest.raises(InvalidSessionError):
        await provide("Invalid authenticator code.")


async def test_non_interactive_without_flag_explains_otp_option() -> None:
    provide = make_otp_provider(None, interactive=False)
    with pytest.raises(KerberosLoginError) as excinfo:
        await provide(None)
    assert "--otp" in (excinfo.value.instructions or "")


async def test_interactive_prompt_reprompts_and_shows_errors() -> None:
    answers = iter(["nope", "654321"])
    said: list[str] = []
    provide = make_otp_provider(
        None, interactive=True, prompt=lambda _: next(answers), say=said.append
    )
    assert await provide("Invalid authenticator code.") == "654321"
    assert said[0].startswith("Invalid authenticator code.")
    assert "6-digit" in said[1]


# --- CLI --------------------------------------------------------------------


def _collector_factory(state: dict[str, Any] | Exception) -> Any:
    calls: list[int] = []

    def factory(_provider: Any) -> Any:
        async def collect(_settings: Settings, _timeout: int | None) -> dict[str, Any]:
            calls.append(1)
            if isinstance(state, Exception):
                raise state
            return state

        return collect

    factory.calls = calls  # type: ignore[attr-defined]
    return factory


def test_cli_signs_in_and_stores_session(
    settings: Settings, valid_storage_state: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    factory = _collector_factory(valid_storage_state)
    assert run([], settings=settings, interactive=True, collector_factory=factory) == 0
    assert factory.calls == [1]
    assert settings.session_path.exists()
    out = capsys.readouterr().out
    assert "Signed in to CERN SSO" in out
    assert "sso-secret-value" not in out


def test_cli_keeps_valid_session_unless_forced(
    settings: Settings, valid_storage_state: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    factory = _collector_factory(valid_storage_state)
    assert run([], settings=settings, interactive=True, collector_factory=factory) == 0
    assert run([], settings=settings, interactive=True, collector_factory=factory) == 0
    assert factory.calls == [1]
    assert "Already signed in" in capsys.readouterr().out
    assert run(["--force"], settings=settings, interactive=True, collector_factory=factory) == 0
    assert factory.calls == [1, 1]


def test_cli_reports_failures_without_traceback(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    error = KerberosLoginError("No usable Kerberos ticket was found", instructions="Run kinit.")
    factory = _collector_factory(error)
    assert run([], settings=settings, interactive=True, collector_factory=factory) == 1
    err = capsys.readouterr().err
    assert "Login failed: No usable Kerberos ticket was found" in err
    assert "Run kinit." in err


def test_cli_rejects_malformed_otp_before_starting(settings: Settings) -> None:
    factory = _collector_factory({})
    assert run(["--otp", "12ab"], settings=settings, collector_factory=factory) == 2
    assert factory.calls == []
