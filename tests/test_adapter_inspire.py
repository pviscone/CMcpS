"""Fixture-driven contract tests for the INSPIRE-HEP adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from cmcps.adapters import inspire
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

PAPER_METADATA: dict[str, Any] = {
    "titles": [{"title": "Study of a Very Generic Process"}],
    "authors": [
        {
            "full_name": "Doe, Jane",
            "affiliations": [{"value": "CERN"}],
            "ids": [{"schema": "INSPIRE BAI", "value": "J.Doe.1"}],
        },
        {"full_name": "Smith, John", "affiliations": []},
    ],
    "abstracts": [{"value": "We study a very generic process."}],
    "arxiv_eprints": [{"value": "2401.12345", "categories": ["hep-ex"]}],
    "dois": [{"value": "10.1007/JHEP05(2020)123"}],
    "publication_info": [
        {"journal_title": "JHEP", "journal_volume": "05", "page_start": "123", "year": 2020}
    ],
    "collaborations": [{"value": "CMS"}],
    "citation_count": 42,
    "citation_count_without_self_citations": 37,
    "earliest_date": "2020-01-30",
    "document_type": ["article"],
    "keywords": [{"value": "colliders"}],
    "inspire_categories": [{"term": "Experiment-HEP"}],
    "texkey": "Doe:2020abc",
    "report_numbers": [{"value": "CMS-PAS-EXO-20-001"}],
    "number_of_pages": 21,
    "references": [
        {"record": {"$ref": "https://inspirehep.net/api/literature/111"}, "texkey": "ref-a"},
        {"texkey": "ref-b"},
    ],
    "documents": [{"key": "paper.pdf", "url": "https://inspirehep.net/files/paper.pdf"}],
    "figures": [
        {"caption": "Figure 1", "url": "https://inspirehep.net/files/fig1.png"},
        {"url": "https://inspirehep.net/files/fig2.png"},
    ],
}

PAPER_HIT = {"id": 1813416, "metadata": PAPER_METADATA, "links": {"self": "/literature/1813416"}}

AUTHOR_HITS = {
    "hits": {
        "total": 1,
        "hits": [
            {
                "id": 1020304,
                "metadata": {
                    "name": {"value": "Doe, Jane", "preferred_name": "Jane Doe"},
                    "ids": [{"schema": "INSPIRE BAI", "value": "J.Doe.1"}],
                },
            }
        ],
    }
}


def literature_hits(
    metadatas: list[dict[str, Any]], *, start_id: int = 1, total: int | None = None
) -> dict[str, Any]:
    hits = [
        {"id": start_id + index, "metadata": metadata} for index, metadata in enumerate(metadatas)
    ]
    return {"hits": {"total": total if total is not None else len(hits), "hits": hits}}


class Recorder:
    """Mock-transport handler that records every request."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def make_client(tmp_path: Path, recorder: Recorder) -> tuple[Settings, SafeHttpClient]:
    settings = Settings(
        state_dir=tmp_path,
        require_sso=False,
        browser_executable=None,
    )
    auth = AuthService(settings)
    transport = httpx.MockTransport(recorder)
    client = SafeHttpClient(
        settings, auth_service=auth, client=httpx.AsyncClient(transport=transport)
    )
    return settings, client


async def test_search_papers_builds_url_and_envelope(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/literature"
        params = request.url.params
        assert params["q"] == "a doe"
        assert params["sort"] == "mostcited"
        assert params["size"] == "5"
        assert params["page"] == "2"
        return httpx.Response(200, json=literature_hits([PAPER_METADATA], total=17))

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await inspire.search_papers(
        client, settings, query="a doe", sort="mostcited", size=5, page=2
    )
    assert result.source == "inspire"
    assert result.total_count == 17
    assert result.page == 2
    assert result.size == 5
    assert result.has_more is True
    assert result.query == "a doe"
    assert result.sort == "mostcited"
    item = result.items[0]
    assert item["inspire_id"] == "1"
    assert item["title"] == "Study of a Very Generic Process"
    assert item["authors"] == [
        {"full_name": "Doe, Jane", "affiliations": ["CERN"]},
        {"full_name": "Smith, John", "affiliations": []},
    ]
    assert item["total_authors"] == 2
    assert item["arxiv_id"] == "2401.12345"
    assert item["arxiv_categories"] == ["hep-ex"]
    assert item["doi"] == "10.1007/JHEP05(2020)123"
    assert item["publication"] == {
        "journal_title": "JHEP",
        "journal_volume": "05",
        "page_start": "123",
        "year": 2020,
    }
    assert item["collaborations"] == ["CMS"]
    assert item["citation_count"] == 42
    assert item["date"] == "2020-01-30"
    assert item["inspire_url"] == "https://inspirehep.net/api/literature/1"


async def test_search_papers_rejects_bad_sort(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(200, json={}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await inspire.search_papers(client, settings, query="x", sort="random")
    assert recorder.requests == []


async def test_get_paper_routes_by_identifier_priority(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(200, json=PAPER_HIT))
    settings, client = make_client(tmp_path, recorder)

    detail = await inspire.get_paper(client, settings, inspire_id="1813416")
    assert recorder.requests[0].url.path == "/api/literature/1813416"
    assert detail["texkey"] == "Doe:2020abc"
    assert detail["citation_count_without_self_citations"] == 37
    assert detail["document_type"] == ["article"]
    assert detail["report_numbers"] == ["CMS-PAS-EXO-20-001"]
    assert detail["references_count"] == 2
    assert detail["urls"]["arxiv_abs"] == "https://arxiv.org/abs/2401.12345"
    assert detail["urls"]["doi"] == "https://doi.org/10.1007/JHEP05(2020)123"
    assert detail["urls"]["bibtex"].endswith("/literature/1813416?format=bibtex")

    await inspire.get_paper(client, settings, arxiv_id="arxiv:2401.12345")
    assert recorder.requests[1].url.path == "/api/arxiv/2401.12345"

    await inspire.get_paper(client, settings, arxiv_id="https://arxiv.org/abs/2401.12345")
    assert recorder.requests[2].url.path == "/api/arxiv/2401.12345"

    await inspire.get_paper(client, settings, doi="https://doi.org/10.1007/JHEP05(2020)123")
    assert recorder.requests[3].url.path == "/api/doi/10.1007/JHEP05(2020)123"

    # Priority: inspire_id wins over arxiv_id wins over doi.
    await inspire.get_paper(client, settings, inspire_id="9", arxiv_id="2401.12345", doi="10.1/x")
    assert recorder.requests[4].url.path == "/api/literature/9"
    await inspire.get_paper(client, settings, arxiv_id="2401.12345", doi="10.1/x")
    assert recorder.requests[5].url.path == "/api/arxiv/2401.12345"

    assert {request.method for request in recorder.requests} == {"GET"}


async def test_get_paper_requires_identifier(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(200, json={}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await inspire.get_paper(client, settings)
    assert recorder.requests == []


async def test_get_citations_query_syntax_and_timeline(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        direction = params["q"]
        if direction.startswith("refersto:"):
            payload = literature_hits(
                [
                    {"citation_count": 1, "earliest_date": "2021-05-01"},
                    {"citation_count": 2, "publication_info": [{"year": 2021}]},
                    {"citation_count": 3, "earliest_date": "2023-01-01"},
                ],
                start_id=900,
                total=40,
            )
        else:
            payload = literature_hits(
                [{"citation_count": 0, "earliest_date": "2019-03-02"}], start_id=800
            )
        return httpx.Response(200, json=payload)

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    citing = await inspire.get_citations(client, settings, inspire_id="123")
    assert recorder.requests[0].url.params["q"] == "refersto:recid:123"
    assert citing["direction"] == "citing"
    assert citing["total_count"] == 40
    assert citing["timeline"] == {"2021": 2, "2023": 1}
    assert citing["papers"].total_count == 40
    assert citing["papers"].has_more is False

    cited_by = await inspire.get_citations(client, settings, inspire_id="123", direction="cited_by")
    assert recorder.requests[1].url.params["q"] == "citedby:recid:123"
    assert cited_by["timeline"] == {"2019": 1}

    with pytest.raises(InvalidInputError):
        await inspire.get_citations(client, settings, inspire_id="123", direction="both")
    assert len(recorder.requests) == 2


async def test_get_author_papers_resolves_bai_and_metrics(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/authors":
            assert request.url.params["q"] == "Jane Doe"
            assert request.url.params["size"] == "1"
            return httpx.Response(200, json=AUTHOR_HITS)
        assert request.url.path == "/api/literature"
        assert request.url.params["q"] == "a J.Doe.1"
        assert request.url.params["sort"] == "mostrecent"
        return httpx.Response(
            200,
            json=literature_hits(
                [
                    {"citation_count": 5, "earliest_date": "2020-01-01"},
                    {"citation_count": 3, "earliest_date": "2021-01-01"},
                    {"citation_count": 1, "earliest_date": "2022-01-01"},
                ],
                start_id=7,
            ),
        )

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await inspire.get_author_papers(client, settings, author_name="Jane Doe")
    assert result["author"] == {
        "name": "Doe, Jane",
        "preferred_name": "Jane Doe",
        "bai": "J.Doe.1",
        "inspire_author_id": "1020304",
    }
    assert result["papers"].query == "a J.Doe.1"
    metrics = result["metrics"]
    assert metrics["total_citations"] == 9
    assert metrics["average_citations"] == 3.0
    # Sorted counts [5, 3, 1]: 5 >= 1, 3 >= 2, 1 < 3 → h-index 2.
    assert metrics["h_index"] == 2
    assert "returned page" in metrics["h_index_note"]
    assert [request.url.path for request in recorder.requests] == [
        "/api/authors",
        "/api/literature",
    ]
    assert all(request.method == "GET" for request in recorder.requests)


async def test_get_author_papers_direct_bai_skips_resolution(tmp_path: Path) -> None:
    recorder = Recorder(
        lambda _: httpx.Response(200, json=literature_hits([{"citation_count": 0}]))
    )
    settings, client = make_client(tmp_path, recorder)
    result = await inspire.get_author_papers(client, settings, author_id="J.Doe.1")
    assert result["author"]["bai"] == "J.Doe.1"
    assert len(recorder.requests) == 1
    assert recorder.requests[0].url.params["q"] == "a J.Doe.1"


async def test_get_author_papers_bai_resolution_failure(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(200, json={"hits": {"total": 0, "hits": []}}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await inspire.get_author_papers(client, settings, author_name="Nobody Here")

    no_bai = {
        "hits": {
            "total": 1,
            "hits": [{"id": 1, "metadata": {"name": {"value": "Doe, Jane"}, "ids": []}}],
        }
    }
    recorder2 = Recorder(lambda _: httpx.Response(200, json=no_bai))
    settings2, client2 = make_client(tmp_path, recorder2)
    with pytest.raises(InvalidInputError):
        await inspire.get_author_papers(client2, settings2, author_name="Jane Doe")


async def test_get_author_papers_requires_identifier(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(200, json={}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await inspire.get_author_papers(client, settings)
    assert recorder.requests == []


async def test_search_collaboration_alias_year_and_stats(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        assert params["q"] == "collaboration:ATLAS and date 2024"
        assert params["sort"] == "mostrecent"
        payload = literature_hits(
            [
                {"citation_count": 10, "publication_info": [{"year": 2024}]},
                {"citation_count": 30, "earliest_date": "2024-06-01"},
                {"citation_count": 5, "earliest_date": "2023-11-11"},
                {"citation_count": 1},
            ]
        )
        return httpx.Response(200, json=payload)

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await inspire.search_collaboration(
        client, settings, collaboration_name="atlas", year=2024
    )
    assert result["collaboration"] == "ATLAS"
    assert result["year"] == 2024
    assert result["statistics"]["year_distribution"] == {"2023": 1, "2024": 2}
    assert result["statistics"]["total_citations"] == 46
    top = result["top_cited_papers"]
    assert [paper["citation_count"] for paper in top] == [30, 10, 5, 1]


async def test_search_collaboration_unknown_name_uppercased(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "collaboration:DUNE"
        return httpx.Response(200, json=literature_hits([]))

    settings, client = make_client(tmp_path, Recorder(handler))
    result = await inspire.search_collaboration(client, settings, collaboration_name="dune")
    assert result["collaboration"] == "DUNE"


async def test_get_paper_figures_request_and_shape(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/literature/1813416"
        assert request.url.params["fields"] == "figures,titles"
        return httpx.Response(200, json=PAPER_HIT)

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await inspire.get_paper_figures(client, settings, inspire_id="1813416")
    assert result["figures_count"] == 2
    assert result["truncated"] is False
    assert result["figures"][0] == {
        "caption": "Figure 1",
        "url": "https://inspirehep.net/files/fig1.png",
        "description": None,
    }
    assert result["figures"][1]["caption"] is None


async def test_get_paper_figures_capped(tmp_path: Path) -> None:
    many_figures = {
        "id": 1,
        "metadata": {
            "figures": [{"url": f"https://example.org/f{i}.png"} for i in range(150)],
        },
    }
    recorder = Recorder(lambda _: httpx.Response(200, json=many_figures))
    settings, client = make_client(tmp_path, recorder)
    result = await inspire.get_paper_figures(client, settings, inspire_id="1")
    assert result["figures_count"] == 100
    assert result["total_figures"] == 150
    assert result["truncated"] is True
    assert len(result["figures"]) == 100
    with pytest.raises(InvalidInputError):
        await inspire.get_paper_figures(client, settings)


async def test_get_bibtex_passthrough_text(tmp_path: Path) -> None:
    bibtex_body = "@article{Doe:2020abc,\n  title = {Study},\n}\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/literature/1813416"
        if request.url.params.get("format") == "bibtex":
            return httpx.Response(
                200, content=bibtex_body.encode(), headers={"content-type": "text/plain"}
            )
        assert request.url.params["fields"] == "titles,texkeys"
        return httpx.Response(200, json=PAPER_HIT)

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await inspire.get_bibtex(client, settings, inspire_id="1813416")
    assert result["bibtex"] == bibtex_body
    assert result["inspire_id"] == "1813416"
    assert result["identifier"] == "1813416"
    assert result["identifier_type"] == "inspire_id"
    assert result["texkey"] == "Doe:2020abc"
    assert result["paper_title"] == "Study of a Very Generic Process"


async def test_get_bibtex_normalizes_arxiv_identifier(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/arxiv/2401.12345"
        if request.url.params.get("format") == "bibtex":
            return httpx.Response(200, content=b"@misc{x,}")
        return httpx.Response(200, json=PAPER_HIT)

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await inspire.get_bibtex(client, settings, arxiv_id="https://arxiv.org/abs/2401.12345")
    assert result["identifier"] == "2401.12345"
    assert result["identifier_type"] == "arxiv_id"
    assert result["bibtex"] == "@misc{x,}"


async def test_get_references_bulk_format_query(tmp_path: Path) -> None:
    bulk_hit = {
        "id": 111,
        "metadata": {"texkey": "ref-a", "formatted": "[1] J. Doe, JHEP (2020)."},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/literature/1813416":
            assert request.url.params["fields"] == "references,titles"
            return httpx.Response(200, json=PAPER_HIT)
        assert request.url.path == "/api/literature"
        params = request.url.params
        assert params["q"] == "recid:111"
        assert params["size"] == "1"
        assert params["format"] == "latex-us"
        return httpx.Response(200, json={"hits": {"total": 1, "hits": [bulk_hit]}})

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await inspire.get_references(
        client, settings, inspire_id="1813416", reference_format="latex-us"
    )
    assert result["paper_title"] == "Study of a Very Generic Process"
    assert result["total_references"] == 2
    assert result["resolvable_references"] == 1
    assert result["returned_references"] == 1
    assert result["truncated"] is False
    assert result["format"] == "latex-us"
    assert result["references"] == [
        {"inspire_id": "111", "texkey": "ref-a", "formatted": "[1] J. Doe, JHEP (2020)."}
    ]
    assert all(request.method == "GET" for request in recorder.requests)


async def test_get_references_json_format_skips_bulk_query(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(200, json=PAPER_HIT))
    settings, client = make_client(tmp_path, recorder)
    result = await inspire.get_references(
        client, settings, inspire_id="1813416", reference_format="json"
    )
    assert len(recorder.requests) == 1
    assert result["total_references"] == 2
    assert result["references"][0]["recid"] == "111"
    assert result["references"][0]["texkey"] == "ref-a"
    assert result["references"][1]["recid"] is None

    with pytest.raises(InvalidInputError):
        await inspire.get_references(client, settings, inspire_id="1", reference_format="xml")


async def test_http_429_raises_retryable_upstream(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(429, json={"error": "slow down"}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError) as caught:
        await inspire.search_papers(client, settings, query="x")
    assert caught.value.retryable is True
    assert caught.value.status_code == 429


async def test_non_json_body_raises_upstream(tmp_path: Path) -> None:
    """An HTML page where JSON was expected keeps the interactive-auth hint."""
    recorder = Recorder(
        lambda _: httpx.Response(
            200, content=b"<html>sign in</html>", headers={"content-type": "text/html"}
        )
    )
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError) as caught:
        await inspire.search_papers(client, settings, query="x")
    assert "could not be parsed as JSON" in caught.value.message
    assert "cern_auth_sign_in" in (caught.value.instructions or "")


async def test_server_error_raises_retryable_upstream(tmp_path: Path) -> None:
    recorder = Recorder(lambda _: httpx.Response(503, json={"error": "busy"}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError) as caught:
        await inspire.get_citations(client, settings, inspire_id="1")
    assert caught.value.retryable is True
