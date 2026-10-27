"""Read-only adapter for the CMS Run Registry (RR) REST API.

Discovery notes: ``cmsrunregistry.web.cern.ch`` is an oauth2-proxy gated
service (anonymous requests 302 to auth.cern.ch with
``client_id=cmsrunregistry-sso-proxy``; the site root and every ``/api/...``
path share the gate). Inside a valid CMcpS SSO session the proxy handshake
is silent and plants chunked ``_oauth2_proxy_<n>`` cookies — kept fresh by
the ``_COOKIE_ONLY_SITES`` entry in :mod:`cmcps.auth.browser` (verified
2026-10: with those cookies the API answers GET and JSON POST inline; without
a stored proxy session, GET still resolves through the silent redirect
handshake, but POST queries can lose their body mid-handshake, which is why
the warm entry exists and the client re-warms once on 401/403).

The API (verified live against the ``runregistry`` PyPI client v3 protocol
and the service itself):

- ``POST /api/runs_filtered_ordered`` — run queries; JSON body
  ``{"page", "page_size", "filter", "sortings"}``; response
  ``{"runs": [...], "count": N, "pages": P}`` with the record split into
  ``oms_attributes`` and ``rr_attributes``, plus per ``triplet_summary``
  GOOD/BAD/... counts; filter errors surface as ``{"err": ...}``.
  Default order (empty sortings) is newest run first.
- ``POST /api/datasets_filtered_ordered`` — per-run certification records
  ("datasets": run number + DQM dataset name like ``/PromptReco/.../DQM``,
  NOT CMS DAS datasets); same envelope with a ``datasets`` key.
- ``GET /api/get_all_dataset_names_of_run/{run_number}`` — JSON array of
  dataset names recorded for one run (always includes ``"online"``).
- ``POST /api/lumisections/rr_lumisection_ranges`` and
  ``POST /api/lumisections/rr_lumisection_ranges_by_component`` — lumisection
  certification ranges of one dataset of one run; body
  ``{"run_number", "dataset_name"}`` (``"online"`` selects the online
  record). Ranges are the compact surface (per-lumisection arrays exceed
  thousands of rows on long runs).

Filter keys (backend syntax, documented by the official client):

- ``run_number`` — bare key; operators ``=`` ``>`` ``<`` ``>=`` ``<=`` ``<>``
  (``{"and": [{">=": lo}, {"<=": hi}]}`` for ranges).
- ``oms_attributes.<attr>`` — OMS-sourced run attributes (``fill_number``,
  ``hlt_key``, ``l1_menu``, ``energy``, ``b_field``, ...); strings support
  ``=`` and ``like`` (percent-wrapped).
- ``rr_attributes.<attr>`` — RR-owned run attributes (``class``, ``state``,
  ``significant``, ``stop_reason``).
- ``triplet_summary.<workspace>-<column>.<STATUS>`` with ``{">": 0}`` —
  "run/dataset has at least one lumisection certified STATUS on
  workspace-column"; STATUS in GOOD/BAD/STANDBY/EXCLUDED/NOTSET/EMPTY.
- Dataset name is the raw ``name`` key (``=`` or ``like``, percent-wrapped).

OMS data (rates, exact luminosity, fill data) belongs to the OMS API — use
the ``cmsonline_*`` tools; this adapter only queries RR-responsible data.
"""

from __future__ import annotations

import json
import re
from typing import Any

from cmcps.adapters.common import json_body
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient

SOURCE = "run-registry"
BASE = "https://cmsrunregistry.web.cern.ch/api"
_POST_ALLOWLIST = ("cmsrunregistry.web.cern.ch",)

_MAX_PAGE_SIZE = 250
_DEFAULT_PAGE_SIZE = 50
_REQUIRED_DATASET_PREFIX = "/"
_TRIPLET_STATUSES = frozenset({"GOOD", "BAD", "STANDBY", "EXCLUDED", "NOTSET", "EMPTY"})
_TRIPLET_SPLIT_RE = re.compile(r"^[a-z][a-z0-9_]*-[a-z][a-z0-9_]*$")


def _page_args(page: int, page_size: int) -> None:
    """Validate page/page_size against the API's 0-based paging."""
    if page < 0:
        raise InvalidInputError("page must be >= 0", instructions="The API pages 0-based.")
    if not 1 <= page_size <= _MAX_PAGE_SIZE:
        raise InvalidInputError(
            f"page_size must be between 1 and {_MAX_PAGE_SIZE}",
            instructions="Page through long lists with page.",
        )


def _together(key: str, low: Any, high: Any) -> dict[str, Any]:
    """Build a {'and': [{>=: low}, {<=: high}]} operator dict for one key."""
    ops = []
    if low is not None:
        ops.append({">=": low})
    if high is not None:
        ops.append({"<=": high})
    if len(ops) == 1:
        return {key: ops[0]}
    return {key: {"and": ops}}


def _component_filters(component_statuses: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """Turn 'workspace-column:STATUS' entries into triplet_summary filter keys."""
    filters: dict[str, Any] = {}
    for entry in component_statuses:
        component, sep, status = entry.partition(":")
        status = status.strip().upper()
        component = component.strip().lower()
        if not sep or not _TRIPLET_SPLIT_RE.fullmatch(component) or status not in _TRIPLET_STATUSES:
            raise InvalidInputError(
                f"invalid component status {entry!r}; expected 'workspace-column:STATUS'",
                instructions=(
                    "Examples: 'tracker-strip:GOOD', 'dt-dt:GOOD', 'l1t-l1tcalo:GOOD'; "
                    f"STATUS in {sorted(_TRIPLET_STATUSES)}."
                ),
            )
        filters[f"triplet_summary.{component}.{status}"] = {">": 0}
    return filters


def _run_filter(
    *,
    run_number: int | None,
    run_from: int | None,
    run_to: int | None,
    fill_number: int | None,
    significant: bool | None,
    class_name: str | None,
    hlt_key_contains: str | None,
    component_statuses: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    """Build the backend run filter from typed convenience arguments."""
    if run_number is not None and (run_from is not None or run_to is not None):
        raise InvalidInputError(
            "run_number cannot be combined with run_from/run_to",
            instructions="Use either an exact run_number or a run_from/run_to range.",
        )
    filters: dict[str, Any] = {}
    if run_number is not None:
        filters["run_number"] = {"=": run_number}
    elif run_from is not None or run_to is not None:
        filters.update(_together("run_number", run_from, run_to))
    if fill_number is not None:
        filters["oms_attributes.fill_number"] = {"=": fill_number}
    if significant is not None:
        filters["rr_attributes.significant"] = {"=": significant}
    if class_name is not None:
        filters["rr_attributes.class"] = {"=": class_name.strip()}
    if hlt_key_contains is not None:
        filters["oms_attributes.hlt_key"] = {"like": f"%{hlt_key_contains.strip()}%"}
    filters.update(_component_filters(component_statuses))
    return filters


async def _post(client: SafeHttpClient, path: str, body: dict[str, Any], *, what: str) -> Any:
    """Issue one allowlisted read-only JSON POST and decode the response."""
    response = await client.request_json_post(
        f"{BASE}{path}", json_body=body, allowlist=_POST_ALLOWLIST
    )
    payload = json_body(response, source=f"run-registry {what}")
    if isinstance(payload, dict) and payload.get("err"):
        raise UpstreamError(
            f"Run Registry rejected the {what} query: {payload['err']}",
            instructions="Adjust the filters (operators, attribute names, statuses) and retry.",
        )
    return payload


async def _paged(
    client: SafeHttpClient,
    *,
    path: str,
    result_key: str,
    filters: dict[str, Any],
    query: str,
    page: int,
    page_size: int,
) -> SearchResult:
    """POST one filtered_ordered query and normalize it into a SearchResult."""
    _page_args(page, page_size)
    payload = await _post(
        client,
        path,
        {"page": page, "page_size": page_size, "filter": filters, "sortings": []},
        what=result_key,
    )
    items = payload.get(result_key) if isinstance(payload, dict) else None
    total = payload.get("count") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise UpstreamError(
            f"Run Registry returned no {result_key} list",
            instructions="Retry; if it persists, check the runregistry_* discovery notes.",
        )
    return SearchResult(
        source=SOURCE,
        query=query,
        items=[item for item in items if isinstance(item, dict)],
        total_count=total if isinstance(total, int) else None,
        page=page + 1,
        size=len(items),
        has_more=(total is not None and page * page_size + len(items) < total)
        or (total is None and len(items) == page_size),
    )


def _run_query(filters: dict[str, Any]) -> str:
    """Compact human-readable summary of a built filter (no secrets inside)."""
    return json.dumps(filters, separators=(",", ":"))


async def search_runs(
    client: SafeHttpClient,
    *,
    run_number: int | None = None,
    run_from: int | None = None,
    run_to: int | None = None,
    fill_number: int | None = None,
    significant: bool | None = None,
    class_name: str | None = None,
    hlt_key_contains: str | None = None,
    component_statuses: list[str] | tuple[str, ...] = (),
    page: int = 0,
    page_size: int = _DEFAULT_PAGE_SIZE,
) -> SearchResult:
    """Search RR runs with typed filter axes; newest first by default."""
    filters = _run_filter(
        run_number=run_number,
        run_from=run_from,
        run_to=run_to,
        fill_number=fill_number,
        significant=significant,
        class_name=class_name,
        hlt_key_contains=hlt_key_contains,
        component_statuses=component_statuses,
    )
    return await _paged(
        client,
        path="/runs_filtered_ordered",
        result_key="runs",
        filters=filters,
        query=_run_query(filters) or "runs",
        page=page,
        page_size=page_size,
    )


async def get_run(client: SafeHttpClient, *, run_number: int) -> dict[str, Any]:
    """Full RR record of one run (oms_attributes + rr_attributes + triplets)."""
    result = await search_runs(client, run_number=run_number, page_size=1)
    if not result.items:
        raise UpstreamError(
            f"Run Registry has no run {run_number}",
            instructions="Verify the run number (cmsonline_list_runs lists CMS runs).",
        )
    return {"source": SOURCE, "run": result.items[0]}


async def list_run_dataset_names(client: SafeHttpClient, *, run_number: int) -> dict[str, Any]:
    """Dataset names recorded in RR for one run (includes 'online')."""
    response = await client.request("GET", f"{BASE}/get_all_dataset_names_of_run/{run_number}")
    payload = json_body(response, source="run-registry dataset names")
    if not isinstance(payload, list):
        raise UpstreamError(
            f"Run Registry returned no dataset-name list for run {run_number}",
            instructions="Verify the run number (cmsonline_list_runs lists CMS runs).",
        )
    names = [name for name in payload if isinstance(name, str)]
    return {"source": SOURCE, "run_number": run_number, "dataset_names": names, "count": len(names)}


async def search_datasets(
    client: SafeHttpClient,
    *,
    run_number: int | None = None,
    run_from: int | None = None,
    run_to: int | None = None,
    dataset_name_contains: str | None = None,
    component_statuses: list[str] | tuple[str, ...] = (),
    page: int = 0,
    page_size: int = _DEFAULT_PAGE_SIZE,
) -> SearchResult:
    """Search RR per-run certification records (run + '/Era/Type/DQM' name)."""
    if run_number is not None and (run_from is not None or run_to is not None):
        raise InvalidInputError(
            "run_number cannot be combined with run_from/run_to",
            instructions="Use either an exact run_number or a run_from/run_to range.",
        )
    filters: dict[str, Any] = {}
    if run_number is not None:
        filters["run_number"] = {"=": run_number}
    elif run_from is not None or run_to is not None:
        filters.update(_together("run_number", run_from, run_to))
    if dataset_name_contains is not None:
        filters["name"] = {"like": f"%{dataset_name_contains.strip()}%"}
    filters.update(_component_filters(component_statuses))
    return await _paged(
        client,
        path="/datasets_filtered_ordered",
        result_key="datasets",
        filters=filters,
        query=_run_query(filters) or "datasets",
        page=page,
        page_size=page_size,
    )


async def get_dataset(
    client: SafeHttpClient, *, run_number: int, dataset_name: str
) -> dict[str, Any]:
    """One RR certification record by run and exact dataset name."""
    name = dataset_name.strip()
    if not name or (name != "online" and not name.startswith(_REQUIRED_DATASET_PREFIX)):
        raise InvalidInputError(
            f"invalid dataset_name {dataset_name!r}",
            instructions="Pass 'online' or a full RR dataset name like "
            "'/PromptReco/Collisions2026A/DQM' (runregistry_list_run_datasets "
            "lists the names recorded for a run).",
        )
    result = await _paged(
        client,
        path="/datasets_filtered_ordered",
        result_key="datasets",
        filters={"run_number": {"=": run_number}, "name": {"=": name}},
        query=f"run {run_number} dataset {name}",
        page=0,
        page_size=1,
    )
    if not result.items:
        raise UpstreamError(
            f"Run Registry has no dataset {name!r} for run {run_number}",
            instructions="runregistry_list_run_datasets lists the dataset "
            "names recorded for a run.",
        )
    return {"source": SOURCE, "run_number": run_number, "dataset": result.items[0]}


async def get_lumisection_ranges(
    client: SafeHttpClient,
    *,
    run_number: int,
    dataset_name: str = "online",
    by_component: bool = False,
) -> dict[str, Any]:
    """Lumisection certification ranges of one RR dataset of one run."""
    name = dataset_name.strip() or "online"
    path = (
        "/lumisections/rr_lumisection_ranges_by_component"
        if by_component
        else "/lumisections/rr_lumisection_ranges"
    )
    payload = await _post(
        client,
        path,
        {"run_number": run_number, "dataset_name": name},
        what="lumisection ranges",
    )
    _missing = UpstreamError(
        f"Run Registry returned no lumisection ranges for run {run_number} / {name}",
        instructions="Verify the run number and dataset name "
        "(runregistry_list_run_datasets lists names recorded for a run).",
    )
    if by_component:
        if not isinstance(payload, dict) or not all(
            isinstance(value, list) for value in payload.values()
        ):
            raise _missing
        count = sum(len(value) for value in payload.values())
    else:
        if not isinstance(payload, list):
            raise _missing
        count = len(payload)
    return {
        "source": SOURCE,
        "run_number": run_number,
        "dataset_name": name,
        "by_component": by_component,
        "range_count": count,
        "ranges": payload,
    }
