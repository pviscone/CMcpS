"""Read-only adapter for the CMS Online E-Log (cmsonline portal ELog webapp).

Discovery (live, authenticated): the portal is Oracle WebCenter + SASL; its
ELog portlet forwards a JSON backend at ``POST /Elog/json.do`` (a
DataTables server-side endpoint). Replicating the working browser exchange
proved these constraints:

- Every call must carry the full DataTables envelope (``sEcho``,
  ``iColumns``/``sSearch_i``/``bRegex_i``/``bSearchable_i``/``bSortable_i``,
  ``iDisplayStart/Length``, ``iSortingCols=1``, ``iSortCol_0=5``,
  ``sSortDir_0=desc``) or the backend silently returns HTTP 200 with an
  empty body.
- ``p_subId=""`` means "all logbooks"; scoped searches use numeric ids
  (user-visible: General → Shift leader = subId 7).
- ``p_imgLink`` and ``p_msgLink`` templates are **mandatory** — without
  them the response is empty; with them rows embed entry links.
- Rows: ``[msg_id, section, icon_html, subject_html, type, date, body_html]``.
- Entry detail: GET portal page with
  ``_piref<pid>.strutsAction=/viewMessageDetails.do?msgId=..&subId=..``
  (the portlet id ``__adfpwp_action_portlet=N`` is read from the elog page).
- Attachments referenced via ``/Elog/htdocs/data/<logId>/<file>`` paths.

The portal POST path is the curated ``request_form_post`` allowlisted to
``cmsonline.cern.ch`` (read-only semantics; no state mutation). Detail GETs
fall back to the headless-browser render (``client.render_page``) when the
portal answers with the SAML login form or its ADF loopback shell.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from cmcps.adapters.common import html_to_text, json_body
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient

SOURCE = "cms-online-elog"

ELOG_PAGE = "https://cmsonline.cern.ch/webcenter/portal/cmsonline/pages_common/elog"
JSON_URL = "https://cmsonline.cern.ch/Elog/json.do"

_DETAIL_SUB_ID = 495  # subId used in the persisted detail-link template

_MAX_LIMIT = 100
_MAX_ENTRIES = 25
_PORTLET_RE = re.compile(r"__adfpwp_action_portlet=(\d+)")
_ENTRY_LINK_RE = re.compile(r"msgId%3D(\d+)%26subId%3D(\d+)")
_ATTACHMENT_RE = re.compile(r"/Elog/htdocs/data/[^\"\s>]+")

_IMG_LINK = (
    '<img border="0" src="/Elog/htdocs/default/entry.png" alt="Entry" title="Entry" />&nbsp;'
)
_MSG_LINK = (
    "<a href=\"javascript:adfpp683379043_doAnchor('"
    "https://cmsonline.cern.ch:443/webcenter/portal/cmsonline/pages_common/elog?"
    "__adfpwp_action_portlet=683379043&__adfpwp_mode.683379043=1&"
    "_piref683379043.strutsAction="
    "%2FviewMessageDetails.do%3FmsgId%3D_id_%26subId%3D495')"
    '">_elog_</a>'
)
_XHR_HEADERS = {"X-Requested-With": "XMLHttpRequest"}
_PORTAL_GATES = ("SAMLRequest", "runLoopback")


def _portal_gate(html: str) -> bool:
    """The portal serves a login/loopback shell instead of content."""
    return any(marker in html for marker in _PORTAL_GATES)


class _TextMin(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join("".join(self.parts).split())


def _mini_text(fragment: str) -> str:
    parser = _TextMin()
    parser.feed(fragment)
    return parser.text()


def _check_text(value: str) -> str:
    cleaned = value.strip()
    if any(c in cleaned for c in ("\x00", "\r", "\n")):
        raise InvalidInputError("text filter must be a single line")
    return cleaned


def _check_limit(limit: int) -> int:
    if not 1 <= limit <= _MAX_LIMIT:
        raise InvalidInputError(f"limit must be between 1 and {_MAX_LIMIT}")
    return limit


def _datatable_payload(
    *,
    text: str,
    sub_id: str,
    author: str,
    subject: str,
    log_type: str,
    from_date: str,
    to_date: str,
    limit: int,
    offset: int,
) -> dict[str, str | int]:
    data: dict[str, str | int] = {
        "sEcho": 1,
        "iColumns": 7,
        "sColumns": "",
        "iDisplayStart": offset,
        "iDisplayLength": min(limit, _MAX_ENTRIES),
        "sSearch": "",
        "bRegex": "false",
        "iSortingCols": 1,
        "iSortCol_0": 5,
        "sSortDir_0": "desc",
        "p_author": author,
        "p_type": log_type,
        "p_subject": subject,
        "p_subId": sub_id,
        "p_text": text,
        "p_caseCheck": 0,
        "p_startDate": from_date,
        "p_endDate": to_date,
        "p_imgLink": _IMG_LINK,
        "p_msgLink": _MSG_LINK,
    }
    for i in range(6):
        fond = "false" if i in (3, 6) else "true"
        data[f"sSearch_{i}"] = ""
        data[f"bRegex_{i}"] = "false"
        data[f"bSearchable_{i}"] = "true"
        data[f"bSortable_{i}"] = fond
    return data


async def _post_json(
    client: SafeHttpClient, payload: dict[str, str | int], *, source: str
) -> dict[str, Any]:
    response = await client.request_form_post(JSON_URL, data=payload, headers=_XHR_HEADERS)
    body = json_body(response, source=source)
    if not isinstance(body, dict) or "aaData" not in body:
        raise UpstreamError(
            "elog backend returned an unexpected payload",
            instructions=(
                "Sign in with cern_auth_sign_in; the portal JSON endpoint "
                "requires the webcenter session."
            ),
        )
    return body


def _col(row: list[Any], idx: int) -> Any:
    return row[idx] if len(row) > idx else None


def _entry_item(row: list[Any]) -> dict[str, Any]:
    msg_id = _col(row, 0)
    try:
        parsed_msg_id = int(msg_id) if msg_id is not None else None
    except (TypeError, ValueError):
        parsed_msg_id = None
    return {
        "msg_id": parsed_msg_id,
        "section": _col(row, 1),
        "subject": _mini_text(str(_col(row, 3) or "")),
        "type": _col(row, 4),
        "date": _col(row, 5),
        "excerpt": _mini_text(str(_col(row, 6) or ""))[:400],
    }


async def search(
    client: SafeHttpClient,
    *,
    query: str,
    sub_id: str = "",
    author: str = "",
    subject: str = "",
    from_date: str = "2015-01-01",
    to_date: str = "2030-01-01",
    limit: int = 25,
    offset: int = 0,
) -> SearchResult:
    """Full-text search of CMS Online E-Log (text/subject/author, date range)."""
    query = _check_text(query)
    limit = _check_limit(limit)
    if offset < 0:
        raise InvalidInputError("offset must be >= 0")
    for name, value in (("sub_id", sub_id), ("from_date", from_date), ("to_date", to_date)):
        if value and not re.fullmatch(r"[0-9-]*", value):
            raise InvalidInputError(f"invalid {name} {value!r}")
    total_pages = (limit + _MAX_ENTRIES - 1) // _MAX_ENTRIES
    collected: list[dict[str, Any]] = []
    total_records = None
    for page in range(total_pages):
        payload = _datatable_payload(
            text=query,
            sub_id=sub_id,
            author=_check_text(author),
            subject=_check_text(subject),
            log_type="",
            from_date=from_date,
            to_date=to_date,
            limit=min(limit - len(collected), _MAX_ENTRIES),
            offset=offset + page * _MAX_ENTRIES,
        )
        data = await _post_json(client, payload, source="elog search")
        rows = data.get("aaData") or []
        total_records = data.get("iTotalRecords")
        collected.extend(_entry_item(row) for row in rows if isinstance(row, list))
        if len(rows) < _MAX_ENTRIES or len(collected) >= limit:
            break
    items = collected[:limit]
    return SearchResult(
        source=SOURCE,
        query=f"text={query!r} sub_id={sub_id or 'all'}",
        items=items,
        total_count=int(total_records) if isinstance(total_records, int) else None,
        page=(offset // max(1, limit)) + 1,
        size=len(items),
        has_more=(total_records is not None and offset + len(items) < int(total_records))
        if total_records is not None
        else len(items) == limit,
        note=(
            "p_subId='' searches all logbooks; restrict via sub_id from "
            "cmsonline_elog_list_logbooks."
        ),
    )


async def _fetch_detail_text(
    client: SafeHttpClient, *, msg_id: int, detail_sub: str
) -> tuple[str, str]:
    """One portal round-trip for the full entry detail page body and its URL."""
    # discover the current portlet id
    shell = await client.request("GET", ELOG_PAGE)
    shell_html = shell.content.decode("utf-8", errors="replace")
    pid_match = _PORTLET_RE.search(shell_html)
    pid = pid_match.group(1) if pid_match else "683379043"
    action = quote(f"/viewMessageDetails.do?msgId={msg_id}&subId={detail_sub}", safe="")
    url = (
        f"{ELOG_PAGE}?__adfpwp_mode.{pid}=1&__adfpwp_action_portlet={pid}"
        f"&_piref{pid}.strutsAction={action}"
    )
    response = await client.request("GET", url)
    return response.content.decode("utf-8", errors="replace"), url


async def get_detail(
    client: SafeHttpClient, *, msg_id: int, sub_id: int | None = None
) -> dict[str, Any]:
    """Full entry detail via the portal portlet dispatch."""
    if int(msg_id) <= 0:
        raise InvalidInputError("msg_id must be a positive integer")
    detail_sub = str(sub_id) if sub_id else str(_DETAIL_SUB_ID)
    text, url = await _fetch_detail_text(client, msg_id=int(msg_id), detail_sub=detail_sub)
    if _portal_gate(text):
        rendered = await client.render_page(url)
        if rendered is not None:
            text = rendered
    if _portal_gate(text):
        raise UpstreamError(
            "portal answered with the login page instead of the entry detail",
            instructions="Call cern_auth_sign_in again to refresh the portal session.",
        )
    body_text = html_to_text(text, limit=20000)
    attachments = sorted(set(_ATTACHMENT_RE.findall(text)))
    return {
        "source": SOURCE,
        "msg_id": int(msg_id),
        "sub_id": int(detail_sub),
        "text": body_text,
        "attachments": [
            f"https://cmsonline.cern.ch{a}" if a.startswith("/") else a for a in attachments
        ],
        "url": url[:200],
    }


async def list_categories(
    client: SafeHttpClient,
) -> dict[str, Any]:
    """Category/subcategory struts ids from the ELOG tree (id → twin lists)."""
    shell = await client.request("GET", ELOG_PAGE)
    text = shell.content.decode("utf-8", errors="replace")
    items = []
    seen: set[tuple[int, int]] = set()
    for cat_id, sub_id in re.findall(
        r"viewSubcatMessages\.do\?catId=(\d+)(?:&amp;|&)subId=(\d+)", text
    ):
        key = (int(cat_id), int(sub_id))
        if key in seen:
            continue
        seen.add(key)
        items.append({"cat_id": key[0], "sub_id": key[1]})
    known = {"495": "Computing", "1771": "Covid-19", "7": "General/Shift leader"}
    for item in items:
        label = known.get(str(item["sub_id"]))
        if label:
            item["label"] = label
    return {
        "source": SOURCE,
        "category_count": len(items),
        "items": items,
        "note": "sub_id identifies the logbook for cmsonline_elog_search; "
        "empty sub_id searches all logbooks.",
    }


async def download_attachment(client: SafeHttpClient, *, attachment_url: str) -> dict[str, Any]:
    """Bounded download of one elog attachment on cmsonline host + /Elog path."""
    parts = urlsplit(attachment_url.strip())
    if parts.hostname != "cmsonline.cern.ch" or not parts.path.startswith("/Elog/"):
        raise InvalidInputError(
            "elog attachments must live under cmsonline.cern.ch/Elog/",
            instructions="Use an attachment URL from cmsonline_elog_get_detail.",
        )
    filename = parts.path.rsplit("/", 1)[-1] or None
    final = await client.download(attachment_url.strip(), Path(filename) if filename else None)
    return {
        "source": SOURCE,
        "download_url": attachment_url.strip(),
        "path": str(final),
        "size_bytes": final.stat().st_size,
    }
