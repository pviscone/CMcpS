"""Read-only adapter for the CMS HLT info service (cmshltinfo).

Endpoint type: custom JSON API v0, routes recovered from the service's own
frontend bundle (``assets/APIServices-*.js``):

- ``GET /api/v0/hltmenus`` — available HLT menus; response ``{"menus": ...}``.
- ``GET /api/v0/pathsummary`` — per-year path summary; ``{"result": ...}``.
- ``GET /api/v0/path/{path_name}`` — full path record (versions, filters
  with per-run semantics); ``{"result": ...}``.
- ``GET /api/v0/pathnames`` (optionally ``/{filter}``) — path names.
- ``GET /api/v0/prescales/path/{path_name}?minrun=&maxrun=`` — prescale
  values per run; ``{"result": ...}``.
- ``GET /api/v0/prescales/path/{path_name}/runlumis`` — run/lumi ranges at
  which the path fires.

Discovery notes: every ``/api/v0/*`` route answers anonymous requests with
``401 {"status":"Computer says no"}`` (a custom handler, not a proxy) on
both the dev (``cmshltinfo-dev.app.cern.ch``) and prod
(``cmshltinfo.app.cern.ch``) instances. The frontend authenticates with a
**Keycloak Bearer token** (``Authorization: Bearer <token>`` header, token
from the app's own Keycloak client). The browser SSO session alone does not
produce such a token, so the operator must configure
``CMCPS_HLTINFO_TOKEN`` (a valid Bearer token for the service); it is
injected as a service header by the trusted HTTP layer and never logged.
``/api/`` without auth returns the backend banner; everything else SPA-falls
through to HTML (detected as non-JSON and reported).
"""

from __future__ import annotations

import json
import time
from typing import Any

from cmcps.adapters.common import json_body
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

SOURCE = "hltinfo"

_UNAUTHORIZED = 401
_PATH_NAME_RE_MAX = 200
_MAX_RUN = 9_999_999


async def _service_headers(client: SafeHttpClient, settings: Settings) -> dict[str, str]:
    token = await client.bearer_for("hltinfo")
    if token is None:
        token = settings.hltinfo_token
    if not token:
        raise InvalidInputError(
            "hltinfo tools require a Keycloak Bearer token",
            instructions=(
                f"The {settings.hltinfo_base_url} Bearer token is minted "
                "automatically from the stored SSO session on first use — sign "
                "in with cern_auth_sign_in first. Alternatively set "
                "CMCPS_HLTINFO_TOKEN manually."
            ),
        )
    return {"Authorization": f"Bearer {token}"}


def _base(settings: Settings) -> str:
    return settings.hltinfo_base_url.rstrip("/")


_SUMMARY_YEARS_LIMIT = 5
_SUMMARY_PATHS_PER_YEAR_LIMIT = 200
# Hard cap for the bounded summary payload (~8 MB measured unbounded for 5
# years in the Run-3 era): the whole envelope must stay far under the 16 MiB
# stdio frame limit.
_SUMMARY_MAX_RESULT_BYTES = 2 * 1024 * 1024


def _json_size(value: Any) -> int:
    return len(json.dumps(value, default=str))


def _check_path_name(path_name: str) -> str:
    cleaned = path_name.strip().strip("/")
    if not cleaned or len(cleaned) > _PATH_NAME_RE_MAX or "/" in cleaned:
        raise InvalidInputError(
            f"invalid HLT path name {path_name!r}",
            instructions=(
                "Use the exact path name, e.g. HLT_Ele32_WPTight_Gsf (see hltinfo_get_path_names)."
            ),
        )
    return cleaned


async def _get(
    client: SafeHttpClient,
    settings: Settings,
    path: str,
    *,
    source: str,
    params: dict[str, str | int] | None = None,
) -> Any:
    response = await client.request(
        "GET",
        f"{_base(settings)}/api/v0/{path.lstrip('/')}",
        params=params,
        service_headers=await _service_headers(client, settings),
    )
    if response.status_code == _UNAUTHORIZED:
        raise UpstreamError(
            "hltinfo rejected the request (401 Computer says no)",
            instructions=(
                "The CMCPS_HLTINFO_TOKEN is missing, expired, or not valid for "
                "this instance; refresh it from an authenticated web session."
            ),
        )
    return json_body(response, source=source)


async def list_menus(client: SafeHttpClient, settings: Settings) -> dict[str, Any]:
    """HLT menus known to the service (``{"menus": ...}``)."""
    payload = await _get(client, settings, "hltmenus", source="hltinfo menus")
    menus = payload.get("menus") if isinstance(payload, dict) else None
    if menus is None:
        raise UpstreamError(
            "hltinfo returned an unexpected menus payload",
            instructions="The API may have changed; check the service frontend.",
        )
    items = menus if isinstance(menus, list) else list(menus)
    return {"source": SOURCE, "menu_count": len(items), "menus": menus}


def _cut_paths(data: Any, limit: int) -> tuple[Any, int]:
    """Cut one year's ``paths`` list to ``limit`` entries; return (data, removed)."""
    if not isinstance(data, dict):
        return data, 0
    paths = data.get("paths")
    if not isinstance(paths, list) or len(paths) <= limit:
        return data, 0
    return {**data, "paths": paths[:limit]}, len(paths) - limit


def _paths_of(data: Any) -> list[Any] | None:
    if isinstance(data, dict) and isinstance(data.get("paths"), list):
        return data["paths"]
    return None


def _write_summary_file(settings: Settings, payload: dict[str, Any]) -> str:
    settings.ensure_directories()
    path = settings.download_dir / f"hltinfo-pathsummary-{time.time_ns()}.json"
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    return str(path)


async def get_path_summary(
    client: SafeHttpClient,
    settings: Settings,
    *,
    year: int | None = None,
    pathname_contains: str | None = None,
) -> dict[str, Any]:
    """Per-year HLT path summary as a file + metadata; rows never inline.

    The raw table (~8 MB in the Run-3 era) holds verbose per-path aggregates
    (active/effective lumi, counts, datasets, first/last run) — unsuitable for
    a tool result. The bounded rows (≤ ``_SUMMARY_MAX_RESULT_BYTES``) are
    written to CMcpS download storage; the result carries years, per-year
    counts, truncation info, and the file path only. ``year`` selects one
    year (default: the 5 latest); ``pathname_contains`` filters path names
    case-insensitively before the byte cap.
    """
    if pathname_contains is not None and not pathname_contains.strip():
        raise InvalidInputError("pathname_contains must not be empty")
    payload = await _get(client, settings, "pathsummary", source="hltinfo pathsummary")
    result = payload.get("result") if isinstance(payload, dict) else payload
    if not isinstance(result, dict):
        raise UpstreamError(
            "hltinfo returned a pathsummary payload of unexpected shape",
            instructions="Expected a year-keyed result object; the API may have changed.",
        )
    all_years = sorted(result.keys(), reverse=True)
    if year is not None:
        key = str(year)
        if key not in result:
            raise InvalidInputError(
                f"no summary for year {year}",
                instructions=f"Available years: {', '.join(all_years)}.",
            )
        kept_years = [key]
    else:
        kept_years = all_years[:_SUMMARY_YEARS_LIMIT]
    needle = pathname_contains.strip().lower() if pathname_contains else None
    filtered: dict[str, Any] = {}
    for key in kept_years:
        data = result[key]
        paths = _paths_of(data)
        if paths is not None and needle is not None:
            paths = [p for p in paths if needle in str(p.get("pathname", "")).lower()]
            data = {**data, "paths": paths}
        filtered[key] = data

    path_limit = _SUMMARY_PATHS_PER_YEAR_LIMIT
    truncated_paths = 0
    while True:
        out: dict[str, Any] = {}
        truncated_paths = 0
        for key in kept_years:
            data, removed = _cut_paths(filtered[key], path_limit)
            truncated_paths += removed
            out[key] = data
        if path_limit <= 1 or _json_size(out) <= _SUMMARY_MAX_RESULT_BYTES:
            break
        path_limit //= 2

    file_path = _write_summary_file(
        settings,
        {
            "source": SOURCE,
            "years_returned": kept_years,
            "pathname_contains": pathname_contains,
            "result": out,
        },
    )
    per_year = {
        key: {
            "paths_total": (
                len(paths) if (paths := _paths_of(filtered[key])) is not None else None
            ),
            "paths_returned": (
                len(paths) if (paths := _paths_of(out.get(key))) is not None else None
            ),
        }
        for key in kept_years
    }
    return {
        "source": SOURCE,
        "file": file_path,
        "years_returned": kept_years,
        "available_years": all_years,
        "pathname_contains": pathname_contains,
        "per_year": per_year,
        "truncated": truncated_paths > 0 or (year is None and len(result) > len(kept_years)),
        "note": "Rows written to CMcpS download storage; only metadata is inline.",
    }


async def get_path_names(
    client: SafeHttpClient, settings: Settings, *, name_filter: str | None = None
) -> dict[str, Any]:
    """Switched HLT path names (carry exact ``_v`` version suffixes).

    ``pathnames/{filter}`` is not a server route (404); filtering is
    case-insensitive client-side substring matching.
    """
    if name_filter is not None and not name_filter.strip():
        raise InvalidInputError("name_filter must not be empty")
    payload = await _get(client, settings, "pathnames", source="hltinfo pathnames")
    result = payload.get("result") if isinstance(payload, dict) else payload
    names = [n for n in result if isinstance(n, str)] if isinstance(result, list) else []
    if name_filter is not None:
        cleaned = name_filter.strip()
        if not cleaned:
            raise InvalidInputError("name_filter must not be empty")
        names = [n for n in names if cleaned.lower() in n.lower()]
    return {
        "source": SOURCE,
        "filter": name_filter,
        "name_count": len(names),
        "names": names,
    }


async def get_path(client: SafeHttpClient, settings: Settings, *, path_name: str) -> dict[str, Any]:
    """Full record of one HLT path (versions, filters, per-run data)."""
    path_name = _check_path_name(path_name)
    payload = await _get(client, settings, f"path/{path_name}", source=f"hltinfo path {path_name}")
    result = payload.get("result") if isinstance(payload, dict) else payload
    if not result:
        raise UpstreamError(
            f"hltinfo returned no data for path {path_name!r}",
            instructions="Check the name via hltinfo_get_path_names.",
        )
    return {"source": SOURCE, "path_name": path_name, "result": result}


async def get_path_prescales(
    client: SafeHttpClient,
    settings: Settings,
    *,
    path_name: str,
    min_run: int = 1,
    max_run: int = _MAX_RUN,
) -> dict[str, Any]:
    """Prescale values of one HLT path across a run range."""
    path_name = _check_path_name(path_name)
    if not 1 <= min_run <= max_run <= _MAX_RUN:
        raise InvalidInputError(
            f"invalid run range {min_run}-{max_run}",
            instructions=f"Runs are positive integers up to {_MAX_RUN}.",
        )
    response = await client.request(
        "GET",
        f"{_base(settings)}/api/v0/prescales/path/{path_name}",
        params={"minrun": min_run, "maxrun": max_run},
        service_headers=await _service_headers(client, settings),
    )
    payload = json_body(response, source=f"hltinfo prescales {path_name}")
    return {
        "source": SOURCE,
        "path_name": path_name,
        "min_run": min_run,
        "max_run": max_run,
        "result": payload.get("result") if isinstance(payload, dict) else payload,
    }


async def get_run_lumis_by_prescale(
    client: SafeHttpClient, settings: Settings, *, path_name: str
) -> dict[str, Any]:
    """Run/lumi ranges where one HLT path was enabled (prescale > 0)."""
    path_name = _check_path_name(path_name)
    response = await client.request(
        "GET",
        f"{_base(settings)}/api/v0/prescales/path/{path_name}/runlumis",
        service_headers=await _service_headers(client, settings),
    )
    payload = json_body(response, source=f"hltinfo runlumis {path_name}")
    return {
        "source": SOURCE,
        "path_name": path_name,
        "result": payload.get("result") if isinstance(payload, dict) else payload,
    }
