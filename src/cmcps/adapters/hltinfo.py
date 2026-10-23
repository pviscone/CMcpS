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
                "Call cern_auth_sign_in: the sign-in flow captures the "
                f"{settings.hltinfo_base_url} Bearer token automatically. "
                "Alternatively set CMCPS_HLTINFO_TOKEN manually."
            ),
        )
    return {"Authorization": f"Bearer {token}"}


def _base(settings: Settings) -> str:
    return settings.hltinfo_base_url.rstrip("/")


_SUMMARY_YEARS_LIMIT = 5
_SUMMARY_PATHS_PER_YEAR_LIMIT = 200


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


async def _bound_summary(result: Any) -> dict[str, Any]:
    years = sorted(result.keys(), reverse=True)[:_SUMMARY_YEARS_LIMIT]
    truncated_years = len(result) - len(years)
    out: dict[str, Any] = {}
    truncated_paths = 0
    for year in years:
        data = result[year]
        if isinstance(data, dict) and len(data) > _SUMMARY_PATHS_PER_YEAR_LIMIT:
            truncated_paths += len(data) - _SUMMARY_PATHS_PER_YEAR_LIMIT
            data = dict(list(data.items())[:_SUMMARY_PATHS_PER_YEAR_LIMIT])
        out[year] = data
    return {
        "result": out,
        "years_returned": years,
        "truncated": truncated_years > 0 or truncated_paths > 0,
    }


async def get_path_summary(client: SafeHttpClient, settings: Settings) -> dict[str, Any]:
    """Per-year summary of HLT paths, bounded to recent years (raw is ~8 MB)."""
    payload = await _get(client, settings, "pathsummary", source="hltinfo pathsummary")
    result = payload.get("result") if isinstance(payload, dict) else payload
    if not isinstance(result, dict):
        return {"source": SOURCE, "result": result, "truncated": False}
    bounded = await _bound_summary(result)
    return {
        "source": SOURCE,
        "years_returned": bounded["years_returned"],
        "truncated": bounded["truncated"],
        "result": bounded["result"],
        "note": "Bounded to the latest "
        f"{_SUMMARY_YEARS_LIMIT} years and {_SUMMARY_PATHS_PER_YEAR_LIMIT} paths/year.",
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
