"""Read-only adapter for CMS Online operations data via the OMS API.

Discovery notes: ``cmsonline.cern.ch`` is an Oracle WebCenter/ADF portal —
every URL returns the same JavaScript bootstrap; no addressable REST surface
behind it. CMS Online detector/status/operations data is served
programmatically by the Online Monitoring System (OMS) at
``https://cmsoms.cern.ch/agg/api/v1/``. OMS uses a JSON:API-style envelope:
``data[]`` rows with ``id``/``type``/``attributes``,
``meta.totalResourceCount``, and ``page[offset]``/``page[limit]``
pagination. Anonymous probing returns ``401 Unauthorized`` — the API sits
behind CERN authentication and is reached through the injected SSO session
or from inside the CERN network.

Endpoints used: ``GET /agg/api/v1/{version,runs,fills,lumisections}`` with
``filter[<attr>][EQ|GEQ|LEQ]`` operators. GET only, JSON only.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from playwright.async_api import Error as PlaywrightError

from cmcps.adapters.common import json_body
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient

SOURCE = "cms-online"
BASE = "https://cmsoms.cern.ch/agg/api/v1"

_MAX_PAGE_LIMIT = 100
_RUN_WINDOW = 250


def _envelope(payload: Any) -> tuple[list[dict[str, Any]], int | None]:
    """Flatten an OMS JSON:API response into attribute rows + total."""
    data = payload.get("data") if isinstance(payload, dict) else None
    items = []
    if isinstance(data, list):
        items = [row.get("attributes") or {} for row in data if isinstance(row, dict)]
    elif isinstance(data, dict):
        items = [data.get("attributes") or data]
    meta = payload.get("meta") if isinstance(payload, dict) else None
    total = meta.get("totalResourceCount") if isinstance(meta, dict) else None
    return items, total if isinstance(total, int) else None


async def _query(
    client: SafeHttpClient,
    resource: str,
    *,
    filters: dict[str, str],
    limit: int,
    offset: int,
) -> SearchResult:
    if not 1 <= limit <= _MAX_PAGE_LIMIT:
        raise InvalidInputError(
            f"limit must be between 1 and {_MAX_PAGE_LIMIT}",
            instructions="Page through long lists with offset.",
        )
    if offset < 0:
        raise InvalidInputError("offset must be >= 0")
    params: dict[str, str | int] = {"page[limit]": limit, "page[offset]": offset}
    params.update(filters)
    response = await client.request("GET", f"{BASE}/{resource}", params=params)
    payload = json_body(response, source=f"cms-online {resource}")
    items, total = _envelope(payload)
    return SearchResult(
        source=SOURCE,
        query="; ".join(f"{k}={v}" for k, v in filters.items()) or resource,
        items=items,
        total_count=total,
        page=(offset // limit) + 1,
        size=len(items),
        has_more=(total is not None and offset + len(items) < total)
        or (total is None and len(items) == limit),
    )


async def get_version(client: SafeHttpClient) -> dict[str, Any]:
    """OMS API version; doubles as a connectivity/auth probe."""
    response = await client.request("GET", f"{BASE}/version")
    payload = json_body(response, source="cms-online version")
    data = payload.get("data") if isinstance(payload, dict) else None
    return {
        "source": SOURCE,
        "version": (data or {}).get("version") if isinstance(data, dict) else None,
        "raw": data if isinstance(data, dict) else payload,
    }


async def list_runs(
    client: SafeHttpClient,
    *,
    run_number: int | None = None,
    fill_number: int | None = None,
    limit: int = 20,
    offset: int = 0,
) -> SearchResult:
    """List runs; run_number expands to a +/-250 window (no exact run endpoint)."""
    filters: dict[str, str] = {}
    if run_number is not None:
        if run_number < 1:
            raise InvalidInputError("run_number must be >= 1")
        filters["filter[run_number][GEQ]"] = str(run_number - _RUN_WINDOW)
        filters["filter[run_number][LEQ]"] = str(run_number + _RUN_WINDOW)
    if fill_number is not None:
        if fill_number < 1:
            raise InvalidInputError("fill_number must be >= 1")
        filters["filter[fill_number][EQ]"] = str(fill_number)
    return await _query(client, "runs", filters=filters, limit=limit, offset=offset)


async def get_run(client: SafeHttpClient, *, run_number: int) -> dict[str, Any]:
    """Full attribute record of one run."""
    if run_number < 1:
        raise InvalidInputError("run_number must be >= 1")
    response = await client.request(
        "GET",
        f"{BASE}/runs",
        params={"filter[run_number][EQ]": run_number, "page[limit]": 1},
    )
    payload = json_body(response, source="cms-online run")
    items, _ = _envelope(payload)
    if not items:
        raise UpstreamError(
            f"run {run_number} not found in OMS",
            instructions="Check the run_number; OMS covers recent data-taking eras.",
        )
    item = dict(items[0])
    item["source"] = SOURCE
    item["url"] = f"{BASE}/runs?filter[run_number][EQ]={run_number}"
    return item


async def list_fills(
    client: SafeHttpClient,
    *,
    fill_number_min: int | None = None,
    limit: int = 20,
    offset: int = 0,
) -> SearchResult:
    """List LHC fills, optionally with a >= lower bound on fill_number."""
    filters: dict[str, str] = {}
    if fill_number_min is not None:
        if fill_number_min < 1:
            raise InvalidInputError("fill_number_min must be >= 1")
        filters["filter[fill_number][GEQ]"] = str(fill_number_min)
    return await _query(client, "fills", filters=filters, limit=limit, offset=offset)


async def list_lumisections(
    client: SafeHttpClient,
    *,
    run_number: int,
    limit: int = 50,
    offset: int = 0,
) -> SearchResult:
    """Lumisection records of one run."""
    if run_number < 1:
        raise InvalidInputError("run_number must be >= 1")
    return await _query(
        client,
        "lumisections",
        filters={"filter[run_number][EQ]": str(run_number)},
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# CMS Online portal: shift schedule (pages_common/shiftlist)
#
# Discovery (live, authenticated): the portal shift-list page renders a
# single-day grid as one HTML table (<table bgcolor="Lavender">):
# - header row 0: time + system columns (e.g. Central, DAQ, HGCAL, TC);
# - header row 1: role per system (e.g. "LS3 Technical Shifter",
#   "sysadmin oncall", "CR2");
# - 24 hourly rows; covered blocks appear as <td bgcolor="ddddbb"
#   rowspan="N"> with <a onclick="showShifterInfo('ID')"><div
#   class="shifterInfo">First<br>Last</div></a>; uncovered cells are empty.
# rowspan N spans the next N hours of that column, so subsequent rows omit
# the cell for that system — the parser tracks per-column spans.
#
# Cookie-less (or fresh-session) answers are not the grid but the SAML login
# form or the ADF ``runLoopback`` shell; those take the headless-browser
# render fallback (``client.render_page``) with an in-page date form submit.
_SHIFT_LIST_URL = "https://cmsonline.cern.ch/webcenter/portal/cmsonline/pages_common/shiftlist"
_SHIFT_CELL_RE = re.compile(r"<t[dh](?P<attrs>[^>]*)>(?P<content>.*?)</t[dh]>", re.DOTALL)
_SHIFT_CLASS_RE = re.compile(r'class="([A-Za-z0-9]+)"')
_SHIFT_SPAN_RE = re.compile(r'rowspan="(\d+)"')
_SHIFT_NAME_RE = re.compile(r'<div class="shifterInfo">(.*?)</div>', re.DOTALL)
_SHIFT_ID_RE = re.compile(r"showShifterInfo\('(\d+)'\)")
_SHIFT_TABLE_RE = re.compile(r'<table bgcolor="Lavender".*?</table>', re.DOTALL)
_MIN_ERROR_STATUS = 400
_MIN_SHIFT_ROWS = 4
_SHIFT_PORTLET_ID = "1552869597"
_SHIFT_ADF_EVENT_PORTLET = "T:dclay:portlet1167597126"
_SHIFT_ADF_PAGE_ID = "194"
_SHIFT_FIELD_PREFIX = "_adfp_portlet_field___adfpfp_f1552869597_adfpsep_f0_adfpsep_"
_SHIFT_HIDDEN_INPUT_RE = re.compile(r'<input[^>]+name="([^"]+)"[^>]*>')
_SHIFT_VALUE_RE = re.compile(r'value="([^"]*)"')
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _shift_text(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return " ".join(text.split())


def _shift_rows(table_html: str) -> list[list[dict[str, Any]]]:
    rows = []
    for row_html in re.findall(r"<tr>(.*?)</tr>", table_html, re.DOTALL):
        cells = []
        for m in _SHIFT_CELL_RE.finditer(row_html):
            attrs = m.group("attrs")
            cls = _SHIFT_CLASS_RE.search(attrs)
            span = _SHIFT_SPAN_RE.search(attrs)
            cells.append(
                {
                    "cls": cls.group(1) if cls else None,
                    "span": int(span.group(1)) if span else 1,
                    "content": m.group("content"),
                }
            )
        rows.append(cells)
    return rows


def _walk_shift_rows(
    rows: list[list[dict[str, Any]]], systems: list[str], roles: list[str]
) -> list[dict[str, Any]]:
    active: dict[int, dict[str, Any]] = {}
    blocks: list[dict[str, Any]] = []
    hour = 0
    for cells in rows[2:]:
        time_label = _shift_text(cells[0]["content"]) if cells else ""
        remaining = list(cells[1:])
        col = 0
        for sys_idx, system in enumerate(systems):
            state = active.get(sys_idx)
            if state and state["remaining"] > 0:
                state["remaining"] -= 1
                continue
            if col >= len(remaining):
                break
            cell = remaining[col]
            col += 1
            name, shifter_id = _shift_name(cell["content"])
            span = cell["span"]
            if name:
                blocks.append(
                    {
                        "system": system,
                        "role": roles[sys_idx] if sys_idx < len(roles) else None,
                        "name": name,
                        "shifter_id": shifter_id,
                        "start_label": time_label,
                        "start_hour": hour,
                        "hours": span,
                    }
                )
                active[sys_idx] = {"remaining": span - 1}
            elif sys_idx in active:
                del active[sys_idx]
        hour += 1
    return blocks


def _shift_name(content: str) -> tuple[str | None, str | None]:
    name_match = _SHIFT_NAME_RE.search(content)
    if not name_match:
        return None, None
    name = _shift_text(name_match.group(1)) or None
    id_match = _SHIFT_ID_RE.search(content)
    return name, id_match.group(1) if id_match else None


def _shift_get_fields(html: str) -> dict[str, str]:
    fields = {}
    for m in _SHIFT_HIDDEN_INPUT_RE.finditer(html):
        value_match = _SHIFT_VALUE_RE.search(m.group(0))
        fields[m.group(1)] = value_match.group(1) if value_match else ""
    return {k: v for k, v in fields.items() if v is not None}


def _shift_post(date: str) -> dict[str, str]:
    if not _DATE_RE.fullmatch(date):
        raise InvalidInputError(
            f"invalid shift date {date!r}",
            instructions="Use ISO date format, e.g. 2026-10-27.",
        )
    year, month, day = date.split("-")
    return {
        _SHIFT_FIELD_PREFIX + "year": year,
        _SHIFT_FIELD_PREFIX + "month": str(int(month)),
        _SHIFT_FIELD_PREFIX + "day": str(int(day)),
        _SHIFT_FIELD_PREFIX + "submit": "Show",
        _SHIFT_FIELD_PREFIX + "shiftType": "2",
        _SHIFT_FIELD_PREFIX + "shiftFlavour": "0",
        _SHIFT_FIELD_PREFIX + "gridType": "1",
        f"_adfp_submitted_portlet.{_SHIFT_PORTLET_ID}": "true",
        f"_adfp_submitted_form.{_SHIFT_PORTLET_ID}": "f0",
        "event": _SHIFT_ADF_EVENT_PORTLET,
        f"event.{_SHIFT_ADF_EVENT_PORTLET}": (
            '<m xmlns="http://oracle.com/richClient/comm"><k v="type"><s>action</s></k></m>'
        ),
        "oracle.adf.view.rich.PROCESS": f"f1,{_SHIFT_ADF_EVENT_PORTLET}",
        "Adf-Page-Id": _SHIFT_ADF_PAGE_ID,
    }


_PORTAL_GATES = ("SAMLRequest", "runLoopback")


def _portal_gate(html: str) -> bool:
    """The portal serves a login/loopback shell instead of content."""
    return any(marker in html for marker in _PORTAL_GATES)


def _shift_render_interact(date: str) -> Callable[[Any], Awaitable[bool]]:
    """Drive the shiftlist date form in the rendered-browser fallback."""
    year, month, day = date.split("-")
    values = [year, str(int(month)), str(int(day))]

    async def interact(page: Any) -> bool:
        applied = await page.evaluate(
            """([y, m, d]) => {
                const fields = [...document.querySelectorAll("input, select")];
                const suffixed = (suffix) =>
                    fields.find((f) => typeof f.name === "string" && f.name.endsWith(suffix));
                const targets = [
                    [suffixed("year"), y],
                    [suffixed("month"), m],
                    [suffixed("day"), d],
                ];
                if (targets.some(([field]) => !field)) return false;
                for (const [field, value] of targets) field.value = value;
                const show = fields.find((f) => f.type === "submit" && f.value === "Show");
                if (!show) return false;
                show.click();
                return true;
            }""",
            values,
        )
        if not applied:
            return False
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightError:
            # ADF may answer with a partial update instead of a navigation.
            await page.wait_for_timeout(1_000)
        return await page.evaluate(
            """([y, m, d]) => {
                const fields = [...document.querySelectorAll("input, select")];
                const suffixed = (suffix) =>
                    fields.find((f) => typeof f.name === "string" && f.name.endsWith(suffix));
                const num = (v) => Number.parseInt(v, 10);
                const fy = suffixed("year"), fm = suffixed("month"), fd = suffixed("day");
                if (!fy || !fm || !fd) return false;
                return (
                    num(fy.value) === num(y) &&
                    num(fm.value) === num(m) &&
                    num(fd.value) === num(d)
                );
            }""",
            values,
        )

    return interact


async def _fetch_shift_html(client: SafeHttpClient, date: str | None) -> str:
    """One full GET (+ optional ADF date POST) round-trip against the portal."""
    response = await client.request("GET", _SHIFT_LIST_URL)
    if response.status_code >= _MIN_ERROR_STATUS:
        raise UpstreamError(
            f"shift list failed with status {response.status_code}",
            status_code=response.status_code,
        )
    html = response.content.decode("utf-8", errors="replace")
    if date is not None and "SAMLRequest" not in html:
        data = _shift_get_fields(html)
        data.update(_shift_post(date))
        post_response = await client.request_form_post(_SHIFT_LIST_URL, data=data)
        html = post_response.content.decode("utf-8", errors="replace")
    return html


async def get_shift_schedule(client: SafeHttpClient, *, date: str | None = None) -> dict[str, Any]:
    """CMS Online shift schedule: role blocks per system for one date.

    Date selection goes through the portal's daily-view form POST (year/
    month/day fields + the ADF action lifecycle parameters, reconstructed
    from the working browser interaction). ``date=None`` = current page
    default (today).
    """
    html = await _fetch_shift_html(client, date)
    if _portal_gate(html):
        rendered = await client.render_page(
            _SHIFT_LIST_URL,
            interact=_shift_render_interact(date) if date is not None else None,
        )
        if rendered is not None:
            html = rendered
    if _portal_gate(html):
        raise UpstreamError(
            "portal answered with the login page instead of the shift grid",
            instructions="Call cern_auth_sign_in again to refresh the portal session.",
        )
    table_match = _SHIFT_TABLE_RE.search(html)
    if table_match is None:
        raise UpstreamError(
            "no shift grid table found on the shift-list page",
            instructions="The portal layout may have changed; report the page structure.",
        )
    rows = _shift_rows(table_match.group(0))
    if len(rows) < _MIN_SHIFT_ROWS:
        raise UpstreamError(
            "shift grid table has unexpected shape",
            instructions=(
                "Report the portal page structure; parser expects 2 header rows + 24 hour rows."
            ),
        )
    systems = [_shift_text(c["content"]) for c in rows[0][1:]]
    roles = [_shift_text(c["content"]) for c in rows[1]]
    blocks = _walk_shift_rows(rows, systems, roles)
    return {
        "source": SOURCE,
        "systems": systems,
        "roles": roles,
        "block_count": len(blocks),
        "items": blocks,
        "date": date,
        "url": _SHIFT_LIST_URL,
        "note": "Daily shift grid from the CMS Online shift-list page; "
        "hours are 1-hour slots starting at start_label.",
    }
