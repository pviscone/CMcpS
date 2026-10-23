"""Read-only adapter for INSPIRE-HEP (https://inspirehep.net/api).

Clean-room reimplementation of the tools of the ``inspirehep-mcp`` reference
service against the public INSPIRE JSON API: literature search and record
detail, arXiv/DOI route resolution, BibTeX/LaTeX formatted output, author
profiles and their papers, citation-direction searches, collaboration
statistics, figure links, and bulk reference formatting.
"""

from __future__ import annotations

from typing import Any

from cmcps.adapters.common import json_body
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient

SOURCE = "inspire"

_SORTS = ("bestmatch", "mostrecent", "mostcited")
_TEXT_FORMATS = ("bibtex", "latex-us", "latex-eu")
_REFERENCE_FORMATS = (*_TEXT_FORMATS, "json")
_AUTHOR_LIST_LIMIT = 50
_SUMMARY_AUTHOR_LIMIT = 10
_FIGURE_LIMIT = 100
_BULK_REFERENCE_LIMIT = 250

_COLLABORATION_ALIASES = {
    "atlas": "ATLAS",
    "cms": "CMS",
    "lhcb": "LHCb",
    "alice": "ALICE",
    "belle2": "Belle-II",
}

_MIN_ERROR_STATUS = 400
_YEAR_PREFIX_LEN = 4


def _date_year(date: Any) -> int | None:
    """Extract a 4-digit year from an ISO-ish date string."""
    if isinstance(date, str) and len(date) >= _YEAR_PREFIX_LEN:
        try:
            return int(date[:_YEAR_PREFIX_LEN])
        except ValueError:
            return None
    return None


def _url(settings: Settings, path: str) -> str:
    return f"{settings.inspire_base_url.rstrip('/')}{path}"


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    return list(value) if isinstance(value, list) else []


def _check_sort(sort: str) -> None:
    if sort not in _SORTS:
        raise InvalidInputError(
            f"sort must be one of {', '.join(_SORTS)}, got {sort!r}",
            instructions="Use bestmatch, mostrecent, or mostcited.",
        )


def _normalize_arxiv(arxiv_id: str) -> str:
    value = arxiv_id.strip()
    lowered = value.lower()
    for prefix in ("arxiv:", "https://arxiv.org/abs/", "http://arxiv.org/abs/"):
        if lowered.startswith(prefix):
            value = value[len(prefix) :]
            break
    if value.lower().endswith(".pdf"):
        value = value[: -len(".pdf")]
    return value.strip()


def _normalize_doi(doi: str) -> str:
    value = doi.strip()
    lowered = value.lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if lowered.startswith(prefix):
            value = value[len(prefix) :]
            break
    return value.strip()


def _metadata(hit: dict[str, Any]) -> dict[str, Any]:
    metadata = hit.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _title(metadata: dict[str, Any]) -> str | None:
    titles = _as_list(metadata.get("titles"))
    if titles and isinstance(titles[0], dict):
        return titles[0].get("title")
    return None


def _abstract(metadata: dict[str, Any]) -> str | None:
    abstracts = _as_list(metadata.get("abstracts"))
    if abstracts and isinstance(abstracts[0], dict):
        return abstracts[0].get("value")
    return None


def _arxiv_id(metadata: dict[str, Any]) -> str | None:
    eprints = _as_list(metadata.get("arxiv_eprints"))
    if eprints and isinstance(eprints[0], dict):
        return eprints[0].get("value")
    return None


def _arxiv_categories(metadata: dict[str, Any]) -> list[str]:
    eprints = _as_list(metadata.get("arxiv_eprints"))
    if not eprints or not isinstance(eprints[0], dict):
        return []
    return [c for c in _as_list(eprints[0].get("categories")) if isinstance(c, str)]


def _doi(metadata: dict[str, Any]) -> str | None:
    dois = _as_list(metadata.get("dois"))
    if dois and isinstance(dois[0], dict):
        return dois[0].get("value")
    return None


def _publication(metadata: dict[str, Any]) -> dict[str, Any] | None:
    info = _as_list(metadata.get("publication_info"))
    if not info or not isinstance(info[0], dict):
        return None
    entry = info[0]
    publication = {
        "journal_title": entry.get("journal_title"),
        "journal_volume": entry.get("journal_volume"),
        "page_start": entry.get("page_start"),
        "year": entry.get("year"),
    }
    if all(value is None for value in publication.values()):
        return None
    return publication


def _collaborations(metadata: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for collaboration in _as_list(metadata.get("collaborations")):
        if isinstance(collaboration, dict) and collaboration.get("value"):
            out.append(collaboration["value"])
    return out


def _item_year(item: dict[str, Any]) -> int | None:
    year = (item.get("publication") or {}).get("year")
    if isinstance(year, int):
        return year
    return _date_year(item.get("date"))


def _paper_item(hit: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Standard paper shape shared by every list/search tool."""
    metadata = _metadata(hit)
    authors_all = _as_list(metadata.get("authors"))
    authors = []
    for author in authors_all[:_SUMMARY_AUTHOR_LIMIT]:
        if not isinstance(author, dict):
            continue
        affiliations = [
            a["value"]
            for a in _as_list(author.get("affiliations"))
            if isinstance(a, dict) and a.get("value")
        ]
        authors.append({"full_name": author.get("full_name"), "affiliations": affiliations})
    recid = str(hit.get("id")) if hit.get("id") is not None else None
    return {
        "inspire_id": recid,
        "title": _title(metadata),
        "authors": authors,
        "total_authors": len(authors_all),
        "abstract": _abstract(metadata),
        "arxiv_id": _arxiv_id(metadata),
        "arxiv_categories": _arxiv_categories(metadata),
        "doi": _doi(metadata),
        "publication": _publication(metadata),
        "collaborations": _collaborations(metadata),
        "citation_count": metadata.get("citation_count"),
        "date": metadata.get("earliest_date") or metadata.get("preprint_date"),
        "inspire_url": (
            f"{settings.inspire_base_url.rstrip('/')}/literature/{recid}" if recid else None
        ),
    }


def _search_result(
    payload: dict[str, Any],
    settings: Settings,
    *,
    page: int,
    size: int,
    query: str | None,
    sort: str | None = None,
) -> SearchResult:
    hits = payload.get("hits") if isinstance(payload, dict) else {}
    total = hits.get("total") if isinstance(hits, dict) else None
    items = [_paper_item(hit, settings) for hit in _as_list(hits.get("hits"))]
    has_more = page * size < total if isinstance(total, int) else None
    return SearchResult(
        source=SOURCE,
        items=items,
        total_count=total if isinstance(total, int) else None,
        page=page,
        size=size,
        has_more=has_more,
        query=query,
        sort=sort,
    )


async def _search_literature(
    client: SafeHttpClient,
    settings: Settings,
    *,
    query: str,
    sort: str | None,
    size: int,
    page: int = 1,
) -> SearchResult:
    params: dict[str, str | int] = {"q": query, "size": size, "page": page}
    if sort:
        _check_sort(sort)
        params["sort"] = sort
    response = await client.request(
        "GET", _url(settings, "/literature"), params=params, authenticated=False
    )
    return _search_result(
        json_body(response, source=SOURCE), settings, page=page, size=size, query=query, sort=sort
    )


async def search_papers(
    client: SafeHttpClient,
    settings: Settings,
    *,
    query: str,
    sort: str = "bestmatch",
    size: int = 10,
    page: int = 1,
) -> SearchResult:
    """Search INSPIRE literature records."""
    return await _search_literature(client, settings, query=query, sort=sort, size=size, page=page)


def _texkey(metadata: dict[str, Any]) -> str | None:
    """Extract the first BibTeX key; the API exposes ``texkeys`` (a list)."""
    texkeys = [t for t in _as_list(metadata.get("texkeys")) if isinstance(t, str)]
    if texkeys:
        return texkeys[0]
    single = metadata.get("texkey")
    return single if isinstance(single, str) else None


def _paper_urls(metadata: dict[str, Any]) -> dict[str, Any]:
    arxiv = _arxiv_id(metadata)
    doi = _doi(metadata)
    fulltext = None
    fallback = None
    for document in _as_list(metadata.get("documents")):
        if not isinstance(document, dict):
            continue
        url = document.get("url")
        if not url:
            continue
        if document.get("fulltext") is True:
            fulltext = url
            break
        if fallback is None:
            fallback = url
    return {
        "arxiv_abs": f"https://arxiv.org/abs/{arxiv}" if arxiv else None,
        "arxiv_pdf": f"https://arxiv.org/pdf/{arxiv}" if arxiv else None,
        "doi": f"https://doi.org/{doi}" if doi else None,
        "fulltext": fulltext or fallback,
        "inspire": None,
        "bibtex": None,
    }


def _detail_item(hit: dict[str, Any], settings: Settings) -> dict[str, Any]:
    metadata = _metadata(hit)
    item = _paper_item(hit, settings)
    authors_all = _as_list(metadata.get("authors"))
    authors = []
    for author in authors_all[:_AUTHOR_LIST_LIMIT]:
        if not isinstance(author, dict):
            continue
        bais = [
            ident["value"]
            for ident in _as_list(author.get("ids"))
            if isinstance(ident, dict) and ident.get("schema") == "INSPIRE BAI"
        ]
        affiliations = [
            a["value"]
            for a in _as_list(author.get("affiliations"))
            if isinstance(a, dict) and a.get("value")
        ]
        authors.append(
            {"full_name": author.get("full_name"), "affiliations": affiliations, "ids": bais}
        )
    urls = _paper_urls(metadata)
    urls["inspire"] = item["inspire_url"]
    urls["bibtex"] = (
        f"{settings.inspire_base_url.rstrip('/')}/literature/{item['inspire_id']}?format=bibtex"
        if item["inspire_id"]
        else None
    )
    item.update(
        {
            "authors": authors,
            "citation_count_without_self_citations": metadata.get(
                "citation_count_without_self_citations"
            ),
            "document_type": _as_list(metadata.get("document_type")),
            "keywords": [
                k["value"]
                for k in _as_list(metadata.get("keywords"))
                if isinstance(k, dict) and k.get("value")
            ],
            "inspire_categories": [
                c["term"]
                for c in _as_list(metadata.get("inspire_categories"))
                if isinstance(c, dict) and c.get("term")
            ],
            "texkey": _texkey(metadata),
            "report_numbers": [
                r["value"]
                for r in _as_list(metadata.get("report_numbers"))
                if isinstance(r, dict) and r.get("value")
            ],
            "number_of_pages": metadata.get("number_of_pages"),
            "references_count": len(_as_list(metadata.get("references")))
            if metadata.get("references")
            else None,
            "urls": urls,
        }
    )
    item["source"] = SOURCE
    return item


async def _fetch_record(
    client: SafeHttpClient,
    settings: Settings,
    path: str,
    *,
    fields: str | None = None,
) -> dict[str, Any]:
    params: dict[str, str] = {}
    if fields:
        params["fields"] = fields
    response = await client.request("GET", _url(settings, path), params=params, authenticated=False)
    payload = json_body(response, source=SOURCE)
    if not isinstance(payload, dict):
        raise UpstreamError(f"{SOURCE} returned an unexpected record payload for {path}")
    return payload


async def _fetch_record_text(
    client: SafeHttpClient,
    settings: Settings,
    path: str,
    *,
    text_format: str,
) -> str:
    response = await client.request(
        "GET",
        _url(settings, path),
        params={"format": text_format},
        headers={"Accept": "*/*"},
        authenticated=False,
    )
    if response.status_code >= _MIN_ERROR_STATUS:
        raise UpstreamError(
            f"{SOURCE} request failed with status {response.status_code} for {response.url}",
            status_code=response.status_code,
            instructions="Check the record identifier and try again.",
        )
    return response.content.decode("utf-8", errors="replace")


def _resolve_identifier(
    *, inspire_id: str | None, arxiv_id: str | None, doi: str | None
) -> tuple[str, str, str]:
    if inspire_id:
        return f"/literature/{inspire_id.strip()}", inspire_id.strip(), "inspire_id"
    if arxiv_id:
        normalized = _normalize_arxiv(arxiv_id)
        return f"/arxiv/{normalized}", normalized, "arxiv_id"
    if doi:
        normalized = _normalize_doi(doi)
        return f"/doi/{normalized}", normalized, "doi"
    raise InvalidInputError(
        "One of inspire_id, arxiv_id, or doi is required",
        instructions="Provide an INSPIRE record id, arXiv id, or DOI.",
    )


async def get_paper(
    client: SafeHttpClient,
    settings: Settings,
    *,
    inspire_id: str | None = None,
    arxiv_id: str | None = None,
    doi: str | None = None,
) -> dict[str, Any]:
    """Full metadata for one paper, resolved by INSPIRE id, arXiv id, or DOI."""
    path, _, _ = _resolve_identifier(inspire_id=inspire_id, arxiv_id=arxiv_id, doi=doi)
    hit = await _fetch_record(client, settings, path)
    return _detail_item(hit, settings)


async def get_paper_figures(
    client: SafeHttpClient,
    settings: Settings,
    *,
    inspire_id: str | None = None,
    arxiv_id: str | None = None,
    doi: str | None = None,
) -> dict[str, Any]:
    """Figure links (caption + URL) extracted from a paper record."""
    path, _, _ = _resolve_identifier(inspire_id=inspire_id, arxiv_id=arxiv_id, doi=doi)
    hit = await _fetch_record(client, settings, path, fields="figures,titles")
    metadata = _metadata(hit)
    figures = []
    for figure in _as_list(metadata.get("figures")):
        if not isinstance(figure, dict) or not figure.get("url"):
            continue
        figures.append(
            {
                "caption": figure.get("caption"),
                "url": figure.get("url"),
                "description": figure.get("description"),
            }
        )
    total = len(figures)
    return {
        "source": SOURCE,
        "inspire_id": str(hit.get("id")) if hit.get("id") is not None else None,
        "title": _title(metadata),
        "figures_count": min(total, _FIGURE_LIMIT),
        "total_figures": total,
        "truncated": total > _FIGURE_LIMIT,
        "figures": figures[:_FIGURE_LIMIT],
    }


async def get_bibtex(
    client: SafeHttpClient,
    settings: Settings,
    *,
    inspire_id: str | None = None,
    arxiv_id: str | None = None,
    doi: str | None = None,
) -> dict[str, Any]:
    """Single-entry BibTeX for one paper (plain-text response passthrough)."""
    path, identifier, identifier_type = _resolve_identifier(
        inspire_id=inspire_id, arxiv_id=arxiv_id, doi=doi
    )
    bibtex = await _fetch_record_text(client, settings, path, text_format="bibtex")
    hit = await _fetch_record(client, settings, path, fields="titles,texkeys")
    metadata = _metadata(hit)
    return {
        "source": SOURCE,
        "inspire_id": str(hit.get("id")) if hit.get("id") is not None else None,
        "identifier": identifier,
        "identifier_type": identifier_type,
        "paper_title": _title(metadata),
        "texkey": _texkey(metadata),
        "bibtex": bibtex,
    }


async def get_citations(
    client: SafeHttpClient,
    settings: Settings,
    *,
    inspire_id: str,
    direction: str = "citing",
    size: int = 50,
) -> dict[str, Any]:
    """Papers citing (or cited by) a record, with a per-year timeline."""
    if direction not in ("citing", "cited_by"):
        raise InvalidInputError(
            f"direction must be 'citing' or 'cited_by', got {direction!r}",
            instructions="Use citing (papers that cite this one) or cited_by (its references).",
        )
    relation = "refersto" if direction == "citing" else "citedby"
    query = f"{relation}:recid:{inspire_id}"
    result = await _search_literature(client, settings, query=query, sort=None, size=size, page=1)
    timeline: dict[str, int] = {}
    for item in result.items:
        year = _item_year(item)
        if year is not None:
            timeline[str(year)] = timeline.get(str(year), 0) + 1
    return {
        "source": SOURCE,
        "inspire_id": inspire_id,
        "direction": direction,
        "total_count": result.total_count,
        "timeline": dict(sorted(timeline.items())),
        "papers": result,
    }


def _author_hit_block(hit: dict[str, Any]) -> dict[str, Any]:
    metadata = _metadata(hit)
    name_block = metadata.get("name") if isinstance(metadata.get("name"), dict) else {}
    bais = [
        ident["value"]
        for ident in _as_list(metadata.get("ids"))
        if isinstance(ident, dict) and ident.get("schema") == "INSPIRE BAI"
    ]
    return {
        "name": name_block.get("value"),
        "preferred_name": name_block.get("preferred_name"),
        "bai": bais[0] if bais else None,
        "inspire_author_id": str(hit.get("id")) if hit.get("id") is not None else None,
    }


async def get_author_papers(
    client: SafeHttpClient,
    settings: Settings,
    *,
    author_name: str | None = None,
    author_id: str | None = None,
    sort: str = "mostrecent",
    size: int = 20,
) -> dict[str, Any]:
    """Papers of one author, resolved by name (via /authors) or direct BAI."""
    _check_sort(sort)
    if author_id:
        author = {
            "name": None,
            "preferred_name": None,
            "bai": author_id.strip(),
            "inspire_author_id": None,
        }
    elif author_name:
        response = await client.request(
            "GET",
            _url(settings, "/authors"),
            params={"q": author_name.strip(), "size": 1},
            authenticated=False,
        )
        payload = json_body(response, source=SOURCE)
        hits = _as_list((payload.get("hits") or {}).get("hits"))
        if not hits:
            raise InvalidInputError(
                f"No INSPIRE author found for {author_name!r}",
                instructions="Try a different spelling or provide the author's BAI as author_id.",
            )
        author = _author_hit_block(hits[0])
        if not author["bai"]:
            raise InvalidInputError(
                f"Author {author_name!r} has no INSPIRE BAI on record",
                instructions="Provide the author's BAI directly as author_id.",
            )
    else:
        raise InvalidInputError(
            "One of author_name or author_id is required",
            instructions="Provide the author's name or INSPIRE BAI.",
        )
    result = await _search_literature(
        client, settings, query=f"a {author['bai']}", sort=sort, size=size, page=1
    )
    citations = sorted(
        (
            item["citation_count"]
            for item in result.items
            if isinstance(item.get("citation_count"), int)
        ),
        reverse=True,
    )
    h_index = 0
    for rank, count in enumerate(citations, start=1):
        if count < rank:
            break
        h_index = rank
    total_citations = sum(citations)
    metrics = {
        "total_citations": total_citations,
        "h_index": h_index,
        "h_index_note": "h-index computed from the returned page of papers only; the true "
        "h-index may differ.",
        "average_citations": round(total_citations / len(citations), 2) if citations else 0.0,
    }
    return {
        "source": SOURCE,
        "author": author,
        "papers": result,
        "metrics": metrics,
    }


async def search_collaboration(
    client: SafeHttpClient,
    settings: Settings,
    *,
    collaboration_name: str,
    sort: str = "mostrecent",
    size: int = 20,
    year: int | None = None,
) -> dict[str, Any]:
    """Papers of an experimental collaboration, with coarse statistics."""
    name = _COLLABORATION_ALIASES.get(
        collaboration_name.strip().lower(), collaboration_name.strip().upper()
    )
    query = f"collaboration:{name}"
    if year is not None:
        query += f" and date {year}"
    result = await _search_literature(client, settings, query=query, sort=sort, size=size, page=1)
    year_distribution: dict[str, int] = {}
    for item in result.items:
        year_value = _item_year(item)
        if year_value is not None:
            key = str(year_value)
            year_distribution[key] = year_distribution.get(key, 0) + 1
    top_cited = sorted(
        (item for item in result.items if isinstance(item.get("citation_count"), int)),
        key=lambda item: item["citation_count"],
        reverse=True,
    )[:5]
    statistics = {
        "year_distribution": dict(sorted(year_distribution.items())),
        "total_citations": sum(item.get("citation_count") or 0 for item in result.items),
    }
    return {
        "source": SOURCE,
        "collaboration": name,
        "year": year,
        "papers": result,
        "statistics": statistics,
        "top_cited_papers": top_cited,
    }


def _reference_recid(reference: dict[str, Any]) -> str | None:
    record = reference.get("record")
    if isinstance(record, dict):
        ref = record.get("$ref")
        if isinstance(ref, str):
            return ref.rstrip("/").rsplit("/", 1)[-1]
    return None


def _json_reference_entry(reference: dict[str, Any]) -> dict[str, Any]:
    embedded = reference.get("reference")
    embedded = embedded if isinstance(embedded, dict) else {}
    return {
        "recid": _reference_recid(reference),
        "authors": _as_list(reference.get("authors")),
        "misc": reference.get("misc"),
        "publication_info": _as_list(reference.get("publication_info")),
        "raw": embedded.get("misc"),
        "texkey": reference.get("texkey"),
        "titles": _as_list(embedded.get("titles")),
    }


async def _bulk_fetch_references(
    client: SafeHttpClient,
    settings: Settings,
    selected: list[tuple[str, dict[str, Any]]],
    reference_format: str,
) -> dict[str, Any]:
    query = " or ".join(f"recid:{recid}" for recid, _ in selected)
    response = await client.request(
        "GET",
        _url(settings, "/literature"),
        params={"q": query, "size": len(selected), "format": reference_format},
        authenticated=False,
    )
    payload = json_body(response, source=SOURCE)
    fetched: dict[str, Any] = {}
    for fetched_hit in _as_list((payload.get("hits") or {}).get("hits")):
        if isinstance(fetched_hit, dict) and fetched_hit.get("id") is not None:
            fetched[str(fetched_hit["id"])] = fetched_hit
    return fetched


def _formatted_reference_entries(
    selected: list[tuple[str, dict[str, Any]]], fetched: dict[str, Any]
) -> list[dict[str, Any]]:
    entries = []
    for recid, reference in selected:
        fetched_hit = fetched.get(recid)
        fetched_metadata = _metadata(fetched_hit) if fetched_hit else {}
        formatted = None
        for key in ("formatted", "reference", "references"):
            if isinstance(fetched_metadata.get(key), str):
                formatted = fetched_metadata[key]
                break
        entries.append(
            {
                "inspire_id": recid,
                "texkey": _texkey(fetched_metadata) or reference.get("texkey"),
                "formatted": formatted,
            }
        )
    return entries


async def get_references(
    client: SafeHttpClient,
    settings: Settings,
    *,
    inspire_id: str,
    reference_format: str = "bibtex",
    limit: int | None = None,
) -> dict[str, Any]:
    """References of a record, bulk-formatted via an ``or recid:`` query."""
    if reference_format not in _REFERENCE_FORMATS:
        raise InvalidInputError(
            f"format must be one of {', '.join(_REFERENCE_FORMATS)}, got {reference_format!r}"
        )
    hit = await _fetch_record(
        client, settings, f"/literature/{inspire_id.strip()}", fields="references,titles"
    )
    metadata = _metadata(hit)
    references = [r for r in _as_list(metadata.get("references")) if isinstance(r, dict)]
    if limit is not None:
        references = references[:limit]
    resolved = [(recid, r) for r in references if (recid := _reference_recid(r))]
    selected = resolved[:_BULK_REFERENCE_LIMIT]
    base: dict[str, Any] = {
        "source": SOURCE,
        "inspire_id": inspire_id.strip(),
        "paper_title": _title(metadata),
        "total_references": len(references),
        "format": reference_format,
        "truncated": len(resolved) > _BULK_REFERENCE_LIMIT,
    }
    if reference_format == "json":
        base["returned_references"] = len(references)
        base["references"] = [_json_reference_entry(r) for r in references]
        return base
    fetched = (
        await _bulk_fetch_references(client, settings, selected, reference_format)
        if selected
        else {}
    )
    formatted_references = _formatted_reference_entries(selected, fetched)
    base["resolvable_references"] = len(resolved)
    base["bulk_query_recids"] = len(selected)
    base["returned_references"] = len(formatted_references)
    base["references"] = formatted_references
    return base
