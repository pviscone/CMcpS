"""Read-only adapter for indico.cern.ch.

Covers the retained tools of the MIT-licensed ``indico-mcp`` reference
implementation (InnovateInfinity87/indico-mcp): the legacy export API
(``/export/categ|event .json``), the newer ``/search/api/search`` endpoint,
``/api/user/``, and bounded local file downloads.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cmcps.adapters.common import json_body
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient

SOURCE = "indico"
_SEARCH_TYPES = (
    "category",
    "event",
    "contribution",
    "subcontribution",
    "attachment",
    "event_note",
)
_DATE_FORMAT = "%Y-%m-%d"


def _url(settings: Settings, path: str) -> str:
    return f"{settings.indico_base_url.rstrip('/')}{path}"


def _service_headers(settings: Settings) -> dict[str, str] | None:
    if settings.indico_api_token:
        return {"Authorization": f"Bearer {settings.indico_api_token}"}
    return None


def _check_date(value: str, *, name: str) -> None:
    try:
        datetime.strptime(value, _DATE_FORMAT)
    except ValueError as exc:
        raise InvalidInputError(f"{name} must use YYYY-MM-DD, got {value!r}") from exc


def _as_list(value: Any) -> list[dict[str, Any]]:
    """Legacy export payloads sometimes replace a singleton list with a dict."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    return [item for item in value if isinstance(item, dict)]


def _event_item(event: dict[str, Any]) -> dict[str, Any]:
    start = event.get("startDate") or {}
    end = event.get("endDate") or {}
    return {
        "id": event.get("id"),
        "title": event.get("title"),
        "description": event.get("description") or None,
        "start_at": start.get("date") or start.get("utc"),
        "end_at": end.get("date") or end.get("utc"),
        "timezone": start.get("tz"),
        "location": event.get("location") or None,
        "room": event.get("room") or None,
        "type": event.get("type") or None,
        "category": event.get("category") or None,
        "category_id": event.get("categoryId") or None,
        "url": event.get("url") or None,
    }


async def get_user_info(client: SafeHttpClient, settings: Settings) -> dict[str, Any]:
    """Return the authenticated Indico user profile (requires an API token)."""
    if not settings.indico_api_token:
        raise InvalidInputError(
            "indico_get_user_info requires an Indico API token",
            instructions="Set CMCPS_INDICO_API_TOKEN with an Indico personal API token.",
        )
    response = await client.request(
        "GET", _url(settings, "/api/user/"), service_headers=_service_headers(settings)
    )
    data = json_body(response, source=SOURCE)
    return {
        "source": SOURCE,
        "id": data.get("id"),
        "first_name": data.get("first_name"),
        "last_name": data.get("last_name"),
        "email": data.get("email"),
        "admin": bool(data.get("admin", False)),
    }


async def search_events(
    client: SafeHttpClient,
    settings: Settings,
    *,
    category_id: str = "0",
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 25,
    offset: int = 0,
    only_public: bool = False,
) -> SearchResult:
    """List events in a category via the legacy export API."""
    if from_date:
        _check_date(from_date, name="from_date")
    if to_date:
        _check_date(to_date, name="to_date")
    params: dict[str, str | int] = {"pretty": "yes", "limit": limit, "offset": offset}
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    if only_public:
        params["onlypublic"] = "yes"
    response = await client.request(
        "GET",
        _url(settings, f"/export/categ/{category_id}.json"),
        params=params,
        service_headers=_service_headers(settings),
    )
    envelope = json_body(response, source=SOURCE)
    count = envelope.get("count")
    events = [_event_item(event) for event in _as_list(envelope.get("results"))]
    page = offset // limit + 1 if limit else 1
    has_more = offset + len(events) < count if isinstance(count, int) else None
    return SearchResult(
        source=SOURCE,
        items=events,
        total_count=count if isinstance(count, int) else None,
        page=page,
        size=limit,
        has_more=has_more,
        note=f"category_id={category_id}",
    )


async def search(
    client: SafeHttpClient,
    settings: Settings,
    *,
    query: str,
    limit: int = 25,
) -> dict[str, Any]:
    """Full-text search across Indico content types via /search/api/search."""
    results: dict[str, Any] = {}
    total = 0
    for content_type in _SEARCH_TYPES:
        response = await client.request(
            "GET",
            _url(settings, "/search/api/search"),
            params={"q": query, "type": content_type, "limit": limit},
            service_headers=_service_headers(settings),
        )
        payload = json_body(response, source=SOURCE)
        hits = payload.get("results") or payload.get("hits") or []
        hits = _as_list(hits)
        results[content_type] = {"count": len(hits), "items": hits}
        total += len(hits)
    return {
        "source": SOURCE,
        "query": query,
        "limit_per_type": limit,
        "total": total,
        "results": results,
    }


async def _fetch_event(
    client: SafeHttpClient,
    settings: Settings,
    event_id: str,
    *,
    detail: str | None,
) -> dict[str, Any]:
    params: dict[str, str] = {"pretty": "yes"}
    if detail:
        params["detail"] = detail
    response = await client.request(
        "GET",
        _url(settings, f"/export/event/{event_id}.json"),
        params=params,
        service_headers=_service_headers(settings),
    )
    envelope = json_body(response, source=SOURCE)
    events = _as_list(envelope.get("results"))
    if not events:
        raise InvalidInputError(f"Indico event {event_id!r} was not found")
    return events[0]


async def get_event(
    client: SafeHttpClient,
    settings: Settings,
    *,
    event_id: str,
    include_contributions: bool = False,
    include_sessions: bool = False,
) -> dict[str, Any]:
    """Return metadata for one Indico event."""
    details: list[str] = []
    if include_contributions:
        details.append("contributions")
    if include_sessions:
        details.append("sessions")
    if include_contributions and include_sessions:
        # The legacy export API accepts a single detail value per request.
        event = await _fetch_event(client, settings, event_id, detail="contributions")
        sessions_payload = await _fetch_event(client, settings, event_id, detail="sessions")
        event["sessions"] = sessions_payload.get("sessions", [])
    else:
        event = await _fetch_event(
            client, settings, event_id, detail=details[0] if details else None
        )
    item = _event_item(event)
    item["source"] = SOURCE
    if include_contributions:
        contributions = _as_list(event.get("contributions"))
        item["contributions"] = [_contribution_item(c) for c in contributions]
        item["contribution_count"] = len(contributions)
    if include_sessions:
        item["sessions"] = _as_list(event.get("sessions"))
    return item


def _contribution_item(contribution: dict[str, Any]) -> dict[str, Any]:
    start = contribution.get("startDate") or {}
    speakers = _as_list(contribution.get("speakers"))
    return {
        "id": contribution.get("id"),
        "title": contribution.get("title"),
        "description": contribution.get("description") or None,
        "duration": contribution.get("duration") or None,
        "start_at": start.get("date") or None,
        "location": contribution.get("location") or None,
        "room": contribution.get("room") or None,
        "speakers": [
            {"name": s.get("fullName") or s.get("name"), "affiliation": s.get("affiliation")}
            for s in speakers
        ],
    }


async def get_event_contributions(
    client: SafeHttpClient,
    settings: Settings,
    *,
    event_id: str,
    include_subcontributions: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    """List the contributions (talks) of one event."""
    detail = "subcontributions" if include_subcontributions else "contributions"
    event = await _fetch_event(client, settings, event_id, detail=detail)
    contributions = _as_list(event.get("contributions"))
    items = []
    for contribution in contributions:
        item = _contribution_item(contribution)
        if include_subcontributions:
            sub = _as_list(contribution.get("subContributions"))
            item["subcontributions"] = [_contribution_item(s) for s in sub]
        items.append(item)
    return {
        "source": SOURCE,
        "event_id": event_id,
        "event_title": event.get("title"),
        "total_count": len(items),
        "items": items[:limit],
        "truncated": len(items) > limit,
    }


def _iter_materials(container: dict[str, Any]) -> Any:
    for folder in _as_list(container.get("folders")):
        for material in _as_list(folder.get("materials")):
            yield folder, material


def _file_entry(
    settings: Settings,
    event_id: str,
    *,
    folder: dict[str, Any],
    material: dict[str, Any],
    resource: dict[str, Any],
    contribution_id: str | None,
) -> dict[str, Any]:
    material_id = str(material.get("id")) if material.get("id") is not None else None
    resource_id = str(resource.get("id")) if resource.get("id") is not None else None
    download_url = resource.get("url")
    if not download_url and material_id and resource_id:
        download_url = _url(
            settings, f"/export/event/{event_id}/material/{material_id}/{resource_id}.bin"
        )
    return {
        "filename": resource.get("name") or resource.get("fileName"),
        "folder_id": folder.get("id"),
        "folder_title": folder.get("title"),
        "material_id": material_id,
        "resource_id": resource_id,
        "contribution_id": contribution_id,
        "size_bytes": resource.get("size"),
        "download_url": download_url,
    }


async def get_event_files(
    client: SafeHttpClient,
    settings: Settings,
    *,
    event_id: str,
    limit: int = 100,
) -> dict[str, Any]:
    """Enumerate all file attachments of an event and its contributions."""
    event = await _fetch_event(client, settings, event_id, detail="contributions")
    files: list[dict[str, Any]] = []
    for folder, material in _iter_materials(event):
        resources = _as_list(material.get("resources")) or [material]
        for resource in resources:
            files.append(
                _file_entry(
                    settings,
                    event_id,
                    folder=folder,
                    material=material,
                    resource=resource,
                    contribution_id=None,
                )
            )
    for contribution in _as_list(event.get("contributions")):
        contribution_id = contribution.get("id")
        for folder, material in _iter_materials(contribution):
            resources = _as_list(material.get("resources")) or [material]
            for resource in resources:
                files.append(
                    _file_entry(
                        settings,
                        event_id,
                        folder=folder,
                        material=material,
                        resource=resource,
                        contribution_id=contribution_id,
                    )
                )
    return {
        "source": SOURCE,
        "event_id": event_id,
        "event_title": event.get("title"),
        "total_count": len(files),
        "items": files[:limit],
        "truncated": len(files) > limit,
    }


async def download_file(
    client: SafeHttpClient,
    settings: Settings,
    *,
    download_url: str,
) -> dict[str, Any]:
    """Download one Indico attachment into CMcpS download storage."""
    host = urlsplit(download_url).hostname
    indico_host = urlsplit(settings.indico_base_url).hostname
    if not host or host != indico_host:
        raise InvalidInputError(
            f"indico_download_file only accepts URLs on {indico_host}",
            instructions="Pass a download_url returned by indico_get_event_files.",
        )
    filename = Path(urlsplit(download_url).path).name or None
    destination = Path(filename) if filename else None
    final = await client.download(
        download_url,
        destination,
        service_headers=_service_headers(settings),
    )
    return {
        "source": SOURCE,
        "download_url": download_url,
        "path": str(final),
        "size_bytes": os.stat(final).st_size,
    }
