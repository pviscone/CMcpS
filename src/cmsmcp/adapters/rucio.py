"""CMS Rucio (cms-rucio.cern.ch): the user's data rules and dataset metadata.

Endpoint type: Rucio REST API. Authentication is X.509: ``GET /auth/x509`` on
``cms-rucio-auth.cern.ch`` with the grid certificate/VOMS proxy (the shared
client loads it for that host only) and ``X-Rucio-Account`` returns a token in
``X-Rucio-Auth-Token`` (cached until shortly before its expiry); data calls
send that token to ``cms-rucio.cern.ch``. No SSO cookies are sent. List
endpoints stream NDJSON. Read-only.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import quote

from cmsmcp.config.settings import Settings
from cmsmcp.contracts.errors import InvalidInputError, UpstreamError, setup_hint
from cmsmcp.contracts.results import SearchResult
from cmsmcp.net.client import SafeHttpClient, SafeResponse

SOURCE = "rucio"
_OK = 200
_TOKEN_MARGIN_SECONDS = 300
_RULE_RE = re.compile(r"^[0-9a-f]{32}$")
_DID_RE = re.compile(r"^([A-Za-z0-9_.-]+):(/?[A-Za-z0-9_./#-]+)$")
_tokens: dict[str, tuple[str, float]] = {}


def _account(settings: Settings, account: str | None) -> str:
    chosen = account or settings.rucio_account or settings.lxplus_user
    if not chosen:
        raise InvalidInputError(
            "no Rucio account configured",
            instructions=setup_hint("rucio", "Rucio access (account name + grid proxy)"),
        )
    return chosen


async def _token(client: SafeHttpClient, settings: Settings, account: str) -> str:
    cached = _tokens.get(account)
    if cached and cached[1] - _TOKEN_MARGIN_SECONDS > time.time():
        return cached[0]
    response = await client.request(
        "GET",
        f"{settings.rucio_auth_url.rstrip('/')}/auth/x509",
        authenticated=False,
        service_headers={"X-Rucio-Account": account},
    )
    token = response.headers.get("x-rucio-auth-token")
    if response.status_code != _OK or not token:
        raise UpstreamError(
            f"Rucio x509 login failed for account {account!r} (HTTP {response.status_code})",
            status_code=response.status_code,
            instructions="Check the account name and that the grid proxy is valid. "
            + setup_hint("grid", "A valid grid proxy"),
        )
    _tokens[account] = (token, time.time() + 3600)
    return token


async def _get(
    client: SafeHttpClient,
    settings: Settings,
    account: str,
    path: str,
    params: dict[str, Any] | None = None,
) -> SafeResponse:
    token = await _token(client, settings, account)
    response = await client.request(
        "GET",
        f"{settings.rucio_base_url.rstrip('/')}{path}",
        params=params,
        authenticated=False,
        service_headers={"X-Rucio-Auth-Token": token},
    )
    if response.status_code != _OK:
        raise UpstreamError(
            f"Rucio answered HTTP {response.status_code} for {path}: "
            f"{response.content.decode(errors='replace')[:200]}",
            status_code=response.status_code,
        )
    return response


def _ndjson(response: SafeResponse, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in response.content.decode(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def _rule(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "rule_id": row.get("id"),
        "name": f"{row.get('scope')}:{row.get('name')}",
        "rse_expression": row.get("rse_expression"),
        "state": row.get("state"),
        "copies": row.get("copies"),
        "locks": f"ok {row.get('locks_ok_cnt')} / replicating {row.get('locks_replicating_cnt')}"
        f" / stuck {row.get('locks_stuck_cnt')}",
        "expires_at": row.get("expires_at"),
        "created_at": row.get("created_at"),
        "comments": row.get("comments"),
        "error": row.get("error"),
    }


async def list_rules(
    client: SafeHttpClient,
    settings: Settings,
    *,
    account: str | None = None,
    state: str | None = None,
    name_contains: str | None = None,
    limit: int = 50,
) -> SearchResult:
    acct = _account(settings, account)
    params: dict[str, Any] = {"account": acct}
    if state:
        params["state"] = state
    rows = _ndjson(await _get(client, settings, acct, "/rules/", params), 2_000)
    if name_contains:
        needle = name_contains.lower()
        rows = [r for r in rows if needle in str(r.get("name", "")).lower()]
    items = [_rule(r) for r in rows[:limit]]
    return SearchResult(
        source=SOURCE,
        query=f"rules of {acct}" + (f" state={state}" if state else ""),
        items=items,
        size=len(items),
        total_count=len(rows),
        has_more=len(rows) > limit,
    )


async def get_rule(
    client: SafeHttpClient, settings: Settings, *, rule_id: str, account: str | None = None
) -> dict[str, Any]:
    if not _RULE_RE.fullmatch(rule_id):
        raise InvalidInputError(f"bad rule id {rule_id!r}", instructions="32 hex characters.")
    acct = _account(settings, account)
    data = json.loads((await _get(client, settings, acct, f"/rules/{rule_id}")).content)
    return {**_rule(data), "activity": data.get("activity"), "grouping": data.get("grouping")}


async def get_did(
    client: SafeHttpClient, settings: Settings, *, did: str, account: str | None = None
) -> dict[str, Any]:
    match = _DID_RE.fullmatch(did.strip())
    if not match:
        raise InvalidInputError(
            f"bad DID {did!r}", instructions="Use scope:name, e.g. cms:/Primary/Processed/TIER."
        )
    acct = _account(settings, account)
    scope, name = match.groups()
    path = f"/dids/{quote(scope, safe='')}/{quote(name, safe='')}"
    meta = json.loads((await _get(client, settings, acct, path, {"dynamic_depth": "FILE"})).content)
    rules = _ndjson(await _get(client, settings, acct, f"{path}/rules"), 50)
    return {
        "did": f"{scope}:{name}",
        "type": meta.get("type"),
        "bytes": meta.get("bytes"),
        "length": meta.get("length"),
        "open": meta.get("open"),
        "updated_at": meta.get("updated_at"),
        "rules": [_rule(r) for r in rules],
    }
