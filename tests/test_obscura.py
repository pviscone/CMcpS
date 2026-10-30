"""Bundled-obscura resolution, launch routing, and CDP lifecycle tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cmcps.auth import browser as browser_module
from cmcps.config.settings import Settings
from cmcps.contracts.errors import BrowserUnavailableError


def _settings(tmp_path: Path, **kwargs: Any) -> Settings:
    return Settings(
        state_dir=tmp_path,
        browser_executable=None,
        browser_type="chromium",
        **kwargs,
    )


def _fake_platform_tag(monkeypatch: Any, tag: str | None = None) -> str:
    real = browser_module._platform_tag()
    monkeypatch.setattr(browser_module, "_platform_tag", lambda: tag or real)
    return tag or real


class _FakeLauncher:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[bool, str | None]] = []

    async def launch(self, headless: bool, executable_path: str | None) -> str:
        self.calls.append((headless, executable_path))
        return f"{self.name}-browser"


class _FakePlaywright:
    def __init__(self) -> None:
        self.chromium = _FakeLauncher("chromium")
        self.firefox = _FakeLauncher("firefox")


def test_bundled_executable_requires_matching_platform_tag(
    monkeypatch: Any, tmp_path: Path
) -> None:
    bundled = tmp_path / "_obscura"
    bundled.mkdir()
    binary = bundled / "obscura"
    binary.write_bytes(b"#!/bin/sh\n")
    tag = _fake_platform_tag(monkeypatch)
    (bundled / "PLATFORM").write_text(f"{tag}\n")
    monkeypatch.setattr(browser_module, "_BUNDLED_OBSCURA_DIR", bundled)
    assert browser_module.bundled_obscura_executable() == binary

    (bundled / "PLATFORM").write_text("darwin-arm64-ish-other\n")
    assert browser_module.bundled_obscura_executable() is None

    (bundled / "PLATFORM").write_text(f"{tag}\n")
    binary.unlink()
    assert browser_module.bundled_obscura_executable() is None


def test_bundled_executable_absent_in_source_checkout(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(browser_module, "_BUNDLED_OBSCURA_DIR", tmp_path / "missing")
    assert browser_module.bundled_obscura_executable() is None


def test_obscura_executable_override_wins(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(browser_module, "_BUNDLED_OBSCURA_DIR", tmp_path / "missing")
    settings = _settings(tmp_path, obscura_executable="/opt/obscura-custom")
    assert browser_module._obscura_executable(settings) == Path("/opt/obscura-custom")


async def test_headless_prefers_obscura_launcher(monkeypatch: Any, tmp_path: Path) -> None:
    calls: list[Path] = []

    async def fake_launch(_playwright: Any, executable: Path) -> str:
        calls.append(executable)
        return "obscura-browser"

    monkeypatch.setattr(browser_module, "_launch_obscura", fake_launch)
    playwright = _FakePlaywright()
    settings = _settings(tmp_path, obscura_executable="/opt/obscura")
    result = await browser_module._launch_browser(settings, playwright, headless=True)
    assert result == "obscura-browser"
    assert calls == [Path("/opt/obscura")]
    assert playwright.chromium.calls == []  # system Chromium never launched


async def test_visible_never_uses_obscura(monkeypatch: Any, tmp_path: Path) -> None:
    async def fail_launch(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("obscura is headless-only and must not serve the SSO login")

    monkeypatch.setattr(browser_module, "_launch_obscura", fail_launch)
    playwright = _FakePlaywright()
    settings = _settings(tmp_path, obscura_executable="/opt/obscura")
    browser = await browser_module._launch_browser(settings, playwright, headless=False)
    assert browser == "chromium-browser"
    assert playwright.chromium.calls == [(False, None)]


async def test_headless_falls_back_when_bundled_missing(monkeypatch: Any, tmp_path: Path) -> None:
    async def fail_launch(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("no obscura binary available; must not launch it")

    monkeypatch.setattr(browser_module, "_launch_obscura", fail_launch)
    monkeypatch.setattr(browser_module, "_BUNDLED_OBSCURA_DIR", tmp_path / "missing")
    playwright = _FakePlaywright()
    result = await browser_module._launch_browser(_settings(tmp_path), playwright, headless=True)
    assert result == "chromium-browser"
    assert playwright.chromium.calls == [(True, None)]


async def test_headless_chromium_setting_skips_obscura(monkeypatch: Any, tmp_path: Path) -> None:
    async def fail_launch(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("CMCPS_HEADLESS_BROWSER=chromium must not touch obscura")

    monkeypatch.setattr(browser_module, "_launch_obscura", fail_launch)
    playwright = _FakePlaywright()
    settings = _settings(tmp_path, headless_browser="chromium", obscura_executable="/opt/obscura")
    result = await browser_module._launch_browser(settings, playwright, headless=True)
    assert result == "chromium-browser"


class _FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.returncode = 0
        return 0


class _FakeCDPBrowser:
    def __init__(self) -> None:
        self.listeners: dict[str, list[Any]] = {}
        self.closed = False

    def on(self, event: str, callback: Any) -> None:
        self.listeners.setdefault(event, []).append(callback)

    async def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, browser: _FakeCDPBrowser) -> None:
        self._browser = browser
        self.endpoint: str | None = None

    async def connect_over_cdp(self, endpoint: str) -> _FakeCDPBrowser:
        self.endpoint = endpoint
        return self._browser


class _FakeCDPPlaywright:
    def __init__(self, chromium: _FakeChromium) -> None:
        self.chromium = chromium


def _patch_launch_obscura_deps(
    monkeypatch: Any,
    *,
    ready: bool,
) -> tuple[list[tuple[Any, ...]], _FakeCDPBrowser, _FakeProcess, _FakeCDPPlaywright]:
    spawned: list[tuple[Any, ...]] = []
    process = _FakeProcess()
    fake_browser = _FakeCDPBrowser()

    async def fake_exec(*args: Any, **_kwargs: Any) -> _FakeProcess:
        spawned.append(args)
        return process

    async def fake_ready(_endpoint: str, _timeout: float) -> bool:
        return ready

    monkeypatch.setattr(browser_module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(browser_module, "_wait_obscura_ready", fake_ready)
    monkeypatch.setattr(browser_module, "_free_loopback_port", lambda: 48123)
    chromium = _FakeChromium(fake_browser)
    return spawned, fake_browser, process, _FakeCDPPlaywright(chromium)


async def test_launch_obscura_lifecycle(monkeypatch: Any, tmp_path: Path) -> None:
    spawned, fake_browser, process, playwright = _patch_launch_obscura_deps(monkeypatch, ready=True)
    browser = await browser_module._launch_obscura(playwright, tmp_path / "obscura")
    assert browser is fake_browser
    binary, subcommand, flag, port = spawned[0][:4]
    assert binary == str(tmp_path / "obscura")
    assert (subcommand, flag, port) == ("serve", "--port", "48123")
    assert playwright.chromium.endpoint == "http://127.0.0.1:48123"

    # Disconnecting the Playwright browser terminates the serve process.
    assert id(fake_browser) in browser_module._OBSCURA_PROCESSES
    disconnect = fake_browser.listeners["disconnected"][0]
    disconnect()
    for _ in range(3):
        await browser_module.asyncio.sleep(0)
    assert process.terminated
    assert id(fake_browser) not in browser_module._OBSCURA_PROCESSES


async def test_close_resource_stops_obscura_serve(monkeypatch: Any, tmp_path: Path) -> None:
    """The normal close path stops the serve process synchronously."""
    _spawned, fake_browser, process, playwright = _patch_launch_obscura_deps(
        monkeypatch, ready=True
    )
    browser = await browser_module._launch_obscura(playwright, tmp_path / "obscura")
    monkeypatch.setattr(browser_module, "Browser", _FakeCDPBrowser)
    await browser_module._close_resource(browser, "test browser")
    assert fake_browser.closed
    assert process.terminated
    assert id(fake_browser) not in browser_module._OBSCURA_PROCESSES


async def test_launch_obscura_not_ready_kills_process(monkeypatch: Any, tmp_path: Path) -> None:
    _spawned, _fake_browser, process, playwright = _patch_launch_obscura_deps(
        monkeypatch, ready=False
    )
    with pytest.raises(BrowserUnavailableError) as excinfo:
        await browser_module._launch_obscura(playwright, tmp_path / "obscura")
    assert "CMCPS_HEADLESS_BROWSER" in (excinfo.value.instructions or "")
    assert process.terminated
    assert playwright.chromium.endpoint is None  # never attached
