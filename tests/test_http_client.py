"""Policy-bound HTTP client tests using in-memory transports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from cmcps.auth.service import AuthService
from cmcps.auth.store import SessionStore
from cmcps.config.settings import Settings
from cmcps.contracts.errors import PolicyViolationError, RequestTooLargeError
from cmcps.net.client import SafeHttpClient


def _client_with_transport(
    handler,
    settings: Settings,
    *,
    authenticated_state: dict[str, Any] | None = None,
) -> tuple[SafeHttpClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def recorder(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    auth = AuthService(settings)
    if authenticated_state is not None:
        SessionStore(settings.session_path).save(authenticated_state)
    client = SafeHttpClient(
        settings,
        auth,
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
    )
    return client, requests


async def test_post_is_rejected_before_transport(settings: Settings) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    client, _ = _client_with_transport(handler, settings)
    with pytest.raises(PolicyViolationError):
        await client.request("POST", "https://service.cern.ch/data", authenticated=False)
    assert calls == []


async def test_sso_cookie_is_destination_scoped(
    settings: Settings,
    valid_storage_state: dict[str, Any],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"ok":true}',
            headers={"Set-Cookie": "server-secret=value"},
        )

    client, requests = _client_with_transport(
        handler,
        settings,
        authenticated_state=valid_storage_state,
    )
    response = await client.request("GET", "https://service.cern.ch/data")

    assert response.status_code == 200
    assert response.content == b'{"ok":true}'
    assert requests[0].headers["cookie"] == "SERVICE=service-secret-value"
    assert "set-cookie" not in response.headers


async def test_cross_domain_redirect_is_rejected_before_cookie_reuse(
    settings: Settings,
    valid_storage_state: dict[str, Any],
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://evil.example/collect"})

    client, requests = _client_with_transport(
        handler,
        settings,
        authenticated_state=valid_storage_state,
    )
    with pytest.raises(PolicyViolationError):
        await client.request("GET", "https://service.cern.ch/start")
    assert len(requests) == 1


async def test_allowlisted_redirect_is_followed(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/done"})
        return httpx.Response(200, content=b"done")

    settings = settings.model_copy(update={"require_sso": False})
    client, requests = _client_with_transport(handler, settings)
    response = await client.request(
        "GET",
        "https://service.cern.ch/start",
        authenticated=True,
    )
    assert response.content == b"done"
    assert [request.url.path for request in requests] == ["/start", "/done"]


async def test_response_size_limit_is_enforced(tmp_path: Path) -> None:
    settings = Settings(state_dir=tmp_path, require_sso=False, max_response_bytes=1024)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 1025)

    client, _ = _client_with_transport(handler, settings)
    with pytest.raises(RequestTooLargeError):
        await client.request("GET", "https://service.cern.ch/large")


async def test_download_is_bounded_and_owner_readable(tmp_path: Path) -> None:
    settings = Settings(state_dir=tmp_path / "state", require_sso=False)
    destination = settings.download_dir / "artifact.bin"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"binary-content")

    client, requests = _client_with_transport(handler, settings)
    result = await client.download(
        "https://service.cern.ch/file.bin",
        destination,
        authenticated=False,
    )
    assert result == destination
    assert result.read_bytes() == b"binary-content"
    assert (result.stat().st_mode & 0o777) == 0o600
    assert requests[0].method == "GET"
    assert "cookie" not in requests[0].headers


async def test_download_bounds_redirects_without_creating_artifact(tmp_path: Path) -> None:
    settings = Settings(state_dir=tmp_path / "state", require_sso=False)
    destination = settings.download_dir / "redirected.bin"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "/target"}, content=b"ignored")

    client, requests = _client_with_transport(handler, settings)
    with pytest.raises(PolicyViolationError):
        await client.download(
            "https://service.cern.ch/start",
            destination,
            authenticated=False,
        )
    assert len(requests) == settings.max_redirects + 1
    assert not destination.exists()
    assert list(settings.download_dir.glob(".redirected.bin.*")) == []


async def test_download_follows_redirect_and_writes_artifact(tmp_path: Path) -> None:
    settings = Settings(state_dir=tmp_path / "state", require_sso=False)
    destination = settings.download_dir / "redirected.bin"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/final"})
        assert request.url.path == "/final"
        return httpx.Response(200, content=b"payload")

    client, requests = _client_with_transport(handler, settings)
    final = await client.download(
        "https://service.cern.ch/start",
        destination,
        authenticated=False,
    )
    assert len(requests) == 2
    assert final.read_bytes() == b"payload"


async def test_download_collision_writes_unique_suffix_for_different_content(
    tmp_path: Path,
) -> None:
    settings = Settings(state_dir=tmp_path / "state", require_sso=False)
    settings.ensure_directories()
    original = settings.download_dir / "image.png"
    original.write_bytes(b"first-payload")
    destination = settings.download_dir / "image.png"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"second-payload")

    client, _ = _client_with_transport(handler, settings)
    final = await client.download(
        "https://service.cern.ch/image.png",
        destination,
        authenticated=False,
    )
    # A genuinely different payload with the same server name gets a suffix;
    # the original bytes are untouched.
    assert final == settings.download_dir / "image-1.png"
    assert final.read_bytes() == b"second-payload"
    assert original.read_bytes() == b"first-payload"


async def test_download_absorbs_identical_content_without_new_file(tmp_path: Path) -> None:
    settings = Settings(state_dir=tmp_path / "state", require_sso=False)
    settings.ensure_directories()
    original = settings.download_dir / "image.png"
    original.write_bytes(b"same-payload")
    destination = settings.download_dir / "image.png"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"same-payload")

    client, _ = _client_with_transport(handler, settings)
    final = await client.download(
        "https://service.cern.ch/image.png",
        destination,
        authenticated=False,
    )
    assert final == original
    assert list(settings.download_dir.glob("image*.png")) == [original]
    assert final.read_bytes() == b"same-payload"


async def test_download_reuses_matching_suffix_slot_and_grows_on_three_versions(
    tmp_path: Path,
) -> None:
    settings = Settings(state_dir=tmp_path / "state", require_sso=False)
    settings.ensure_directories()
    (settings.download_dir / "image.png").write_bytes(b"payload-A")
    (settings.download_dir / "image-1.png").write_bytes(b"payload-B")
    destination = settings.download_dir / "image.png"

    content = {"A": b"payload-A", "C": b"payload-C"}

    def handler(request: httpx.Request) -> httpx.Response:
        variant = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=content[variant])

    client, requests = _client_with_transport(handler, settings)
    again_a = await client.download(
        "https://service.cern.ch/dl/A", destination, authenticated=False
    )
    assert again_a == settings.download_dir / "image.png"
    version_c = await client.download(
        "https://service.cern.ch/dl/C", destination, authenticated=False
    )
    assert version_c == settings.download_dir / "image-2.png"
    assert len(requests) == 2
    assert {p.name for p in settings.download_dir.glob("image*.png")} == {
        "image.png",
        "image-1.png",
        "image-2.png",
    }


async def test_head_large_content_length_is_allowed(tmp_path: Path) -> None:
    settings = Settings(state_dir=tmp_path, require_sso=False, max_response_bytes=1024)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": "10485760"})

    client, requests = _client_with_transport(handler, settings)
    response = await client.request(
        "HEAD",
        "https://service.cern.ch/large-file",
        authenticated=False,
    )
    assert response.status_code == 200
    assert response.content == b""
    assert requests[0].method == "HEAD"


async def test_download_rejects_destination_outside_managed_dir(tmp_path: Path) -> None:
    settings = Settings(state_dir=tmp_path / "state", require_sso=False)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"must-not-arrive")

    client, requests = _client_with_transport(handler, settings)
    with pytest.raises(PolicyViolationError) as exc_info:
        await client.download(
            "https://service.cern.ch/file.bin",
            tmp_path / "outside.bin",
            authenticated=False,
        )
    assert requests == []
    assert str(settings.download_dir) not in (exc_info.value.instructions or "")
    assert not (tmp_path / "outside.bin").exists()


async def test_request_json_post_sends_json_body(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["content-type"].startswith("application/json")
        assert json.loads(request.content) == {"query": "certificate", "count": 10}
        return httpx.Response(201, json={"result": {"results": []}})

    client, requests = _client_with_transport(handler, settings)
    response = await client.request_json_post(
        "https://cern.service-now.com/api/now/sp/search",
        json_body={"query": "certificate", "count": 10},
        allowlist=("cern.service-now.com",),
        authenticated=False,
    )
    assert response.status_code == 201
    assert len(requests) == 1


async def test_request_json_post_rejects_unallowlisted_host(settings: Settings) -> None:
    client, requests = _client_with_transport(lambda _r: httpx.Response(200), settings)
    with pytest.raises(PolicyViolationError):
        await client.request_json_post(
            "https://evil.cern.ch/anything",
            json_body={"x": 1},
            allowlist=("cern.service-now.com",),
            authenticated=False,
        )
    assert requests == []


async def test_request_warms_site_and_retries_once(
    settings: Settings, valid_storage_state: dict[str, Any], future_epoch: float
) -> None:
    """A 401 from a mapped site triggers one headless warm, then retries with fresh cookies."""
    warmed: list[list[str]] = []

    async def site_warmer(
        _: Settings, _state: dict[str, Any], urls: list[str] | tuple[str, ...]
    ) -> dict[str, Any]:
        warmed.append(list(urls))
        return {
            "cookies": [
                *valid_storage_state["cookies"],
                {
                    "name": "TWIKISID",
                    "value": "fresh",
                    "domain": ".twiki.cern.ch",
                    "path": "/",
                    "expires": future_epoch,
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "Lax",
                },
            ],
            "origins": [],
        }

    def handler(request: httpx.Request) -> httpx.Response:
        if "TWIKISID" in request.headers.get("Cookie", ""):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(401)

    requests: list[httpx.Request] = []

    def recorder(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    auth = AuthService(settings, site_warmer=site_warmer)
    SessionStore(settings.session_path).save(valid_storage_state)
    client = SafeHttpClient(
        settings,
        auth,
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
    )
    response = await client.request("GET", "https://twiki.cern.ch/twiki/bin/view/CMS/WebHome")
    assert response.status_code == 200
    assert len(requests) == 2
    assert warmed == [["https://twiki.cern.ch/twiki/bin/viewauth/CMS/WebHome"]]


async def test_request_unmapped_host_401_never_warms(
    settings: Settings, valid_storage_state: dict[str, Any]
) -> None:
    """A 401 from a host without a mapped cookie site passes through untouched."""
    warmed: list[list[str]] = []

    async def site_warmer(
        _: Settings, _state: dict[str, Any], urls: list[str] | tuple[str, ...]
    ) -> dict[str, Any]:
        warmed.append(list(urls))
        return {}

    auth = AuthService(settings, site_warmer=site_warmer)
    SessionStore(settings.session_path).save(valid_storage_state)
    requests: list[httpx.Request] = []
    client = SafeHttpClient(
        settings,
        auth,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: (requests.append(request), httpx.Response(403))[1]
            )
        ),
    )
    response = await client.request("GET", "https://gitlab.cern.ch/api/v4/projects")
    assert response.status_code == 403
    assert len(requests) == 1
    assert warmed == []


async def test_request_unauthenticated_401_never_warms(
    settings: Settings, valid_storage_state: dict[str, Any]
) -> None:
    """Unauthenticated requests (no SSO header) never trigger the warm path."""
    warmed: list[list[str]] = []

    async def site_warmer(
        _: Settings, _state: dict[str, Any], urls: list[str] | tuple[str, ...]
    ) -> dict[str, Any]:
        warmed.append(list(urls))
        return {}

    auth = AuthService(settings, site_warmer=site_warmer)
    SessionStore(settings.session_path).save(valid_storage_state)
    requests: list[httpx.Request] = []
    client = SafeHttpClient(
        settings,
        auth,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: (requests.append(request), httpx.Response(401))[1]
            )
        ),
    )
    response = await client.request(
        "GET", "https://twiki.cern.ch/twiki/bin/view/CMS/WebHome", authenticated=False
    )
    assert response.status_code == 401
    assert len(requests) == 1
    assert warmed == []


async def test_json_post_warms_site_and_retries_once(
    settings: Settings, valid_storage_state: dict[str, Any], future_epoch: float
) -> None:
    """Read-only JSON POST searches self-heal like GETs when a mapped site 401s."""
    warmed: list[list[str]] = []

    async def site_warmer(
        _: Settings, _state: dict[str, Any], urls: list[str] | tuple[str, ...]
    ) -> dict[str, Any]:
        warmed.append(list(urls))
        return {
            "cookies": [
                *valid_storage_state["cookies"],
                {
                    "name": "MMAUTHTOKEN",
                    "value": "fresh",
                    "domain": ".mattermost.web.cern.ch",
                    "path": "/",
                    "expires": future_epoch,
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "Lax",
                },
            ],
            "origins": [],
        }

    def handler(request: httpx.Request) -> httpx.Response:
        if "MMAUTHTOKEN" in request.headers.get("Cookie", ""):
            return httpx.Response(200, json={"order": [], "posts": {}})
        return httpx.Response(401)

    requests: list[httpx.Request] = []

    def recorder(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    auth = AuthService(settings, site_warmer=site_warmer)
    SessionStore(settings.session_path).save(valid_storage_state)
    client = SafeHttpClient(
        settings,
        auth,
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
    )
    response = await client.request_json_post(
        "https://mattermost.web.cern.ch/api/v4/teams/1/posts/search",
        json_body={"terms": "jet"},
        allowlist=("mattermost.web.cern.ch",),
    )
    assert response.status_code == 200
    assert len(requests) == 2
    assert warmed == [["https://mattermost.web.cern.ch/oauth/gitlab/login"]]


async def test_json_post_sso_bounce_warms_site_and_retries_once(
    settings: Settings, valid_storage_state: dict[str, Any], future_epoch: float
) -> None:
    """An oauth2-proxy bounce to the Keycloak authorize endpoint (any status)
    triggers the same warm-and-retry as a 401: the cold-cookie Run Registry case."""
    warmed: list[list[str]] = []

    async def site_warmer(
        _: Settings, _state: dict[str, Any], urls: list[str] | tuple[str, ...]
    ) -> dict[str, Any]:
        warmed.append(list(urls))
        return {
            "cookies": [
                *valid_storage_state["cookies"],
                {
                    "name": "_oauth2_proxy_0",
                    "value": "fresh",
                    "domain": ".cmsrunregistry.web.cern.ch",
                    "path": "/",
                    "expires": future_epoch,
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "Lax",
                },
            ],
            "origins": [],
        }

    def handler(request: httpx.Request) -> httpx.Response:
        if "_oauth2_proxy_0" in request.headers.get("Cookie", ""):
            return httpx.Response(200, json={"data": []})
        if request.url.host == "auth.cern.ch":
            # The oauth2-proxy bounce ends here: Keycloak answers 400 mid-handshake.
            return httpx.Response(400)
        # Cookie-less call redirected onto the authorize endpoint.
        return httpx.Response(
            302,
            headers={
                "location": "https://auth.cern.ch/auth/realms/cern/protocol/openid-connect/auth?x=1"
            },
        )

    requests: list[httpx.Request] = []

    def recorder(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    auth = AuthService(settings, site_warmer=site_warmer)
    SessionStore(settings.session_path).save(valid_storage_state)
    client = SafeHttpClient(
        settings,
        auth,
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
    )
    response = await client.request_json_post(
        "https://cmsrunregistry.web.cern.ch/api/runs_filtered_ordered",
        json_body={"page": 0, "page_size": 1, "filter": {}, "sortings": []},
        allowlist=("cmsrunregistry.web.cern.ch",),
    )
    assert response.status_code == 200
    hosts = [request.url.host for request in requests]
    assert hosts == ["cmsrunregistry.web.cern.ch", "auth.cern.ch", "cmsrunregistry.web.cern.ch"]
    assert warmed == [["https://cmsrunregistry.web.cern.ch/"]]
