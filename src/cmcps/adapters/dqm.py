"""Read-only adapter for the CMS DQM GUI (cmsweb.cern.ch/dqm).

Endpoint type: legacy DQM GUI JSON/HTTP API at
``https://cmsweb.cern.ch/dqm/<instance>/`` (offline, online, relval, ...).
Discovery notes (all probed live, 2026-10-27):

- **Auth**: the cmsweb front-end answers ``401`` with an empty body to every
  unauthenticated request and performs **no** SSO redirect — there is no
  cookie login flow. It requests a client certificate at the TLS handshake
  and authenticates X509 grid credentials (dmwm/deployment
  ``frontend/app_dqm_ssl.conf``: ``AUTH_SPEC:aucookie;limited-proxy;cert``;
  the ``cms-auth`` cookie is only minted *after* cert access). All requests
  therefore carry the grid client certificate via the shared client's
  grid SSL context (``Settings.grid_cert_hosts``), never SSO cookies.
- ``GET data/json/samples?match=<regex>&run=<run>`` — ``match`` is a Python
  **regex** (``re.search`` semantics: ``Run2026F*`` matches ``Run2026A``),
  ``run`` an exact run number. Response:
  ``{"samples": [{"type": "offline_data", "items": [{"run": "401605",
  "dataset": "/ZeroBias/.../DQMIO", "version": "", "importversion": 1},
  ...]}]}``. Calling without ``match``/``run`` returns every sample (~138 MB)
  and must never happen: the adapter requires at least one selector.
- ``GET data/json/archive/<run>/<dataset>/<folder..>`` — folder content
  listing. The URL must use the dataset **without its leading slash** (a
  leading slash is a normalize middleware 307). Response:
  ``{"contents": [{"streamerinfo": ""}, {"subdir": "JetMET"}, {"obj": "lumisec",
  "path": "JetMET/lumisec", "properties": {"kind", "type", "lumisect",
  "report", ...}, "tag", "qresults": [...], "nentries",
  "stats": {"x": {...}, ...}}]}``. A **nonexistent (run, dataset) pair
  answers 200 with the folder tree of whatever sample the backend currently
  holds** — the adapter validates every (run, dataset) against the samples
  endpoint (exact lookup, regex-escaped dataset) before any archive call. A
  nonexistent folder answers 200 with only the streamerinfo pseudo-entry;
  ``search=<substring>`` filters the listing server-side.
- ``GET jsonfairy/archive/<run>/<dataset>/<me>`` — JSON-serialized ME content
  (``{"hist": {"type": "TH1", "stats", "xaxis", "values", "bins": {"integral",
  "content": [...]}}}``; TProfile2D objects serialize as nested lists).
  Non-serializable ROOT types and **nonexistent MEs alike** answer
  ``{"hist": "unsupported type"}`` with content-type ``text/html`` — the
  adapter resolves the ME through its parent folder first, both for a real
  not-found signal and for the ROOT type / stats / quality-test metadata.
- ``GET plotfairy/archive/<run>/<dataset>/<me>?w=<px>&h=<px>`` — rendered
  PNG. ``w``/``h`` are **required**: without them and for nonexistent MEs the
  backend returns the same small placeholder image (observed 974-byte PNG,
  identical hash for missing and unsized requests) with HTTP 200, so the
  adapter pre-verifies the ME and always sends explicit dimensions.
- Version-1 routes (``/api/v1/...``) of the newer GUI code answer 404 on the
  deployed ``offline``/``offline-new`` instances — legacy API only.

GET only, JSON + PNG only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

from cmcps.adapters.common import json_body
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient

SOURCE = "dqm"

BASE = "https://cmsweb.cern.ch/dqm"

_MAX_ITEMS = 1000
_MAX_QTESTS = 20
_MAX_CONTENTS = 500
_MAX_RUN = 1_000_000
_MAX_PATH_LENGTH = 500
_MIN_PLOT_DIM = 16
_MAX_PLOT_DIM = 4096

_INSTANCES = ("offline", "online", "relval", "relval-test", "offline-test", "dev")

_DATASET_RE = re.compile(r"^/[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+/[A-Za-z0-9_-]+$")
_ROOT_PATH_BAD_RE = re.compile(r"(?:\.\.(?:/|$)|//|\\\\)")


def _check_instance(instance: str) -> str:
    cleaned = instance.strip()
    if cleaned not in _INSTANCES:
        raise InvalidInputError(
            f"unknown DQM instance {instance!r}",
            instructions=(
                "Instance must be one of: " + ", ".join(_INSTANCES) + ". "
                "The newer /api/v1 GUI routes are not deployed on cmsweb."
            ),
        )
    return cleaned


def _check_run(run: int | str) -> int:
    text = str(run).strip()
    if not text.isdigit() or not 0 < int(text) <= _MAX_RUN:
        raise InvalidInputError(
            f"invalid run number {run!r}",
            instructions="Expected a positive integer run number (e.g. 401605).",
        )
    return int(text)


def _check_dataset(dataset: str) -> str:
    cleaned = dataset.strip()
    if not _DATASET_RE.fullmatch(cleaned):
        raise InvalidInputError(
            f"invalid dataset name {dataset!r}",
            instructions=(
                "Expected /primary/processed/tier, e.g. /ZeroBias/Run2026A-PromptReco-v1/DQMIO."
            ),
        )
    return cleaned


def _check_root_path(path: str, *, allow_empty: bool) -> str:
    cleaned = path.strip().strip("/")
    if not cleaned and allow_empty:
        return ""
    if not cleaned or len(cleaned) > _MAX_PATH_LENGTH or _ROOT_PATH_BAD_RE.search(cleaned):
        raise InvalidInputError(
            f"invalid ROOT-file path {path!r}",
            instructions=(
                "Expected a slash-separated folder/ME path inside the DQM ROOT file, "
                "e.g. JetMET/lumisec (no leading slash, no '..' segments)."
            ),
        )
    return cleaned


def _check_monitor_regex(match: str | None) -> str | None:
    if match is None:
        return None
    cleaned = match.strip()
    if not cleaned:
        return None
    try:
        re.compile(cleaned)
    except re.error as exc:
        raise InvalidInputError(
            f"match is not a valid regular expression ({exc})",
            instructions=(
                "match uses regex syntax (re.search), not glob: write .* for "
                "any characters, e.g. /ZeroBias/Run2026A.*/DQMIO."
            ),
        ) from exc
    return cleaned


def _check_dimension(value: int, *, name: str) -> int:
    if not isinstance(value, int) or not _MIN_PLOT_DIM <= value <= _MAX_PLOT_DIM:
        raise InvalidInputError(
            f"{name} must be an integer between {_MIN_PLOT_DIM} and {_MAX_PLOT_DIM} pixels",
            instructions=f"Pass explicit plotfairy dimensions, e.g. {name}=1000.",
        )
    return value


def _quoted_dataset(dataset: str) -> str:
    """URL-quoted dataset without its leading slash (the GUI's URL form)."""
    return "/".join(quote(part, safe="") for part in dataset.strip("/").split("/"))


def _quoted_path(path: str) -> str:
    return "/".join(quote(part, safe="") for part in path.split("/"))


async def _get_json(
    client: SafeHttpClient,
    url: str,
    *,
    params: Mapping[str, str | int] | None = None,
) -> Any:
    # cmsweb authenticates the grid client certificate at TLS level; SSO
    # cookies do not exist there, so requests never run authenticated.
    response = await client.request("GET", url, params=params, authenticated=False)
    if response.status_code in (401, 403):
        raise UpstreamError(
            f"cmsweb rejected the grid client credential (HTTP {response.status_code})",
            status_code=response.status_code,
            instructions=(
                "Renew the grid credential (voms-proxy-init, or refresh "
                "~/.globus/usercert.pem and userkey.pem) or point "
                "CMCPS_GRID_CERT_FILE/CMCPS_GRID_KEY_FILE at a valid one and retry."
            ),
        )
    return json_body(response, source=SOURCE)


def _parse_samples(payload: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    samples = payload.get("samples") if isinstance(payload, dict) else None
    if not isinstance(samples, list):
        return items
    seen: set[tuple[int, int | None, str]] = set()
    for group in samples:
        entries = group.get("items") if isinstance(group, dict) else None
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            dataset = entry.get("dataset")
            run_text = str(entry.get("run", ""))
            run_part, _, lumi_part = run_text.partition(":")
            if not dataset or not run_part.isdigit():
                continue
            run = int(run_part)
            lumi = int(lumi_part) if lumi_part.isdigit() else None
            key = (run, lumi, dataset)
            if key not in seen:
                seen.add(key)
                items.append({"run": run, "lumi": lumi, "dataset": dataset})
    return items


async def _sample_exists(
    client: SafeHttpClient,
    base: str,
    run: int,
    dataset: str,
) -> bool:
    """Exact (run, dataset) presence check against the samples index.

    The archive endpoints silently serve another sample's content when the
    requested pair does not exist, so every archive/jsonfairy/plotfairy call
    goes through this check first. The regex-escaped dataset makes the
    backend's regex matcher a literal comparison.
    """
    payload = await _get_json(
        client,
        f"{base}/data/json/samples",
        params={"match": re.escape(dataset), "run": run},
    )
    return any(
        item["run"] == run and item["dataset"] == dataset for item in _parse_samples(payload)
    )


async def _require_sample(
    client: SafeHttpClient,
    base: str,
    run: int,
    dataset: str,
) -> None:
    if not await _sample_exists(client, base, run, dataset):
        raise UpstreamError(
            f"DQM sample (run {run}, {dataset}) does not exist",
            status_code=404,
            instructions=(
                "List samples with dqm_list_samples (match is a regex, e.g. "
                f"'{dataset}', run={run}) and retry with an existing pair."
            ),
        )


def _compact_me(entry: dict[str, Any]) -> dict[str, Any]:
    properties = entry.get("properties") or {}
    stats = entry.get("stats") or {}
    axes: dict[str, Any] = {}
    for axis in ("x", "y", "z"):
        axis_stats = stats.get(axis)
        if isinstance(axis_stats, dict):
            axes[axis] = {
                key: axis_stats.get(key) for key in ("nbins", "mean", "rms", "min", "max")
            }
    qresults = entry.get("qresults")
    me: dict[str, Any] = {
        "name": entry.get("obj"),
        "path": entry.get("path"),
        "kind": properties.get("kind"),
        "root_type": properties.get("type"),
        "lumi_plot": properties.get("lumisect") == "1",
        "report": properties.get("report"),
        "tag": entry.get("tag") or None,
        "qresults": qresults[:_MAX_QTESTS] if isinstance(qresults, list) else [],
        "nentries": entry.get("nentries"),
        "stats": axes,
    }
    return me


def _parse_archive(payload: Any) -> dict[str, Any]:
    contents = payload.get("contents") if isinstance(payload, dict) else None
    subdirs: list[dict[str, Any]] = []
    mes: list[dict[str, Any]] = []
    for entry in contents if isinstance(contents, list) else []:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("subdir"), str):
            subdir: dict[str, Any] = {"name": entry["subdir"]}
            if isinstance(entry.get("me_count"), int):
                subdir["me_count"] = entry["me_count"]
            subdirs.append(subdir)
        elif isinstance(entry.get("obj"), str):
            mes.append(_compact_me(entry))
    return {"subdirs": subdirs, "monitoring_elements": mes}


async def _list_folder(
    client: SafeHttpClient,
    base: str,
    run: int,
    dataset: str,
    folder: str,
    *,
    search: str | None = None,
) -> dict[str, Any]:
    url = f"{base}/data/json/archive/{run}/{_quoted_dataset(dataset)}"
    if folder:
        url = f"{url}/{_quoted_path(folder)}"
    params: dict[str, str] = {}
    if search:
        params["search"] = search
    payload = await _get_json(client, url, params=params or None)
    return _parse_archive(payload)


async def _me_info(
    client: SafeHttpClient,
    base: str,
    run: int,
    dataset: str,
    me_path: str,
) -> dict[str, Any]:
    """Resolve one monitoring element through its parent folder listing."""
    folder, _, basename = me_path.rpartition("/")
    listing = await _list_folder(client, base, run, dataset, folder, search=basename)
    for me in listing["monitoring_elements"]:
        if me["path"] == me_path:
            return me
    raise UpstreamError(
        f"monitoring element {me_path!r} does not exist for run {run}, {dataset}",
        status_code=404,
        instructions=(
            "Inspect the parent folder with dqm_get_workspace and pass one of its "
            "monitoring_elements paths."
        ),
    )


async def list_samples(
    client: SafeHttpClient,
    *,
    instance: str = "offline",
    match: str | None = None,
    run: int | str | None = None,
) -> SearchResult:
    """List (run, dataset) samples of one DQM instance (regex match, exact run)."""
    inst = _check_instance(instance)
    pattern = _check_monitor_regex(match)
    number = _check_run(run) if run is not None else None
    if pattern is None and number is None:
        raise InvalidInputError(
            "provide at least one selector (match and/or run)",
            instructions=(
                "Without selectors the GUI returns every sample (~140 MB). "
                "match is a regex (re.search), e.g. '/ZeroBias/Run2026A.*'; "
                "run is exact, e.g. 401605."
            ),
        )
    params: dict[str, str | int] = {}
    if pattern is not None:
        params["match"] = pattern
    if number is not None:
        params["run"] = number
    payload = await _get_json(client, f"{BASE}/{inst}/data/json/samples", params=params)
    items = _parse_samples(payload)
    note = None
    if len(items) > _MAX_ITEMS:
        items = items[:_MAX_ITEMS]
        note = f"Result truncated to {_MAX_ITEMS} samples; narrow with match/run."
    return SearchResult(
        source=f"{SOURCE}:{inst}",
        items=items,
        size=len(items),
        total_count=None,
        has_more=False,
        query=pattern,
        note=note,
    )


async def get_workspace(
    client: SafeHttpClient,
    *,
    run: int | str,
    dataset: str,
    folder: str = "",
    search: str | None = None,
    instance: str = "offline",
) -> dict[str, Any]:
    """List one folder (subfolders + monitoring elements) of a DQM sample."""
    inst = _check_instance(instance)
    number = _check_run(run)
    name = _check_dataset(dataset)
    root_folder = _check_root_path(folder, allow_empty=True)
    base = f"{BASE}/{inst}"
    await _require_sample(client, base, number, name)
    listing = await _list_folder(client, base, number, name, root_folder, search=search)
    if root_folder and not listing["subdirs"] and not listing["monitoring_elements"]:
        raise UpstreamError(
            f"DQM folder {root_folder!r} does not exist for run {number}, {name}",
            status_code=404,
            instructions="List the parent folder with dqm_get_workspace first.",
        )
    truncated = len(listing["monitoring_elements"]) > _MAX_CONTENTS
    result: dict[str, Any] = {
        "source": f"{SOURCE}:{inst}",
        "instance": inst,
        "run": number,
        "dataset": name,
        "folder": root_folder,
        "search": search,
        "subdirs": listing["subdirs"][:_MAX_CONTENTS],
        "monitoring_elements": listing["monitoring_elements"][:_MAX_CONTENTS],
    }
    if truncated or len(listing["subdirs"]) > _MAX_CONTENTS:
        result["note"] = f"Folder content truncated to {_MAX_CONTENTS} entries; use search."
    return result


async def get_monitoring_element(
    client: SafeHttpClient,
    *,
    run: int | str,
    dataset: str,
    me_path: str,
    instance: str = "offline",
) -> dict[str, Any]:
    """One monitoring element: ROOT/quality metadata plus TH1 bin content."""
    inst = _check_instance(instance)
    number = _check_run(run)
    name = _check_dataset(dataset)
    path = _check_root_path(me_path, allow_empty=False)
    base = f"{BASE}/{inst}"
    await _require_sample(client, base, number, name)
    info = await _me_info(client, base, number, name, path)
    payload = await _get_json(
        client,
        f"{base}/jsonfairy/archive/{number}/{_quoted_dataset(name)}/{_quoted_path(path)}",
    )
    hist = payload.get("hist") if isinstance(payload, dict) else None
    result: dict[str, Any] = {
        "source": f"{SOURCE}:{inst}",
        "instance": inst,
        "run": number,
        "dataset": name,
        "me_path": path,
        "me": info,
    }
    if isinstance(hist, dict):
        result["hist"] = hist
    else:
        result["hist"] = None
        result["note"] = (
            "The GUI does not serialize bin content for this ROOT type; "
            "statistics are available under 'me' and the plot via dqm_download_plot."
        )
    return result


_PLOT_FILENAME_BAD_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _plot_filename(instance: str, run: int, me_path: str) -> Path:
    slug = _PLOT_FILENAME_BAD_RE.sub("_", me_path).strip("_")[-120:] or "plot"
    return Path(f"dqm-{instance}-{run}-{slug}.png")


async def download_plot(
    client: SafeHttpClient,
    *,
    run: int | str,
    dataset: str,
    me_path: str,
    width: int = 1000,
    height: int = 600,
    instance: str = "offline",
) -> dict[str, Any]:
    """Bounded PNG download of a rendered monitoring element.

    The ME is resolved first: plotfairy answers 200 with a small placeholder
    PNG for nonexistent MEs (and without explicit w/h), which must never be
    delivered as a real plot.
    """
    inst = _check_instance(instance)
    number = _check_run(run)
    name = _check_dataset(dataset)
    path = _check_root_path(me_path, allow_empty=False)
    wide = _check_dimension(width, name="width")
    high = _check_dimension(height, name="height")
    base = f"{BASE}/{inst}"
    await _require_sample(client, base, number, name)
    await _me_info(client, base, number, name, path)
    url = (
        f"{base}/plotfairy/archive/{number}/{_quoted_dataset(name)}/{_quoted_path(path)}"
        f"?w={wide}&h={high}"
    )
    final = await client.download(url, _plot_filename(inst, number, path), authenticated=False)
    return {
        "source": f"{SOURCE}:{inst}",
        "plot_url": url,
        "path": str(final),
        "size_bytes": final.stat().st_size,
        "width": wide,
        "height": high,
    }
