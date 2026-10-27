"""Tests for cookie-only SSO site capture in cmcps.auth.browser."""

from __future__ import annotations

from pathlib import Path

from cmcps.auth import browser
from cmcps.config.settings import Settings


class _FakeContext:
    """Minimal stand-in for a Playwright BrowserContext cookie jar."""

    def __init__(self, cookies_by_url: dict[str, list[dict[str, str]]]) -> None:
        self._cookies = cookies_by_url

    async def cookies(self, url: str) -> list[dict[str, str]]:
        if isinstance(url, str):
            return list(self._cookies.get(url, []))
        return []


async def test_cds_session_ready_requires_openidc_cookie() -> None:
    logged_in = _FakeContext(
        {"https://cds.cern.ch": [{"name": "mod_auth_openidc_session", "value": "s"}]}
    )
    anonymous = _FakeContext(
        {"https://cds.cern.ch": [{"name": "INVENIOSESSIONstub", "value": "NO"}]}
    )
    assert await browser._cds_session_ready(logged_in) is True
    assert await browser._cds_session_ready(anonymous) is False


async def test_cms_alcm_session_ready_requires_openidc_cookie() -> None:
    logged_in = _FakeContext(
        {"https://cms-alcm.web.cern.ch": [{"name": "mod_auth_openidc_session", "value": "s"}]}
    )
    assert await browser._cms_alcm_session_ready(logged_in) is True
    assert await browser._cms_alcm_session_ready(_FakeContext({})) is False


async def test_cmsoms_session_ready_requires_openidc_cookie() -> None:
    logged_in = _FakeContext(
        {"https://cmsoms.cern.ch": [{"name": "mod_auth_openidc_session", "value": "s"}]}
    )
    assert await browser._cmsoms_session_ready(logged_in) is True
    assert await browser._cmsoms_session_ready(_FakeContext({})) is False


async def test_mattermost_session_ready_requires_mmathtoken() -> None:
    logged_in = _FakeContext(
        {"https://mattermost.web.cern.ch": [{"name": "MMAUTHTOKEN", "value": "s"}]}
    )
    anonymous = _FakeContext(
        {"https://mattermost.web.cern.ch": [{"name": "MMOAUTH", "value": "transient"}]}
    )
    assert await browser._mattermost_session_ready(logged_in) is True
    assert await browser._mattermost_session_ready(anonymous) is False


def test_cookie_only_sites_cover_restricted_hosts() -> None:
    urls = {url for url, _, _ in browser._COOKIE_ONLY_SITES}
    assert "https://cds.cern.ch/youraccount/login?ln=en" in urls
    assert "https://cms-alcm.web.cern.ch/" in urls
    assert "https://cmsoms.cern.ch/" in urls
    assert "https://mattermost.web.cern.ch/oauth/gitlab/login" in urls


def test_cookie_site_url_for_host_reverse_map() -> None:
    assert (
        browser.cookie_site_url_for_host("its.cern.ch")
        == "https://its.cern.ch/jira/login.jsp?os_destination=%2Fsecure%2FDashboard.jspa"
    )
    assert browser.cookie_site_url_for_host("cmsoms.cern.ch") == "https://cmsoms.cern.ch/"
    assert (
        browser.cookie_site_url_for_host("mattermost.web.cern.ch")
        == "https://mattermost.web.cern.ch/oauth/gitlab/login"
    )
    assert browser.cookie_site_url_for_host("gitlab.cern.ch") is None


async def test_warm_cookie_sites_no_targets_is_noop(tmp_path: Path) -> None:
    settings = Settings(state_dir=tmp_path, require_sso=False, browser_executable=None)
    result = await browser.warm_cookie_sites(
        settings, {"cookies": [], "origins": []}, ["https://example.edu"]
    )
    assert result is None
