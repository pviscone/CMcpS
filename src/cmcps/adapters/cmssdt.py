"""Read-only adapter for the CMS Software Development Tools (cmssdt SDT).

Endpoint type: legacy CGI/export, publicly readable:

- ``GET /SDT/cgi-bin/ReleasesXML`` — single XML document listing every
  announced CMSSW release per architecture
  (``<projects><architecture name=...><project label type state/></architecture></projects>``;
  ~20 architectures, ~1200 projects).

Discovery notes: the interactive IB dashboards (``/SDT/html/cmssdt-ib``)
and ``/SDT/jenkins-artifacts/*`` sit behind OIDC
(``client_id=cmssdt-forntend``) — the shared SSO session covers them. The
classic ``showIB.html``/``showmatrix.py`` CGI scripts return 404 (removed).
``ReleasesXML`` is the stable machine-readable public surface, parsed with
stdlib XML and filtered client-side.

IB dashboard data files (live-verified via authenticated session):

- ``GET /SDT/html/data/structure.json`` — release families to IB queues
  (``{<family>: [<queue>, ...], all_prefixes, all_release_queues,
  default_release}``).
- ``GET /SDT/html/data/LatestIBsSummary.json`` —
  ``{<queue>: {<arch>: {latest_IB, status}}}``.
- ``GET /SDT/html/data/<QUEUE>.json`` (~380KB) — base branch, release name
  and ``comparisons`` (newest first) with per-IB build/relval/utest status
  fields, ``merged_prs`` and ``cmsdist_merged_prs``.
- ``GET /SDT/public/cms-sw.github.io/data/cmssw_labels.json`` —
  ``{label: color}``.
- ``GET /SDT/public/cms-sw.github.io/data/prs/<FAMILY>.json`` — PR metadata
  keyed by repo (``cmssw``/``cmsdist``) then PR number.
- ``GET /SDT/public/cms-sw.github.io/data/RelvalsAvailableResults.json`` —
  ``{<full_IB_name>: "<arch>[,<arch>...]"}``.
- ``GET /SDT/cgi-bin/showBuildLogs.py[/fwlite]/<arch>/www/<day>/<queue_dir>/
  <full_IB_name>`` — build-log pages (URL construction only, no fetch);
  ``<queue_dir>`` is e.g. ``20.1-thu-11`` for ``CMSSW_20_1_X_2026-10-22-1100``
  or ``20.1.CPP23-thu-11`` for ``CMSSW_20_1_CPP23_X_2026-10-22-1100``.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from defusedxml.ElementTree import ParseError
from defusedxml.ElementTree import fromstring as safe_xml_parse

from cmcps.adapters.common import json_body
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient

SOURCE = "cmssdt"

RELEASES_URL = "https://cmssdt.cern.ch/SDT/cgi-bin/ReleasesXML"

SDT_BASE = "https://cmssdt.cern.ch/SDT"

_MIN_ERROR_STATUS = 400
_XML_LIMIT = 2000
_LIST_LIMIT = 2000

_IB_NAME_RE = re.compile(
    r"^CMSSW_(\d+)_(\d+)((?:_[A-Za-z0-9]+)*)_X_(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})$"
)
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Per-comparison status-string fields surfaced as-is by the IB details tool.
_STATUS_FIELDS = (
    "check-headers",
    "clang_analyzer",
    "class_versions",
    "crab_tests",
    "dqm_tests",
    "flawfinder",
    "hlt_tests",
    "hlt-p2-timing",
    "igprof",
    "lizard",
    "material_budget",
    "material_budget_comparison",
    "piechart",
    "profiling",
    "reco_event_loop",
    "static_checks",
    "static_checks_rules",
    "python3_tests",
)
# Per-comparison list fields surfaced as counts only (they are large).
_COUNT_FIELDS = ("builds", "relvals", "utests", "addons", "fwlite")

# structure.json top-level keys that are metadata, not family→queue mappings.
_STRUCTURE_META_KEYS = {"all_prefixes", "all_release_queues", "default_release"}


def _check_paging(limit: int, offset: int, cap: int = _LIST_LIMIT) -> None:
    if not 1 <= limit <= cap:
        raise InvalidInputError(f"limit must be between 1 and {cap}")
    if offset < 0:
        raise InvalidInputError("offset must be >= 0")


def _paged_result(
    items: list[dict[str, Any]], query: str, limit: int, offset: int, note: str
) -> SearchResult:
    total = len(items)
    page = items[offset : offset + limit]
    return SearchResult(
        source=SOURCE,
        query=query,
        items=page,
        total_count=total,
        page=(offset // limit) + 1,
        size=len(page),
        has_more=offset + len(page) < total,
        note=note,
    )


async def _fetch_json(client: SafeHttpClient, url: str, source: str) -> Any:
    response = await client.request("GET", url)
    return json_body(response, source=source)


async def list_ib_queues(
    client: SafeHttpClient,
    *,
    family: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> SearchResult:
    """List IB queues and their release family from structure.json."""
    _check_paging(limit, offset)
    payload = await _fetch_json(client, f"{SDT_BASE}/html/data/structure.json", "cmssdt structure")
    if not isinstance(payload, dict):
        raise UpstreamError("cmssdt structure.json returned an unexpected payload")
    items: list[dict[str, Any]] = []
    for fam, queues in payload.items():
        if fam in _STRUCTURE_META_KEYS or not isinstance(queues, list):
            continue  # skips all_prefixes / all_release_queues / default_release
        if family and family.lower() not in fam.lower():
            continue
        for queue in queues:
            items.append({"queue": queue, "family": fam})
    default = payload.get("default_release")
    return _paged_result(
        items,
        f"family~{family!r}" if family else "all IB queues",
        limit,
        offset,
        f"default_release={default}. Filters match family as a case-insensitive substring.",
    )


async def ib_summary(
    client: SafeHttpClient,
    *,
    queue: str | None = None,
    arch: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> SearchResult:
    """Latest IB per queue/architecture from LatestIBsSummary.json."""
    _check_paging(limit, offset)
    payload = await _fetch_json(
        client, f"{SDT_BASE}/html/data/LatestIBsSummary.json", "cmssdt latest IBs"
    )
    if not isinstance(payload, dict):
        raise UpstreamError("cmssdt LatestIBsSummary.json returned an unexpected payload")
    items: list[dict[str, Any]] = []
    for queue_name, archs in payload.items():
        if queue and queue.lower() not in queue_name.lower():
            continue
        if not isinstance(archs, dict):
            continue
        for arch_name, info in archs.items():
            if arch and arch.lower() not in arch_name.lower():
                continue
            arch_info = info if isinstance(info, dict) else {}
            status_value = arch_info.get("status")
            if status and (status_value or "").lower() != status.lower():
                continue
            items.append(
                {
                    "queue": queue_name,
                    "arch": arch_name,
                    "latest_ib": arch_info.get("latest_IB"),
                    "status": status_value,
                }
            )
    query = (
        "; ".join(
            part
            for part in (
                f"queue~{queue!r}" if queue else "",
                f"arch~{arch!r}" if arch else "",
                f"status={status}" if status else "",
            )
            if part
        )
        or "latest IBs"
    )
    return _paged_result(
        items,
        query,
        limit,
        offset,
        "queue/arch filters are case-insensitive substrings; status is an exact match.",
    )


async def get_ib_details(
    client: SafeHttpClient,
    *,
    queue: str,
    comparison_index: int = 0,
    ib_date: str | None = None,
) -> dict[str, Any]:
    """Bounded details of one IB comparison for a queue (newest first)."""
    if comparison_index < 0:
        raise InvalidInputError("comparison_index must be >= 0")
    payload = await _fetch_json(
        client, f"{SDT_BASE}/html/data/{queue}.json", f"cmssdt IB details for {queue}"
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("comparisons"), list):
        raise UpstreamError(
            f"cmssdt returned no IB comparisons for queue {queue!r}",
            instructions="Check the queue name with cmssdt_list_ib_queues.",
        )
    comparisons: list[Any] = payload["comparisons"]
    if ib_date is not None:
        match = next(
            (c for c in comparisons if isinstance(c, dict) and c.get("ib_date") == ib_date),
            None,
        )
        if match is None:
            raise UpstreamError(
                f"no IB comparison with ib_date {ib_date!r} in queue {queue!r}",
                status_code=404,
                instructions="Use cmssdt_ib_summary to find valid IB dates.",
            )
        comparison = match
    else:
        if comparison_index >= len(comparisons):
            raise InvalidInputError(
                f"comparison_index {comparison_index} out of range "
                f"(queue {queue!r} has {len(comparisons)} comparisons, 0 = newest)"
            )
        comparison = comparisons[comparison_index]
    if not isinstance(comparison, dict):
        raise UpstreamError("cmssdt IB comparison entry has an unexpected shape")
    result: dict[str, Any] = {
        "queue": queue,
        "base_branch": payload.get("base_branch"),
        "release_name": payload.get("release_name"),
        "ib_date": comparison.get("ib_date"),
        "isIB": comparison.get("isIB"),
        "inProgress": comparison.get("inProgress"),
        "next_ib": comparison.get("next_ib"),
        "compared_tags": comparison.get("compared_tags"),
        "cmsdist_compared_tags": comparison.get("cmsdist_compared_tags"),
        "comp_baseline": comparison.get("comp_baseline"),
        "comp_baseline_state": comparison.get("comp_baseline_state"),
        "status": {key: comparison[key] for key in _STATUS_FIELDS if key in comparison},
        "counts": {
            key: len(comparison[key])
            for key in _COUNT_FIELDS
            if isinstance(comparison.get(key), list)
        },
        "merged_prs": comparison.get("merged_prs") or [],
        "cmsdist_merged_prs": comparison.get("cmsdist_merged_prs") or {},
    }
    return result


async def list_cmssw_labels(
    client: SafeHttpClient,
    *,
    prefix: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> SearchResult:
    """List CMSSW GitHub labels and colors."""
    _check_paging(limit, offset)
    payload = await _fetch_json(
        client,
        f"{SDT_BASE}/public/cms-sw.github.io/data/cmssw_labels.json",
        "cmssw labels",
    )
    if not isinstance(payload, dict):
        raise UpstreamError("cmssw labels returned an unexpected payload")
    items = [
        {"label": label, "color": color}
        for label, color in sorted(payload.items())
        if not prefix or label.lower().startswith(prefix.lower())
    ]
    return _paged_result(
        items,
        f"label prefix {prefix!r}" if prefix else "all labels",
        limit,
        offset,
        "Labels are sorted alphabetically; prefix filter is case-insensitive.",
    )


async def list_open_prs(
    client: SafeHttpClient,
    *,
    family: str,
    repo: str | None = None,
    state: str | None = "open",
    label: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> SearchResult:
    """List pull requests for a release family, newest first."""
    _check_paging(limit, offset)
    if repo is not None and repo not in ("cmssw", "cmsdist"):
        raise InvalidInputError("repo must be 'cmssw' or 'cmsdist' (omit for both)")
    payload = await _fetch_json(
        client,
        f"{SDT_BASE}/public/cms-sw.github.io/data/prs/{family}.json",
        f"PRs for {family}",
    )
    if not isinstance(payload, dict):
        raise UpstreamError(f"cmssw PR data for {family!r} returned an unexpected payload")
    repos = [repo] if repo else [r for r, v in payload.items() if isinstance(v, dict)]
    items: list[dict[str, Any]] = []
    for repo_name in repos:
        prs = payload.get(repo_name)
        if not isinstance(prs, dict):
            continue
        for number, pr in prs.items():
            if not isinstance(pr, dict):
                continue
            pr_state = pr.get("state")
            if state and (pr_state or "").lower() != state.lower():
                continue
            labels = pr.get("labels") or []
            if label and not any(label.lower() in str(one).lower() for one in labels):
                continue
            items.append(
                {
                    "repo": repo_name,
                    "number": pr.get("number", int(number) if str(number).isdigit() else number),
                    "title": pr.get("title"),
                    "author": pr.get("author"),
                    "state": pr_state,
                    "labels": labels,
                    "branch": pr.get("branch"),
                    "created_at": pr.get("created_at"),
                    "merged_at": pr.get("merged_at"),
                }
            )
    items.sort(key=lambda item: int(item.get("created_at") or 0), reverse=True)
    query = "; ".join(
        part
        for part in (
            f"family={family}",
            f"repo={repo}" if repo else "",
            f"state={state}" if state else "",
            f"label~{label!r}" if label else "",
        )
        if part
    )
    return _paged_result(
        items,
        query,
        limit,
        offset,
        "Timestamps are unix seconds as strings; items are sorted newest first.",
    )


async def relval_archs(
    client: SafeHttpClient,
    *,
    ib_name_contains: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> SearchResult:
    """List which architectures have RelVal results per full IB name."""
    _check_paging(limit, offset)
    payload = await _fetch_json(
        client,
        f"{SDT_BASE}/public/cms-sw.github.io/data/RelvalsAvailableResults.json",
        "relval results",
    )
    if not isinstance(payload, dict):
        raise UpstreamError("relval results returned an unexpected payload")
    items = [
        {
            "ib_name": ib_name,
            "archs": [a for a in str(archs).split(",") if a] if archs else [],
        }
        for ib_name, archs in sorted(payload.items())
        if not ib_name_contains or ib_name_contains.lower() in ib_name.lower()
    ]
    return _paged_result(
        items,
        f"IB name ~ {ib_name_contains!r}" if ib_name_contains else "all IBs with relval results",
        limit,
        offset,
        "Filter is a case-insensitive substring match on the full IB name.",
    )


def build_log_url(ib_name: str, *, arch: str, fwlite: bool = False) -> dict[str, Any]:
    """Construct the showBuildLogs URL for an IB (pure derivation, no fetch)."""
    match = _IB_NAME_RE.match(ib_name)
    if not match:
        raise InvalidInputError(
            f"invalid IB name {ib_name!r}",
            instructions="Expected: CMSSW_<M>_<m>[_<variant>...]_X_YYYY-MM-DD-HHMM, "
            "e.g. CMSSW_20_1_X_2026-10-22-1100.",
        )
    major, minor, rest, year, month, day_of_month, hour, _minute = match.groups()
    try:
        weekday = _WEEKDAYS[date(int(year), int(month), int(day_of_month)).weekday()]
    except ValueError as exc:
        raise InvalidInputError(f"invalid IB date in {ib_name!r}: {exc}") from exc
    parts = [major, minor]
    if rest:
        parts.extend(part for part in rest.split("_") if part)
    queue_dir = f"{'.'.join(parts)}-{weekday}-{hour}"
    prefix = f"{SDT_BASE}/cgi-bin/showBuildLogs.py"
    if fwlite:
        prefix += "/fwlite"
    url = f"{prefix}/{arch}/www/{weekday}/{queue_dir}/{ib_name}"
    return {"url": url, "ib_name": ib_name, "arch": arch}


async def list_releases(
    client: SafeHttpClient,
    *,
    arch_contains: str | None = None,
    label_contains: str | None = None,
    state: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> SearchResult:
    """List announced CMSSW releases per architecture (with client filters)."""
    if not 1 <= limit <= _XML_LIMIT:
        raise InvalidInputError(f"limit must be between 1 and {_XML_LIMIT}")
    if offset < 0:
        raise InvalidInputError("offset must be >= 0")
    response = await client.request("GET", RELEASES_URL)
    if response.status_code >= _MIN_ERROR_STATUS:
        raise UpstreamError(
            f"cmssdt ReleasesXML failed with status {response.status_code}",
            status_code=response.status_code,
        )
    try:
        root = safe_xml_parse(response.content.decode("utf-8", errors="replace"))
    except ParseError as exc:
        raise UpstreamError(
            "cmssdt ReleasesXML returned non-XML content",
            instructions="The CGI may be down or moved; check cmssdt.cern.ch/SDT.",
        ) from exc
    items: list[dict[str, Any]] = []
    for arch in root.iter("architecture"):
        arch_name = arch.get("name") or ""
        if arch_contains and arch_contains.lower() not in arch_name.lower():
            continue
        for project in arch.iter("project"):
            label = project.get("label") or ""
            if label_contains and label_contains.lower() not in label.lower():
                continue
            state_value = project.get("state") or ""
            if state and state_value.lower() != state.lower():
                continue
            items.append(
                {
                    "architecture": arch_name,
                    "label": label,
                    "type": project.get("type"),
                    "state": state_value,
                    "url": f"https://cmssdt.cern.ch/SDT/html/showIB.html#CMSSW_{label}?srt_log",
                }
            )
    total = len(items)
    paged = items[offset : offset + limit]
    return SearchResult(
        source=SOURCE,
        query="; ".join(
            part
            for part in (
                f"arch~{arch_contains!r}" if arch_contains else "",
                f"label~{label_contains!r}" if label_contains else "",
                f"state={state}" if state else "",
            )
            if part
        )
        or "all releases",
        items=paged,
        total_count=total,
        page=(offset // limit) + 1,
        size=len(paged),
        has_more=offset + len(paged) < total,
        note="Single XML feed parsed and filtered client-side; filters are "
        "substring matches (case-insensitive).",
    )
