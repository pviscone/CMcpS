"""Fixture-driven contract tests for the Discourse forums adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cmcps.adapters import discourse
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import AuthRequiredError, InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

SEARCH_PAYLOAD = {
    "posts": [
        {
            "id": 19380,
            "username": "rajotte",
            "created_at": "2007-05-17T15:39:41.000Z",
            "like_count": 0,
            "blurb": "Hi, I would like to create a histogram from a TTree...",
            "post_number": 1,
            "topic_id": 30804,
        }
    ],
    "topics": [
        {
            "id": 30804,
            "title": "TTree with TCut",
            "slug": "ttree-with-tcut",
            "category_id": 5,
            "posts_count": 3,
            "reply_count": 2,
        }
    ],
    "grouped_search_result": {"more_full_page_results": True, "more_posts": None},
}

LATEST_PAYLOAD = {
    "topic_list": {
        "more_topics_url": "/latest?page=1",
        "topics": [
            {
                "id": 30804,
                "title": "TTree with TCut",
                "slug": "ttree-with-tcut",
                "category_id": 5,
                "tags": ["tree"],
                "posts_count": 3,
                "reply_count": 2,
                "like_count": 1,
                "views": 42,
                "created_at": "2007-05-17T15:39:41.000Z",
                "last_posted_at": "2007-05-18T00:00:00.000Z",
            }
        ],
    }
}

TOPIC_PAYLOAD = {
    "id": 30804,
    "title": "TTree with TCut",
    "slug": "ttree-with-tcut",
    "category_id": 5,
    "tags": ["tree"],
    "created_at": "2007-05-17T15:39:41.000Z",
    "posts_count": 2,
    "reply_count": 1,
    "like_count": 1,
    "views": 42,
    "highest_post_number": 2,
    "post_stream": {
        "stream": [19380, 19381],
        "posts": [
            {
                "id": 19380,
                "post_number": 1,
                "username": "rajotte",
                "cooked": "<p>Hi, I want a <code>TTree</code>.</p><p>See "
                '<a href="/uploads/default/original/1/root.dat">data</a></p>',
                "created_at": "2007-05-17T15:39:41.000Z",
                "like_count": 0,
            },
            {
                "id": 19381,
                "post_number": 2,
                "username": "Axel",
                "cooked": "<p>Use <code>TTree::Project</code>.</p>",
                "created_at": "2007-05-17T16:00:00.000Z",
                "like_count": 5,
                "accepted_answer": True,
                "reply_to_post_number": 1,
            },
        ],
    },
}

POST_PAYLOAD = {
    "id": 19381,
    "post_number": 2,
    "username": "Axel",
    "cooked": "<p>Use <code>TTree::Project</code>.</p>",
    "raw": "Use `TTree::Project`.",
    "topic_id": 30804,
    "topic_slug": "ttree-with-tcut",
    "created_at": "2007-05-17T16:00:00.000Z",
    "like_count": 5,
}

USER_PAYLOAD = {
    "user": {
        "id": 33,
        "username": "Axel",
        "name": "Axel Naumann",
        "title": "ROOT dev",
        "bio_cooked": "<p>I work on ROOT.</p>",
        "created_at": "2006-01-01T00:00:00.000Z",
        "post_count": 100,
        "topic_count": 10,
        "likes_given": 5,
        "likes_received": 50,
        "trust_level": 4,
        "admin": True,
    }
}

CATEGORIES_PAYLOAD = {
    "category_list": {
        "categories": [
            {
                "id": 5,
                "name": "ROOT",
                "slug": "root",
                "description_excerpt": "ROOT questions",
                "topic_count": 100,
                "post_count": 500,
                "subcategory_ids": [],
                "read_restricted": False,
            }
        ]
    }
}


class Recorder:
    """Mock-transport handler that records every request."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def make_client(tmp_path: Path, recorder: Recorder) -> tuple[Settings, SafeHttpClient]:
    settings = Settings(state_dir=tmp_path, require_sso=False, browser_executable=None)
    auth = AuthService(settings)
    transport = httpx.MockTransport(recorder)
    client = SafeHttpClient(
        settings, auth_service=auth, client=httpx.AsyncClient(transport=transport)
    )
    return settings, client


def test_all_sites_known() -> None:
    assert set(discourse.SITES) == {
        "cms-talk",
        "cms-pub-talk",
        "root-forum",
        "geant4-forum",
    }


async def test_search_builds_url_and_envelope(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search.json"
        assert request.url.params["q"] == "TTree"
        assert request.url.params["page"] == "2"
        return httpx.Response(200, json=SEARCH_PAYLOAD)

    recorder = Recorder(handler)
    _settings, client = make_client(tmp_path, recorder)
    result = await discourse.search(client, site="root-forum", query="TTree", page=2)
    assert result.source == "discourse:root-forum"
    assert result.page == 2
    assert result.has_more is True
    item = result.items[0]
    assert item["post_id"] == 19380
    assert item["topic_id"] == 30804
    assert item["topic_title"] == "TTree with TCut"
    assert item["url"] == "https://root-forum.cern.ch/t/ttree-with-tcut/30804/1"


async def test_search_rejects_unknown_site(tmp_path: Path) -> None:
    recorder = Recorder(lambda _request: httpx.Response(200, json={}))
    _settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await discourse.search(client, site="example-org", query="x")
    assert recorder.requests == []


async def test_list_topics_category_and_top(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/c/5.json":
            return httpx.Response(200, json=LATEST_PAYLOAD)
        assert request.url.path == "/top.json"
        assert request.url.params["period"] == "weekly"
        return httpx.Response(200, json=LATEST_PAYLOAD)

    recorder = Recorder(handler)
    _settings, client = make_client(tmp_path, recorder)
    by_cat = await discourse.list_topics(client, site="root-forum", category_id=5)
    assert by_cat.source == "discourse:root-forum"
    assert by_cat.has_more is True
    assert by_cat.items[0]["url"] == "https://root-forum.cern.ch/t/ttree-with-tcut/30804"
    top = await discourse.list_topics(client, site="geant4-forum", listing="top", period="weekly")
    assert top.items[0]["topic_id"] == 30804


async def test_list_topics_rejects_bad_period(tmp_path: Path) -> None:
    recorder = Recorder(lambda _request: httpx.Response(200, json={}))
    _settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await discourse.list_topics(client, site="root-forum", listing="top", period="hourly")
    assert recorder.requests == []


async def test_get_topic_strips_html_and_finds_uploads(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/t/30804.json"
        return httpx.Response(200, json=TOPIC_PAYLOAD)

    recorder = Recorder(handler)
    _settings, client = make_client(tmp_path, recorder)
    result = await discourse.get_topic(client, site="root-forum", topic_id=30804)
    assert result["topic_id"] == 30804
    assert result["has_more"] is False
    first = result["posts"][0]
    assert first["text"] == "Hi, I want a TTree.\nSee data"
    assert first["uploads"] == ["https://root-forum.cern.ch/uploads/default/original/1/root.dat"]
    second = result["posts"][1]
    assert second["accepted_answer"] is True
    assert second["reply_to_post_number"] == 1


async def test_get_topic_upstream_error_on_missing_stream(tmp_path: Path) -> None:
    recorder = Recorder(lambda _request: httpx.Response(200, json={"unexpected": True}))
    _settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await discourse.get_topic(client, site="root-forum", topic_id=1)


async def test_get_post_includes_raw(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/posts/19381.json"
        # Anonymous requests on a protected site get the login HTML.
        return httpx.Response(200, json=POST_PAYLOAD)

    recorder = Recorder(handler)
    _settings, client = make_client(tmp_path, recorder)
    result = await discourse.get_post(client, site="root-forum", post_id=19381)
    assert result["raw"] == "Use `TTree::Project`."
    assert result["topic_id"] == 30804


async def test_get_post_html_body_raises_upstream(tmp_path: Path) -> None:
    recorder = Recorder(lambda _request: httpx.Response(200, content=b"<html>login</html>"))
    _settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await discourse.get_post(client, site="cms-talk", post_id=1)


async def test_get_user_summary(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/u/Axel.json"
        return httpx.Response(200, json=USER_PAYLOAD)

    recorder = Recorder(handler)
    _settings, client = make_client(tmp_path, recorder)
    result = await discourse.get_user(client, site="root-forum", username="Axel")
    assert result["bio"] == "I work on ROOT."
    assert result["post_count"] == 100
    assert result["url"] == "https://root-forum.cern.ch/u/Axel"


async def test_get_user_missing_raises_upstream(tmp_path: Path) -> None:
    recorder = Recorder(lambda _request: httpx.Response(404, json={"errors": ["nf"]}))
    _settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError):
        await discourse.get_user(client, site="root-forum", username="nobody")


async def test_list_categories(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/categories.json"
        return httpx.Response(200, json=CATEGORIES_PAYLOAD)

    recorder = Recorder(handler)
    _settings, client = make_client(tmp_path, recorder)
    result = await discourse.list_categories(client, site="root-forum")
    assert result["category_count"] == 1
    assert result["items"][0]["url"] == "https://root-forum.cern.ch/c/root/5"


async def test_download_rejects_foreign_host(tmp_path: Path) -> None:
    recorder = Recorder(lambda _request: httpx.Response(200, content=b"data"))
    _settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await discourse.download_file(
            client,
            site="root-forum",
            download_url="https://evil.example.com/file.bin",
        )
    assert recorder.requests == []


async def test_download_writes_bounded_file(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"binary-content")

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await discourse.download_file(
        client,
        site="root-forum",
        download_url="https://root-forum.cern.ch/uploads/default/original/1/root.dat",
    )
    saved = Path(result["path"])
    assert saved.read_bytes() == b"binary-content"
    assert result["size_bytes"] == len(b"binary-content")
    assert saved.parent == settings.download_dir


async def test_sso_site_403_raises_auth_required(tmp_path: Path) -> None:
    recorder = Recorder(lambda _request: httpx.Response(403, text="Forbidden"))
    _settings, client = make_client(tmp_path, recorder)
    for site in ("cms-talk", "cms-pub-talk"):
        with pytest.raises(AuthRequiredError):
            await discourse.search(client, site=site, query="x")


async def test_anonymous_site_error_stays_upstream(tmp_path: Path) -> None:
    recorder = Recorder(lambda _request: httpx.Response(403, text="Forbidden"))
    _settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError) as exc_info:
        await discourse.search(client, site="root-forum", query="x")
    assert exc_info.value.status_code == 403
