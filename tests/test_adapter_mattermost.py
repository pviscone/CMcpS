"""Fixture-driven contract tests for the CERN Mattermost adapter."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cmcps.adapters import mattermost
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import AuthRequiredError, InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

TEAM_ID = "t" * 26
CHANNEL_ID = "c" * 26
POST_ID_1 = "p" * 25 + "1"
POST_ID_2 = "p" * 25 + "2"
POST_ID_3 = "p" * 25 + "3"
USER_ID = "u" * 26
FILE_ID = "f" * 26
NEXT_ID = "n" * 26

TEST_PAT = "mm-pat-secret"

USER_DOC = {
    "id": USER_ID,
    "create_at": 1_600_000_000_000,
    "update_at": 1_610_000_000_000,
    "delete_at": 0,
    "username": "jdoe",
    "auth_service": "gitlab",
    "email": "jdoe@example.com",
    "nickname": "Jane",
    "first_name": "Jane",
    "last_name": "Doe",
    "position": "Physicist",
    "roles": "system_user",
}

TEAM_DOC = {
    "id": TEAM_ID,
    "create_at": 1_500_000_000_000,
    "update_at": 1_500_000_000_000,
    "delete_at": 0,
    "display_name": "CERN",
    "name": "cern",
    "description": "CERN community",
    "type": "O",
    "invite_id": "x" * 26,
}

CHANNEL_DOC = {
    "id": CHANNEL_ID,
    "create_at": 1_550_000_000_000,
    "update_at": 1_550_000_000_000,
    "delete_at": 0,
    "team_id": TEAM_ID,
    "type": "O",
    "display_name": "Town Square",
    "name": "town-square",
    "header": "Channel header",
    "purpose": "Channel purpose",
    "last_post_at": 1_700_000_000_000,
    "total_msg_count": 123,
}

POST_DOC_1 = {
    "id": POST_ID_1,
    "create_at": 1_700_000_000_000,
    "update_at": 1_700_000_000_000,
    "edit_at": 0,
    "delete_at": 0,
    "is_pinned": False,
    "user_id": USER_ID,
    "channel_id": CHANNEL_ID,
    "root_id": "",
    "original_id": "",
    "message": "hello world",
    "type": "",
    "props": {},
    "hashtags": "#cms",
    "file_ids": [FILE_ID],
    "reply_count": 2,
}

POST_DOC_2 = {
    **POST_DOC_1,
    "id": POST_ID_2,
    "create_at": 1_700_000_100_000,
    "message": "a reply",
    "root_id": POST_ID_1,
    "hashtags": "",
    "file_ids": [],
    "reply_count": 0,
}

POST_DOC_3 = {
    **POST_DOC_1,
    "id": POST_ID_3,
    "create_at": 1_700_000_200_000,
    "message": "second reply",
    "root_id": POST_ID_1,
    "reply_count": 0,
}

FILE_INFO = {
    "id": FILE_ID,
    "user_id": USER_ID,
    "post_id": POST_ID_1,
    "create_at": 1_700_000_000_000,
    "update_at": 1_700_000_000_000,
    "delete_at": 0,
    "name": "plot.pdf",
    "extension": "pdf",
    "size": 11,
    "mime_type": "application/pdf",
}


class Router:
    """Route mock requests by (method, path); records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], Handler] = {}

    def add(self, method: str, path: str, handler: Handler) -> None:
        self.routes[(method, path)] = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get((request.method, request.url.path))
        if handler is None:
            return httpx.Response(404, json={"id": "unknown.route", "status_code": 404})
        return handler(request)


def make_client(tmp_path: Path, router: Router, *, token: str | None = None) -> SafeHttpClient:
    settings = Settings(
        state_dir=tmp_path,
        require_sso=False,
        browser_executable=None,
        mattermost_token=token,
    )
    auth = AuthService(settings)
    transport = httpx.MockTransport(router)
    return SafeHttpClient(
        settings, auth_service=auth, client=httpx.AsyncClient(transport=transport)
    )


async def test_get_me_shape(tmp_path: Path) -> None:
    router = Router()
    router.add("GET", "/api/v4/users/me", lambda _r: httpx.Response(200, json=USER_DOC))
    client = make_client(tmp_path, router)
    result = await mattermost.get_me(client, client.settings)
    assert result["source"] == "mattermost"
    assert result["user_id"] == USER_ID
    assert result["username"] == "jdoe"
    assert result["email"] == "jdoe@example.com"
    assert result["created_at"] == "2020-09-13T12:26:40Z"


async def test_session_expired_401_is_auth_required(tmp_path: Path) -> None:
    router = Router()
    router.add(
        "GET",
        "/api/v4/users/me",
        lambda _r: httpx.Response(
            401,
            json={"id": "api.context.session_expired.app_error", "status_code": 401},
        ),
    )
    client = make_client(tmp_path, router)
    with pytest.raises(AuthRequiredError):
        await mattermost.get_me(client, client.settings)


async def test_403_is_permission_error_not_auth_required(tmp_path: Path) -> None:
    router = Router()
    router.add(
        "GET",
        "/api/v4/users/me",
        lambda _r: httpx.Response(
            403, json={"id": "api.user.get_profile.permissions", "status_code": 403}
        ),
    )
    client = make_client(tmp_path, router)
    with pytest.raises(UpstreamError):
        await mattermost.get_me(client, client.settings)


async def test_get_user_modes(tmp_path: Path) -> None:
    router = Router()
    router.add(
        "GET", "/api/v4/users/username/jdoe", lambda _r: httpx.Response(200, json=USER_DOC)
    )
    router.add("GET", f"/api/v4/users/{USER_ID}", lambda _r: httpx.Response(200, json=USER_DOC))
    client = make_client(tmp_path, router)

    by_name = await mattermost.get_user(client, client.settings, username="jdoe")
    by_id = await mattermost.get_user(client, client.settings, user_id=USER_ID)
    assert by_name["username"] == by_id["username"] == "jdoe"
    assert [r.url.path for r in router.requests] == [
        "/api/v4/users/username/jdoe",
        f"/api/v4/users/{USER_ID}",
    ]

    with pytest.raises(InvalidInputError):
        await mattermost.get_user(client, client.settings)
    with pytest.raises(InvalidInputError):
        await mattermost.get_user(client, client.settings, username="jdoe", user_id=USER_ID)
    with pytest.raises(InvalidInputError):
        await mattermost.get_user(client, client.settings, user_id="not-an-id")
    assert len(router.requests) == 2


async def test_list_teams_returns_full_list_unpaged(tmp_path: Path) -> None:
    router = Router()

    def handler(request: httpx.Request) -> httpx.Response:
        # The CERN fork ignores paging; the adapter no longer sends it.
        assert "page" not in request.url.params
        assert "per_page" not in request.url.params
        return httpx.Response(200, json=[TEAM_DOC, {**TEAM_DOC, "id": "s" * 26, "name": "cms"}])

    router.add("GET", "/api/v4/users/me/teams", handler)
    client = make_client(tmp_path, router)
    result = await mattermost.list_teams(client, client.settings)
    assert result.has_more is False
    assert result.size == 2
    item = result.items[0]
    assert item["team_id"] == TEAM_ID
    assert item["name"] == "cern"
    assert item["url"] == "https://mattermost.web.cern.ch/cern"
    assert item["deleted"] is False


async def test_list_channels_filters_and_builds_urls(tmp_path: Path) -> None:
    dm_channel = {
        **CHANNEL_DOC,
        "id": "d" * 26,
        "team_id": "",
        "type": "D",
        "name": "a__b",
        "display_name": "Alice, Bob",
    }
    router = Router()
    router.add("GET", f"/api/v4/teams/{TEAM_ID}", lambda _r: httpx.Response(200, json=TEAM_DOC))
    router.add(
        "GET",
        f"/api/v4/users/me/teams/{TEAM_ID}/channels",
        lambda _r: httpx.Response(200, json=[CHANNEL_DOC, dm_channel]),
    )
    client = make_client(tmp_path, router)

    result = await mattermost.list_channels(client, client.settings, team_id=TEAM_ID)
    assert result.has_more is False
    # Direct conversations are filtered out: only channels of this team.
    assert [item["name"] for item in result.items] == ["town-square"]
    town = result.items[0]
    assert town["type"] == "O"
    assert town["url"] == "https://mattermost.web.cern.ch/cern/channels/town-square"
    assert town["last_post_at"] == "2023-11-14T22:13:20Z"

    filtered = await mattermost.list_channels(
        client, client.settings, team_id=TEAM_ID, name_contains="TOWN"
    )
    assert [item["name"] for item in filtered.items] == ["town-square"]

    with pytest.raises(InvalidInputError):
        await mattermost.list_channels(client, client.settings, team_id="bad")


async def test_get_channel_direct_message_has_no_team_url(tmp_path: Path) -> None:
    dm_channel = {
        **CHANNEL_DOC,
        "id": "d" * 26,
        "team_id": "",
        "type": "D",
        "name": "a__b",
        "display_name": "Alice, Bob",
    }
    router = Router()
    dm_id = "d" * 26
    router.add("GET", f"/api/v4/channels/{dm_id}", lambda _r: httpx.Response(200, json=dm_channel))
    client = make_client(tmp_path, router)
    result = await mattermost.get_channel(client, client.settings, channel_id=dm_id)
    assert result["type"] == "D"
    assert result["url"] is None
    # No team lookup is attempted for direct conversations.
    assert [r.url.path for r in router.requests] == [f"/api/v4/channels/{dm_id}"]


async def test_get_channel_modes(tmp_path: Path) -> None:
    router = Router()
    router.add(
        "GET", f"/api/v4/channels/{CHANNEL_ID}", lambda _r: httpx.Response(200, json=CHANNEL_DOC)
    )
    router.add(
        "GET",
        f"/api/v4/teams/{TEAM_ID}/channels/name/town-square",
        lambda _r: httpx.Response(200, json=CHANNEL_DOC),
    )
    router.add("GET", f"/api/v4/teams/{TEAM_ID}", lambda _r: httpx.Response(200, json=TEAM_DOC))
    client = make_client(tmp_path, router)

    by_id = await mattermost.get_channel(client, client.settings, channel_id=CHANNEL_ID)
    assert by_id["display_name"] == "Town Square"
    assert by_id["url"] == "https://mattermost.web.cern.ch/cern/channels/town-square"

    by_name = await mattermost.get_channel(
        client, client.settings, team_id=TEAM_ID, channel_name="town-square"
    )
    assert by_name["channel_id"] == CHANNEL_ID

    with pytest.raises(InvalidInputError):
        await mattermost.get_channel(client, client.settings)
    with pytest.raises(InvalidInputError):
        await mattermost.get_channel(client, client.settings, team_id=TEAM_ID)
    with pytest.raises(InvalidInputError):
        await mattermost.get_channel(
            client, client.settings, channel_id=CHANNEL_ID, team_id=TEAM_ID
        )


async def test_search_posts_posts_json_and_orders_results(tmp_path: Path) -> None:
    router = Router()
    router.add("GET", f"/api/v4/teams/{TEAM_ID}", lambda _r: httpx.Response(200, json=TEAM_DOC))
    captured: list[dict[str, object]] = []

    def search(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "order": [POST_ID_3, POST_ID_1],
                "posts": {POST_ID_1: POST_DOC_1, POST_ID_3: POST_DOC_3},
                "matches": {POST_ID_1: ["hello"], POST_ID_3: ["hello"]},
            },
        )

    router.add("POST", f"/api/v4/teams/{TEAM_ID}/posts/search", search)
    client = make_client(tmp_path, router)
    result = await mattermost.search_posts(
        client,
        client.settings,
        team_id=TEAM_ID,
        terms="hello world",
        is_or_search=True,
        page=2,
    )
    # Terms go over in full; only `page` drives paging (0-based backend).
    assert captured == [
        {
            "terms": "hello world",
            "is_or_search": True,
            "include_deleted_channels": False,
            "time_zone": "UTC",
            "page": 1,
        }
    ]
    # The CERN fork 401s on cookie-auth POSTs without this header.
    assert router.requests[-1].headers["X-Requested-With"] == "XMLHttpRequest"
    # Below the ~100-hit server cap: the result set is exhausted.
    assert result.has_more is False
    # Items follow the backend's order list, not dict order.
    assert [item["post_id"] for item in result.items] == [POST_ID_3, POST_ID_1]
    assert result.items[0]["url"] == f"https://mattermost.web.cern.ch/cern/pl/{POST_ID_3}"
    assert result.items[1]["file_ids"] == [FILE_ID]
    assert result.items[1]["created_at"] == "2023-11-14T22:13:20Z"


async def test_search_posts_at_cap_marks_has_more(tmp_path: Path) -> None:
    """A page hitting the server's ~100-hit clamp may have a follow-up page."""
    cap = mattermost._SEARCH_RESULT_CAP
    order = [f"{i:026}" for i in range(cap)]
    posts = {
        pid: {
            **POST_DOC_1,
            "id": pid,
            "message": "hit",
            "root_id": "",
            "file_ids": [],
        }
        for i, pid in enumerate(order)
    }
    router = Router()
    router.add("GET", f"/api/v4/teams/{TEAM_ID}", lambda _r: httpx.Response(200, json=TEAM_DOC))
    router.add(
        "POST",
        f"/api/v4/teams/{TEAM_ID}/posts/search",
        lambda _r: httpx.Response(200, json={"order": order, "posts": posts}),
    )
    client = make_client(tmp_path, router)
    result = await mattermost.search_posts(client, client.settings, team_id=TEAM_ID, terms="hit")
    assert len(result.items) == cap
    assert result.has_more is True


async def test_search_posts_rejects_empty_terms_and_bad_ids(tmp_path: Path) -> None:
    client = make_client(tmp_path, Router())
    with pytest.raises(InvalidInputError):
        await mattermost.search_posts(client, client.settings, team_id=TEAM_ID, terms="  ")
    with pytest.raises(InvalidInputError):
        await mattermost.search_posts(client, client.settings, team_id="bad", terms="x")


async def test_search_401_raises_auth_required(tmp_path: Path) -> None:
    router = Router()
    router.add("GET", f"/api/v4/teams/{TEAM_ID}", lambda _r: httpx.Response(200, json=TEAM_DOC))
    router.add(
        "POST",
        f"/api/v4/teams/{TEAM_ID}/posts/search",
        lambda _r: httpx.Response(
            401, json={"id": "api.context.session_expired.app_error", "status_code": 401}
        ),
    )
    client = make_client(tmp_path, router)
    with pytest.raises(AuthRequiredError):
        await mattermost.search_posts(client, client.settings, team_id=TEAM_ID, terms="x")


async def test_get_posts_cursors_and_paging(tmp_path: Path) -> None:
    router = Router()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["page"] == "2"
        assert request.url.params["per_page"] == "5"
        assert request.url.params["before"] == NEXT_ID
        return httpx.Response(
            200,
            json={
                "prev_post_id": "q" * 26,
                "next_post_id": NEXT_ID,
                "order": [POST_ID_2, POST_ID_1],
                "posts": {POST_ID_1: POST_DOC_1, POST_ID_2: POST_DOC_2},
            },
        )

    router.add("GET", f"/api/v4/channels/{CHANNEL_ID}/posts", handler)
    client = make_client(tmp_path, router)
    result = await mattermost.get_posts(
        client, client.settings, channel_id=CHANNEL_ID, page=3, per_page=5, before=NEXT_ID
    )
    # Older history exists ⇔ prev_post_id non-empty (verified against the live
    # server: on the newest page next_post_id is empty even with 100+ older
    # posts).
    assert result["has_more"] is True
    assert result["next_post_id"] == NEXT_ID
    assert result["prev_post_id"] == "q" * 26
    assert [item["post_id"] for item in result["posts"]] == [POST_ID_2, POST_ID_1]
    assert result["posts"][0]["root_id"] == POST_ID_1
    # No team known in this context: no permalink, only ids.
    assert result["posts"][0]["url"] is None


async def test_get_posts_has_more_follows_prev_cursor(tmp_path: Path) -> None:
    """Newest page: next_post_id empty while older posts exist → has_more
    must be true; exhausted history (both empty) → false."""
    cases = {
        "newest": ({"prev_post_id": "q" * 26, "next_post_id": ""}, True),
        "exhausted": ({"prev_post_id": "", "next_post_id": ""}, False),
    }

    for label, (cursors, expected) in cases.items():
        router = Router()
        router.add(
            "GET",
            f"/api/v4/channels/{CHANNEL_ID}/posts",
            lambda _r, cursors=cursors: httpx.Response(
                200,
                json={
                    **cursors,
                    "order": [POST_ID_1],
                    "posts": {POST_ID_1: POST_DOC_1},
                },
            ),
        )
        client = make_client(tmp_path, router)
        result = await mattermost.get_posts(client, client.settings, channel_id=CHANNEL_ID)
        assert result["has_more"] is expected, label


async def test_get_thread_root_first(tmp_path: Path) -> None:
    router = Router()
    router.add(
        "GET",
        f"/api/v4/posts/{POST_ID_1}/thread",
        lambda _r: httpx.Response(
            200,
            json={
                # The server's order list is not time-sorted (verified live);
                # the adapter must still return root first, then oldest first.
                "order": [POST_ID_1, POST_ID_3, POST_ID_2],
                "posts": {POST_ID_1: POST_DOC_1, POST_ID_2: POST_DOC_2, POST_ID_3: POST_DOC_3},
            },
        ),
    )
    client = make_client(tmp_path, router)
    result = await mattermost.get_thread(client, client.settings, post_id=POST_ID_1)
    assert result["reply_count"] == 2
    assert [item["post_id"] for item in result["posts"]] == [POST_ID_1, POST_ID_2, POST_ID_3]
    assert result["channel_id"] == CHANNEL_ID


async def test_get_thread_empty_raises(tmp_path: Path) -> None:
    router = Router()
    router.add(
        "GET",
        f"/api/v4/posts/{POST_ID_1}/thread",
        lambda _r: httpx.Response(200, json={"order": [], "posts": {}}),
    )
    client = make_client(tmp_path, router)
    with pytest.raises(UpstreamError):
        await mattermost.get_thread(client, client.settings, post_id=POST_ID_1)


async def test_download_file_sanitizes_name_and_writes(tmp_path: Path) -> None:
    router = Router()
    malicious = {**FILE_INFO, "name": "../../evil.pdf"}
    router.add(
        "GET", f"/api/v4/files/{FILE_ID}/info", lambda _r: httpx.Response(200, json=malicious)
    )
    router.add(
        "GET",
        f"/api/v4/files/{FILE_ID}",
        lambda _r: httpx.Response(200, content=b"%PDF-fixture"),
    )
    client = make_client(tmp_path, router)
    client.settings.ensure_directories()
    result = await mattermost.download_file(client, client.settings, file_id=FILE_ID)
    assert result["name"] == "evil.pdf"
    path = Path(result["path"])
    assert path.parent == client.settings.download_dir
    assert path.read_bytes() == b"%PDF-fixture"
    assert result["size_bytes"] == len(b"%PDF-fixture")
    assert result["mime_type"] == "application/pdf"
    assert [r.url.path for r in router.requests] == [
        f"/api/v4/files/{FILE_ID}/info",
        f"/api/v4/files/{FILE_ID}",
    ]


async def test_download_unknown_file_is_upstream_error(tmp_path: Path) -> None:
    router = Router()
    router.add(
        "GET",
        f"/api/v4/files/{FILE_ID}/info",
        lambda _r: httpx.Response(404, json={"id": "app.file.get.app_error", "status_code": 404}),
    )
    client = make_client(tmp_path, router)
    with pytest.raises(UpstreamError):
        await mattermost.download_file(client, client.settings, file_id=FILE_ID)


async def test_pat_header_reaches_get_and_post(tmp_path: Path) -> None:
    router = Router()
    router.add("GET", "/api/v4/users/me", lambda _r: httpx.Response(200, json=USER_DOC))
    router.add("GET", f"/api/v4/teams/{TEAM_ID}", lambda _r: httpx.Response(200, json=TEAM_DOC))
    router.add(
        "POST",
        f"/api/v4/teams/{TEAM_ID}/posts/search",
        lambda _r: httpx.Response(200, json={"order": [], "posts": {}}),
    )
    client = make_client(tmp_path, router, token=TEST_PAT)
    await mattermost.get_me(client, client.settings)
    await mattermost.search_posts(client, client.settings, team_id=TEAM_ID, terms="x")
    assert router.requests[0].headers["Authorization"] == f"Bearer {TEST_PAT}"
    assert router.requests[-1].headers["Authorization"] == f"Bearer {TEST_PAT}"
    assert router.requests[-1].method == "POST"
