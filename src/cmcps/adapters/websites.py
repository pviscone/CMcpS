"""CERN website inventory search (Web Services portal inventory + crt.sh CT logs).

The primary source is the inventory behind webservices-portal.web.cern.ch
(``GET /api/search/hostnames``), which enumerates every centrally hosted CERN
site (webeos/paas/appCatalogue/drupal/wsdb) with owner, category, description
and environment metadata. It is merged with public certificate-transparency
names from crt.sh, which additionally covers self-managed hosts with publicly
trusted certificates. LANDB itself cannot be enumerated by regular accounts
(owner-scoped listings, capped fuzzy typeahead), so it is intentionally not a
source.

The merged inventory is cached in a SQLite database (FTS5 index when the
build supports it, substring scan otherwise) under the CMcpS state directory
and refreshed on demand or automatically when older than
``settings.websites_cache_max_age_days``.

The portal API requires a Bearer token minted by the app backend into
``window.localStorage["token"]``; it is obtained through a headless SSO
browser visit (``SafeHttpClient.browser_local_storage``) and never stored.
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cmcps.adapters.common import json_body
from cmcps.config.settings import Settings
from cmcps.contracts.errors import CMCPSError, InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

SOURCE = "websites"
_SCHEMA_VERSION = "1"
_WSP_SOURCE = "wsp"
_CTLOG_SOURCE = "ctlog"
_UNAUTHORIZED_STATUSES = frozenset({401, 403})
_MAX_LIMIT = 200
_MAX_QUERY_TOKENS = 16
_QUERY_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
_HOSTNAME_RE = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}")
_CERN_HOSTNAME_RE = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)*\.cern\.ch")
_REFRESH_LOCK = asyncio.Lock()

_SITE_COLUMNS = (
    "hostname",
    "url",
    "source",
    "project_name",
    "owner",
    "category",
    "environment",
    "display_type",
    "description",
    "created",
)
_SITE_COLUMN_CSV = ", ".join(_SITE_COLUMNS)
_SITE_PLACEHOLDERS = ", ".join("?" for _ in _SITE_COLUMNS)


def _portal_base(settings: Settings) -> str:
    return settings.websites_portal_base_url.rstrip("/")


async def _service_headers(client: SafeHttpClient, settings: Settings) -> dict[str, str]:
    if settings.websites_token:
        return {"Authorization": f"Bearer {settings.websites_token}"}
    token = await client.browser_local_storage(settings.websites_portal_app_url, "token")
    if not token:
        raise InvalidInputError(
            "websites tools require a Bearer token for the Web Services portal",
            instructions=(
                "Call cern_auth_sign_in first: the token is minted headlessly from "
                "the stored SSO session on each inventory refresh. Alternatively set "
                "CMCPS_WEBSITES_TOKEN manually."
            ),
        )
    return {"Authorization": f"Bearer {token}"}


def _normalize_wsp_record(record: dict[str, Any]) -> dict[str, str | None] | None:
    raw_url = str(record.get("url") or "").strip().lower()
    hostname = raw_url.removeprefix("https://").removeprefix("http://").split("/")[0].split(":")[0]
    if not hostname or not _HOSTNAME_RE.fullmatch(hostname):
        return None

    def _text(key: str) -> str | None:
        value = record.get(key)
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    return {
        "hostname": hostname,
        "url": hostname,
        "source": _WSP_SOURCE,
        "project_name": _text("projectName"),
        "owner": _text("owner"),
        "category": _text("category"),
        "environment": _text("environment"),
        "display_type": _text("displayType"),
        "description": _text("description"),
        "created": _text("creationTimestamp"),
    }


def _normalize_ctlog_hostname(raw: str) -> str | None:
    name = raw.strip().lower().removeprefix("*.")
    if not name or not _CERN_HOSTNAME_RE.fullmatch(name):
        return None
    return name


async def _fetch_wsp_sites(client: SafeHttpClient, settings: Settings) -> list[dict[str, Any]]:
    response = await client.request(
        "GET",
        f"{_portal_base(settings)}/api/search/hostnames",
        service_headers=await _service_headers(client, settings),
    )
    if response.status_code in _UNAUTHORIZED_STATUSES:
        raise UpstreamError(
            f"websites portal rejected the request ({response.status_code})",
            instructions=(
                "Re-run cern_auth_sign_in (the SSO identity is used to mint the "
                "portal token) or refresh CMCPS_WEBSITES_TOKEN."
            ),
        )
    payload = json_body(response, source="websites portal inventory")
    if not isinstance(payload, list):
        raise UpstreamError(
            "websites portal returned an unexpected payload shape",
            instructions="Expected a JSON list of site records from /api/search/hostnames.",
        )
    return payload


async def _fetch_ctlog_hostnames(
    client: SafeHttpClient, settings: Settings
) -> list[dict[str, Any]]:
    response = await client.request(
        "GET",
        settings.websites_ctlog_url,
        params={"q": "%.cern.ch", "output": "json"},
        authenticated=False,
    )
    payload = json_body(response, source="crt.sh certificate transparency")
    if not isinstance(payload, list):
        raise UpstreamError(
            "crt.sh returned an unexpected payload shape",
            instructions="Expected a JSON list of certificate records.",
        )
    return payload


def _fts5_supported(con: sqlite3.Connection) -> bool:
    try:
        con.execute("CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(probe)")
        con.execute("DROP TABLE temp.fts5_probe")
    except sqlite3.Error:
        return False
    return True


def _rebuild_cache(
    db_path: Path,
    wsp_records: list[dict[str, Any]],
    ctlog_records: list[dict[str, Any]],
) -> dict[str, int]:
    """Rebuild the inventory cache atomically. Pure SQLITE work, run in a thread."""
    wsp_rows: dict[str, dict[str, str | None]] = {}
    for record in wsp_records:
        if isinstance(record, dict):
            row = _normalize_wsp_record(record)
            if row is not None:
                wsp_rows[row["hostname"]] = row  # type: ignore[typeddict-item]
    ct_names = {
        name
        for record in ctlog_records
        if isinstance(record, dict)
        for name in map(_normalize_ctlog_hostname, str(record.get("name_value") or "").split("\n"))
        if name is not None and name not in wsp_rows
    }
    rows: list[tuple[str | None, ...]] = [
        tuple(row.get(col) for col in _SITE_COLUMNS) for row in wsp_rows.values()
    ]
    rows.extend(
        (name, name, _CTLOG_SOURCE, None, None, None, None, None, None, None) for name in ct_names
    )
    fetched_at = datetime.now(UTC)
    tmp_path = db_path.with_suffix(".tmp")
    tmp_path.unlink(missing_ok=True)
    con = sqlite3.connect(tmp_path)
    try:
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        con.execute(
            "CREATE TABLE site ("
            "hostname TEXT PRIMARY KEY, url TEXT NOT NULL, source TEXT NOT NULL, "
            "project_name TEXT, owner TEXT, category TEXT, environment TEXT, "
            "display_type TEXT, description TEXT, created TEXT)"
        )
        fts5_ok = _fts5_supported(con)
        con.executemany(
            f"INSERT INTO site ({_SITE_COLUMN_CSV}) VALUES ({_SITE_PLACEHOLDERS})",  # noqa: S608
            rows,
        )
        if fts5_ok:
            con.execute(
                "CREATE VIRTUAL TABLE site_fts USING fts5("
                "hostname, project_name, description, owner)"
            )
            con.execute(
                "INSERT INTO site_fts (rowid, hostname, project_name, description, owner) "
                "SELECT rowid, hostname, COALESCE(project_name, ''), COALESCE(description, ''), "
                "COALESCE(owner, '') FROM site"
            )
        meta = {
            "schema_version": _SCHEMA_VERSION,
            "fetched_at": fetched_at.isoformat(),
            "fetched_at_epoch": str(fetched_at.timestamp()),
            "site_count": str(len(rows)),
            "wsp_count": str(len(wsp_rows)),
            "ctlog_count": str(len(ct_names)),
            "fts5": "1" if fts5_ok else "0",
        }
        con.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", meta.items())
        con.commit()
    finally:
        con.close()
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, db_path)
    return {"site_count": len(rows), "wsp_count": len(wsp_rows), "ctlog_count": len(ct_names)}


def _read_meta(db_path: Path) -> dict[str, str]:
    if not db_path.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return {
                str(key): str(value)
                for key, value in con.execute("SELECT key, value FROM meta")
            }
        finally:
            con.close()
    except sqlite3.Error:
        return {}


def _iso_from_meta(meta: dict[str, str], key: str = "fetched_at") -> str | None:
    value = meta.get(key)
    return value if value else None


def _age_days(meta: dict[str, str], *, now: float) -> float | None:
    epoch = meta.get("fetched_at_epoch")
    if not epoch:
        return None
    try:
        return max(0.0, (now - float(epoch)) / 86_400.0)
    except (TypeError, ValueError):
        return None


def _query_tokens(query: str) -> list[str]:
    return _QUERY_TOKEN_RE.findall(query.lower())[:_MAX_QUERY_TOKENS]


def _fts_query(tokens: list[str]) -> str:
    return " AND ".join(f'"{token}"*' for token in tokens)


def _filter_clause(
    environment: str | None,
    category: str | None,
    display_type: str | None,
    owner: str | None,
) -> tuple[list[str], list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    for column, value in (
        ("environment", environment),
        ("category", category),
        ("display_type", display_type),
        ("owner", owner),
    ):
        if value is not None and value.strip():
            clauses.append(f"lower(s.{column}) = ?")
            params.append(value.strip().lower())
    return clauses, params


def _row_to_item(row: tuple[Any, ...]) -> dict[str, Any]:
    item = dict(zip(_SITE_COLUMNS, row, strict=True))
    return {key: value for key, value in item.items() if value is not None}


def _search_db(
    *,
    db_path: Path,
    tokens: list[str],
    fts5: bool,
    filters: tuple[list[str], list[str]],
    limit: int,
    offset: int,
) -> tuple[int, list[dict[str, Any]]]:
    clauses, params = filters
    extra = (" AND " + " AND ".join(clauses)) if clauses else ""
    order = "bm25(site_fts)" if fts5 and tokens else "s.hostname"
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if tokens:
            if fts5:
                select_from = (
                    "FROM site_fts JOIN site s ON s.rowid = site_fts.rowid "
                    "WHERE site_fts MATCH ?"
                )
                match_params: list[Any] = [_fts_query(tokens)]
            else:
                haystack = (
                    "lower(s.hostname || ' ' || COALESCE(s.project_name, '') || ' ' || "
                    "COALESCE(s.description, '') || ' ' || COALESCE(s.owner, ''))"
                )
                needles = " AND ".join(f"instr({haystack}, ?) > 0" for _ in tokens)
                select_from = f"FROM site s WHERE {needles}"
                match_params = list(tokens)
        else:
            select_from = "FROM site s WHERE 1=1"
            match_params = []
        all_params = [*match_params, *params]
        total = con.execute(
            f"SELECT count(*) {select_from}{extra}", all_params
        ).fetchone()[0]
        rows = con.execute(
            f"SELECT {', '.join('s.' + col for col in _SITE_COLUMNS)} {select_from}{extra} "
            f"ORDER BY {order} LIMIT ? OFFSET ?",
            [*all_params, limit, offset],
        ).fetchall()
        return total, [_row_to_item(row) for row in rows]
    finally:
        con.close()


async def _do_refresh(client: SafeHttpClient, settings: Settings) -> dict[str, Any]:
    """Fetch both sources and rebuild the local inventory cache atomically.

    Costs 10-30 s (headless token mint + portal + crt.sh + rebuild); the two
    independent network legs run concurrently. Never call this directly from a
    tool handler behind a short client timeout — use ``refresh``.
    """
    wsp_result, ctlog_result = await asyncio.gather(
        _fetch_wsp_sites(client, settings),
        _fetch_ctlog_hostnames(client, settings),
        return_exceptions=True,
    )
    if isinstance(wsp_result, BaseException):
        raise wsp_result
    wsp_records = wsp_result
    ctlog_note: str | None = None
    if isinstance(ctlog_result, BaseException):
        detail = ctlog_result.message if isinstance(ctlog_result, UpstreamError) else (
            str(ctlog_result) or type(ctlog_result).__name__
        )
        ctlog_note = f"crt.sh source skipped: {detail}"
        ctlog_records: list[dict[str, Any]] = []
    else:
        ctlog_records = ctlog_result
    counts = await asyncio.to_thread(
        _rebuild_cache, settings.websites_cache_path, wsp_records, ctlog_records
    )
    environments = Counter(
        str(record.get("environment")).lower()
        for record in wsp_records
        if isinstance(record, dict) and record.get("environment")
    )
    result: dict[str, Any] = {
        "source": SOURCE,
        "refreshed": True,
        "fetched_at": _iso_from_meta(_read_meta(settings.websites_cache_path)),
        "site_count": counts["site_count"],
        "wsp_count": counts["wsp_count"],
        "ctlog_count": counts["ctlog_count"],
        "environment_counts": dict(sorted(environments.items())),
        "cache_path": settings.websites_cache_path.name,
    }
    if ctlog_note:
        result["note"] = ctlog_note
    return result


async def refresh_sync(client: SafeHttpClient, settings: Settings) -> dict[str, Any]:
    """Serialized front door to the full refresh; used by the background task."""
    async with _REFRESH_LOCK:
        return await _do_refresh(client, settings)


_REFRESH_STATE: dict[str, Any] = {
    "task": None,
    "started_at": None,
    "last_finished_at": None,
    "last_error": None,
    "last_result": None,
}


def _full_result_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "fetched_at": result.get("fetched_at"),
        "site_count": result.get("site_count"),
        "wsp_count": result.get("wsp_count"),
        "ctlog_count": result.get("ctlog_count"),
        "note": result.get("note"),
    }


def _record_task_outcome(task: asyncio.Task) -> None:
    _REFRESH_STATE["last_finished_at"] = datetime.now(UTC).isoformat()
    if task.cancelled():
        _REFRESH_STATE["last_error"] = "refresh task was cancelled"
        return
    exc = task.exception()
    if exc is not None:
        _REFRESH_STATE["last_error"] = (
            exc.message if isinstance(exc, CMCPSError) else (str(exc) or type(exc).__name__)
        )
        return
    _REFRESH_STATE["last_result"] = _full_result_summary(task.result())


def _refresh_running() -> bool:
    task = _REFRESH_STATE.get("task")
    return task is not None and not task.done()


def _start_background(client: SafeHttpClient, settings: Settings) -> None:
    """Launch a background refresh task unless one is already running."""
    if _refresh_running():
        return
    _REFRESH_STATE["started_at"] = datetime.now(UTC).isoformat()
    _REFRESH_STATE["last_error"] = None
    task = asyncio.ensure_future(refresh_sync(client, settings))
    task.add_done_callback(_record_task_outcome)
    _REFRESH_STATE["task"] = task


async def refresh(client: SafeHttpClient, settings: Settings) -> dict[str, Any]:
    """Start a background inventory refresh and return immediately.

    A full refresh costs 10-30 s (headless token mint, two slow network
    sources), which exceeds typical MCP client request timeouts — progress is
    observable via ``websites_status`` (``refresh_running``/``refresh_error``).
    """
    if _refresh_running():
        return {
            "source": SOURCE,
            "refresh_started": False,
            "refresh_running": True,
            "started_at": _REFRESH_STATE.get("started_at"),
            "note": "refresh already running in the background; poll websites_status",
        }
    _start_background(client, settings)
    return {
        "source": SOURCE,
        "refresh_started": True,
        "refresh_running": True,
        "started_at": _REFRESH_STATE["started_at"],
        "note": (
            "refresh running in the background (~15-30 s); poll websites_status "
            "(refresh_running / refresh_error / last_refresh)"
        ),
    }


async def _ensure_fresh(client: SafeHttpClient, settings: Settings) -> dict[str, Any]:
    """Trigger a background refresh when the cache is absent or older than the TTL.

    Stale-but-existing caches are served immediately with a note while the
    refresh runs; a missing cache fails with a retryable error (there is
    nothing to serve until the first background refresh completes).
    """
    meta = _read_meta(settings.websites_cache_path)
    age = _age_days(meta, now=time.time())
    expired = meta.get("fetched_at_epoch") is None or (
        age is not None and age > settings.websites_cache_max_age_days
    )
    if not expired:
        return {"stale_served": False}
    age_note = f"{age:.1f}" if age is not None else "unknown"
    _start_background(client, settings)
    if meta.get("fetched_at_epoch") is None:
        raise UpstreamError(
            "website inventory is initializing (background refresh just started)",
            status_code=503,
            instructions="Poll websites_status (refresh_running/refresh_error); retry shortly.",
        )
    return {
        "stale_served": True,
        "note": (
            f"cache age {age_note}d exceeds the TTL; refresh running in the "
            f"background; serving stale cache from {_iso_from_meta(meta)}"
        ),
    }


async def search(
    client: SafeHttpClient,
    settings: Settings,
    *,
    query: str | None = None,
    environment: str | None = None,
    category: str | None = None,
    display_type: str | None = None,
    owner: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Ranked full-text search over the cached website inventory."""
    limit = max(1, min(limit, _MAX_LIMIT))
    offset = max(0, offset)
    query = (query or "").strip()
    tokens = _query_tokens(query)
    if query and not tokens:
        raise InvalidInputError(
            f"query {query!r} contains no searchable terms",
            instructions="Use letters, digits, dots or hyphens (host/project words).",
        )
    freshness = await _ensure_fresh(client, settings)
    meta = _read_meta(settings.websites_cache_path)
    fts5 = meta.get("fts5") == "1"
    filters = _filter_clause(environment, category, display_type, owner)
    total, items = await asyncio.to_thread(
        _search_db,
        db_path=settings.websites_cache_path,
        tokens=tokens,
        fts5=fts5,
        filters=filters,
        limit=limit,
        offset=offset,
    )
    result: dict[str, Any] = {
        "source": SOURCE,
        "query": query,
        "items": items,
        "total_count": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(items) < total,
        "cache": {
            "fetched_at": _iso_from_meta(meta),
            "stale_served": freshness["stale_served"],
        },
    }
    if freshness.get("note"):
        result["note"] = freshness["note"]
    if not fts5:
        result["note"] = (result.get("note", "") + " FTS5 unavailable; substring matching.").strip()
    return result


async def lookup(
    client: SafeHttpClient, settings: Settings, *, hostname: str
) -> dict[str, Any]:
    """Exact record(s) for one hosted website hostname."""
    cleaned = (
        hostname.strip().lower().removeprefix("https://").removeprefix("http://")
        .split("/")[0].split(":")[0]
    )
    if not cleaned or not _HOSTNAME_RE.fullmatch(cleaned):
        raise InvalidInputError(
            f"invalid hostname {hostname!r}",
            instructions="Pass a bare hostname, e.g. atlas.cern.ch",
        )
    freshness = await _ensure_fresh(client, settings)
    meta = _read_meta(settings.websites_cache_path)

    def _lookup() -> list[dict[str, Any]]:
        con = sqlite3.connect(f"file:{settings.websites_cache_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                f"SELECT {_SITE_COLUMN_CSV} FROM site WHERE hostname = ?",  # noqa: S608
                (cleaned,),
            ).fetchall()
            return [_row_to_item(row) for row in rows]
        finally:
            con.close()

    items = await asyncio.to_thread(_lookup)
    result: dict[str, Any] = {
        "source": SOURCE,
        "hostname": cleaned,
        "found": bool(items),
        "items": items,
        "cache": {
            "fetched_at": _iso_from_meta(meta),
            "stale_served": freshness["stale_served"],
        },
    }
    if freshness.get("note"):
        result["note"] = freshness["note"]
    return result


async def status(settings: Settings) -> dict[str, Any]:
    """Cache metadata only — never triggers a refresh."""
    meta = _read_meta(settings.websites_cache_path)
    now = time.time()
    age = _age_days(meta, now=now)
    source_counts: dict[str, int] = {}
    if meta:
        con = sqlite3.connect(f"file:{settings.websites_cache_path}?mode=ro", uri=True)
        try:
            source_counts = {
                str(source): int(count)
                for source, count in con.execute(
                    "SELECT source, count(*) FROM site GROUP BY source"
                )
            }
        except sqlite3.Error:
            source_counts = {}
        finally:
            con.close()
    return {
        "source": SOURCE,
        "cache_exists": bool(meta),
        "fetched_at": _iso_from_meta(meta),
        "age_days": round(age, 1) if age is not None else None,
        "max_age_days": settings.websites_cache_max_age_days,
        "stale": age is None or age > settings.websites_cache_max_age_days,
        "site_count": int(meta.get("site_count", 0)) if meta else 0,
        "source_counts": source_counts,
        "fts5": meta.get("fts5") == "1",
        "refresh_running": _refresh_running(),
        "refresh_started_at": _REFRESH_STATE.get("started_at"),
        "refresh_finished_at": _REFRESH_STATE.get("last_finished_at"),
        "refresh_error": _REFRESH_STATE.get("last_error"),
        "last_refresh": _REFRESH_STATE.get("last_result"),
    }
