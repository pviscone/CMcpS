"""Read-only adapter for GLANCE/CMSFence (cmsfence.cern.ch).

GLANCE (replacing iCMS/CADI) consists of two SPAs under the same host, each
with its own Keycloak client and a Bearer-token-only JSON API:

- ALCM (``/alcm/api/...``, audience ``cms-alcm-api-prod``) — the CADI-like
  analysis/notes registry.
- Membership (``/membership/api/...``, audience ``cms-membership-api-prod``) —
  CMS membership/author lists.

Tokens are captured per app during ``cern_auth_sign_in`` (each app root is
loaded by the sign-in flow to trigger its own PKCE exchange), or supplied via
``CMCPS_GLANCE_ALCM_TOKEN`` / ``CMCPS_GLANCE_MEMBERSHIP_TOKEN``. Cookies or a
token from the other app answer 401; there is no session-cookie API access.

Endpoints verified live (authenticated):

- ``GET /alcm/api/user`` — current user (``{"user": ...}``).
- ``GET /alcm/api/analysis`` — full internal analysis list
  (``{"analysis": [...]}``, ~1.7 MB).
- ``GET /alcm/api/public/analysis`` — published analyses
  (``{"analyses": [...]}``).
- ``GET /alcm/api/notes`` — all notes (``{"notes": [...]}``, ~15 MB single
  object; no server-side pagination or filters).
- ``GET /alcm/api/notes/{id}`` — note detail (``{"note": ...}``).
- ``GET /alcm/api/notes/types`` — note types (``{"types": [...]}``).
- ``GET /alcm/api/projects`` — projects (``{"projects": [...]}``).
- ``GET /alcm/api/working-groups`` — non-physics working groups.
- ``GET /alcm/api/physics-working-groups`` — physics working groups.
- ``GET /alcm/api/members`` — ALCM member roster (~1.1 MB).
- ``GET /membership/api/user`` — current user incl. primary employment.
- ``GET /membership/api/members/search?q=<text>`` — member search
  (``{"results": [...], "numberOfResults": "..."}``).

Analysis-page profile endpoints (verified live): ``GET /alcm/api/analysis/
{id}`` and ``/alcm/api/analysis/ancode/{code}`` plus per-analysis support
endpoints ``analysis/{id}/{paper,review-committee,history}``,
``transitions/{id}``, ``attachments/{documents,gitlab-repositories,
indico-contributions,paths}/entity/{id}``, ``hashtags/entity/{id}``, and the
ARC statistics roster ``analysis/statistics-arc-members``. The incubator SPA
exposes nothing useful read-only and is not implemented.

Size note: the notes index (~15 MB) exceeds the default
``CMCPS_MAX_RESPONSE_BYTES`` (10 MB); raise it (e.g. to 20 MB) to use
``glance_search_notes``. The payload is fetched in full on each call and
filtered client-side — output is always bounded by ``limit``.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date
from typing import Any, Literal

from cmcps.adapters.common import json_body
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient

SOURCE = "glance"

_UNAUTHORIZED = {401, 403}
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100
_MAX_QUERY_LEN = 200

# Field projections keep list output bounded against future schema growth.
_ANALYSIS_KEYS = (
    "id",
    "customIdentifier",
    "title",
    "paperIdentifier",
    "workingGroups",
    "currentStage",
    "proponents",
    "registrationDate",
    "gitlabpasrepo",
    "pasCDS",
    "gitlabpaperrepo",
)
_PUBLISHED_KEYS = (
    "id",
    "title",
    "paperIdentifier",
    "arxivNumber",
    "workingGroups",
    "submittedOn",
    "finalPaperDraftCdsLink",
    "journalReference",
    "journalUrl",
)
_NOTE_KEYS = (
    "id",
    "type",
    "title",
    "registrationDate",
    "reportNumber",
    "publishedIn",
    "gitlabRepositoryUrl",
    "cdsLink",
    "workingGroups",
    "projects",
    "proponents",
    "authors",
    "currentStage",
)

NotKind = Literal["physics", "non-physics", "all"]


def _app_config(settings: Settings, app: Literal["alcm", "membership"]) -> tuple[str, str, str]:
    """Return (service name, env token, Keycloak audience) for one app."""
    if app == "alcm":
        return "glance_alcm", settings.glance_alcm_token or "", "cms-alcm-api-prod"
    return (
        "glance_membership",
        settings.glance_membership_token or "",
        "cms-membership-api-prod",
    )


async def _service_headers(
    client: SafeHttpClient, settings: Settings, app: Literal["alcm", "membership"]
) -> dict[str, str]:
    service, env_token, audience = _app_config(settings, app)
    token = await client.bearer_for(service)
    if token is None:
        token = env_token
    if not token:
        env_var = f"CMCPS_{service.upper()}_TOKEN"
        raise InvalidInputError(
            f"GLANCE {app} tools require a Keycloak Bearer token",
            instructions=(
                f"Call cern_auth_sign_in: the sign-in flow captures the {audience} "
                f"Bearer token from {settings.glance_base_url}/{app} automatically. "
                f"Alternatively set {env_var} manually."
            ),
        )
    return {"Authorization": f"Bearer {token}"}


async def _app_get(
    client: SafeHttpClient,
    settings: Settings,
    app: Literal["alcm", "membership"],
    path: str,
    *,
    source: str,
    params: dict[str, str | int] | None = None,
) -> Any:
    _, _, audience = _app_config(settings, app)
    response = await client.request(
        "GET",
        f"{settings.glance_base_url.rstrip('/')}/{app}/api/{path.lstrip('/')}",
        params=params,
        service_headers=await _service_headers(client, settings, app),
    )
    if response.status_code in _UNAUTHORIZED:
        raise UpstreamError(
            f"GLANCE {app} rejected the request ({response.status_code})",
            instructions=(
                f"The Bearer token for audience {audience} is missing or expired. "
                "Re-run cern_auth_sign_in to capture a fresh token, or refresh the "
                f"CMCPS_GLANCE_{app.upper()}_TOKEN environment variable."
            ),
        )
    return json_body(response, source=source)


async def _alcm_get(client: SafeHttpClient, settings: Settings, path: str, *, source: str) -> Any:
    return await _app_get(client, settings, "alcm", path, source=source)


def _check_page(limit: int, offset: int) -> None:
    if not 1 <= limit <= _MAX_LIMIT:
        raise InvalidInputError(
            f"invalid limit {limit}",
            instructions=f"limit must be between 1 and {_MAX_LIMIT}.",
        )
    if offset < 0:
        raise InvalidInputError("offset must be >= 0")


def _check_text(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > _MAX_QUERY_LEN:
        raise InvalidInputError(
            f"{name} must be 1..{_MAX_QUERY_LEN} non-blank characters",
            instructions="Provide a plain substring (case-insensitive match).",
        )
    return cleaned


def _envelope(
    items: list[dict[str, Any]],
    total: int,
    *,
    query: str,
    limit: int,
    offset: int,
    note: str | None = None,
) -> SearchResult:
    paged = items[offset : offset + limit]
    return SearchResult(
        source=SOURCE,
        query=query,
        items=paged,
        total_count=total,
        page=(offset // limit) + 1,
        size=len(paged),
        has_more=offset + len(paged) < total,
        note=note,
    )


def _project(item: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: item.get(key) for key in keys if key in item}


def _wg_names(item: dict[str, Any]) -> list[str]:
    groups = item.get("workingGroups")
    if not isinstance(groups, list):
        return []
    return [g.get("name", "") for g in groups if isinstance(g, dict)]


async def alcm_user(client: SafeHttpClient, settings: Settings) -> dict[str, Any]:
    """Current ALCM user record (``{"user": ...}``)."""
    payload = await _alcm_get(client, settings, "user", source="glance alcm user")
    return {"source": SOURCE, "app": "alcm", **payload}


async def membership_user(client: SafeHttpClient, settings: Settings) -> dict[str, Any]:
    """Current Membership user record incl. primary employment."""
    payload = await _app_get(
        client, settings, "membership", "user", source="glance membership user"
    )
    return {"source": SOURCE, "app": "membership", **payload}


_SORT_KEYS = {"title": "title", "registrationDate": "registrationDate", "date": "registrationDate"}
_SORT_ORDERS = {"asc", "desc"}


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_date(value: str, name: str) -> str:
    """Validate ``YYYY-MM-DD`` and pass through (list rows carry that format)."""
    if not _DATE_RE.match(value):
        raise InvalidInputError(
            f"{name} must be YYYY-MM-DD",
            instructions="Use a zero-padded calendar date, e.g. 2026-10-04.",
        )
    try:
        year, month, day = (int(p) for p in value.split("-"))
        date(year, month, day)
    except ValueError as exc:
        raise InvalidInputError(
            f"{name} is not a real calendar date",
            instructions="Use a zero-padded calendar date, e.g. 2026-10-04.",
        ) from exc
    return value


class _AnalysisFilter:
    """Lower-cased matcher for one analysis row (ALCM SPA filter semantics)."""

    def __init__(self, args: dict[str, str | None]) -> None:
        self.custom = (args["custom"] or "").lower()
        self.code = (args["code"] or "").lower()
        self.title = (args["title"] or "").lower()
        self.stage = (args["stage"] or "").lower()
        self.wg = (args["working_group"] or "").lower()
        self.contacts = (args["contacts"] or "").lower()
        self.start = args["start_date"] or ""
        self.end = args["end_date"] or ""

    def _stage_matches(self, row: dict[str, Any]) -> bool:
        if not self.stage:
            return True
        current = row.get("currentStage")
        stage_name = current.get("name", "") if isinstance(current, dict) else ""
        return self.stage in stage_name.lower()

    def _contacts_match(self, row: dict[str, Any]) -> bool:
        if not self.contacts:
            return True
        people = row.get("proponents")
        names = (
            [str(p.get("name") or "").lower() for p in people if isinstance(p, dict)]
            if isinstance(people, list)
            else []
        )
        return any(self.contacts in n for n in names)

    def matches(self, row: dict[str, Any]) -> bool:
        registered = str(row.get("registrationDate") or "")
        return (
            (not self.custom or self.custom in str(row.get("customIdentifier") or "").lower())
            and (not self.code or self.code in str(row.get("paperIdentifier") or "").lower())
            and (not self.title or self.title in str(row.get("title") or "").lower())
            and (not self.wg or self.wg in [n.lower() for n in _wg_names(row)])
            and self._stage_matches(row)
            and self._contacts_match(row)
            and (not self.start or registered >= self.start)
            and (not self.end or registered <= self.end)
        )


async def list_analyses(
    client: SafeHttpClient,
    settings: Settings,
    *,
    custom: str | None = None,
    code: str | None = None,
    title: str | None = None,
    working_group: str | None = None,
    stage: str | None = None,
    contacts: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    sort_by: str = "registrationDate",
    sort_order: str = "desc",
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
) -> SearchResult:
    """Search analyses with the same fields as the ALCM analysis page.

    Mirrors the SPA's filters (``custom``/``code`` identifiers, ``c_title``,
    ``c_stage``, ``c_wg``, ``c_contacts``, ``startDate``/``endDate`` on the
    registration date, ``sortBy``/``sortOrder``). The API ignores those query
    params (the SPA filters client-side over the full list), so this function
    applies the same filtering client-side.
    """
    _check_page(limit, offset)
    custom = _check_text(custom, "custom")
    code = _check_text(code, "code")
    title = _check_text(title, "title")
    working_group = _check_text(working_group, "working_group")
    stage = _check_text(stage, "stage")
    contacts = _check_text(contacts, "contacts")
    clean_start = _parse_date(start_date, "start_date") if start_date else None
    clean_end = _parse_date(end_date, "end_date") if end_date else None
    sort_key = _SORT_KEYS.get(sort_by)
    if sort_key is None:
        raise InvalidInputError(
            f"unsupported sort_by {sort_by!r}",
            instructions=f"sort_by must be one of {sorted(_SORT_KEYS)}.",
        )
    if sort_order.lower() not in _SORT_ORDERS:
        raise InvalidInputError(
            f"unsupported sort_order {sort_order!r}",
            instructions="sort_order must be 'asc' or 'desc'.",
        )
    payload = await _alcm_get(client, settings, "analysis", source="glance alcm analysis")
    rows = payload.get("analysis") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise UpstreamError(
            "GLANCE ALCM returned an unexpected analysis payload",
            instructions="The API may have changed; check the ALCM frontend.",
        )
    matcher = _AnalysisFilter(
        {
            "custom": custom,
            "code": code,
            "title": title,
            "stage": stage,
            "working_group": working_group,
            "contacts": contacts,
            "start_date": clean_start,
            "end_date": clean_end,
        }
    )
    items = [
        _project(row, _ANALYSIS_KEYS)
        for row in rows
        if isinstance(row, dict) and matcher.matches(row)
    ]
    items.sort(
        key=lambda r: str(r.get(sort_key) or ""),
        reverse=sort_order.lower() == "desc",
    )
    query = (
        "; ".join(
            part
            for part in (
                f"custom~{custom!r}" if custom else "",
                f"code~{code!r}" if code else "",
                f"title~{title!r}" if title else "",
                f"wg={working_group}" if working_group else "",
                f"stage~{stage!r}" if stage else "",
                f"contacts~{contacts!r}" if contacts else "",
                f"{clean_start}..{clean_end}" if (clean_start or clean_end) else "",
            )
            if part
        )
        or "all analyses"
    )
    return _envelope(
        items,
        len(items),
        query=query,
        limit=limit,
        offset=offset,
        note=(
            "Filters mirror the ALCM analysis-page fields and are applied client-side "
            "over the full list (~1.7 MB fetched per call); 'contacts' matches "
            "proponent names; dates filter on registrationDate."
        ),
    )


async def list_published_analyses(
    client: SafeHttpClient,
    settings: Settings,
    *,
    identifier: str | None = None,
    title_contains: str | None = None,
    working_group: str | None = None,
    has_arxiv: bool | None = None,
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
) -> SearchResult:
    """Published (public) analysis list with client-side filters."""
    _check_page(limit, offset)
    identifier = _check_text(identifier, "identifier")
    title_contains = _check_text(title_contains, "title_contains")
    working_group = _check_text(working_group, "working_group")
    payload = await _alcm_get(
        client, settings, "public/analysis", source="glance alcm public analysis"
    )
    rows = payload.get("analyses") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise UpstreamError(
            "GLANCE ALCM returned an unexpected public-analysis payload",
            instructions="The API may have changed; check the ALCM frontend.",
        )
    ident = identifier.lower() if identifier else None
    title = title_contains.lower() if title_contains else None
    wg = working_group.lower() if working_group else None
    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if ident and ident not in str(row.get("paperIdentifier") or "").lower():
            continue
        if title and title not in str(row.get("title") or "").lower():
            continue
        if wg and wg not in [n.lower() for n in _wg_names(row)]:
            continue
        if has_arxiv is not None and bool(row.get("arxivNumber")) != has_arxiv:
            continue
        items.append(_project(row, _PUBLISHED_KEYS))
    query = (
        "; ".join(
            part
            for part in (
                f"identifier~{identifier!r}" if identifier else "",
                f"title~{title_contains!r}" if title_contains else "",
                f"wg={working_group}" if working_group else "",
                f"has_arxiv={has_arxiv}" if has_arxiv is not None else "",
            )
            if part
        )
        or "all published analyses"
    )
    return _envelope(
        items,
        len(items),
        query=query,
        limit=limit,
        offset=offset,
        note="Filters are client-side; identifier matches paperIdentifier.",
    )


async def list_note_types(client: SafeHttpClient, settings: Settings) -> dict[str, Any]:
    """Note types known to ALCM (``{"types": [...]}``)."""
    payload = await _alcm_get(client, settings, "notes/types", source="glance alcm note types")
    types = payload.get("types") if isinstance(payload, dict) else None
    if not isinstance(types, list):
        raise UpstreamError(
            "GLANCE ALCM returned an unexpected notes/types payload",
            instructions="The API may have changed; check the ALCM frontend.",
        )
    return {"source": SOURCE, "type_count": len(types), "types": types}


async def search_notes(
    client: SafeHttpClient,
    settings: Settings,
    *,
    title_contains: str | None = None,
    working_group: str | None = None,
    type_name: str | None = None,
    project: str | None = None,
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
) -> SearchResult:
    """Search the ALCM notes index (one ~15 MB payload, filtered client-side)."""
    _check_page(limit, offset)
    title_contains = _check_text(title_contains, "title_contains")
    working_group = _check_text(working_group, "working_group")
    type_name = _check_text(type_name, "type_name")
    project = _check_text(project, "project")
    payload = await _alcm_get(client, settings, "notes", source="glance alcm notes")
    rows = payload.get("notes") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise UpstreamError(
            "GLANCE ALCM returned an unexpected notes payload",
            instructions="The API may have changed; check the ALCM frontend.",
        )
    title = title_contains.lower() if title_contains else None
    wg = working_group.lower() if working_group else None
    ntype = type_name.lower() if type_name else None
    proj = project.lower() if project else None
    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if title and title not in str(row.get("title") or "").lower():
            continue
        if wg and wg not in [n.lower() for n in _wg_names(row)]:
            continue
        row_type = row.get("type")
        if ntype and (not isinstance(row_type, dict) or row_type.get("name", "").lower() != ntype):
            continue
        if proj:
            projects = row.get("projects")
            names = (
                [p.get("name", "") for p in projects if isinstance(p, dict)]
                if isinstance(projects, list)
                else []
            )
            if proj not in [n.lower() for n in names]:
                continue
        items.append(_project(row, _NOTE_KEYS))
    query = (
        "; ".join(
            part
            for part in (
                f"title~{title_contains!r}" if title_contains else "",
                f"wg={working_group}" if working_group else "",
                f"type={type_name}" if type_name else "",
                f"project={project}" if project else "",
            )
            if part
        )
        or "all notes"
    )
    return _envelope(
        items,
        len(items),
        query=query,
        limit=limit,
        offset=offset,
        note="The notes index is a single ~15 MB payload fetched in full on every "
        "call (requires CMCPS_MAX_RESPONSE_BYTES >= ~20 MB); filtering is "
        "client-side and output is bounded by limit.",
    )


_ANCODE_RE_MAX = 64


def _page_links(analysis_doc: dict[str, Any]) -> dict[str, Any]:
    """PubTalk / public-result / twiki links exactly as the ALCM page derives them."""
    paper_identifier = analysis_doc.get("paperIdentifier") or analysis_doc.get("customId")
    wg_names = _wg_names(analysis_doc)
    published = bool(analysis_doc.get("publishedDate") or analysis_doc.get("journalReference"))
    public_result = None
    if analysis_doc.get("publicResults") == "Yes" and isinstance(paper_identifier, str):
        kind = "publications" if published else "preliminary-results"
        public_result = (
            f"https://cms-results.web.cern.ch/cms-results/public-results/{kind}/{paper_identifier}/"
        )
    pubtalk_forum = None
    if wg_names and isinstance(paper_identifier, str):
        pubtalk_forum = f"https://cms-pub-talk.web.cern.ch/c/{wg_names[0]}/{paper_identifier}"
    return {
        "pubtalk_forum": pubtalk_forum,
        "public_result": public_result,
        "twiki": analysis_doc.get("twikiUrl"),
    }


async def get_analysis(
    client: SafeHttpClient, settings: Settings, *, analysis: str
) -> dict[str, Any]:
    """Full page-level profile of one ALCM analysis.

    Accepts the analysis code (``EXO-24-010``) or the numeric id. Composes
    everything the ALCM details page shows: identity/dates/authors/abstract,
    the review committee (ARC chair, LEs, members), paper information,
    workflow (current/target stage, entry conditions, past events), analysis
    information history, attachments (documents with versions, GitLab
    repositories, Indico contributions, paths), and hashtags.
    """
    code_or_id = analysis.strip()
    if not code_or_id or len(code_or_id) > _ANCODE_RE_MAX:
        raise InvalidInputError(
            "analysis must be an analysis code (e.g. EXO-24-010) or a numeric id",
            instructions="Look up codes with glance_list_analyses.",
        )
    path = f"analysis/{code_or_id}" if code_or_id.isdigit() else f"analysis/ancode/{code_or_id}"
    source = f"glance alcm analysis {code_or_id}"
    payload = await _app_get(client, settings, "alcm", path, source=source)
    analysis_doc = payload.get("analysis") if isinstance(payload, dict) else None
    if not isinstance(analysis_doc, dict):
        raise UpstreamError(
            f"GLANCE ALCM returned no analysis for {code_or_id}",
            instructions="Check the code via glance_list_analyses.",
        )
    analysis_id = analysis_doc.get("id")
    if not isinstance(analysis_id, int):
        raise UpstreamError(
            f"GLANCE ALCM returned an analysis without numeric id for {code_or_id}",
            instructions="The API may have changed; check the ALCM frontend.",
        )

    async def _fetch(name: str, endpoint: str) -> Any:
        try:
            return await _app_get(
                client, settings, "alcm", endpoint, source=f"glance alcm {name} {analysis_id}"
            )
        except UpstreamError as exc:
            return {"error": f"{name} unavailable: {exc.message}"}

    (
        paper,
        committee,
        transitions,
        history,
        documents,
        gitlab_repos,
        indico_contribs,
        paths,
        hashtags,
    ) = await asyncio.gather(
        _fetch("paper", f"analysis/{analysis_id}/paper"),
        _fetch("review-committee", f"analysis/{analysis_id}/review-committee"),
        _fetch("transitions", f"transitions/{analysis_id}"),
        _fetch("history", f"analysis/{analysis_id}/history"),
        _fetch("documents", f"attachments/documents/entity/{analysis_id}"),
        _fetch("gitlab-repositories", f"attachments/gitlab-repositories/entity/{analysis_id}"),
        _fetch("indico-contributions", f"attachments/indico-contributions/entity/{analysis_id}"),
        _fetch("paths", f"attachments/paths/entity/{analysis_id}"),
        _fetch("hashtags", f"hashtags/entity/{analysis_id}"),
    )

    def _section(payload: Any, key: str) -> Any:
        """Unwrap ``{key: value}`` payloads; keep per-endpoint error markers."""
        if not isinstance(payload, dict):
            return payload
        if "error" in payload:
            return payload
        return payload.get(key)

    # Related analysis notes (notesList) each have their own document store;
    # the page renders a 'Download latest PDF' link per note from it.
    related_notes = analysis_doc.get("notesList")
    note_ids = (
        [int(n["id"]) for n in related_notes if isinstance(n, dict) and n.get("id") is not None]
        if isinstance(related_notes, list)
        else []
    )
    note_documents = dict(
        zip(
            note_ids,
            await asyncio.gather(
                *(
                    _fetch("note documents", f"attachments/documents/entity/{nid}")
                    for nid in note_ids
                )
            ),
            strict=True,
        )
    )

    links = _page_links(analysis_doc)
    return {
        "source": SOURCE,
        "analysis_id": analysis_id,
        "analysis_code": analysis_doc.get("customId"),
        "links": links,
        "analysis": analysis_doc,
        "paper": _section(paper, "paper"),
        "review_committee": _section(committee, "reviewCommittee"),
        "workflow": _section(transitions, "transition"),
        "history": _section(history, "history"),
        "attachments": {
            "documents": _section(documents, "documents"),
            "gitlab_repositories": _section(gitlab_repos, "gitlabRepositories"),
            "indico_contributions": _section(indico_contribs, "indicoContributions"),
            "paths": _section(paths, "paths"),
            "related_note_documents": {
                note_id: _section(payload, "documents")
                for note_id, payload in note_documents.items()
            },
        },
        "hashtags": _section(hashtags, "hashtags"),
        "note": (
            "links.pubtalk_forum and links.public_result are DERIVED from the paper "
            "identifier exactly as the ALCM page builds them (they are not part of "
            "any endpoint response); workflow aggregates the workflow tracker "
            "(current/target stage, entryConditions, pastEvents); "
            "attachments.documents entries carry full version lists; the workflow "
            "history rows live under workflow.pastEvents and the analysis-information "
            "history under history.analysisUpdates; a section holding "
            "{'error': ...} marks one support endpoint that failed."
        ),
    }


_ARC_KEYS = (
    "memberId",
    "memberName",
    "instCode",
    "numberOfArcsParticipated",
    "activeAnalysesCount",
    "activeAnalyses",
)


async def arc_members(
    client: SafeHttpClient,
    settings: Settings,
    *,
    analysis: str | None = None,
    member_contains: str | None = None,
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
) -> SearchResult:
    """Analysis Review Committee (ARC) per-member statistics.

    From ``GET /alcm/api/analysis/statistics-arc-members`` (~0.7 MB). With
    ``analysis``, rows are filtered to ARC members/chairs/LEs of one analysis
    by its identifier (e.g. ``EXO-24-010``) and the matching entries are
    echoed under ``matches``. ``finishedAnalyses`` is dropped to bound output.
    """
    _check_page(limit, offset)
    analysis = _check_text(analysis, "analysis")
    member_contains = _check_text(member_contains, "member_contains")
    payload = await _alcm_get(
        client, settings, "analysis/statistics-arc-members", source="glance alcm arc members"
    )
    rows = payload.get("statistics") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise UpstreamError(
            "GLANCE ALCM returned an unexpected ARC-statistics payload",
            instructions="The API may have changed; check the ALCM frontend.",
        )
    ident = analysis.lower() if analysis else None
    name = member_contains.lower() if member_contains else None
    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        active = row.get("activeAnalyses")
        active = [a for a in active if isinstance(a, str)] if isinstance(active, list) else []
        matches = [a for a in active if ident and ident in a.lower()]
        if ident and not matches:
            continue
        if name and name not in str(row.get("memberName") or "").lower():
            continue
        item = _project(row, _ARC_KEYS)
        if ident:
            item["matches"] = matches
        items.append(item)
    query_parts = []
    if ident:
        query_parts.append(f"analysis={analysis}")
    if name:
        query_parts.append(f"member_contains={member_contains}")
    note = (
        "Roles are encoded in the activeAnalyses strings as '<Role> of <ID> (<stage>)' "
        "with Role in {Member, LE, Chair}."
    )
    return _envelope(
        items,
        len(items),
        query="; ".join(query_parts) or "all",
        limit=limit,
        offset=offset,
        note=note,
    )


async def get_note(client: SafeHttpClient, settings: Settings, *, note_id: int) -> dict[str, Any]:
    """Full detail of one ALCM note by numeric id."""
    if note_id < 1:
        raise InvalidInputError("note_id must be a positive integer")
    payload = await _app_get(
        client, settings, "alcm", f"notes/{note_id}", source=f"glance alcm note {note_id}"
    )
    note = payload.get("note") if isinstance(payload, dict) else None
    if not isinstance(note, dict):
        raise UpstreamError(
            f"GLANCE ALCM returned no note for id {note_id}",
            instructions="Check the id via glance_search_notes.",
        )
    return {"source": SOURCE, "note_id": note_id, "note": note}


async def list_projects(client: SafeHttpClient, settings: Settings) -> dict[str, Any]:
    """ALCM projects with their e-groups (``{"projects": [...]}``)."""
    payload = await _alcm_get(client, settings, "projects", source="glance alcm projects")
    projects = payload.get("projects") if isinstance(payload, dict) else None
    if not isinstance(projects, list):
        raise UpstreamError(
            "GLANCE ALCM returned an unexpected projects payload",
            instructions="The API may have changed; check the ALCM frontend.",
        )
    return {"source": SOURCE, "project_count": len(projects), "projects": projects}


async def list_working_groups(
    client: SafeHttpClient,
    settings: Settings,
    *,
    kind: NotKind = "all",
) -> dict[str, Any]:
    """ALCM working groups; physics and/or non-physics via ``kind``."""
    out: dict[str, Any] = {"source": SOURCE, "kind": kind}
    if kind in ("non-physics", "all"):
        payload = await _alcm_get(
            client, settings, "working-groups", source="glance alcm working-groups"
        )
        groups = payload.get("workingGroups") if isinstance(payload, dict) else None
        if not isinstance(groups, list):
            raise UpstreamError(
                "GLANCE ALCM returned an unexpected working-groups payload",
                instructions="The API may have changed; check the ALCM frontend.",
            )
        out["working_groups"] = groups
    if kind in ("physics", "all"):
        payload = await _alcm_get(
            client,
            settings,
            "physics-working-groups",
            source="glance alcm physics-working-groups",
        )
        groups = payload.get("physicsWorkingGroups") if isinstance(payload, dict) else None
        if not isinstance(groups, list):
            raise UpstreamError(
                "GLANCE ALCM returned an unexpected physics-working-groups payload",
                instructions="The API may have changed; check the ALCM frontend.",
            )
        out["physics_working_groups"] = groups
    return out


async def alcm_members(
    client: SafeHttpClient,
    settings: Settings,
    *,
    name_contains: str | None = None,
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
) -> SearchResult:
    """ALCM member roster, substring name filter and pagination."""
    _check_page(limit, offset)
    name_contains = _check_text(name_contains, "name_contains")
    payload = await _alcm_get(client, settings, "members", source="glance alcm members")
    rows = payload.get("members") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise UpstreamError(
            "GLANCE ALCM returned an unexpected members payload",
            instructions="The API may have changed; check the ALCM frontend.",
        )
    needle = name_contains.lower() if name_contains else None
    items = [
        {"id": row.get("id"), "name": row.get("name")}
        for row in rows
        if isinstance(row, dict) and (needle is None or needle in str(row.get("name")).lower())
    ]
    query = f"name~{name_contains!r}" if name_contains else "all members"
    return _envelope(
        items,
        len(items),
        query=query,
        limit=limit,
        offset=offset,
        note="Full roster (~1.1 MB) fetched per call; filtering is client-side.",
    )


async def membership_search_members(
    client: SafeHttpClient,
    settings: Settings,
    *,
    query: str,
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
) -> SearchResult:
    """Membership member search (server-side ``q``, client-side pagination)."""
    _check_page(limit, offset)
    query = _check_text(query, "query")
    if query is None:
        raise InvalidInputError("query is required")
    payload = await _app_get(
        client,
        settings,
        "membership",
        "members/search",
        source="glance membership members search",
        params={"q": query},
    )
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise UpstreamError(
            "GLANCE Membership returned an unexpected search payload",
            instructions="The API may have changed; check the Membership frontend.",
        )
    items = [row for row in rows if isinstance(row, dict)]
    upstream_total = payload.get("numberOfResults") if isinstance(payload, dict) else None
    try:
        upstream_total_int = int(upstream_total) if upstream_total is not None else None
    except (TypeError, ValueError):
        upstream_total_int = None
    note = None
    if upstream_total_int is not None and upstream_total_int != len(items):
        note = f"Upstream reports {upstream_total_int} matches; pagination covers returned rows."
    return _envelope(
        items,
        len(items),
        query=f"q={query!r}",
        limit=limit,
        offset=offset,
        note=note,
    )
