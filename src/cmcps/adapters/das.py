"""Read-only adapter for CMS Data Aggregation System (cmsweb.cern.ch/das).

Backend: the ``dasgoclient`` CLI (https://github.com/dmwm/das2go). The
cmsweb DAS endpoint presents a CERN Grid CA certificate (not in public
bundles) and authenticates via GSI client certificates, so this adapter
spawns ``dasgoclient -json`` through :mod:`cmcps.net.proc` instead of the
SSO-cookie HTTP client. Auth comes from a local grid credential:
``X509_USER_CERT``/``X509_USER_KEY`` are injected from Settings
(``grid_cert_file``/``grid_key_file``, default ``~/.globus``).

Queries: DAS QL, e.g. ``dataset=/TT*/RunII*/AODSIM``,
``dataset dataset=/X/Y/Z``, ``site site=T2_CH_CERN``, ``file file=...``,
``run run=379611``. ``instance`` (default ``prod/global``) is appended to
the query string.

Output: ``-json`` returns a JSON array of records shaped
``{"das": {...}, "<entity>": [{...}], "qhash": ...}``; the adapter lifts
the entity's first attribute dict per record and deduplicates on the
primary key (``name``; replicas repeat rows). ``limit`` is capped at 50
and a ``limit+1`` probe drives ``has_more``; ``idx`` is the page offset.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.proc import CommandResult, run_command

SOURCE = "das"

_MAX_LIMIT = 50
_META_KEYS = frozenset({"das", "qhash"})
_DATASET_RE = re.compile(r"^/?[A-Za-z0-9_*][A-Za-z0-9_.*~-]*/[^/]+/[A-Za-z0-9_*-]+/?$")
_UNSAFE_QL_RE = re.compile(r"[;`'\"$]")
_QL_ERROR_MARKERS = ("DAS QL ERROR", "Not a DAS key")


@dataclass(frozen=True, slots=True)
class DasGoRunner:
    """Bound ``dasgoclient`` invocation configured from Settings."""

    binary: str
    env: Mapping[str, str]
    timeout_seconds: float
    max_output_bytes: int

    async def __call__(self, argv: Sequence[str]) -> CommandResult:
        return await run_command(
            [self.binary, *argv],
            env=self.env,
            timeout_seconds=self.timeout_seconds,
            max_output_bytes=self.max_output_bytes,
        )


def make_runner(settings: Settings) -> Callable[[Sequence[str]], Awaitable[CommandResult]]:
    """Resolve the dasgoclient binary and grid credentials from Settings."""
    binary = settings.dasgoclient_path
    if not binary:
        raise UpstreamError(
            "dasgoclient is not available",
            instructions=(
                "Install dasgoclient (https://github.com/dmwm/das2go) or set "
                "CMCPS_DASGOCLIENT_PATH to its absolute path."
            ),
        )
    cert = settings.grid_cert_file
    key = settings.grid_key_file
    for credential in (cert, key):
        if not credential.is_file() or not os.access(credential, os.R_OK):
            raise UpstreamError(
                f"grid credential is not readable: {credential}",
                instructions=(
                    "Point CMCPS_GRID_CERT_FILE/CMCPS_GRID_KEY_FILE at a valid "
                    "grid user certificate and key (default ~/.globus), or run "
                    "voms-proxy-init and set them to the proxy."
                ),
            )
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(settings.state_dir)),
        "X509_USER_CERT": str(cert),
        "X509_USER_KEY": str(key),
    }
    return DasGoRunner(
        binary=binary,
        env=env,
        timeout_seconds=settings.request_timeout_seconds,
        max_output_bytes=settings.max_response_bytes,
    )


def _check_query(query: str) -> str:
    cleaned = query.strip()
    if not cleaned:
        raise InvalidInputError(
            "query must not be empty",
            instructions="Use DAS QL, e.g. dataset=/TT*/RunII*/AODSIM or dataset dataset=/X/Y/Z.",
        )
    if _UNSAFE_QL_RE.search(cleaned):
        raise InvalidInputError(
            "query contains unsafe characters",
            instructions="Pass a plain DAS QL expression; no shell metacharacters.",
        )
    return cleaned


def _parse_records(payload: bytes) -> list[dict[str, Any]]:
    try:
        records = json.loads(payload)
    except ValueError as exc:
        raise UpstreamError(
            "dasgoclient did not return JSON",
            instructions="Retry with -json output; report if it persists.",
        ) from exc
    if not isinstance(records, list):
        raise UpstreamError(
            "dasgoclient returned an unexpected JSON shape",
            instructions="Expected a JSON array of DAS records.",
        )
    items: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        entities = {key: value for key, value in record.items() if key not in _META_KEYS}
        if not entities:
            continue
        entity, values = next(iter(entities.items()))
        attrs = values[0] if isinstance(values, list) and values else values
        if isinstance(attrs, dict):
            items.append({"entity": entity, **attrs})
    return items


def _check_exit(result: CommandResult) -> None:
    if result.exit_code == 0:
        return
    stderr = result.stderr.decode(errors="replace")
    # dasgoclient writes QL parse errors to stderr (exit 17) but pattern
    # validation errors to stdout (exit 18); scan both.
    output = stderr + result.stdout.decode(errors="replace")
    detail = stderr.strip() or output.strip()
    detail = detail.splitlines()[0][:200] if detail else f"exit code {result.exit_code}"
    if any(marker in output for marker in _QL_ERROR_MARKERS):
        raise InvalidInputError(
            f"das rejected the query: {detail}",
            instructions="Check the DAS QL syntax, e.g. dataset=/TT*/RunII*/AODSIM.",
        )
    if "Validation error" in output:
        raise InvalidInputError(
            f"dasgoclient rejected the pattern: {detail}",
            instructions=(
                "dasgoclient is stricter than the DAS web form for wildcards "
                "(e.g. use /Primary*/*Processed*/*Tier); rephrase the query."
            ),
        )
    err = UpstreamError(
        f"dasgoclient failed: {detail}",
        instructions="Retry; check X509 grid credentials if the error mentions auth.",
    )
    err.retryable = True
    raise err


async def search(
    runner: Callable[[Sequence[str]], Awaitable[CommandResult]],
    *,
    query: str,
    limit: int = 10,
    offset: int = 0,
    instance: str = "prod/global",
) -> SearchResult:
    """DAS QL search (datasets, blocks, files, sites, runs, lumis)."""
    ql = _check_query(query)
    if not 1 <= limit <= _MAX_LIMIT:
        raise InvalidInputError(
            f"limit must be between 1 and {_MAX_LIMIT}",
            instructions="DAS throttles large pages; page with offset (idx).",
        )
    if offset < 0:
        raise InvalidInputError("offset must be >= 0")
    if not instance or any(c in instance for c in " ;'\""):
        raise InvalidInputError(
            f"invalid DAS instance {instance!r}",
            instructions="Common values: prod/global, prod/phys01, prod/phys02, prod/phys03.",
        )
    full_query = f"{ql} instance={instance}"
    result = await runner(
        [
            "-query",
            full_query,
            "-json",
            "-limit",
            str(limit + 1),  # +1 probe for has_more; -json has no nresults
            "-idx",
            str(offset),
        ]
    )
    _check_exit(result)
    records = _parse_records(result.stdout)
    has_more = len(records) > limit
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in records[: limit + 1]:
        name = item.get("name") or item.get("block_name") or item.get("file")
        key = f"{item['entity']}:{name}"
        if name is not None and key in seen:
            continue
        seen.add(key)
        if len(items) < limit:
            items.append(item)
    return SearchResult(
        source=SOURCE,
        query=f"[{instance}] {ql}",
        items=items,
        total_count=None,
        page=(offset // limit) + 1,
        size=len(items),
        has_more=has_more,
        note="Rows are deduplicated per replica. Entities: dataset, block, "
        "file, site, run, lumi, summary. total_count is None: dasgoclient "
        "-json has no nresults; has_more comes from a limit+1 probe.",
    )


async def get_dataset_info(
    runner: Callable[[Sequence[str]], Awaitable[CommandResult]],
    *,
    dataset: str,
) -> dict[str, Any]:
    """Attribute record of one exact dataset (/primary/processed/tier)."""
    cleaned = dataset.strip()
    if not _DATASET_RE.fullmatch(cleaned) or "*" in cleaned:
        raise InvalidInputError(
            f"invalid exact dataset name {dataset!r}",
            instructions="Format: /primary/processed/tier without wildcards.",
        )
    if not cleaned.startswith("/"):
        cleaned = f"/{cleaned}"
    result = await search(
        runner, query=f"dataset={cleaned}", limit=_MAX_LIMIT, instance="prod/global"
    )
    if not result.items:
        raise UpstreamError(
            f"dataset {cleaned} not found in DAS (prod/global)",
            instructions="Check the name; try das_search with a wildcard query.",
        )
    rows = [item for item in result.items if item.get("name") == cleaned]
    if not rows:
        raise UpstreamError(
            f"das returned no exact match for {cleaned}",
            instructions="Try das_search with the wildcard form.",
        )
    item = rows[0]
    item["source"] = SOURCE
    item["url"] = f"https://cmsweb.cern.ch/das/request?input=dataset%3D{cleaned}"
    return item
