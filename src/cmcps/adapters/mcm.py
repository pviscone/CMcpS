"""Read-only adapter for McM (Monte Carlo Management, cms-PdmV).

Endpoint type: public REST API (no SSO) at
``https://cms-pdmv-prod.web.cern.ch/mcm/public/restapi/`` — the service
publishes its own route index there. Discovery notes:

- ``GET requests/get/<request_id>`` — full request document as
  ``{"results": {...}}``; unknown ids return ``{"results": {}}`` (HTTP 200),
  so emptiness must be detected client-side.
- ``GET requests/get_status_and_approval/<prepid>`` —
  ``{"<prepid>": "submit-done"}`` (status/approval joined).
- ``GET requests/output/<prepid>`` — ``{"<prepid>": [dataset, ...]}``.
- ``GET requests/produces/<path:dataset>`` — dataset name **without the
  leading slash**; returns the full producing request document in
  ``results`` (empty object when nothing matches).
- ``GET requests/from_dataset_name/<string:...>`` is listed but 404s on
  real dataset names (route rejects slashes); not implemented.
- Chained-request GETs are not public except script-generation routes
  (``get_dict``/``get_setup``/...), which are test-script generators, not
  read surface; not implemented.
- Full-text search (``/mcm/restapi/...``) requires SSO and is out of the
  public surface; request ids come from callers or DAS/XSDB results.

GET only, JSON only.
"""

from __future__ import annotations

import re
from typing import Any

from cmcps.adapters.common import json_body
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient

SOURCE = "mcm"

BASE = "https://cms-pdmv-prod.web.cern.ch/mcm/public/restapi"

_HISTORY_KEEP = 20
_STATUS_PARTS = 2

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9-]{4,200}$")
_DATASET_RE = re.compile(r"^/?[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+/[A-Za-z0-9_-]+/?$")


def _check_request_id(request_id: str) -> str:
    cleaned = request_id.strip().strip("/")
    if not _REQUEST_ID_RE.fullmatch(cleaned):
        raise InvalidInputError(
            f"invalid McM request id {request_id!r}",
            instructions="Expected a prepid like TTJets-RunIISummer20UL17wmLHEGEN-00001.",
        )
    return cleaned


def _check_dataset(dataset: str) -> str:
    cleaned = dataset.strip().strip("/")
    if not _DATASET_RE.fullmatch(f"/{cleaned}"):
        raise InvalidInputError(
            f"invalid dataset name {dataset!r}",
            instructions="Expected /primary/processed/tier, e.g. /TTbar/SIM-v1/AODSIM.",
        )
    return cleaned


async def _get_json(client: SafeHttpClient, path: str, *, source: str) -> dict[str, Any]:
    # Public REST API (no SSO): never gate on or inject the CERN SSO session.
    response = await client.request("GET", f"{BASE}/{path}", authenticated=False)
    payload = json_body(response, source=source)
    if not isinstance(payload, dict):
        raise UpstreamError(
            f"mcm returned an unexpected payload for {path}",
            instructions="The public API may be down; check https://cms-pdmv-prod.web.cern.ch/mcm.",
        )
    return payload


def _summarize_request(doc: dict[str, Any]) -> dict[str, Any]:
    """Compact view of a McM request document (full doc can be megabytes)."""
    keys = (
        "prepid",
        "priority",
        "approval",
        "status",
        "pwg",
        "member_of_campaign",
        "dataset_name",
        "process_string",
        "total_events",
        "completed_events",
        "submission_date",
        "reqmgr_name",
        "cadi_line",
        "member_of_chain",
        "time_event",
        "size_event",
        "extension",
        "generator_parameters",
        "history",
    )
    summary = {key: doc.get(key) for key in keys if key in doc}
    raw_history = summary.get("history")
    if isinstance(raw_history, list) and len(raw_history) > _HISTORY_KEEP:
        summary["history"] = raw_history[-_HISTORY_KEEP:]
        summary["history_truncated"] = True
    return summary


async def get_request(
    client: SafeHttpClient,
    *,
    request_id: str,
) -> dict[str, Any]:
    """Full document of one McM request by prepid/request id."""
    rid = _check_request_id(request_id)
    payload = await _get_json(client, f"requests/get/{rid}", source=f"mcm request {rid}")
    results = payload.get("results")
    if not results:
        raise UpstreamError(
            f"mcm request {rid} not found (empty results)",
            instructions="Check the prepid; full-text search is SSO-only in McM.",
        )
    if not isinstance(results, dict):
        raise UpstreamError(
            f"mcm returned unexpected request data for {rid}",
            instructions="Retry; the public API sometimes returns partial payloads.",
        )
    item = _summarize_request(results)
    item["source"] = SOURCE
    item["request_id"] = results.get("prepid") or rid
    item["url"] = f"https://cms-pdmv-prod.web.cern.ch/mcm/requests?prepid={rid}"
    return item


async def get_status_and_approval(
    client: SafeHttpClient,
    *,
    prepid: str,
) -> dict[str, Any]:
    """Status+approval string of one request (e.g. ``submit-done``)."""
    rid = _check_request_id(prepid)
    payload = await _get_json(
        client, f"requests/get_status_and_approval/{rid}", source=f"mcm status {rid}"
    )
    value = payload.get(rid)
    if value is None:
        raise UpstreamError(
            f"mcm returned no status for {rid}",
            instructions="Check the prepid.",
        )
    parts = str(value).split("-", 1)
    return {
        "source": SOURCE,
        "prepid": rid,
        "approval_status": value,
        "approval": parts[0] if len(parts) == _STATUS_PARTS else None,
        "status": parts[1] if len(parts) == _STATUS_PARTS else parts[0],
    }


async def get_output_datasets(
    client: SafeHttpClient,
    *,
    prepid: str,
) -> dict[str, Any]:
    """Output datasets produced by one request (list of dataset names)."""
    rid = _check_request_id(prepid)
    payload = await _get_json(client, f"requests/output/{rid}", source=f"mcm output {rid}")
    datasets = payload.get(rid)
    if datasets is None:
        raise UpstreamError(
            f"mcm returned no output for {rid}",
            instructions="Check the prepid.",
        )
    return {
        "source": SOURCE,
        "prepid": rid,
        "dataset_count": len(datasets) if isinstance(datasets, list) else 0,
        "datasets": datasets if isinstance(datasets, list) else [],
    }


async def find_producing_requests(
    client: SafeHttpClient,
    *,
    dataset: str,
) -> SearchResult:
    """Request(s) that produced a given dataset (name without leading slash)."""
    dsn = _check_dataset(dataset)
    payload = await _get_json(client, f"requests/produces/{dsn}", source=f"mcm produces {dsn}")
    results = payload.get("results")
    items: list[dict[str, Any]] = []
    if isinstance(results, dict) and results:
        items = [{**_summarize_request(results), "source": SOURCE}]
    elif isinstance(results, list):
        items = [
            {**_summarize_request(r), "source": SOURCE} for r in results if isinstance(r, dict)
        ]
    return SearchResult(
        source=SOURCE,
        query=f"produces: /{dsn}",
        items=items,
        total_count=len(items),
        page=1,
        size=len(items),
        has_more=False,
        note="McM public API has no search; dataset must be exact (no wildcards).",
    )
